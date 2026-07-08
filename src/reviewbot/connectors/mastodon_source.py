"""Mastodon mentions via public hashtag timelines, keyless.

Mastodon is open and federated, so a large instance's public tag timeline is
readable with no auth: GET /api/v1/timelines/tag/<tag>. We turn the brand (and
its keywords) into hashtags and read recent public posts under them, paginating
with the `max_id` cursor. A big hub instance (mastodon.social by default) sees a
broad slice of the fediverse; override with MASTODON_INSTANCE.

Full-text status search needs auth on most instances, so we deliberately use the
tag timeline, which is the reliable public path. No credential needed.
"""

from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import Iterator

import requests

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"[^0-9a-zA-Z]+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PER_PAGE = 40  # Mastodon caps tag timelines at 40 per page


def _instance() -> str:
    inst = (os.environ.get("MASTODON_INSTANCE") or "mastodon.social").strip()
    return inst.split("://")[-1].strip("/")


def _to_tag(term: str) -> str:
    """A hashtag is alphanumeric only: 'Blue Bottle Coffee' -> 'bluebottlecoffee'."""
    return _TAG_RE.sub("", term or "").lower()


def _strip_html(content: str) -> str:
    text = _HTML_TAG_RE.sub(" ", content or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


class MastodonConnector(BaseConnector):
    source_name = "mastodon"

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        base = f"https://{_instance()}/api/v1/timelines/tag/"
        # Dedupe tags derived from the brand + keywords (short/empty tags dropped).
        tags: list[str] = []
        for term in [brand, *(keywords or [])]:
            t = _to_tag(term)
            if len(t) >= 3 and t not in tags:
                tags.append(t)

        seen_ids: set[str] = set()
        emitted = 0
        for tag in tags:
            if emitted >= limit:
                return
            max_id: str | None = None
            while emitted < limit:
                params: dict = {"limit": min(_PER_PAGE, limit - emitted)}
                if max_id:
                    params["max_id"] = max_id
                try:
                    resp = requests.get(base + tag, params=params, timeout=30)
                    resp.raise_for_status()
                    statuses = resp.json() or []
                except Exception:  # noqa: BLE001 — one tag failing must not abort the rest
                    log.exception("mastodon: tag timeline failed brand=%s tag=%r", brand, tag)
                    break
                if not statuses:
                    break

                for st in statuses:
                    if emitted >= limit:
                        return
                    review = self._map_status(brand, st)
                    if not review or review.id in seen_ids:
                        continue
                    seen_ids.add(review.id)
                    emitted += 1
                    yield review

                max_id = statuses[-1].get("id")
                if not max_id:
                    break

    @staticmethod
    def _map_status(brand: str, st: dict) -> NormalizedReview | None:
        url = st.get("url") or st.get("uri")
        text = _strip_html(st.get("content") or "")
        if not url or not text:
            return None
        acct = (st.get("account") or {}).get("acct")
        return NormalizedReview(
            brand=brand,
            source="mastodon",
            source_url=url,
            text=text,
            author=acct,
            rating=None,
            created_at=st.get("created_at"),
            extra={
                "favourites": st.get("favourites_count"),
                "reblogs": st.get("reblogs_count"),
                "engine": "mastodon_tag",
            },
        )
