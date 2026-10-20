"""Which engine should read which page, and when to stop.

The brief for this part was explicit about what *not* to build: do not add two
connectors and call both on every website, and do not add an LLM to decide which
scraper to run. So this is rules, with evidence, and every decision it makes is
inspectable and logged.

**The shape of the problem.** ReviewBay has four classes of extractor with very
different cost and reliability:

| stage | engines | cost | when it is right |
|---|---|---|---|
| A | existing specialized connectors | free / cheap | an official API exists for that platform |
| B | Scrapling | free (our CPU) | the page is public and parseable |
| C | ScrapeGraphAI | per-call LLM spend | the page defeats selectors and the budget allows |
| D | Firecrawl / Apify | per-call vendor spend | nothing else can reach it and a token exists |

The orchestration question is never "which is best" in the abstract. It is "for
*this URL*, what is the cheapest engine that is likely to work", and the honest
answer depends on things we can actually observe: who owns the URL, whether a
dedicated connector already covers it, what happened the last time we tried this
domain, and how much budget is left.

**The rules, in order.**

1. **Never duplicate a dedicated connector.** An `apps.apple.com` URL goes to
   `app_store`, which reads Apple's own feed. Fetching that page would be slower,
   more fragile and less complete. Same for Play, Hacker News and Mastodon.
2. **Never fetch what must not be fetched.** The URL policy and robots.txt decide
   this, not the router, but the router asks before spending anything.
3. **Deterministic before AI.** Scrapling runs first on every eligible page. The
   AI engine is only offered a page that Scrapling fetched successfully and
   extracted nothing useful from — which means the AI engine never pays for a
   fetch, never sees a page we could already read, and always has real content to
   work from.
4. **Remember what worked.** `marts.engine_stats` records per-domain outcomes.
   A domain where Scrapling has failed its last several attempts is demoted, so
   the run stops paying for a fetch that is not going to work.
5. **Stop at enough.** The target is a target, not a quota. When the floor is met
   from genuine sources, later stages are skipped rather than run to pad a number.

**What this does not do.** It does not reorder the existing `collect_until`
stages or take over source selection: `collect.py` still owns the plan. This
module answers "for this URL, which engine?" and "is it worth continuing?".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

from ..scraping import urlpolicy

log = logging.getLogger(__name__)


class Engine(str, Enum):
    """The engines a URL can be routed to."""

    CONNECTOR = "connector"      # stage A: an existing specialized connector
    SCRAPLING = "scrapling"      # stage B
    SCRAPEGRAPH = "scrapegraphai"  # stage C
    VENDOR = "vendor"            # stage D: Firecrawl / Apify
    SKIP = "skip"


#: Hosts a dedicated connector already covers properly. Routed to CONNECTOR,
#: which in practice means "this engine leaves it alone".
#: Kept here rather than in the connector so the routing table is readable in
#: one place; `scrapling_source.SKIP_HOSTS` enforces the same thing at the fetch.
CONNECTOR_HOSTS = {
    "apps.apple.com": "app_store",
    "itunes.apple.com": "app_store",
    "play.google.com": "google_play",
    "news.ycombinator.com": "hackernews",
    "mastodon.social": "mastodon",
}

#: Platforms whose terms prohibit direct fetching, or that require a login.
#: ReviewBay reaches these only through search indexing or a vendor actor, which
#: is the existing "social SEO" strategy — not by fetching them ourselves.
NO_DIRECT_FETCH_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "threads.net", "threads.com", "tiktok.com", "youtube.com", "youtu.be",
}

#: Search engines. A SERP is not a review page.
SEARCH_HOSTS = {"google.com", "bing.com", "duckduckgo.com", "search.yahoo.com", "baidu.com"}

#: Domains where review content is reliably behind client-side rendering. Worth
#: the browser tier when it is available; still Scrapling, just a costlier tier.
JS_HEAVY_HINTS = tuple(
    h.strip().lower()
    for h in os.environ.get("SCRAPLING_JS_HOSTS", "").split(",")
    if h.strip()
)

#: Below this many successes out of the last attempts, stop trying a domain.
MIN_DOMAIN_SUCCESS_RATE = float(os.environ.get("ROUTING_MIN_SUCCESS_RATE", "0.2"))
#: Only apply that rule once there is enough history to mean anything.
MIN_ATTEMPTS_TO_JUDGE = int(os.environ.get("ROUTING_MIN_ATTEMPTS", "4"))

STRATEGY = os.environ.get("SCRAPING_ENGINE_STRATEGY", "balanced").strip().lower()


@dataclass
class Decision:
    """One routing decision, with the reason it was made.

    The reason is not decoration: it goes into the run metrics, so an operator
    can see *why* a collection used what it used.
    """

    engine: Engine
    reason: str
    url: str = ""
    tier: str = ""          # for SCRAPLING: "requests" | "scrapling" | "browser"
    connector: str = ""     # for CONNECTOR: which one covers it

    @property
    def fetchable(self) -> bool:
        return self.engine in (Engine.SCRAPLING, Engine.VENDOR)


@dataclass
class DomainHistory:
    """What previous runs learned about a domain, per engine."""

    attempts: int = 0
    successes: int = 0
    items: int = 0

    @property
    def success_rate(self) -> float | None:
        return (self.successes / self.attempts) if self.attempts else None

    @property
    def judgeable(self) -> bool:
        return self.attempts >= MIN_ATTEMPTS_TO_JUDGE


@dataclass
class Router:
    """Rule-based engine selection for one collection run.

    `history` is loaded once per run (one query), so routing adds no per-URL
    database traffic. An empty history is fine: the static rules carry it, and the
    stats only ever demote a domain that has actually been failing.
    """

    history: dict[tuple[str, str], DomainHistory] = field(default_factory=dict)
    strategy: str = STRATEGY
    ai_enabled: bool = False
    browser_enabled: bool = False
    #: Decisions made, by reason, for the run record.
    decisions: dict[str, int] = field(default_factory=dict)

    # -- the rules -----------------------------------------------------------

    def route(self, url: str) -> Decision:
        """Pick the engine for `url`. Cheap: no network, no database."""
        decision = self._route(url)
        self.decisions[f"{decision.engine.value}: {decision.reason}"] = (
            self.decisions.get(f"{decision.engine.value}: {decision.reason}", 0) + 1
        )
        return decision

    def _route(self, url: str) -> Decision:
        verdict = urlpolicy.check(url, resolve=False)
        if not verdict.allowed:
            return Decision(Engine.SKIP, f"url policy: {verdict.reason}", url)

        host = (verdict.host or "").lower()
        domain = urlpolicy.registrable_domain(url)

        # Rule 1: a dedicated connector already does this better.
        for candidate, connector in CONNECTOR_HOSTS.items():
            if host == candidate or host.endswith("." + candidate):
                return Decision(
                    Engine.CONNECTOR,
                    f"the {connector} connector already covers this platform via its own API",
                    url, connector=connector,
                )

        # Rule 2: platforms we do not fetch directly.
        for candidate in NO_DIRECT_FETCH_HOSTS:
            if host == candidate or host.endswith("." + candidate) or domain == candidate:
                return Decision(
                    Engine.VENDOR if self.strategy == "thorough" else Engine.SKIP,
                    "platform does not permit direct fetching; reachable only via "
                    "search indexing or a vendor actor",
                    url,
                )
        for candidate in SEARCH_HOSTS:
            if host == candidate or host.endswith("." + candidate) or domain == candidate:
                return Decision(Engine.SKIP, "a search results page is not a review page", url)

        # Rule 4: demote a domain that has been failing.
        scrapling_history = self.history.get((domain, Engine.SCRAPLING.value))
        if (
            scrapling_history
            and scrapling_history.judgeable
            and (scrapling_history.success_rate or 0) < MIN_DOMAIN_SUCCESS_RATE
        ):
            if self.ai_enabled and self.strategy != "cheap":
                return Decision(
                    Engine.SCRAPEGRAPH,
                    f"scrapling succeeds on {domain} only "
                    f"{scrapling_history.success_rate:.0%} of the time",
                    url,
                )
            return Decision(
                Engine.SKIP,
                f"scrapling succeeds on {domain} only "
                f"{scrapling_history.success_rate:.0%} of the time and no fallback is enabled",
                url,
            )

        # Rule 3: deterministic first, at the cheapest tier likely to work.
        tier = "auto"
        if self.browser_enabled and any(
            domain == hint or host.endswith(hint) for hint in JS_HEAVY_HINTS
        ):
            tier = "browser"
        return Decision(Engine.SCRAPLING, "public page, deterministic extraction first", url, tier=tier)

    def may_escalate_to_ai(
        self,
        url: str,
        *,
        had_content: bool,
        extracted: int,
        reviews: int | None = None,
        strategy: str = "",
    ) -> Decision:
        """Whether a page Scrapling already fetched is worth an AI call.

        Called *after* a deterministic attempt, which is what makes stage C cheap
        to offer: the content is already in hand, so an escalation costs one model
        call and no fetch.

        `reviews` is the number of records the page yielded that are actual
        *reviews* rather than a whole-page mention, and it is the condition that
        matters. The benchmark caught why: `extract.extract_article` is a floor
        that returns a single page-level mention for almost any page, so judging
        on `extracted > 0` meant a page of real reviews written as prose — the
        exact case AI extraction exists for — was never offered to it, because the
        fallback had "already produced a record". A mention is not a review, and
        treating it as one made stage C unreachable in practice.
        """
        if not self.ai_enabled:
            return Decision(Engine.SKIP, "AI extraction is disabled", url)
        if self.strategy == "cheap":
            return Decision(Engine.SKIP, "strategy=cheap never escalates to AI", url)
        if not had_content:
            return Decision(
                Engine.SKIP, "nothing was fetched, so there is no content to reason over", url
            )

        # Prefer the explicit review count; fall back to the record count for
        # callers that do not distinguish (which then behaves as before).
        real_reviews = extracted if reviews is None else reviews
        if real_reviews > 0:
            return Decision(
                Engine.SKIP,
                f"deterministic extraction already produced {real_reviews} review(s)",
                url,
            )
        if strategy == "article":
            return Decision(
                Engine.SCRAPEGRAPH,
                "only a whole-page mention was extractable; AI extraction may find "
                "the individual reviews",
                url,
            )
        return Decision(
            Engine.SCRAPEGRAPH,
            "page fetched but no selectors matched; AI extraction may read it",
            url,
        )

    def should_continue(self, collected: int, target: int, floor: int, *, paid: bool = False) -> tuple[bool, str]:
        """Whether to run another stage.

        The product's `collect_until` contract is a soft target of ~200 with a
        floor of 150 "where sufficient genuine public data exists". So: stop at
        the target always, and stop *paid* work at the floor, because spending
        money to turn 160 genuine records into 200 is not what the floor is for.
        """
        if collected >= target:
            return False, f"target reached ({collected}/{target})"
        if paid and collected >= floor:
            return False, f"floor met ({collected}/{floor}); not spending to reach the target"
        return True, ""

    def snapshot(self) -> dict:
        return {
            "strategy": self.strategy,
            "ai_enabled": self.ai_enabled,
            "browser_enabled": self.browser_enabled,
            "decisions": dict(self.decisions),
        }


# --- per-domain history ------------------------------------------------------

def load_history(domains: list[str] | None = None) -> dict[tuple[str, str], DomainHistory]:
    """Per-(domain, engine) outcomes from `marts.engine_stats`.

    Advisory only: an unavailable table (migration not run) or an empty one just
    means the router uses its static rules, so this never raises.
    """
    try:
        from .. import db

        with db.connection() as conn:
            with conn.cursor() as cur:
                if domains:
                    cur.execute(
                        "SELECT domain, engine, attempts, successes, items "
                        "FROM marts.engine_stats WHERE domain = ANY(%s)",
                        (list({d for d in domains if d}),),
                    )
                else:
                    cur.execute(
                        "SELECT domain, engine, attempts, successes, items "
                        "FROM marts.engine_stats"
                    )
                rows = cur.fetchall()
    except Exception:  # noqa: BLE001 — routing history is an optimization, not a requirement
        log.debug("routing: engine_stats unavailable; using static rules", exc_info=True)
        return {}

    return {
        (domain, engine): DomainHistory(
            attempts=attempts or 0, successes=successes or 0, items=items or 0
        )
        for domain, engine, attempts, successes, items in rows
    }


def record_history(metrics) -> None:
    """Persist this run's per-domain outcomes so the next run routes better.

    Best-effort: observability must never fail a collection.
    """
    try:
        rows = []
        for engine_name, engine in (metrics.as_dict().get("engines") or {}).items():
            for domain, stats in (engine.get("by_domain") or {}).items():
                attempted = int(stats.get("attempted") or 0)
                if not attempted or not domain:
                    continue
                valid = int(stats.get("valid") or 0)
                rows.append(
                    (
                        domain,
                        engine_name,
                        attempted,
                        # "Success" for routing purposes means it produced data.
                        1 if valid else 0,
                        valid,
                        int(stats.get("ms") or 0),
                        "ok" if valid else "empty",
                    )
                )
        if not rows:
            return

        from .. import db

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO marts.engine_stats
                        (domain, engine, attempts, successes, items, total_ms,
                         last_status, last_attempt)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (domain, engine) DO UPDATE SET
                        attempts     = marts.engine_stats.attempts + EXCLUDED.attempts,
                        successes    = marts.engine_stats.successes + EXCLUDED.successes,
                        items        = marts.engine_stats.items + EXCLUDED.items,
                        total_ms     = marts.engine_stats.total_ms + EXCLUDED.total_ms,
                        last_status  = EXCLUDED.last_status,
                        last_attempt = EXCLUDED.last_attempt
                    """,
                    rows,
                )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.debug("routing: could not record engine stats", exc_info=True)
