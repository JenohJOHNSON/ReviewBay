"""Deep web-mentions via Firecrawl (search + scrape the actual page content).

Where Tavily/google-search return a short SERP snippet, Firecrawl SCRAPES each
result and returns the page's real content (it renders JavaScript and gets past
light bot-blocking), so the text is fuller and better for the chat and themes.
That depth costs more per result, so this source is OPT-IN: it only runs for a
brand that lists `firecrawl` in its sources. Default brands never trigger it, so
it can never surprise you with cost.

Rows are host-tagged like the other web sources (reddit.com -> "reddit", etc.),
so Firecrawl-found content still lands in the right dashboard category.

Set FIRECRAWL_API_KEY in the environment. No key means this disables itself
cleanly (raises KeyError) and build_connectors just skips it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import requests

from ..models import NormalizedReview
from ..sources import source_from_url
from .base import BaseConnector

log = logging.getLogger(__name__)

_URL = "https://api.firecrawl.dev/v1/search"
_MAX_RESULTS = 20
# Firecrawl returns whole-page markdown; keep a bounded slice so one page does not
# become a giant single row (and a low-quality single embedding).
_MAX_CHARS = 2000


class FirecrawlConnector(BaseConnector):
    source_name = "web"

    def __init__(self) -> None:
        # Missing key -> KeyError, so build_connectors disables this cleanly.
        self._key = os.environ["FIRECRAWL_API_KEY"]

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        terms = keywords or [brand]
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        per_call = max(1, min(limit, _MAX_RESULTS))
        seen = 0

        for kw in terms:
            if seen >= limit:
                return
            payload = {
                "query": f"{kw} reviews",
                "limit": per_call,
                "scrapeOptions": {"formats": ["markdown"]},
            }
            try:
                resp = requests.post(_URL, json=payload, headers=headers, timeout=60)
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception:  # noqa: BLE001 — one query failing must not abort the rest
                log.exception("firecrawl: search failed for brand=%s q=%r", brand, kw)
                continue

            for r in data.get("data") or []:
                if seen >= limit:
                    return
                review = self._map_result(brand, r)
                if review and review.source_url and review.text.strip():
                    seen += 1
                    yield review

    @staticmethod
    def _map_result(brand: str, r: dict) -> NormalizedReview | None:
        url = r.get("url")
        title = (r.get("title") or "").strip()
        # Prefer the scraped page content; fall back to the search description.
        body = (r.get("markdown") or r.get("description") or "").strip()
        if len(body) > _MAX_CHARS:
            body = body[:_MAX_CHARS].rsplit(" ", 1)[0] + "..."
        text = (title + ("\n\n" + body if body else "")).strip()
        if not url or not text:
            return None
        return NormalizedReview(
            brand=brand,
            source=source_from_url(url),
            source_url=url,
            text=text,
            author=None,
            rating=None,
            created_at=None,
            extra={"engine": "firecrawl"},
        )
