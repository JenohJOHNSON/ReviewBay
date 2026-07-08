"""Target-driven collection: gather as many real mentions as we can, anywhere.

The product goal: for ANY brand, pull public reviews and mentions from across the
internet toward a soft target (200), keeping at least a floor (150) when we can,
using the cheapest and most open sources first and treating Apify as the last
resort. It must be resilient: one dead source, or a broken/irrelevant brand
website, must NEVER make the whole run return nothing.

Order of attack:
  1. Cloud-safe, keyless/cheap sources (web search, Hacker News, Mastodon,
     app stores).
  2. Social-SEO discovery: extra web queries biased toward the social platforms
     (X, LinkedIn, Instagram, ...), whose public posts are indexed by search.
     Host-tagging sorts those hits into the right platform bucket.
  3. Broaden: domain-free brand search with intent terms, deeper pagination, so a
     dead or wrong website never means zero results.
  4. Walled gardens via Apify (Instagram, Facebook, Maps, Yelp, TripAdvisor) as a
     LAST resort, and only when an Apify token is configured.

The brand `website` is a soft anchor: used to scope step 1's web search, then
dropped for the broad steps so it can only help, never block.
"""

from __future__ import annotations

import logging
import os

from . import loader
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


def _apify_ready() -> bool:
    return bool(os.environ.get("APIFY_TOKENS") or os.environ.get("APIFY_TOKEN"))


def collect_until(
    brand_cfg: dict,
    target: int = DEFAULT_TARGET,
    floor: int = DEFAULT_FLOOR,
    on_progress=None,
) -> dict:
    """Collect toward `target` distinct mentions; never raises, never returns
    nothing just because one source (or the website) failed.

    Returns {total, by_source, reached_target, floor_met, target, floor}.
    """
    brand = brand_cfg["name"]
    base_keywords = brand_cfg.get("keywords") or [brand]
    website = brand_cfg.get("website") or None
    per_source = int(brand_cfg.get("limit") or 50)

    loaded_ids: set[str] = set()          # distinct reviews written this run
    by_source: dict[str, int] = {}

    def _run(source: str, limit: int, website_arg: str | None, keywords: list[str]) -> None:
        for connector in build_connectors([source]):
            try:
                reviews = list(connector.fetch(brand, keywords, limit, website=website_arg))
            except Exception:  # noqa: BLE001 — a source blowing up must not abort the run
                log.exception("collect: source=%s fetch failed for brand=%s", source, brand)
                reviews = []
            # Dedupe against everything already loaded this run so re-found posts
            # (e.g. the same URL from two passes) don't inflate the count.
            fresh = [r for r in reviews if r.id not in loaded_ids]
            try:
                loader.load(fresh)
            except Exception:  # noqa: BLE001 — a load failure on one source is not fatal
                log.exception("collect: load failed for source=%s brand=%s", source, brand)
                continue
            for r in fresh:
                loaded_ids.add(r.id)
            by_source[connector.source_name] = by_source.get(connector.source_name, 0) + len(fresh)
            if on_progress:
                try:
                    on_progress(connector.source_name, len(fresh), len(loaded_ids))
                except Exception:  # noqa: BLE001
                    log.exception("collect: progress callback failed")

    # 1) Free sources. Only the web search uses the website anchor.
    for source in FREE_PLAN:
        if len(loaded_ids) >= target:
            break
        _run(source, per_source, website if source == "web" else None, base_keywords)

    # 2) Social-SEO discovery via extra web queries (host-tagging buckets them).
    if len(loaded_ids) < target:
        _run("web", per_source, None, [f"{brand} {hint}" for hint in SOCIAL_HINTS])

    # 3) Broaden: domain-free brand search with intent terms, deeper.
    if len(loaded_ids) < floor:
        broad = list(base_keywords) + [f"{brand} review", f"{brand} complaints", f"{brand} experience"]
        _run("web", per_source * 2, None, broad)

    # 4) Walled gardens via Apify, last resort, only if a token is set.
    if len(loaded_ids) < floor and _apify_ready():
        for source in APIFY_PLAN:
            if len(loaded_ids) >= target:
                break
            _run(source, per_source, website, base_keywords)

    total = len(loaded_ids)
    result = {
        "total": total,
        "by_source": by_source,
        "reached_target": total >= target,
        "floor_met": total >= floor,
        "target": target,
        "floor": floor,
    }
    log.info("collect_until brand=%s total=%d by_source=%s", brand, total, by_source)
    return result
