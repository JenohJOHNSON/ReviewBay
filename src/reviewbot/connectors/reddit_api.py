"""Reddit connector via Reddit's OFFICIAL API, kept as a ready fallback.

NOTE (2026): Reddit closed its API to self-service in late 2025. The Responsible
Builder Policy now requires MANUAL APPROVAL before a token is issued, so you can
no longer just create a "script" app and use it. Because of that, ReviewBay does
NOT list `reddit` as a default source. Reddit coverage instead comes for free
from the web search (google_search.py tags reddit.com hits as "reddit"/Social).

This connector stays in the tree for the case where you ARE approved for API
keys: it reads via app-only OAuth (the "client_credentials" grant), uses the same
source name ("reddit") and NormalizedReview shape, so nothing downstream can tell
the difference. With no keys it disables itself cleanly (raises KeyError) and
build_connectors falls back to the Apify actor, so nothing breaks either way.

If you were approved and have keys, set:
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and optionally REDDIT_USER_AGENT
    (e.g. "reviewbay/1.0 by u/yourname").
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import requests

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_SEARCH_URL = "https://oauth.reddit.com/search"
_DEFAULT_UA = "reviewbay/1.0 (brand-reputation monitor)"


class RedditApiConnector(BaseConnector):
    source_name = "reddit"

    def __init__(self) -> None:
        # Missing creds -> KeyError, so build_connectors disables this cleanly
        # (and falls back to the Apify actor).
        self._client_id = os.environ["REDDIT_CLIENT_ID"]
        self._client_secret = os.environ["REDDIT_CLIENT_SECRET"]
        self._user_agent = os.environ.get("REDDIT_USER_AGENT") or _DEFAULT_UA
        self._token: str | None = None
        self._token_exp: float = 0.0

    def _bearer(self) -> str:
        """App-only OAuth token, cached until shortly before it expires."""
        if self._token and time.time() < self._token_exp:
            return self._token
        resp = requests.post(
            _TOKEN_URL,
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self._user_agent},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        # Refresh a minute early to avoid using a token that expires mid-request.
        self._token_exp = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        # One OR query across the brand's keywords, newest first.
        terms = keywords or [brand]
        query = " OR ".join(f'"{t}"' if " " in t else t for t in terms)

        try:
            token = self._bearer()
        except Exception:  # noqa: BLE001 — auth failed: yield nothing, don't crash
            log.exception("reddit: could not get an access token; skipping")
            return

        headers = {"Authorization": f"bearer {token}", "User-Agent": self._user_agent}
        remaining = max(1, limit)
        after: str | None = None

        while remaining > 0:
            params: dict[str, Any] = {
                "q": query,
                "sort": "new",
                "type": "link",  # posts (t3), not comments
                "limit": min(100, remaining),
                "raw_json": 1,
            }
            if after:
                params["after"] = after
            try:
                resp = requests.get(_SEARCH_URL, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
                data = (resp.json() or {}).get("data") or {}
            except Exception:  # noqa: BLE001 — search failed: stop, keep what we have
                log.exception("reddit: search request failed for %s", brand)
                return

            children = data.get("children") or []
            if not children:
                return

            for child in children:
                review = self._map_post(brand, child.get("data") or {})
                if review and review.source_url and review.text.strip():
                    yield review

            remaining -= len(children)
            after = data.get("after")
            if not after:
                return

    @staticmethod
    def _map_post(brand: str, d: dict[str, Any]) -> NormalizedReview | None:
        title = (d.get("title") or "").strip()
        body = (d.get("selftext") or "").strip()
        text = "\n\n".join(x for x in (title, body) if x)
        permalink = d.get("permalink")
        url = ("https://www.reddit.com" + permalink) if permalink else d.get("url")
        if not text or not url:
            return None
        return NormalizedReview(
            brand=brand,
            source="reddit",
            source_url=url,
            text=text,
            author=d.get("author"),
            rating=None,  # Reddit has no stars; sentiment fills this role
            created_at=_iso(d.get("created_utc")),
            extra={
                "subreddit": d.get("subreddit"),
                "up_votes": d.get("ups"),
                "num_comments": d.get("num_comments"),
                "over_18": d.get("over_18"),
            },
        )


def _iso(ts: Any) -> str | None:
    """Reddit's created_utc (unix seconds) -> ISO 8601 string."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None
