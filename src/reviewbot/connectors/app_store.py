"""Apple App Store reviews — via Apple's free, official RSS feed.

No API key and no scraping. Two public Apple endpoints:
  1. iTunes Search API    -> find the app's numeric id from the brand name
  2. Customer Reviews RSS  -> that app's recent reviews (50/page, up to ~500)

Both are documented Apple endpoints returning JSON, so the field mapping here is
VALIDATED against live output (2026-07) — unlike the Apify review actors.

Per-brand override (optional): set env APP_STORE_ID_<BRAND> to pin an exact app
and skip the search, e.g. APP_STORE_ID_BLUE_BOTTLE_COFFEE=1440573734. Otherwise
the connector searches the App Store for the brand and takes the best-matching
app. Country defaults to `us` (env APP_STORE_COUNTRY to change).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import requests

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_SEARCH_URL = "https://itunes.apple.com/search"
# Segment order matters: id → sortBy → page is the combo Apple serves reliably;
# putting page= first returns an empty page 1 (verified 2026-07).
_RSS_URL = (
    "https://itunes.apple.com/{country}/rss/customerreviews/"
    "id={app_id}/sortBy=mostRecent/page={page}/json"
)
_TIMEOUT = 20
_MAX_PAGES = 10  # Apple caps the reviews feed at ~10 pages (500 reviews)


class AppStoreConnector(BaseConnector):
    source_name = "app_store"

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        country = os.environ.get("APP_STORE_COUNTRY", "us")
        app = self._find_app(brand, keywords, country)
        if not app:
            log.warning("app_store: no matching app for brand=%s — skipping", brand)
            return
        app_id, app_name, app_url = app
        log.info("app_store: brand=%s -> app '%s' (id=%s)", brand, app_name, app_id)

        seen = 0
        pages = max(1, min((limit + 49) // 50, _MAX_PAGES))
        for page in range(1, pages + 1):
            entries = self._reviews_page(app_id, country, page)
            if not entries:
                break  # no more pages
            for entry in entries:
                if seen >= limit:
                    return
                try:
                    review = self._map_entry(brand, entry, app_id, app_name, app_url, country)
                except Exception:  # noqa: BLE001
                    log.exception("app_store: skipping malformed review")
                    continue
                if review:
                    seen += 1
                    yield review

    # -- Apple endpoints -----------------------------------------------------

    def _find_app(self, brand: str, keywords: list[str], country: str):
        pinned = os.environ.get("APP_STORE_ID_" + _slug(brand))
        if pinned:
            return pinned, brand, f"https://apps.apple.com/{country}/app/id{pinned}"

        term = (keywords or [brand])[0]
        try:
            resp = requests.get(
                _SEARCH_URL,
                params={"term": term, "entity": "software", "country": country, "limit": 5},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except (requests.RequestException, ValueError):
            log.exception("app_store: search failed for brand=%s", brand)
            return None

        needle = brand.split()[0].lower() if brand else ""
        for r in results:
            hay = f"{r.get('trackName', '')} {r.get('sellerName', '')}".lower()
            if needle and needle in hay:
                return str(r["trackId"]), r.get("trackName") or brand, r.get("trackViewUrl") or ""
        if results:  # nothing matched by name — fall back to the top hit
            r = results[0]
            return str(r["trackId"]), r.get("trackName") or brand, r.get("trackViewUrl") or ""
        return None

    def _reviews_page(self, app_id: str, country: str, page: int) -> list[dict]:
        url = _RSS_URL.format(country=country, page=page, app_id=app_id)
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            entries = resp.json().get("feed", {}).get("entry", [])
        except (requests.RequestException, ValueError):
            log.exception("app_store: reviews fetch failed id=%s page=%s", app_id, page)
            return []
        if isinstance(entries, dict):  # single-review feeds come back as one object
            entries = [entries]
        # The first entry of page 1 is the app itself (no im:rating) — keep only reviews.
        return [e for e in entries if isinstance(e, dict) and "im:rating" in e]

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _map_entry(brand, entry, app_id, app_name, app_url, country) -> NormalizedReview | None:
        title = _label(entry.get("title"))
        body = _label(entry.get("content"))
        text = "\n\n".join(x for x in (title, body) if x).strip()
        if not text:
            return None
        review_id = _label(entry.get("id"))
        base = app_url or f"https://apps.apple.com/{country}/app/id{app_id}"
        # Anchor the review id so each citation URL is unique (stable_id keys on it)
        # while still opening the app's reviews page when clicked.
        source_url = f"{base}?see-all=reviews"
        if review_id:
            source_url += f"#review-{review_id}"
        return NormalizedReview(
            brand=brand,
            source="app_store",
            source_url=source_url,
            text=text,
            author=_label((entry.get("author") or {}).get("name")),
            rating=_as_float(_label(entry.get("im:rating"))),
            created_at=_label(entry.get("updated")),
            extra={
                "app_id": app_id,
                "app_name": app_name,
                "country": country,
                "review_id": review_id,
                "version": _label(entry.get("im:version")),
                "apple_review_link": _first_link_href(entry.get("link")),
            },
        )


def _label(node):
    """Apple's RSS JSON wraps values as {'label': ...}. Pull the label safely."""
    if isinstance(node, dict):
        v = node.get("label")
        return v.strip() if isinstance(v, str) else v
    return node


def _first_link_href(link):
    if isinstance(link, list):
        link = link[0] if link else None
    if isinstance(link, dict):
        return (link.get("attributes") or {}).get("href")
    return None


def _slug(brand: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in (brand or "").upper())


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
