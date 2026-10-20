"""Target-driven collection: gather as many real mentions as we can, anywhere.

The product goal: for ANY brand, pull public reviews and mentions from across the
internet toward a soft target (200), keeping at least a floor (150) when we can,
using the cheapest and most open sources first and treating paid services as the
last resort. It must be resilient: one dead source, or a broken/irrelevant brand
website, must NEVER make the whole run return nothing.

Order of attack:
  1. Cloud-safe, keyless/cheap sources (web search, Hacker News, Mastodon,
     app stores).
  2. **Depth pass (Scrapling).** The pages stage 1's web search surfaced, read
     properly. This is the stage that changes the character of a collection: a
     search API returns a two-sentence SERP snippet, so three search passes over
     the same forum thread return three snippets of it, while one fetch of that
     thread returns the whole discussion. Breadth comes from search; depth comes
     from here.
  3. Social-SEO discovery: extra web queries biased toward the social platforms
     (X, LinkedIn, Instagram, ...), whose public posts are indexed by search.
     Host-tagging sorts those hits into the right platform bucket.
  4. Broaden: domain-free brand search with intent terms, deeper pagination, so a
     dead or wrong website never means zero results.
  5. **AI extraction (ScrapeGraphAI), opt-in.** Only for pages stage 2 fetched
     successfully and could not parse, and only while the run's spend budget
     allows. Never a fetch, never a page we could already read.
  6. Walled gardens via Apify (Instagram, Facebook, Maps, Yelp, TripAdvisor) as a
     LAST resort, and only when an Apify token is configured.

The brand `website` is a soft anchor: used to scope step 1's web search, then
dropped for the broad steps so it can only help, never block.

**On the target.** `target`/`floor` are unchanged (200/150 by default) and are
reached by collecting *more genuine data*, never by relaxing what counts as a
record. Every record from every stage goes through `quality.prepare()`, so the
count reported is validated, deduplicated records — and `collected_new`
distinguishes rows that were genuinely new from rows re-found from a previous
run, which the old count did not.
"""

from __future__ import annotations

import logging
import os

from .. import quality, runs
from ..scraping import budget as budgets
from ..scraping import metrics as metricsmod
from ..scraping import urlpolicy
from . import loader, routing
from .run import build_connectors

log = logging.getLogger("reviewbot.collect")

# Cloud-safe default sources, tried first. Direct Reddit and Trustpilot remain
# opt-in source choices: Railway IPs are often blocked by Reddit, and Trustpilot
# needs a brand domain plus optional Playwright browser dependencies.
FREE_PLAN = ["web", "hackernews", "mastodon", "app_store", "google_play"]
# Walled gardens reachable only via Apify. Last resort, skipped without a token.
APIFY_PLAN = ["instagram", "facebook", "google_maps", "yelp", "tripadvisor"]
# Search hints that bias the web source toward socially-indexed content.
SOCIAL_HINTS = ["X Twitter", "LinkedIn", "Instagram", "Facebook", "TikTok"]

DEFAULT_TARGET = int(os.environ.get("COLLECT_TARGET", "200"))
DEFAULT_FLOOR = int(os.environ.get("COLLECT_FLOOR", "150"))

#: How many discovered URLs the depth pass may consider. The crawl budget bounds
#: what it actually fetches; this bounds what it looks at.
MAX_DEPTH_URLS = int(os.environ.get("SCRAPLING_MAX_URLS", "30"))


def _apify_ready() -> bool:
    return bool(os.environ.get("APIFY_TOKENS") or os.environ.get("APIFY_TOKEN"))


def _scrapling_ready() -> bool:
    if os.environ.get("SCRAPLING_ENABLED", "1") == "0":
        return False
    try:
        from ..connectors.scrapling_source import ScraplingEngine

        return ScraplingEngine.parser_available()
    except Exception:  # noqa: BLE001
        return False


def _scrapegraph_ready() -> bool:
    if os.environ.get("SCRAPEGRAPH_ENABLED", "0") != "1":
        return False
    try:
        from ..connectors import scrapegraph_source as sg

        return bool(sg.resolve_backend())
    except Exception:  # noqa: BLE001
        return False


