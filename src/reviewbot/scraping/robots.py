"""robots.txt: ask permission, and honour the delay the site asks for.

ReviewBay's stated position (`README.md` caveats) is that it prefers official
APIs and search-indexed content and does not attempt anti-bot evasion. Fetching
pages directly is a step closer to the line, so the engines that do it check
robots.txt first and respect `Crawl-delay` / `Request-rate` when a site sets one.

Implemented on the standard library's `urllib.robotparser` rather than a new
dependency. `protego` (which arrives with Scrapling's `fetchers` extra) handles
more exotic directives, and is used when present; the stdlib path is the floor so
this works in the base image too.

Behaviour on failure is deliberate:

* robots.txt says *disallow* → do not fetch.
* robots.txt is missing (404) → fetch. That is what a 404 means: no restrictions.
* robots.txt is unreachable (timeout, DNS, 5xx) → **do not fetch**, and cache
  that decision briefly. A site whose robots we cannot read is a site whose
  wishes we do not know, and the cost of being wrong is asymmetric.

The cache is per-process and bounded. On a restart the first request per domain
pays one extra round trip, which is the right trade for not holding state.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from . import urlpolicy

log = logging.getLogger(__name__)

#: What ReviewBay calls itself. An honest, contactable user agent is the single
#: most important courtesy in a scraper: a site operator who wants us to stop has
#: something to put in their robots.txt.
USER_AGENT = os.environ.get(
    "SCRAPING_USER_AGENT",
    "ReviewBayBot/1.0 (+https://github.com/JenohJOHNSON/ReviewBay; brand-reputation research)",
)
#: The token a site would use to address us in robots.txt.
UA_TOKEN = os.environ.get("SCRAPING_UA_TOKEN", "ReviewBayBot")

ROBOTS_ENABLED = os.environ.get("SCRAPING_OBEY_ROBOTS", "1") != "0"
ROBOTS_TIMEOUT = float(os.environ.get("SCRAPING_ROBOTS_TIMEOUT", "10"))
ROBOTS_TTL = float(os.environ.get("SCRAPING_ROBOTS_TTL", "3600"))
#: Fall back to this between requests when a site asks for no particular delay.
DEFAULT_DELAY = float(os.environ.get("SCRAPING_DEFAULT_DELAY", "1.0"))
#: Never wait longer than this for one page, however large Crawl-delay is; we
#: skip the domain instead of stalling a collection behind it.
MAX_HONOURED_DELAY = float(os.environ.get("SCRAPING_MAX_DELAY", "10"))

_CACHE_MAX = 512


@dataclass
class _Rules:
    """One domain's parsed robots.txt, plus when we fetched it."""

    fetched_at: float
    parser: object | None          # RobotFileParser, Protego, or None
    reachable: bool
    crawl_delay: float | None = None
    _kind: str = field(default="stdlib")

    def expired(self, now: float) -> bool:
        return (now - self.fetched_at) > ROBOTS_TTL


_RULES: dict[str, _Rules] = {}
_LOCK = threading.Lock()


def _robots_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}".lower()
    except ValueError:
        return ""


