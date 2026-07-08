"""Hacker News mentions via the Algolia HN Search API, free and keyless.

HN indexes every story and comment through Algolia at hn.algolia.com/api. It is
free, needs no key, and is one of the few good open sources of substantive
opinion on B2B and technical brands (hardware, SaaS, dev tools, energy) that have
little consumer-app or review-site presence. Each story/comment mentioning the
brand becomes one sample linking back to the HN item.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone

import requests

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_SEARCH = "https://hn.algolia.com/api/v1/search"
_ITEM = "https://news.ycombinator.com/item?id="
_PER_PAGE = 100  # Algolia allows up to 1000, but keep pages modest


class HackerNewsConnector(BaseConnector):
    source_name = "hackernews"

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        terms = keywords or [brand]
        seen_ids: set[str] = set()
        emitted = 0

        for kw in terms:
            if emitted >= limit:
                return
            page = 0
            while emitted < limit:
                params = {
                    "query": kw,
                    "tags": "(story,comment)",
                    "hitsPerPage": min(_PER_PAGE, limit - emitted),
                    "page": page,
                }
                try:
                    resp = requests.get(_SEARCH, params=params, timeout=30)
                    resp.raise_for_status()
                    data = resp.json() or {}
                except Exception:  # noqa: BLE001 — one query failing must not abort the rest
                    log.exception("hackernews: search failed for brand=%s q=%r", brand, kw)
                    break

                hits = data.get("hits") or []
                if not hits:
                    break
                for h in hits:
                    if emitted >= limit:
                        return
                    review = self._map_hit(brand, h)
                    if not review or review.id in seen_ids:
                        continue
                    seen_ids.add(review.id)
                    emitted += 1
                    yield review

                page += 1
                if page >= (data.get("nbPages") or 0):
                    break

    @staticmethod
    def _map_hit(brand: str, h: dict) -> NormalizedReview | None:
        object_id = h.get("objectID")
        if not object_id:
            return None
        # A hit is either a story (title + optional story_text) or a comment.
        title = (h.get("title") or "").strip()
        story_text = (h.get("story_text") or "").strip()
        comment_text = (h.get("comment_text") or "").strip()
        parts = [p for p in (title, story_text, comment_text) if p]
        text = "\n\n".join(parts).strip()
        if not text:
            return None
        ts = h.get("created_at_i")
        created_iso = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if isinstance(ts, (int, float))
            else h.get("created_at")
        )
        return NormalizedReview(
            brand=brand,
            source="hackernews",
            source_url=_ITEM + str(object_id),
            text=text,
            author=h.get("author"),
            rating=None,  # HN has points, not star ratings
            created_at=created_iso,
            extra={"points": h.get("points"), "engine": "hn_algolia"},
        )
