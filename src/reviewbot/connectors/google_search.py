"""Web-mentions connector via Apify's google-search-scraper.

Surfaces brand mentions across the whole web (Reddit threads, blogs, forums,
news) that the per-platform connectors miss. It's the "any online platform"
catch-all.

Reddit is a special case: Reddit closed its own API to self-service in late 2025
(the Responsible Builder Policy needs manual approval), so we do NOT hit it
directly. Instead, Reddit is one of the most Google-indexed sites, so this web
search already surfaces the important Reddit threads for free. Results whose URL
is on reddit.com (or youtube.com) are TAGGED with that source, so they land in
the Social category on the dashboard instead of the generic "web" bucket. That
is what stops "web" from being a junk drawer.

Field mapping below is VALIDATED against real actor output (run
apify/google-search-scraper, 2026-07): each dataset item is one SERP whose
`organicResults` array holds the individual results (a different shape from the
review actors, where one item = one review), which is why this is its own
connector rather than another ActorSpec.

Set APIFY_TOKEN in the environment.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterator

from ..models import NormalizedReview
from ..sources import source_from_url
from .apify_client import ordered_tokens, run_with_fallback
from .base import BaseConnector

log = logging.getLogger(__name__)

_ACTOR = os.environ.get("APIFY_GOOGLE_SEARCH_ACTOR", "apify/google-search-scraper")


class GoogleSearchConnector(BaseConnector):
    source_name = "web"

    def __init__(self) -> None:
        ordered_tokens()  # validate ≥1 Apify token now; disables cleanly if none

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        # ~8-10 organic results per page; ask for enough pages to reach `limit`.
        pages = max(1, min(math.ceil(limit / 8), 8))
        # Anchor the query to the brand's domain when known, so an ambiguous name
        # (e.g. "Amadeus") targets the real company instead of a movie or an app.
        domain = _domain(website)
        suffix = f" {domain}" if domain else ""
        queries = "\n".join(f"{k} reviews{suffix}" for k in (keywords or [brand]))
        run_input = {
            "queries": queries,
            "maxPagesPerQuery": pages,
            "saveHtmlToKeyValueStore": False,
        }
        client, dataset_id = run_with_fallback(_ACTOR, run_input)
        if not client:
            return

        seen = 0
        for serp in client.dataset(dataset_id).iterate_items():
            # Each dataset item is one SERP; the results live in organicResults[].
            for r in serp.get("organicResults") or []:
                if seen >= limit:
                    return
                review = self._map_result(brand, r)
                if review:
                    seen += 1
                    yield review

    @staticmethod
    def _map_result(brand: str, r: dict) -> NormalizedReview | None:
        url = r.get("url")
        title = (r.get("title") or "").strip()
        desc = (r.get("description") or "").strip()
        text = (title + ("\n\n" + desc if desc else "")).strip()
        if not url or not text:
            return None
        return NormalizedReview(
            brand=brand,
            source=source_from_url(url),
            source_url=url,
            text=text,
            # websiteTitle is the surfacing site, e.g. "Reddit · r/Coffee", "Yelp".
            author=r.get("websiteTitle"),
            rating=_as_float(r.get("averageRating")),  # present on some results
            created_at=r.get("date"),  # ISO when Google exposes it; else None
            extra={
                "position": r.get("position"),
                "website_title": r.get("websiteTitle"),
                "displayed_url": r.get("displayedUrl"),
                "number_of_reviews": r.get("numberOfReviews"),
                "last_updated": r.get("lastUpdated"),  # relative, e.g. "6 years ago"
                "emphasized_keywords": r.get("emphasizedKeywords"),
            },
        )


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _domain(website: str | None) -> str | None:
    """Bare domain from a URL: 'https://amadeus.com/fr' -> 'amadeus.com'."""
    if not website:
        return None
    d = website.strip().lower().split("://")[-1].split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None