def _fetch_robots(robots_url: str) -> tuple[str | None, bool]:
    """Return (body, reachable). body is None for a 404 (= no restrictions)."""
    import requests

    try:
        resp = requests.get(
            robots_url,
            timeout=ROBOTS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("robots: %s unreachable (%s)", robots_url, type(exc).__name__)
        return None, False
    if resp.status_code in (401, 403):
        # An access-controlled robots.txt means the site is gated. Treat as
        # "everything disallowed" rather than "no rules".
        log.info("robots: %s returned %d — treating as disallow-all", robots_url, resp.status_code)
        return "User-agent: *\nDisallow: /", True
    if resp.status_code == 404:
        return None, True
    if resp.status_code >= 400:
        return None, False
    return resp.text or "", True


def _parse(body: str):
    """Parse a robots body with protego when available, else the stdlib."""
    try:
        from protego import Protego  # type: ignore

        return Protego.parse(body), "protego"
    except ImportError:
        pass
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(body.splitlines())
    return parser, "stdlib"


def _rules_for(url: str) -> _Rules:
    origin = _origin(url)
    now = time.monotonic()
    with _LOCK:
        cached = _RULES.get(origin)
        if cached is not None and not cached.expired(now):
            return cached

    robots_url = _robots_url(url)
    if not robots_url:
        rules = _Rules(fetched_at=now, parser=None, reachable=False)
    elif not urlpolicy.is_allowed(robots_url):
        # The page URL itself has already been policy-checked by the caller, but
        # check robots.txt too: it is a separate server-side fetch.
        rules = _Rules(fetched_at=now, parser=None, reachable=False)
    else:
        body, reachable = _fetch_robots(robots_url)
        if body is None:
            # Reachable 404 = no rules at all (allow); unreachable = unknown (deny).
            rules = _Rules(fetched_at=now, parser=None, reachable=reachable)
        else:
            try:
                parser, kind = _parse(body)
                rules = _Rules(fetched_at=now, parser=parser, reachable=True, _kind=kind)
                rules.crawl_delay = _read_delay(parser, kind)
            except Exception:  # noqa: BLE001
                log.exception("robots: could not parse %s", robots_url)
                rules = _Rules(fetched_at=now, parser=None, reachable=True)

    with _LOCK:
        if len(_RULES) >= _CACHE_MAX:
            # Crude but adequate: drop the oldest half rather than grow forever.
            for key in sorted(_RULES, key=lambda k: _RULES[k].fetched_at)[: _CACHE_MAX // 2]:
                _RULES.pop(key, None)
        _RULES[origin] = rules
    return rules


def _read_delay(parser, kind: str) -> float | None:
    """Crawl-delay (or an equivalent Request-rate) in seconds, if the site set one."""
    try:
        if kind == "protego":
            delay = parser.crawl_delay(UA_TOKEN)
            if delay is None:
                delay = parser.crawl_delay("*")
            if delay is not None:
                return float(delay)
            rate = parser.request_rate(UA_TOKEN) or parser.request_rate("*")
            if rate and getattr(rate, "requests", 0):
                return float(rate.seconds) / float(rate.requests)
            return None
        delay = parser.crawl_delay(UA_TOKEN)
        if delay is None:
            delay = parser.crawl_delay("*")
        if delay is not None:
            return float(delay)
        rate = parser.request_rate(UA_TOKEN) or parser.request_rate("*")
        if rate and getattr(rate, "requests", 0):
            return float(rate.seconds) / float(rate.requests)
    except Exception:  # noqa: BLE001 — a malformed directive is not fatal
        return None
    return None


def can_fetch(url: str) -> bool:
    """Whether robots.txt permits fetching `url` as ReviewBayBot.

    Returns True when the check is disabled, when robots.txt is absent, or when
    it explicitly allows us. Returns False when it disallows us or when we could
    not read it (see the module docstring on why).
    """
    if not ROBOTS_ENABLED:
        return True
    rules = _rules_for(url)
    if rules.parser is None:
        return rules.reachable  # reachable 404 -> allow; unreachable -> deny
    try:
        if rules._kind == "protego":
            return bool(rules.parser.can_fetch(url, UA_TOKEN))
        return bool(rules.parser.can_fetch(UA_TOKEN, url))
    except Exception:  # noqa: BLE001
        log.exception("robots: can_fetch failed for %s — denying", url)
        return False


def crawl_delay(url: str) -> float:
    """How long to wait between requests to this host, in seconds.

    The site's `Crawl-delay` when it sets one (clamped to `MAX_HONOURED_DELAY`),
    otherwise `DEFAULT_DELAY`.
    """
    if not ROBOTS_ENABLED:
        return DEFAULT_DELAY
    rules = _rules_for(url)
    if rules.crawl_delay is None:
        return DEFAULT_DELAY
    return max(DEFAULT_DELAY, min(rules.crawl_delay, MAX_HONOURED_DELAY))


def requested_delay_exceeds_limit(url: str) -> bool:
    """True when a site asks for a longer pause than we are willing to hold.

    The caller should skip the domain rather than ignore the request.
    """
    if not ROBOTS_ENABLED:
        return False
    rules = _rules_for(url)
    return rules.crawl_delay is not None and rules.crawl_delay > MAX_HONOURED_DELAY


def reset_cache() -> None:
    """Forget every cached robots.txt (tests, long-running processes)."""
    with _LOCK:
        _RULES.clear()
