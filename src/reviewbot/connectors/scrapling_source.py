"""Self-hosted page fetching and extraction, built on Scrapling.

**Why this exists.** The audit found that ReviewBay's real bottleneck is not the
number of search results, it is the *depth* of each one. `collect_until` reaches
its ~200 target largely by running the Tavily web search three times with
different phrasings, and a search API returns a SERP snippet: a title and two
sentences. Three passes over the same Reddit thread yield three snippets of it.
One fetch of that thread yields the whole discussion.

So this connector does not search. It takes URLs that the *existing* search
connectors already discovered, fetches those pages, and extracts the reviews on
them. It is a depth engine sitting behind the breadth engines, which is why it
replaces nothing: `web` still finds the pages, this reads them properly.

**Three fetch tiers, cheapest first** (`scrapling` installs in layers, and this
degrades through them rather than failing):

| tier | needs | used for |
|---|---|---|
| `requests` | nothing beyond the base image | the default. Scrapling parses, `requests` fetches. |
| `scrapling` | `scrapling[fetchers]` | `FetcherSession` — connection reuse, HTTP/2, retries |
| `browser` | `scrapling[fetchers]` + `playwright install chromium` | only pages that genuinely need JS |

Tier is chosen per page: a page is only re-fetched with a browser when the plain
fetch produced no extractable content *and* the HTML looks like a JS shell. That
check is what keeps browser launches rare, which is what keeps memory flat on a
small instance.

**What it deliberately does not do.** Scrapling ships `solve_cloudflare`,
`StealthyFetcher` fingerprint spoofing and TLS `impersonate` profiles. None of
them are enabled, and there is no setting to enable them. ReviewBay's stated
position is that it does not attempt anti-bot evasion, so:

* `robots.txt` is checked before every fetch and honoured, including `Crawl-delay`
* the user agent says what this is and links to the project
* a 403 or 429 marks the domain as declined and stops, rather than retrying
  harder or disguising itself
* every URL passes the SSRF policy in `scraping.urlpolicy`, before the fetch and
  again after every redirect

**Provenance.** `source` stays the *platform* (`reddit`, `trustpilot`, `web`),
derived from the URL host exactly as the search connectors do. "scrapling" is
how we read the page, not who published the review, and recording it as the
source would misattribute the content. It goes in
`extra["extraction"]["engine"]` instead.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from .. import text as textclean
from ..models import NormalizedReview
from ..scraping import budget as budgets
from ..scraping import extract as extractor
from ..scraping import metrics as metricsmod
from ..scraping import robots, urlpolicy
from ..sources import source_from_url
from .base import BaseConnector

log = logging.getLogger(__name__)

ENGINE = "scrapling"

ENABLED = os.environ.get("SCRAPLING_ENABLED", "1") != "0"
#: "requests" | "scrapling" | "browser" | "auto". "auto" picks the cheapest tier
#: that is installed and escalates to a browser only when a page needs it.
FETCH_TIER = os.environ.get("SCRAPLING_FETCH_TIER", "auto").strip().lower()
BROWSER_ENABLED = os.environ.get("SCRAPLING_BROWSER_ENABLED", "0") == "1"
MAX_RETRIES = int(os.environ.get("SCRAPLING_MAX_RETRIES", "2"))
FOLLOW_PAGINATION = os.environ.get("SCRAPLING_FOLLOW_PAGINATION", "1") != "0"
MAX_PAGINATION_DEPTH = int(os.environ.get("SCRAPLING_MAX_PAGINATION_DEPTH", "3"))

#: Hosts whose content is already covered by a purpose-built connector, or whose
#: terms make direct fetching the wrong tool. Skipped so this engine never
#: competes with (or duplicates) a source that already works well.
SKIP_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get(
        "SCRAPLING_SKIP_HOSTS",
        # app stores: covered by app_store/google_play via official feeds
        "apps.apple.com,itunes.apple.com,play.google.com,"
        # HN: covered by the Algolia API, which is better than scraping it
        "news.ycombinator.com,"
        # these actively prohibit scraping and/or require login
        "facebook.com,instagram.com,linkedin.com,x.com,twitter.com,threads.net,"
        "tiktok.com,youtube.com,"
        # search engines: fetching a SERP is not reading a review
        "google.com,bing.com,duckduckgo.com,search.yahoo.com",
    ).split(",")
    if h.strip()
)

#: Status codes that mean "stop asking", not "try again". Retrying any of these
#: harder is the behaviour this project has committed to not having.
_DECLINED = (401, 402, 403, 405, 406, 410, 429, 451)
#: Of those, the ones that are about the whole site rather than one URL. A 429 is
#: a rate-limit signal for the host, and a 451 is a legal block on the host, so
#: both quarantine the domain for the rest of the run. A 403 on a single
#: login-gated page says nothing about the rest of a site that otherwise serves
#: public reviews, so it skips that URL only.
_DECLINED_DOMAIN_WIDE = (429, 451)
#: Markers of a page whose content is rendered client-side.
_JS_SHELL = re.compile(
    r"(<div\s+id=[\"'](?:root|app|__next|__nuxt)[\"']|"
    r"window\.__(?:INITIAL_STATE|NUXT|NEXT_DATA)__|"
    r"please enable javascript|you need to enable javascript)",
    re.I,
)
_MIN_HTML_BYTES = 200


@dataclass
class FetchResult:
    """One page fetch, however it was performed."""

    url: str
    status: int = 0
    html: str = ""
    tier: str = ""
    error: str = ""
    declined: bool = False
    elapsed_ms: int = 0
    redirects: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.html) and 200 <= self.status < 300


@dataclass
class ScraplingEngine:
    """The fetch-and-extract engine, usable without the connector wrapper.

    Separated so the benchmark script and the tests can drive it directly with
    their own budget and metrics, which is what makes the numbers in
    `docs/SCRAPING_BENCHMARKS.md` reproducible.
    """

    crawl_budget: budgets.CrawlBudget = field(default_factory=budgets.CrawlBudget)
    limiter: budgets.RateLimiter = field(default_factory=budgets.RateLimiter)
    metrics: metricsmod.RunMetrics = field(default_factory=metricsmod.RunMetrics)
    tier: str = FETCH_TIER
    allow_browser: bool = BROWSER_ENABLED
    #: domains that returned a "stop asking" status this run
    declined_domains: set[str] = field(default_factory=set)
    _session: object | None = field(default=None, init=False, repr=False)

    # -- capability detection ------------------------------------------------

    @staticmethod
    def parser_available() -> bool:
        """Whether Scrapling's parser (the light core) is importable."""
        try:
            import scrapling.parser  # type: ignore  # noqa: F401

            return True
        except ImportError:
            return False

    @staticmethod
    def fetchers_available() -> bool:
        """Whether `scrapling[fetchers]` (curl_cffi) is installed."""
        try:
            from scrapling.fetchers import FetcherSession  # type: ignore  # noqa: F401

            return True
        except Exception:  # noqa: BLE001 — ImportError for curl_cffi, or anything else
            return False

    @staticmethod
    def browser_available() -> bool:
        """Whether a Scrapling browser fetcher can be constructed."""
        try:
            from scrapling.fetchers import StealthyFetcher  # type: ignore  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        try:
            import playwright  # type: ignore  # noqa: F401

            return True
        except ImportError:
            return False

    def resolved_tier(self) -> str:
        """The tier this engine will actually use for a first attempt."""
        if self.tier in ("requests", "scrapling", "browser"):
            if self.tier == "scrapling" and not self.fetchers_available():
                log.warning(
                    "SCRAPLING_FETCH_TIER=scrapling but scrapling[fetchers] is not "
                    "installed; falling back to requests"
                )
                return "requests"
            if self.tier == "browser" and not self.browser_available():
                log.warning(
                    "SCRAPLING_FETCH_TIER=browser but no browser is available; "
                    "falling back to the cheapest working tier"
                )
                return "scrapling" if self.fetchers_available() else "requests"
            return self.tier
        return "scrapling" if self.fetchers_available() else "requests"

    # -- gating --------------------------------------------------------------

    def should_skip(self, url: str) -> str:
        """Why this URL must not be fetched, or '' when it may be.

        Ordered cheapest-check-first so an obviously bad URL costs nothing.
        """
        verdict = urlpolicy.check(url, resolve=False)
        if not verdict.allowed:
            return f"url policy: {verdict.reason}"

        host = (verdict.host or "").lower()
        domain = urlpolicy.registrable_domain(url)
        for skip in SKIP_HOSTS:
            if host == skip or host.endswith("." + skip) or domain == skip:
                return f"covered by a dedicated connector or not permitted ({skip})"

        if domain in self.declined_domains:
            return "domain declined earlier in this run"
        if not self.crawl_budget.allows(url):
            return self.crawl_budget.stop_reason or "crawl budget reached for this domain"

        # DNS-resolving check last: it is the only one that costs a round trip.
        verdict = urlpolicy.check(url, resolve=True)
        if not verdict.allowed:
            return f"url policy: {verdict.reason}"
        if robots.requested_delay_exceeds_limit(url):
            return f"robots.txt asks for a longer delay than we hold ({robots.MAX_HONOURED_DELAY}s)"
        if not robots.can_fetch(url):
            return "robots.txt disallows it"
        return ""

    # -- fetching ------------------------------------------------------------

    def _requests_fetch(self, url: str) -> FetchResult:
        """Plain HTTP with the `requests` already in the base image.

        Redirects are NOT followed automatically: each hop is re-checked against
        the URL policy, because a policy-clean host can redirect to the metadata
        endpoint.
        """
        import requests

        headers = {
            "User-Agent": robots.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        current = url
        hops: list[str] = []
        for _ in range(5):
            resp = requests.get(
                current,
                headers=headers,
                timeout=self.crawl_budget.request_timeout,
                allow_redirects=False,
                stream=True,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                target = resp.headers.get("location") or ""
                resp.close()
                if not target:
                    return FetchResult(url=current, status=resp.status_code,
                                       error="redirect with no location")
                if "://" not in target:
                    from urllib.parse import urljoin

                    target = urljoin(current, target)
                verdict = urlpolicy.check(target)
                if not verdict.allowed:
                    # The interesting SSRF case: validated host, hostile redirect.
                    return FetchResult(
                        url=current, status=resp.status_code,
                        error=f"redirect blocked by url policy: {verdict.reason}",
                        redirects=tuple(hops),
                    )
                hops.append(target)
                current = target
                continue

            if resp.status_code in _DECLINED:
                resp.close()
                return FetchResult(url=current, status=resp.status_code, declined=True,
                                   error=f"HTTP {resp.status_code}", redirects=tuple(hops))
            if resp.status_code >= 400:
                resp.close()
                return FetchResult(url=current, status=resp.status_code,
                                   error=f"HTTP {resp.status_code}", redirects=tuple(hops))

            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and not any(
                t in ctype for t in ("text/html", "application/xhtml", "text/plain", "application/xml")
            ):
                resp.close()
                return FetchResult(url=current, status=resp.status_code,
                                   error=f"not a web page ({ctype.split(';')[0]})",
                                   redirects=tuple(hops))

            # Bounded read: a multi-hundred-MB page must not become a MemoryError.
            # The chunk size is clamped to the cap so a small cap is honoured
            # exactly rather than overshot by one chunk.
            cap = max(1, self.crawl_budget.max_bytes_per_page)
            chunks, total = [], 0
            for chunk in resp.iter_content(min(64 * 1024, cap)):
                chunks.append(chunk)
                total += len(chunk)
                if total >= cap:
                    log.info("scrapling: truncating %s at %d bytes", current, total)
                    break
            resp.close()
            body = b"".join(chunks)[:cap]
            encoding = resp.encoding or resp.apparent_encoding or "utf-8"
            try:
                html = body.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = body.decode("utf-8", errors="replace")
            return FetchResult(url=current, status=resp.status_code, html=html,
                               tier="requests", redirects=tuple(hops))
        return FetchResult(url=current, status=0, error="too many redirects",
                           redirects=tuple(hops))

    def _session_fetch(self, url: str) -> FetchResult:
        """Scrapling's `FetcherSession`: connection reuse across pages.

        No `impersonate` and no stealth options — the point of using the session
        here is keep-alive and its retry handling, not fingerprint spoofing.
        """
        from scrapling.fetchers import FetcherSession  # type: ignore

        if self._session is None:
            self._session = FetcherSession(
                timeout=self.crawl_budget.request_timeout,
                headers={
                    "User-Agent": robots.USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                stealthy_headers=False,
                follow_redirects=True,
                max_redirects=5,
                retries=0,  # retries are this class's job, with its own backoff
            ).__enter__()

        page = self._session.get(url)
        status = int(getattr(page, "status", 0) or 0)
        final_url = str(getattr(page, "url", url) or url)
        # The session follows redirects itself, so re-validate where we landed.
        if final_url != url:
            verdict = urlpolicy.check(final_url)
            if not verdict.allowed:
                return FetchResult(
                    url=url, status=status,
                    error=f"redirect blocked by url policy: {verdict.reason}",
                )
        if status in _DECLINED:
            return FetchResult(url=final_url, status=status, declined=True,
                               error=f"HTTP {status}")
        if status >= 400 or status == 0:
            return FetchResult(url=final_url, status=status, error=f"HTTP {status}")
        html = str(getattr(page, "html_content", "") or "")
        return FetchResult(url=final_url, status=status, html=html, tier="scrapling")

    def _browser_fetch(self, url: str) -> FetchResult:
        """A real browser, for pages whose content is rendered client-side.

        Resources are blocked (`disable_resources`) because we want text, not
        images or fonts, which is most of the memory and most of the time.
        `solve_cloudflare` is NOT set: a challenge means the site does not want
        this, and the correct response is to stop.
        """
        from scrapling.fetchers import StealthyFetcher  # type: ignore

        page = StealthyFetcher.fetch(
            url,
            headless=True,
            disable_resources=True,
            block_ads=True,
            network_idle=False,
            timeout=self.crawl_budget.request_timeout * 1000,
            google_search=False,
            solve_cloudflare=False,
        )
        status = int(getattr(page, "status", 0) or 0)
        final_url = str(getattr(page, "url", url) or url)
        if final_url != url:
            verdict = urlpolicy.check(final_url)
            if not verdict.allowed:
                return FetchResult(url=url, status=status,
                                   error=f"redirect blocked by url policy: {verdict.reason}")
        if status in _DECLINED:
            return FetchResult(url=final_url, status=status, declined=True,
                               error=f"HTTP {status}")
        html = str(getattr(page, "html_content", "") or "")
        if not html:
            return FetchResult(url=final_url, status=status, error="browser returned no HTML")
        return FetchResult(url=final_url, status=status, html=html, tier="browser")

    def fetch(self, url: str, tier: str | None = None) -> FetchResult:
        """Fetch one page with politeness, retries and metrics. Never raises."""
        domain = urlpolicy.registrable_domain(url)
        use = tier or self.resolved_tier()
        self.metrics.attempt(ENGINE, domain)

        if not self.crawl_budget.take(url):
            self.metrics.skip(ENGINE, "crawl budget")
            return FetchResult(url=url, error="crawl budget exhausted")

        attempt = 0
        last = FetchResult(url=url, error="not attempted")
        while attempt <= MAX_RETRIES:
            attempt += 1
            try:
                self.limiter.wait(url)
            except Exception:  # noqa: BLE001
                pass
            started = time.monotonic()
            try:
                if use == "browser":
                    last = self._browser_fetch(url)
                elif use == "scrapling":
                    last = self._session_fetch(url)
                else:
                    last = self._requests_fetch(url)
            except Exception as exc:  # noqa: BLE001 — one page must not kill a run
                last = FetchResult(url=url, error=f"{type(exc).__name__}: {exc}"[:200])
                log.info("scrapling: fetch failed for %s (%s)", url, type(exc).__name__)
            last.elapsed_ms = int((time.monotonic() - started) * 1000)

            if last.ok:
                self.metrics.success(
                    ENGINE, duration_ms=last.elapsed_ms,
                    nbytes=len(last.html), domain=domain,
                )
                return last
            if last.declined:
                # The site said no. Never retry a decline; the only question is
                # whether it was about the host or about this one URL.
                domain_wide = last.status in _DECLINED_DOMAIN_WIDE
                if domain_wide and domain:
                    self.declined_domains.add(domain)
                self.metrics.skip(ENGINE, f"declined HTTP {last.status}")
                self.metrics.failure(ENGINE, duration_ms=last.elapsed_ms, domain=domain)
                log.info(
                    "scrapling: %s declined (HTTP %s) — skipping %s",
                    url, last.status, "this domain for the rest of the run" if domain_wide else "this URL",
                )
                return last
            if attempt <= MAX_RETRIES:
                self.metrics.retry(ENGINE)
                # Exponential backoff with jitter: lockstep retries just
                # reproduce whatever rate limit caused the failure.
                import random

                time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.4))

        self.metrics.failure(ENGINE, duration_ms=last.elapsed_ms, domain=domain)
        return last

    def needs_browser(self, result: FetchResult, extraction) -> bool:
        """Whether re-fetching this page with a browser is likely to help.

        Only when the cheap fetch found nothing AND the HTML looks like a
        client-rendered shell. Without that second condition, every page with no
        reviews on it would trigger a browser launch.
        """
        if not self.allow_browser or result.tier == "browser":
            return False
        if not self.browser_available():
            return False
        if extraction is not None and extraction.candidates:
            return False
        html = result.html or ""
        if len(html) < _MIN_HTML_BYTES:
            return True
        return bool(_JS_SHELL.search(html[:60000]))

    # -- the public operation ------------------------------------------------

    def harvest(self, url: str, depth: int = 0) -> list[extractor.Candidate]:
        """Fetch `url`, extract it, and follow its own "next page" links.

        Returns candidates with no brand attached: the connector decides which
        brand a page belongs to.
        """
        reason = self.should_skip(url)
        if reason:
            self.metrics.skip(ENGINE, reason)
            log.debug("scrapling: skipping %s (%s)", url, reason)
            return []

        result = self.fetch(url)
        if not result.ok:
            return []

        page = extractor.extract(result.html, result.url)
        if self.needs_browser(result, page):
            log.info("scrapling: %s looks client-rendered — retrying with a browser", url)
            retried = self.fetch(url, tier="browser")
            if retried.ok:
                page = extractor.extract(retried.html, retried.url)
                result = retried

        candidates = list(page.candidates)
        for cand in candidates:
            cand.extra.setdefault("fetch_tier", result.tier)
            cand.extra.setdefault("http_status", result.status)
            if result.redirects:
                cand.extra.setdefault("redirects", list(result.redirects))

        self.metrics.records(
            ENGINE, valid=0, rejected=0,
            domain=urlpolicy.registrable_domain(result.url),
        )

        if (
            FOLLOW_PAGINATION
            and depth < MAX_PAGINATION_DEPTH
            and page.next_urls
            and self.crawl_budget.remaining_pages > 0
        ):
            for next_url in page.next_urls[:1]:
                candidates.extend(self.harvest(next_url, depth + 1))

        return candidates

    def close(self) -> None:
        """Release the HTTP session, if one was opened."""
        session, self._session = self._session, None
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def to_review(
    brand: str,
    candidate: extractor.Candidate,
    *,
    discovered_via: str = "",
) -> NormalizedReview | None:
    """A `Candidate` as a `NormalizedReview`, or None when it is not usable.

    `source` is the PLATFORM the URL belongs to, never "scrapling": the engine is
    how we read the page, not who published the review. The engine detail lives
    under `extra["extraction"]`, which is what Phase 6 asks for — provenance
    recorded without overwriting the source identity.
    """
    text = textclean.clean(candidate.text)
    url = urlpolicy.canonical(candidate.source_url)
    # `canonical()` passes a non-http(s) URL through unchanged rather than
    # raising, so the scheme is checked here too: a citation link ends up as an
    # href on the dashboard, and `javascript:` has no business there.
    if not text or not url or not urlpolicy.is_allowed(url, resolve=False):
        return None

    extra = dict(candidate.extra)
    extra["extraction"] = {
        "engine": ENGINE,
        "strategy": candidate.strategy,
        "kind": candidate.kind,
        "evidence": candidate.evidence,
        "parser_version": PARSER_VERSION,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if discovered_via:
        extra["extraction"]["discovered_via"] = discovered_via
    if candidate.title:
        extra.setdefault("title", candidate.title)

    return NormalizedReview(
        brand=brand,
        source=source_from_url(url),
        source_url=url,
        text=text,
        author=candidate.author,
        rating=candidate.rating,
        created_at=candidate.created_at,
        extra=extra,
    )


#: Bumped when the extractors change in a way that would alter stored output, so
#: a row's provenance says which parser produced it.
PARSER_VERSION = "1.0.0"


class ScraplingConnector(BaseConnector):
    """Depth engine over URLs the search connectors already found.

    Unlike every other connector, this one does not discover its own URLs: it is
    handed them. `collect.py` passes the URLs that `web` (Tavily/Apify) surfaced
    for this brand, which is what keeps this a *complement* to search rather than
    a competitor to it.

    `fetch()` keeps the `BaseConnector` signature so the existing
    `build_connectors` / `run_source` / `run_brand` plumbing works unchanged. With
    no URLs supplied it yields nothing and says why, rather than inventing a
    crawl of its own.
    """

    source_name = "web"  # provenance is per-URL; see to_review()

    def __init__(
        self,
        urls: list[str] | None = None,
        *,
        engine: ScraplingEngine | None = None,
        metrics: metricsmod.RunMetrics | None = None,
        crawl_budget: budgets.CrawlBudget | None = None,
    ) -> None:
        if not ENABLED:
            # Same contract the other connectors use for "not configured": a
            # KeyError makes build_connectors disable this cleanly.
            raise KeyError("SCRAPLING_ENABLED")
        if not ScraplingEngine.parser_available():
            raise KeyError("scrapling")
        self.urls = list(urls or [])
        self.engine = engine or ScraplingEngine(
            metrics=metrics or metricsmod.RunMetrics(),
            crawl_budget=crawl_budget or budgets.CrawlBudget(),
        )

    @property
    def metrics(self) -> metricsmod.RunMetrics:
        return self.engine.metrics

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        targets = list(self.urls)
        if website:
            # The brand's own site is a legitimate target (many brands host
            # testimonials), but it is policy-checked like any other URL — this
            # is the field a user types, so it is the SSRF entry point.
            canonical_site = urlpolicy.canonical(website)
            if canonical_site and canonical_site not in targets:
                targets.append(canonical_site)

        if not targets:
            log.info(
                "scrapling: no URLs supplied for brand=%s — this engine reads pages "
                "that the search sources discovered, it does not search",
                brand,
            )
            return

        seen_urls: set[str] = set()
        emitted = 0
        for raw_url in targets:
            if emitted >= limit:
                return
            url = urlpolicy.canonical(raw_url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                candidates = self.engine.harvest(url)
            except Exception:  # noqa: BLE001 — one URL must not abort the source
                log.exception("scrapling: harvest failed for %s", url)
                continue

            for candidate in candidates:
                if emitted >= limit:
                    return
                review = to_review(brand, candidate, discovered_via=url)
                if review is None:
                    continue
                emitted += 1
                yield review

        log.info(
            "scrapling: brand=%s emitted=%d from %d url(s) | %s",
            brand, emitted, len(seen_urls), self.engine.metrics.summary_line(),
        )

    def close(self) -> None:
        self.engine.close()
