"""Trustpilot connector via Playwright (render) + Selectolax (parse), self-hosted.

This is the "no vendor" path: instead of paying an Apify actor, we drive a real
headless Chromium ourselves (Playwright) to load a brand's Trustpilot page, then
parse the review cards with Selectolax (a very fast HTML parser). Free to run,
but it needs the browser installed in the image, so it is heavier than an API
call. Because of that it is OPT-IN: it only runs for a brand that lists
`trustpilot` in its sources.

The brand's Trustpilot page is derived from its `website` domain
(https://www.trustpilot.com/review/<domain>), or pinned per brand with env
TRUSTPILOT_URL_<BRAND>. No website and no override means the source skips itself.

CAVEAT: Trustpilot's markup uses obfuscated, drifting class names. The selectors
below cover the stable data-* hooks plus class fallbacks, but if Trustpilot
changes its HTML the parser may need a tweak. Run once and eyeball the output
before trusting it (same honesty as the Apify mappers).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator

from ..models import NormalizedReview
from .base import BaseConnector

log = logging.getLogger(__name__)

_BASE = "https://www.trustpilot.com"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
_MAX_PAGES = 10  # safety cap regardless of limit


class TrustpilotConnector(BaseConnector):
    source_name = "trustpilot"

    def fetch(
        self, brand: str, keywords: list[str], limit: int, website: str | None = None
    ) -> Iterator[NormalizedReview]:
        url = _page_url(brand, website)
        if not url:
            log.info(
                "trustpilot: no domain/URL for brand=%s (set website or "
                "TRUSTPILOT_URL_<BRAND>); skipping",
                brand,
            )
            return

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            from selectolax.parser import HTMLParser  # type: ignore
        except Exception:  # noqa: BLE001 (optional deps not installed: disable cleanly)
            log.warning(
                "trustpilot: optional deps missing (pip install playwright selectolax && "
                "playwright install chromium); skipping"
            )
            return

        seen = 0
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_UA)
                for n in range(1, _MAX_PAGES + 1):
                    if seen >= limit:
                        break
                    page_url = url if n == 1 else f"{url}?page={n}"
                    try:
                        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(1500)  # let review cards hydrate
                        html = page.content()
                    except Exception:  # noqa: BLE001 (page failed: stop paging)
                        log.exception("trustpilot: failed to load %s", page_url)
                        break

                    reviews = _parse_html(brand, html, HTMLParser)
                    if not reviews:
                        break  # no more cards (past the last page)
                    for review in reviews:
                        if seen >= limit:
                            break
                        if review.source_url and review.text.strip():
                            seen += 1
                            yield review
            finally:
                browser.close()


def _parse_html(brand: str, html: str, parser_cls=None) -> list[NormalizedReview]:
    """Parse Trustpilot review cards out of a rendered page into NormalizedReview.

    Kept separate from the browser so it is unit-testable with static HTML.
    """
    if parser_cls is None:
        from selectolax.parser import HTMLParser as parser_cls  # type: ignore

    tree = parser_cls(html)
    cards = tree.css(
        "article[data-service-review-card-paper], "
        'article[class*="reviewCard"], [class*="styles_reviewCard"]'
    )
    reviews: list[NormalizedReview] = []
    for card in cards:
        title = _text(card, "[data-review-title-typography]", 'a[class*="reviewTitle"]')
        body = _text(
            card,
            "[data-service-review-text-typography]",
            'p[class*="reviewContent"]',
            'p[class*="reviewText"]',
        )
        text = "\n\n".join(x for x in (title, body) if x)
        if not text:
            continue
        author = _text(card, "[data-consumer-name-typography]", 'span[class*="consumerName"]')
        rating = _rating(card)
        created = _attr(card, "time", "datetime")
        url = _review_url(card)
        reviews.append(
            NormalizedReview(
                brand=brand,
                source="trustpilot",
                source_url=url,
                text=text,
                author=author or None,
                rating=rating,
                created_at=created,
                extra={"site": "trustpilot"},
            )
        )
    return reviews


def _first(card, *selectors):
    for sel in selectors:
        node = card.css_first(sel)
        if node is not None:
            return node
    return None


def _text(card, *selectors) -> str:
    node = _first(card, *selectors)
    return node.text(strip=True) if node is not None else ""


def _attr(card, selector: str, name: str) -> str | None:
    node = card.css_first(selector)
    if node is None:
        return None
    val = node.attributes.get(name)
    return val or None


def _rating(card) -> float | None:
    """Rating from the data-service-review-rating attr, or the star image alt."""
    node = card.css_first("[data-service-review-rating]")
    if node is not None:
        val = node.attributes.get("data-service-review-rating")
        f = _as_float(val)
        if f is not None:
            return f
    img = card.css_first("img[alt*='Rated'], img[alt*='rated']")
    if img is not None:
        m = re.search(r"([0-5](?:\.\d)?)\s+out of\s+5", img.attributes.get("alt") or "")
        if m:
            return _as_float(m.group(1))
    return None


def _review_url(card) -> str:
    """Absolute link to the individual review, else empty."""
    link = card.css_first('a[href*="/reviews/"], a[data-review-title-typography]')
    href = link.attributes.get("href") if link is not None else None
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return _BASE + href if href.startswith("/") else f"{_BASE}/{href}"


def _page_url(brand: str, website: str | None) -> str | None:
    slug = "".join(ch if ch.isalnum() else "_" for ch in (brand or "").upper())
    override = os.environ.get("TRUSTPILOT_URL_" + slug)
    if override:
        return override
    domain = _domain(website)
    return f"{_BASE}/review/{domain}" if domain else None


def _domain(website: str | None) -> str | None:
    if not website:
        return None
    d = website.strip().lower().split("://")[-1].split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
