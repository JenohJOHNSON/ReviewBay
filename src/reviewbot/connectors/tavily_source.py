"""Web-mentions via the Tavily search API (cheaper than the Apify google-search).

Tavily is a search API built for exactly this: give it a query, get back clean,
ranked results with a content snippet already extracted. It is a drop-in
replacement for the Apify google-search "web" source (source_name stays "web"),
and it is much cheaper (a generous free tier), which is why build_connectors
prefers it for the `web` source whenever TAVILY_API_KEY is set.

Like the google search, results are host-tagged: a reddit.com or youtube.com hit
becomes source "reddit"/"youtube" (Social), everything else stays "web".

Set TAVILY_API_KEY in the environment. No key means this disables itself cleanly
(raises KeyError) and build_connectors falls back to the Apify google-search.
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

_URL = "https://api.tavily.com/search"
# Tavily returns at most 20 results per call; ask for as many as the limit wants,
# capped there. A handful of keywords each get their own call.
_MAX_PER_CALL = 20


class TavilyConnector(BaseConnector):
    source_name = "web"

    def __init__(self) -> None:
        # Missing key -> KeyError, so build_connectors disables this cleanly and
        # falls back to the Apify google-search.
        self._key = os.environ["TAVILY_API_KEY"]

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        terms = keywords or [brand]
        domain = _domain(website)
        per_call = max(1, min(limit, _MAX_PER_CALL))
        seen = 0

        for kw in terms:
            if seen >= limit:
                return
            # Anchor to the brand's domain when known, so an ambiguous name
            # targets the real company (same trick the google search uses).
            query = f"{kw} reviews" + (f" {domain}" if domain else "")
            payload = {
                "api_key": self._key,
                "query": query,
                "search_depth": "basic",
                "max_results": per_call,
                "topic": "general",
            }
            try:
                resp = requests.post(_URL, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception:  # noqa: BLE001 — one query failing must not abort the rest
                log.exception("tavily: search failed for brand=%s query=%r", brand, query)
                continue

            for r in data.get("results") or []:
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
        content = (r.get("content") or "").strip()
        text = (title + ("\n\n" + content if content else "")).strip()
        if not url or not text:
            return None
        return NormalizedReview(
            brand=brand,
            source=source_from_url(url),
            source_url=url,
            text=text,
            author=None,
            rating=None,  # search snippets have no star rating
            created_at=r.get("published_date"),
            extra={"score": r.get("score"), "engine": "tavily"},
        )


def _domain(website: str | None) -> str | None:
    if not website:
        return None
    d = website.strip().lower().split("://")[-1].split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None