def collect_until(
    brand_cfg: dict,
    target: int = DEFAULT_TARGET,
    floor: int = DEFAULT_FLOOR,
    on_progress=None,
) -> dict:
    """Collect toward `target` distinct mentions; never raises, never returns
    nothing just because one source (or the website) failed.

    Returns {total, collected_new, by_source, by_engine, quality, reached_target,
    floor_met, target, floor}. `total` keeps its original meaning (distinct
    records written this run) so existing callers are unaffected.
    """
    brand = brand_cfg["name"]
    base_keywords = brand_cfg.get("keywords") or [brand]
    website = brand_cfg.get("website") or None
    per_source = int(brand_cfg.get("limit") or 50)

    started = runs.now()
    dedup = quality.Deduplicator()
    report = quality.QualityReport()
    metrics = metricsmod.RunMetrics()
    by_source: dict[str, int] = {}
    new_rows = 0

    # URLs the search sources surfaced, in discovery order, for the depth pass.
    discovered: list[str] = []
    # (url, html) for pages the depth pass fetched but could not parse — the only
    # input the AI stage is ever offered.
    unparsed: list[tuple[str, str]] = []
    # url -> page-level mentions held back pending an AI attempt on that page.
    mention_fallback: dict[str, list] = {}

    def _note_urls(reviews) -> None:
        for review in reviews:
            url = urlpolicy.canonical(review.source_url)
            if url and url not in discovered:
                discovered.append(url)

    def _run(source: str, limit: int, website_arg: str | None, keywords: list[str]) -> None:
        nonlocal new_rows
        for connector in build_connectors([source]):
            try:
                reviews = list(connector.fetch(brand, keywords, limit, website=website_arg))
            except Exception:  # noqa: BLE001 — a source blowing up must not abort the run
                log.exception("collect: source=%s fetch failed for brand=%s", source, brand)
                metrics.failure(source)
                reviews = []
            else:
                metrics.success(source, items=len(reviews))
            metrics.attempt(source)
            _note_urls(reviews)
            _store(connector.source_name, reviews)

    def _store(engine_label: str, reviews) -> None:
        """Validate, deduplicate and load one engine's output."""
        nonlocal new_rows
        before = quality.QualityReport()
        fresh = quality.prepare(
            reviews, keywords=base_keywords, dedup=dedup, report=before
        )
        # Fold the per-call report into the run's.
        report.kept += before.kept
        report.rejected += before.rejected
        report.duplicates += before.duplicates
        report.repaired += before.repaired
        for reason, count in before.reasons.items():
            report.reasons[reason] = report.reasons.get(reason, 0) + count
        metrics.records(
            engine_label, valid=before.kept, rejected=before.rejected,
            duplicates=before.duplicates,
        )

        if not fresh:
            if on_progress:
                _progress(engine_label, 0)
            return
        try:
            sent, inserted = loader.load_counted(fresh)
        except Exception:  # noqa: BLE001 — a load failure on one source is not fatal
            log.exception("collect: load failed for engine=%s brand=%s", engine_label, brand)
            # These records were not stored, so they must not count as collected.
            for review in fresh:
                dedup.ids.discard(review.id)
                report.kept -= 1
            return
        new_rows += inserted
        by_source[engine_label] = by_source.get(engine_label, 0) + sent
        if on_progress:
            _progress(engine_label, sent)

    def _progress(engine_label: str, written: int) -> None:
        try:
            on_progress(engine_label, written, len(dedup))
        except Exception:  # noqa: BLE001
            log.exception("collect: progress callback failed")

    # ---- 1) Free sources. Only the web search uses the website anchor. ------
    for source in FREE_PLAN:
        if len(dedup) >= target:
            break
        _run(source, per_source, website if source == "web" else None, base_keywords)

    # ---- 2) Depth pass: read the pages stage 1 only glimpsed ---------------
    router = routing.Router(
        ai_enabled=_scrapegraph_ready(),
        browser_enabled=os.environ.get("SCRAPLING_BROWSER_ENABLED", "0") == "1",
    )
    crawl_budget = budgets.CrawlBudget()
    if len(dedup) < target and _scrapling_ready():
        try:
            router.history = routing.load_history(
                [urlpolicy.registrable_domain(u) for u in discovered[:MAX_DEPTH_URLS]]
            )
            unparsed = _depth_pass(
                brand=brand,
                urls=discovered[:MAX_DEPTH_URLS],
                website=website,
                router=router,
                crawl_budget=crawl_budget,
                metrics=metrics,
                store=_store,
                remaining=lambda: target - len(dedup),
                mention_fallback=mention_fallback,
            )
        except Exception:  # noqa: BLE001 — the depth pass is additive; never fatal
            log.exception("collect: depth pass failed for brand=%s", brand)

    # ---- 3) Social-SEO discovery via extra web queries --------------------
    if len(dedup) < target:
        _run("web", per_source, None, [f"{brand} {hint}" for hint in SOCIAL_HINTS])

    # ---- 4) Broaden: domain-free brand search with intent terms, deeper ----
    if len(dedup) < floor:
        broad = list(base_keywords) + [
            f"{brand} review", f"{brand} complaints", f"{brand} experience"
        ]
        _run("web", per_source * 2, None, broad)

    # ---- 5) AI extraction, only for pages we fetched but could not parse ---
    keep_going, why = router.should_continue(len(dedup), target, floor, paid=True)
    if unparsed and router.ai_enabled and keep_going:
        try:
            _ai_pass(
                brand=brand,
                pages=unparsed,
                router=router,
                metrics=metrics,
                store=_store,
                remaining=lambda: target - len(dedup),
                mention_fallback=mention_fallback,
            )
        except Exception:  # noqa: BLE001
            log.exception("collect: AI extraction pass failed for brand=%s", brand)
            # Do not lose the held-back mentions to a failure in the AI stage.
            for held in mention_fallback.values():
                _store("scrapling", held)
    else:
        if unparsed and not keep_going:
            metrics.skip("scrapegraphai", why)
        # No AI stage ran, so nothing is going to supersede these.
        for held in mention_fallback.values():
            _store("scrapling", held)

    # ---- 6) Walled gardens via Apify, last resort, only if a token is set --
    keep_going, why = router.should_continue(len(dedup), target, floor, paid=True)
    if keep_going and _apify_ready():
        for source in APIFY_PLAN:
            if len(dedup) >= target:
                break
            _run(source, per_source, website, base_keywords)
    elif not keep_going:
        metrics.skip("apify", why)

    total = len(dedup)
    metrics.stop_reason = crawl_budget.stop_reason or metrics.stop_reason
    result = {
        "total": total,
        # The honest novelty number: rows that did not already exist. The old
        # count included rows re-found from a previous run, which made a repeat
        # onboard look like a full fresh collection.
        "collected_new": new_rows,
        "by_source": by_source,
        "by_engine": metrics.as_dict(),
        "quality": report.as_dict(),
        "routing": router.snapshot(),
        "reached_target": total >= target,
        "floor_met": total >= floor,
        "target": target,
        "floor": floor,
    }

    # Record the run so it shows up in the dashboard's scraping history. The
    # onboarding path never did this — only `run_brand` did — so every brand
    # added through the product's own front door had an empty history.
    try:
        runs.record_run(
            brand,
            list(by_source.keys()),
            total,
            "ok" if total else "empty",
            started,
            runs.now(),
            engines=metrics.engines_used(),
            by_engine=result["by_engine"],
            target=target,
            floor=floor,
            ai_calls=metrics.total_ai_calls(),
            ai_cost_usd=metrics.total_ai_cost(),
        )
    except Exception:  # noqa: BLE001
        log.exception("collect: could not record the run for brand=%s", brand)

    try:
        routing.record_history(metrics)
    except Exception:  # noqa: BLE001
        pass

    log.info(
        "collect_until brand=%s total=%d new=%d by_source=%s | %s | quality=%s",
        brand, total, new_rows, by_source, metrics.summary_line(), report.as_dict(),
    )
    return result


