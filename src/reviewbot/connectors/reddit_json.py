"""Reddit mentions via the public search JSON, no API app or key required.

Reddit closed self-service API app creation, but the site still serves public
listings as JSON: append `.json` to any search or subreddit URL. This connector
queries `https://www.reddit.com/search.json?q=<brand>` and reads back posts that
mention the brand, paginating with the `after` cursor until it hits `limit`.

No credential. A descriptive User-Agent is REQUIRED or Reddit returns 429, so we
always send one. IMPORTANT: Reddit blocks datacenter IPs with a 403 "Blocked",
so this works from residential IPs (a laptop) but is usually blocked from cloud
hosts (Railway, CI). It fails quietly there and yields nothing; Reddit coverage
still arrives through the `web` search, which tags reddit.com hits as "reddit".
Set REDDIT_CLIENT_ID/SECRET to use the official API connector instead (not IP
blocked); the Apify actor is the last resort.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone

import requests

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_SEARCH = "https://www.reddit.com/search.json"
# Reddit caps a listing page at 100 items; paginate with `after` for more.
_PER_PAGE = 100
_UA = os.environ.get(
    "REDDIT_USER_AGENT", "ReviewBay/1.0 (brand-reputation research; contact via app)"
)


class RedditJsonConnector(BaseConnector):
    source_name = "reddit"

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        terms = keywords or [brand]
        seen_ids: set[str] = set()
        emitted = 0

        for kw in terms:
            if emitted >= limit:
                return
            after: str | None = None
            # Walk pages for this keyword until we run out or hit the limit.
            while emitted < limit:
                params = {
                    "q": kw,
                    "limit": min(_PER_PAGE, limit - emitted),
                    "sort": "relevance",
                    "t": "all",
                    "raw_json": 1,
                }
                if after:
                    params["after"] = after
                try:
                    resp = requests.get(
                        _SEARCH, params=params, headers={"User-Agent": _UA}, timeout=30
                    )
                    # 403/429 = IP block or rate limit: expected from cloud hosts,
                    # so log one concise line (not a stack trace) and move on.
                    if resp.status_code in (403, 429):
                        log.warning(
                            "reddit: %s from this IP (blocked/rate-limited); "
                            "coverage will come via web search instead", resp.status_code
                        )
                        break
                    resp.raise_for_status()
                    data = resp.json() or {}
                except Exception:  # noqa: BLE001 — one query failing must not abort the rest
                    log.warning("reddit: search failed for brand=%s q=%r", brand, kw)
                    break

                listing = (data.get("data") or {})
                children = listing.get("children") or []
                if not children:
                    break

                for child in children:
                    if emitted >= limit:
                        return
                    review = self._map_post(brand, child.get("data") or {})
                    if not review:
                        continue
                    if review.id in seen_ids:
                        continue
                    seen_ids.add(review.id)
                    emitted += 1
                    yield review

                after = listing.get("after")
                if not after:
                    break

    @staticmethod
    def _map_post(brand: str, d: dict) -> NormalizedReview | None:
        permalink = d.get("permalink")
        if not permalink:
            return None
        title = (d.get("title") or "").strip()
        body = (d.get("selftext") or "").strip()
        text = (title + ("\n\n" + body if body else "")).strip()
        if not text:
            return None
        created = d.get("created_utc")
        created_iso = (
            datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
            if isinstance(created, (int, float))
            else None
        )
        author = d.get("author")
        return NormalizedReview(
            brand=brand,
            source="reddit",
            source_url="https://www.reddit.com" + permalink,
            text=text,
            author=(author if author and author != "[deleted]" else None),
            rating=None,  # Reddit posts have no star rating
            created_at=created_iso,
            extra={
                "subreddit": d.get("subreddit"),
                "score": d.get("score"),
                "num_comments": d.get("num_comments"),
                "engine": "reddit_json",
            },
        )
