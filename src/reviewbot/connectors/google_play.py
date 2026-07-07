"""Google Play Store reviews — via the free `google-play-scraper` library.

Google, unlike Apple, has NO free public reviews feed — the official Play
Developer API only returns reviews for apps *you own*. The community
`google-play-scraper` library reads the same endpoints the Play website uses:
no API key, no Apify credits. It IS a scraper (can break if Google changes its
internal API), but it's well-maintained and stable — far less fragile than Meta.
Field mapping VALIDATED against live output (2026-07).

Resolving the app id (a package name like com.bluebottlecoffee.bbcandroid):
  1. env GOOGLE_PLAY_ID_<BRAND> wins (pin it for reliability), else
  2. the library's search, taking the first real appId that matches the brand,
     else
  3. a fallback that reads Play's search HTML — because the library returns
     appId=None for the highlighted result, which is exactly the exact-name match
     we want (a known library bug).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator

import requests

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_LANG = os.environ.get("GOOGLE_PLAY_LANG", "en")
_COUNTRY = os.environ.get("GOOGLE_PLAY_COUNTRY", "us")
_TIMEOUT = 20
_APP_URL = "https://play.google.com/store/apps/details?id={pkg}"


class GooglePlayConnector(BaseConnector):
    source_name = "google_play"

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        from google_play_scraper import Sort, reviews  # lazy: only when this source runs

        pkg = self._resolve_app(brand, keywords)
        if not pkg:
            log.warning("google_play: no app found for brand=%s — skipping", brand)
            return
        log.info("google_play: brand=%s -> package %s", brand, pkg)

        try:
            revs, _ = reviews(pkg, lang=_LANG, country=_COUNTRY, sort=Sort.NEWEST, count=limit)
        except Exception:  # noqa: BLE001
            log.exception("google_play: reviews fetch failed for %s", pkg)
            return

        app_url = _APP_URL.format(pkg=pkg)
        seen = 0
        for rv in revs:
            if seen >= limit:
                return
            try:
                review = self._map_review(brand, rv, pkg, app_url)
            except Exception:  # noqa: BLE001
                log.exception("google_play: skipping malformed review")
                continue
            if review:
                seen += 1
                yield review

    # -- app resolution ------------------------------------------------------

    def _resolve_app(self, brand: str, keywords: list[str]) -> str | None:
        slug = "".join(ch if ch.isalnum() else "_" for ch in (brand or "").upper())
        pinned = os.environ.get("GOOGLE_PLAY_ID_" + slug)
        if pinned:
            return pinned

        needle = "".join(ch for ch in (brand or "").lower() if ch.isalnum())
        term = (keywords or [brand])[0]

        try:
            from google_play_scraper import search

            for r in search(term, n_hits=6, lang=_LANG, country=_COUNTRY):
                appid = r.get("appId")
                hay = f"{appid} {r.get('title', '')} {r.get('developer', '')}".lower().replace(" ", "")
                if appid and needle[:8] and needle[:8] in hay:
                    return appid
        except Exception:  # noqa: BLE001
            log.exception("google_play: library search failed for brand=%s", brand)

        return self._search_html(term, needle)

    def _search_html(self, term: str, needle: str) -> str | None:
        """The highlighted match often has appId=None; dig the package out of the
        Play search HTML, which is full of /details?id=<pkg> links."""
        try:
            resp = requests.get(
                "https://play.google.com/store/search",
                params={"q": term, "c": "apps", "hl": _LANG, "gl": _COUNTRY},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException:
            log.exception("google_play: search-HTML fallback failed for '%s'", term)
            return None
        ids = re.findall(r"id=([a-zA-Z0-9._]+)", resp.text)
        probe = needle[:10] or needle
        for pkg in dict.fromkeys(ids):  # dedupe, preserve order
            if "." in pkg and probe and probe in pkg.lower().replace(".", ""):
                return pkg
        return None

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _map_review(brand: str, rv: dict, pkg: str, app_url: str) -> NormalizedReview | None:
        text = (rv.get("content") or "").strip()
        if not text:
            return None
        review_id = rv.get("reviewId") or ""
        # Google Play honours &reviewId= as a deep link to the specific review,
        # which also makes each citation URL unique for the stable id.
        source_url = app_url + (f"&reviewId={review_id}" if review_id else "")
        at = rv.get("at")
        created_at = at.isoformat() if hasattr(at, "isoformat") else (str(at) if at else None)
        return NormalizedReview(
            brand=brand,
            source="google_play",
            source_url=source_url,
            text=text,
            author=rv.get("userName"),
            rating=_as_float(rv.get("score")),
            created_at=created_at,
            extra={
                "package": pkg,
                "review_id": review_id,
                "thumbs_up": rv.get("thumbsUpCount"),
                "app_version": rv.get("reviewCreatedVersion") or rv.get("appVersion"),
                "reply": rv.get("replyContent"),
            },
        )


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
