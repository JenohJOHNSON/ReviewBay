"""Source taxonomy: which bucket each source belongs to.

Keeps "web" from meaning everything. App reviews, review sites, and social are
distinct kinds of feedback, so the dashboard groups them instead of showing one
flat list. Add a new source here when you add a new connector.
"""

from __future__ import annotations

# One category per source. Anything unknown falls into "Web & News" (the catch-all).
SOURCE_CATEGORIES = {
    "app_store": "App Reviews",
    "google_play": "App Reviews",
    "google_maps": "Review Sites",
    "yelp": "Review Sites",
    "tripadvisor": "Review Sites",
    "trustpilot": "Review Sites",
    "reddit": "Social",
    "mastodon": "Social",
    "hackernews": "Social",
    "instagram": "Social",
    "facebook": "Social",
    "youtube": "Social",
    "twitter": "Social",
    "linkedin": "Social",
    "tiktok": "Social",
    "threads": "Social",
    "web": "Web & News",
}

# Display order for the dashboard.
CATEGORY_ORDER = ["App Reviews", "Review Sites", "Social", "Web & News"]

# Human-friendly labels for the raw source ids.
SOURCE_LABELS = {
    "app_store": "App Store",
    "google_play": "Google Play",
    "google_maps": "Google Maps",
    "yelp": "Yelp",
    "tripadvisor": "TripAdvisor",
    "trustpilot": "Trustpilot",
    "reddit": "Reddit",
    "mastodon": "Mastodon",
    "hackernews": "Hacker News",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "twitter": "X (Twitter)",
    "linkedin": "LinkedIn",
    "tiktok": "TikTok",
    "threads": "Threads",
    "web": "Web & News",
}


# A web/search hit on one of these hosts is re-tagged from "web" to the platform,
# so it categorizes correctly on the dashboard. This is how we cover "Social
# SEO'd" content: X / LinkedIn / Instagram posts that are publicly indexed show
# up in web search and get tagged to their platform here, no direct scraping.
# Double-counting is not a concern: every review has a stable id keyed on
# (source, url, text), so the same post found twice upserts to one row.
HOST_SOURCES = (
    ("reddit.com", "reddit"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("trustpilot.com", "trustpilot"),
    ("news.ycombinator.com", "hackernews"),
    ("x.com", "twitter"),
    ("twitter.com", "twitter"),
    ("linkedin.com", "linkedin"),
    ("instagram.com", "instagram"),
    ("facebook.com", "facebook"),
    ("tiktok.com", "tiktok"),
    ("threads.net", "threads"),
    ("threads.com", "threads"),
)


def source_from_url(url: str, default: str = "web") -> str:
    """Tag a URL by its host (reddit/youtube/trustpilot), else the default.

    Host is matched exactly or as a subdomain, so 'notreddit.com.evil.com' does
    NOT match 'reddit.com'.
    """
    host = (url or "").lower().split("://")[-1].split("/")[0]
    for needle, source in HOST_SOURCES:
        if host == needle or host.endswith("." + needle):
            return source
    return default


def category_for(source: str) -> str:
    return SOURCE_CATEGORIES.get(source, "Web & News")


def label_for(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def connector_statuses() -> list[dict]:
    """Which scraping connectors are available, as a simple Active/Inactive flag.

    A source is "active" when it is configured and ready to run (it has whatever
    credential it needs, or needs none), and "inactive" otherwise. We deliberately
    do NOT expose implementation details (which actor/library, verification notes),
    just the user-facing on/off state.
    """
    import os

    has_apify = bool(os.environ.get("APIFY_TOKENS") or os.environ.get("APIFY_TOKEN"))
    has_tavily = bool(os.environ.get("TAVILY_API_KEY"))
    has_firecrawl = bool(os.environ.get("FIRECRAWL_API_KEY"))

    def row(key, label, category, active):
        return {"key": key, "label": label, "category": category,
                "status": "active" if active else "inactive"}

    return [
        # Web & News
        row("web", "Web & News", "Web & News", has_tavily or has_apify),
        row("firecrawl", "Firecrawl", "Web & News", has_firecrawl),
        # App Reviews (free, no key)
        row("app_store", "App Store", "App Reviews", True),
        row("google_play", "Google Play", "App Reviews", True),
        # Review Sites
        row("trustpilot", "Trustpilot", "Review Sites", True),  # self-hosted, no key
        row("google_maps", "Google Maps", "Review Sites", has_apify),
        row("yelp", "Yelp", "Review Sites", has_apify),
        row("tripadvisor", "TripAdvisor", "Review Sites", has_apify),
        # Social (Reddit / Mastodon / Hacker News are free and keyless)
        row("reddit", "Reddit", "Social", True),
        row("mastodon", "Mastodon", "Social", True),
        row("hackernews", "Hacker News", "Social", True),
        row("instagram", "Instagram", "Social", has_apify),
        row("facebook", "Facebook", "Social", has_apify),
    ]


def group_by_category(by_source: list[dict]) -> list[dict]:
    """Fold a [{source, count}] list into ordered category buckets.

    Returns [{category, count, sources: [{source, label, count}]}], sorted by the
    fixed CATEGORY_ORDER, with any leftover categories after it.
    """
    buckets: dict[str, dict] = {}
    for row in by_source:
        src = row.get("source") or "web"
        cat = category_for(src)
        b = buckets.setdefault(cat, {"category": cat, "count": 0, "sources": []})
        b["count"] += row.get("count") or 0
        b["sources"].append(
            {"source": src, "label": label_for(src), "count": row.get("count") or 0}
        )

    ordered = [buckets[c] for c in CATEGORY_ORDER if c in buckets]
    ordered += [b for c, b in buckets.items() if c not in CATEGORY_ORDER]
    for b in ordered:
        b["sources"].sort(key=lambda s: s["count"], reverse=True)
    return ordered