def _depth_pass(
    *, brand, urls, website, router, crawl_budget, metrics, store, remaining,
    mention_fallback=None,
) -> list[tuple[str, str]]:
    """Fetch and extract the discovered pages. Returns pages that stayed unparsed.

    The unparsed list is what makes stage C cheap: the AI engine is handed
    content that is already fetched and already known to defeat selectors.

    `mention_fallback` collects page-level mentions that are being held back
    pending an AI attempt on the same page (see the escalation logic below).
    """
    from ..connectors.scrapling_source import ScraplingEngine, to_review
    from ..scraping import extract as extractor

    engine = ScraplingEngine(crawl_budget=crawl_budget, metrics=metrics)
    unparsed: list[tuple[str, str]] = []
    if mention_fallback is None:
        mention_fallback = {}

    targets = list(urls)
    if website:
        site = urlpolicy.canonical(website)
        if site and site not in targets:
            # The brand's own site often hosts testimonials. It is also the field
            # a user types, so it is policy-checked like any other URL.
            targets.append(site)

    try:
        for url in targets:
            if remaining() <= 0:
                metrics.skip("scrapling", "target reached")
                break
            if crawl_budget.expired():
                metrics.skip("scrapling", crawl_budget.stop_reason or "deadline reached")
                break

            decision = router.route(url)
            if decision.engine is not routing.Engine.SCRAPLING:
                metrics.skip("scrapling", decision.reason)
                continue

            skip = engine.should_skip(url)
            if skip:
                metrics.skip("scrapling", skip)
                continue

            result = engine.fetch(url, tier=decision.tier if decision.tier != "auto" else None)
            if not result.ok:
                continue

            page = extractor.extract(result.html, result.url)
            if engine.needs_browser(result, page):
                retried = engine.fetch(url, tier="browser")
                if retried.ok:
                    result = retried
                    page = extractor.extract(retried.html, retried.url)

            reviews = []
            for candidate in page.candidates:
                candidate.extra.setdefault("fetch_tier", result.tier)
                review = to_review(brand, candidate, discovered_via=url)
                if review is not None:
                    reviews.append(review)
            real_reviews = sum(
                1 for c in page.candidates if c.kind == "review"
            )

            # Should the AI stage see this page? It should when the page yielded
            # no actual reviews, which includes the case where the only thing
            # extractable was a whole-page mention from the article fallback.
            escalation = router.may_escalate_to_ai(
                url,
                had_content=bool(result.html),
                extracted=len(page.candidates),
                reviews=real_reviews,
                strategy=page.strategy,
            )
            escalate = escalation.engine is routing.Engine.SCRAPEGRAPH

            if reviews and not (escalate and page.strategy == "article"):
                store("scrapling", reviews)
            elif reviews:
                # Hold the page-level mention back: if AI extraction finds the
                # real reviews, they supersede it, and storing both would
                # double-count the page. If it finds nothing, the mention is
                # stored by the AI pass's fallback below.
                mention_fallback[result.url] = reviews

            if escalate:
                unparsed.append((result.url, result.html))
            elif not reviews:
                metrics.skip("scrapegraphai", escalation.reason)

            # Follow the page's own "next" link while there is headroom.
            if reviews:
                for next_url in page.next_urls[:1]:
                    if remaining() <= 0 or not crawl_budget.allows(next_url):
                        break
                    nxt = engine.fetch(next_url)
                    if not nxt.ok:
                        break
                    nxt_page = extractor.extract(nxt.html, nxt.url)
                    more = [
                        r for r in (
                            to_review(brand, c, discovered_via=next_url)
                            for c in nxt_page.candidates
                        ) if r is not None
                    ]
                    if more:
                        store("scrapling", more)
    finally:
        engine.close()

    return unparsed


def _ai_pass(
    *, brand, pages, router, metrics, store, remaining, mention_fallback=None
) -> None:
    """AI-extract the pages that defeated deterministic extraction.

    For a page whose only deterministic yield was a whole-page mention, that
    mention was held back. If AI extraction finds the real reviews, they are
    stored and the mention is dropped as superseded; if it finds nothing, the
    mention is stored so the page still contributes what it can.
    """
    from ..connectors import scrapegraph_source as sg

    mention_fallback = mention_fallback or {}
    extractor = sg.AiExtractor(spend=budgets.SpendBudget(), metrics=metrics)
    if not extractor.available:
        metrics.skip("scrapegraphai", "no AI backend available")
        # Nothing was going to supersede the held-back mentions, so store them.
        for held in mention_fallback.values():
            store("scrapling", held)
        return

    handled: set[str] = set()
    for url, html in pages:
        if remaining() <= 0:
            metrics.skip("scrapegraphai", "target reached")
            break
        outcome = extractor.extract(html, url)
        handled.add(url)
        reviews = [
            r for r in (
                sg.to_review(brand, record, url, outcome, discovered_via=url)
                for record in outcome.records
            ) if r is not None
        ]
        if reviews:
            store("scrapegraphai", reviews)
        elif url in mention_fallback:
            # AI found nothing better: fall back to the page-level mention.
            store("scrapling", mention_fallback[url])

    # Pages we never got to (budget, target reached) keep their mention.
    for url, held in mention_fallback.items():
        if url not in handled:
            store("scrapling", held)

    log.info("collect: AI extraction spend for brand=%s: %s", brand, extractor.spend.snapshot())
