"""Turning a fetched page into candidate reviews, deterministically.

The guiding rule is **precision before recall, and never guess a field**. A
missing author is fine; an invented one is a lie in a product whose whole value
is that every claim links back to a real person's words. So the extractors run in
order of how much the page *tells* us versus how much we would have to infer, and
the first one that yields usable records wins:

1. ``jsonld``    — schema.org ``Review`` objects in ``<script type=ld+json>``.
                   The site has explicitly labelled author, rating, date and body.
                   No inference at all. This is the single highest-value extractor:
                   review platforms, e-commerce product pages and Google-indexed
                   review markup all publish it because search engines read it.
2. ``microdata`` — the same vocabulary expressed as ``itemprop`` attributes.
3. ``site``      — hand-written CSS selectors for a specific domain, from
                   ``config/extraction_strategies.yml``. Exact, but needs upkeep,
                   so it exists for domains worth the maintenance.
4. ``structural``— repeated sibling blocks that look like review cards, found by
                   structure rather than by class name. This is where Scrapling
                   earns its place: ``find_similar()`` locates siblings with the
                   same shape, which survives the obfuscated, drifting class names
                   that broke the hand-written Trustpilot selectors.
5. ``article``   — the page's readable text as ONE record. Not a review: a
                   *mention*. Tagged as such so it never inflates a review count.

Every record carries ``kind``: ``review`` when the page presented it as a review
(structured markup, or a card with a rating or an author), ``mention``
otherwise. Phase 6 asks us to tell genuine customer reviews from general web
mentions, and this is where that distinction is established — downstream code
reads it rather than re-deriving it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import text as textclean
from . import urlpolicy

log = logging.getLogger(__name__)

#: Minimum characters for a record to be worth storing. Shorter than this is a
#: nav label or a rating with no words, not feedback.
MIN_TEXT_CHARS = int(os.environ.get("SCRAPING_MIN_TEXT_CHARS", "40"))
#: Cap per record, so one runaway page cannot become a single giant embedding.
MAX_TEXT_CHARS = int(os.environ.get("SCRAPING_MAX_TEXT_CHARS", "4000"))
#: Cap on records from one page.
MAX_RECORDS_PER_PAGE = int(os.environ.get("SCRAPING_MAX_RECORDS_PER_PAGE", "60"))

_STRATEGY_CONFIG = os.environ.get(
    "SCRAPING_STRATEGIES", os.path.join("config", "extraction_strategies.yml")
)


@dataclass
class Candidate:
    """One extracted record, before validation and normalization.

    Deliberately not a `NormalizedReview`: that requires a brand, and extraction
    does not know about brands. The connector attaches the brand and the
    provenance, `quality.py` validates, and only then does it become a review.
    """

    text: str
    source_url: str
    kind: str = "mention"            # "review" | "mention"
    author: str | None = None
    rating: float | None = None
    rating_scale: float | None = None  # the page's own max, when it states one
    created_at: str | None = None
    title: str | None = None
    strategy: str = ""
    evidence: str = ""               # where on the page this came from
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageExtraction:
    """Everything one page yielded, plus how it went."""

    url: str
    candidates: list[Candidate] = field(default_factory=list)
    strategy: str = ""
    next_urls: list[str] = field(default_factory=list)
    title: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.candidates)


# --- parser ------------------------------------------------------------------

def parse(html: str, url: str = "", adaptive: bool = False):
    """A Scrapling `Selector` over `html`, or None when Scrapling is absent.

    Scrapling's core install is light (lxml, cssselect, orjson, tld, w3lib) with
    no browser and no curl_cffi, so this is available anywhere the base image
    runs. `adaptive=True` enables its element-relocation storage, which writes a
    file; left off by default because the container filesystem is ephemeral.
    """
    try:
        from scrapling.parser import Selector  # type: ignore
    except ImportError:
        log.debug("scrapling is not installed; HTML extraction unavailable")
        return None
    try:
        return Selector(content=html, url=url or "", adaptive=adaptive)
    except Exception:  # noqa: BLE001 — malformed markup must not raise
        log.exception("could not parse page %s", url)
        return None


def _first_text(node, *selectors: str) -> str:
    """The text of the first selector that matches, cleaned."""
    for sel in selectors:
        if not sel:
            continue
        try:
            found = node.css(sel)
        except Exception:  # noqa: BLE001 — a bad selector in config is not fatal
            continue
        if found:
            try:
                value = found[0].get_all_text(strip=True)
            except Exception:  # noqa: BLE001
                continue
            cleaned = textclean.clean(value)
            if cleaned:
                return cleaned
    return ""


def _first_attr(node, selector: str, attr: str) -> str:
    try:
        found = node.css(selector)
    except Exception:  # noqa: BLE001
        return ""
    if not found:
        return ""
    try:
        return str(found[0].attrib.get(attr) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# --- rating parsing ----------------------------------------------------------

_RATING_PATTERNS = (
    # "4.5 out of 5", "Rated 4 out of 5 stars", "4/5"
    re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:out\s+of|/|of)\s*(\d+(?:[.,]\d+)?)", re.I),
    # "4.5 stars", "4 star"
    re.compile(r"(\d+(?:[.,]\d+)?)\s*stars?\b", re.I),
)


def parse_rating(value: Any, scale_hint: float | None = None) -> tuple[float | None, float | None]:
    """A (rating, scale) pair from whatever the page gave us.

    Returns `(None, None)` rather than a guess when the value is unreadable. A
    rating is a factual claim about what a customer said, so a wrong one is worse
    than a missing one.

    >>> parse_rating("4.5 out of 5")
    (4.5, 5.0)
    >>> parse_rating("Rated 3 out of 10 stars")
    (1.5, 5.0)
    >>> parse_rating("5")
    (5.0, None)
    >>> parse_rating("great")
    (None, None)
    """
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        rating, scale = float(value), scale_hint
    else:
        raw = str(value).strip()
        if not raw:
            return None, None
        rating = scale = None
        for pattern in _RATING_PATTERNS:
            m = pattern.search(raw)
            if m:
                try:
                    rating = float(m.group(1).replace(",", "."))
                except ValueError:
                    return None, None
                if m.lastindex and m.lastindex >= 2:
                    try:
                        scale = float(m.group(2).replace(",", "."))
                    except ValueError:
                        scale = None
                break
        if rating is None:
            try:
                rating = float(raw.replace(",", "."))
            except ValueError:
                return None, None
            scale = scale_hint

    if rating is None:
        return None, None
    # Rescale to the 1-5 contract the rest of ReviewBay uses (models.py:40).
    if scale and scale > 0 and abs(scale - 5.0) > 0.01:
        rating = rating * 5.0 / scale
        scale = 5.0
    if not (0 <= rating <= 5.0001):
        return None, None
    return round(rating, 2), (5.0 if scale else None)


# --- 1. JSON-LD --------------------------------------------------------------

def _walk_jsonld(obj: Any) -> Iterable[dict]:
    """Every dict inside a JSON-LD blob, however deeply nested or wrapped."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_jsonld(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_jsonld(item)


def _jsonld_type(node: dict) -> set[str]:
    raw = node.get("@type") or node.get("type") or ""
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).split("/")[-1].lower() for v in values if v}


def _jsonld_person(value: Any) -> str | None:
    """An author name from schema.org's several shapes, never a guess."""
    if isinstance(value, str):
        name = textclean.clean(value)
        return name or None
    if isinstance(value, dict):
        for key in ("name", "alternateName", "givenName"):
            name = textclean.clean(value.get(key) or "")
            if name:
                return name
    if isinstance(value, list) and value:
        return _jsonld_person(value[0])
    return None


def _jsonld_rating(node: dict) -> tuple[float | None, float | None]:
    rating_node = node.get("reviewRating") or node.get("ratingValue")
    if isinstance(rating_node, dict):
        best = rating_node.get("bestRating")
        try:
            scale = float(best) if best is not None else None
        except (TypeError, ValueError):
            scale = None
        return parse_rating(rating_node.get("ratingValue"), scale)
    return parse_rating(rating_node)


def extract_jsonld(tree, url: str) -> list[Candidate]:
    """schema.org Review objects the page published for search engines."""
    out: list[Candidate] = []
    try:
        blocks = tree.css('script[type="application/ld+json"]')
    except Exception:  # noqa: BLE001
        return out

    for block in blocks:
        # NOT get_all_text(): it ignores <script> by default (ignore_tags), so it
        # returns "" for exactly the node we are reading. `.text` is the raw
        # character data.
        try:
            raw = str(block.text or "")
        except Exception:  # noqa: BLE001
            continue
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Some sites emit JS-ish JSON-LD with trailing commas. One retry with
            # the obvious repair; past that, leave it alone rather than guess.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:  # noqa: BLE001
                continue

        for node in _walk_jsonld(data):
            types = _jsonld_type(node)
            if not types & {"review", "usercomment", "comment", "criticreview", "employerreview"}:
                continue
            body = textclean.clean(
                node.get("reviewBody") or node.get("description") or node.get("text") or ""
            )
            headline = textclean.clean(node.get("name") or node.get("headline") or "")
            text = "\n\n".join(p for p in (headline, body) if p)
            if len(text) < MIN_TEXT_CHARS:
                continue
            rating, scale = _jsonld_rating(node)
            item_url = node.get("url") or node.get("@id") or ""
            if isinstance(item_url, dict):
                item_url = item_url.get("@id") or ""
            resolved = _resolve_url(tree, url, str(item_url or "")) or url
            out.append(
                Candidate(
                    text=textclean.truncate(text, MAX_TEXT_CHARS),
                    source_url=resolved,
                    # JSON-LD said this is a Review, so it is one.
                    kind="review",
                    author=_jsonld_person(node.get("author")),
                    rating=rating,
                    rating_scale=scale,
                    created_at=_clean_date(
                        node.get("datePublished") or node.get("dateCreated") or node.get("uploadDate")
                    ),
                    title=headline or None,
                    strategy="jsonld",
                    evidence="script[type=application/ld+json]",
                    extra={"schema_type": sorted(types)},
                )
            )
            if len(out) >= MAX_RECORDS_PER_PAGE:
                return out
    return out


# --- 2. Microdata ------------------------------------------------------------

def extract_microdata(tree, url: str) -> list[Candidate]:
    """The same schema.org vocabulary, as itemprop attributes."""
    out: list[Candidate] = []
    try:
        nodes = tree.css('[itemtype*="schema.org/Review" i], [itemtype*="schema.org/UserReview" i]')
    except Exception:  # noqa: BLE001
        return out

    for node in nodes:
        body = _first_text(node, '[itemprop="reviewBody"]', '[itemprop="description"]')
        headline = _first_text(node, '[itemprop="name"]', '[itemprop="headline"]')
        text = "\n\n".join(p for p in (headline, body) if p)
        if len(text) < MIN_TEXT_CHARS:
            continue
        rating_value = (
            _first_attr(node, '[itemprop="ratingValue"]', "content")
            or _first_text(node, '[itemprop="ratingValue"]')
        )
        best = (
            _first_attr(node, '[itemprop="bestRating"]', "content")
            or _first_text(node, '[itemprop="bestRating"]')
        )
        try:
            scale_hint = float(best) if best else None
        except ValueError:
            scale_hint = None
        rating, scale = parse_rating(rating_value, scale_hint)
        date = (
            _first_attr(node, '[itemprop="datePublished"]', "datetime")
            or _first_attr(node, '[itemprop="datePublished"]', "content")
            or _first_text(node, '[itemprop="datePublished"]')
        )
        out.append(
            Candidate(
                text=textclean.truncate(text, MAX_TEXT_CHARS),
                source_url=url,
                kind="review",
                author=_first_text(node, '[itemprop="author"]', '[itemprop="name"]') or None,
                rating=rating,
                rating_scale=scale,
                created_at=_clean_date(date),
                title=headline or None,
                strategy="microdata",
                evidence='[itemtype*="schema.org/Review"]',
            )
        )
        if len(out) >= MAX_RECORDS_PER_PAGE:
            break
    return out


# --- 3. Site strategies ------------------------------------------------------

@dataclass
class SiteStrategy:
    """Hand-written selectors for one domain.

    Loaded from `config/extraction_strategies.yml` so adding a site is a config
    change, not a deploy of new code — the same control-surface convention as
    `config/brands.yml`.
    """

    name: str
    domains: tuple[str, ...]
    container: str
    text: tuple[str, ...] = ()
    title: tuple[str, ...] = ()
    author: tuple[str, ...] = ()
    rating: tuple[str, ...] = ()
    rating_attr: str = ""
    date: tuple[str, ...] = ()
    date_attr: str = "datetime"
    link: tuple[str, ...] = ()
    next_page: tuple[str, ...] = ()
    rating_scale: float | None = None

    def matches(self, url: str) -> bool:
        domain = urlpolicy.registrable_domain(url)
        host = ""
        try:
            from urllib.parse import urlsplit

            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            pass
        return any(d == domain or d == host or host.endswith("." + d) for d in self.domains)


def _as_tuple(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value if v)


_SITE_CACHE: list[SiteStrategy] | None = None


def site_strategies(path: str | None = None) -> list[SiteStrategy]:
    """Site strategies from config. Missing or broken config means no strategies."""
    global _SITE_CACHE
    if _SITE_CACHE is not None and path is None:
        return _SITE_CACHE
    target = path or _STRATEGY_CONFIG
    loaded: list[SiteStrategy] = []
    if target and os.path.exists(target):
        try:
            import yaml

            with open(target, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
            for entry in doc.get("strategies") or []:
                if not entry.get("container") or not entry.get("domains"):
                    log.warning(
                        "extraction strategy %r needs both 'domains' and 'container' — skipping",
                        entry.get("name"),
                    )
                    continue
                loaded.append(
                    SiteStrategy(
                        name=str(entry.get("name") or entry["domains"][0]),
                        domains=_as_tuple(entry.get("domains")),
                        container=str(entry["container"]),
                        text=_as_tuple(entry.get("text")),
                        title=_as_tuple(entry.get("title")),
                        author=_as_tuple(entry.get("author")),
                        rating=_as_tuple(entry.get("rating")),
                        rating_attr=str(entry.get("rating_attr") or ""),
                        date=_as_tuple(entry.get("date")),
                        date_attr=str(entry.get("date_attr") or "datetime"),
                        link=_as_tuple(entry.get("link")),
                        next_page=_as_tuple(entry.get("next_page")),
                        rating_scale=(
                            float(entry["rating_scale"]) if entry.get("rating_scale") else None
                        ),
                    )
                )
        except Exception:  # noqa: BLE001 — bad config disables site strategies, nothing more
            log.exception("could not load extraction strategies from %s", target)
            loaded = []
    if path is None:
        _SITE_CACHE = loaded
    return loaded


def reset_strategy_cache() -> None:
    global _SITE_CACHE
    _SITE_CACHE = None


def extract_with_strategy(tree, url: str, strategy: SiteStrategy) -> list[Candidate]:
    out: list[Candidate] = []
    try:
        cards = tree.css(strategy.container)
    except Exception:  # noqa: BLE001
        log.warning("strategy %s: container selector failed on %s", strategy.name, url)
        return out

    for card in cards:
        title = _first_text(card, *strategy.title) if strategy.title else ""
        body = _first_text(card, *strategy.text) if strategy.text else ""
        if not body and not title:
            # No field selectors matched: take the card's own text rather than
            # dropping a card the site clearly marked as a review.
            try:
                body = textclean.clean(card.get_all_text(separator=" ", strip=True))
            except Exception:  # noqa: BLE001
                body = ""
        text = "\n\n".join(p for p in (title, body) if p)
        if len(text) < MIN_TEXT_CHARS:
            continue

        rating = scale = None
        if strategy.rating:
            raw = ""
            if strategy.rating_attr:
                for sel in strategy.rating:
                    raw = _first_attr(card, sel, strategy.rating_attr)
                    if raw:
                        break
            if not raw:
                raw = _first_text(card, *strategy.rating)
            rating, scale = parse_rating(raw, strategy.rating_scale)

        date = ""
        if strategy.date:
            for sel in strategy.date:
                date = _first_attr(card, sel, strategy.date_attr) or _first_text(card, sel)
                if date:
                    break

        link = ""
        for sel in strategy.link or ():
            link = _first_attr(card, sel, "href")
            if link:
                break

        out.append(
            Candidate(
                text=textclean.truncate(text, MAX_TEXT_CHARS),
                source_url=_resolve_url(tree, url, link) or url,
                # A site strategy is written against a page of reviews, so these
                # are reviews even when the card shows no rating.
                kind="review",
                author=(_first_text(card, *strategy.author) or None) if strategy.author else None,
                rating=rating,
                rating_scale=scale,
                created_at=_clean_date(date),
                title=title or None,
                strategy=f"site:{strategy.name}",
                evidence=strategy.container,
            )
        )
        if len(out) >= MAX_RECORDS_PER_PAGE:
            break
    return out


# --- 4. Structural -----------------------------------------------------------

# Containers that commonly hold review lists. Tried as seeds before falling back
# to whole-page sibling analysis.
_REVIEW_CONTAINER_HINTS = (
    '[class*="review" i]', '[id*="review" i]', '[data-testid*="review" i]',
    '[class*="comment" i]', '[id*="comment" i]', '[class*="testimonial" i]',
    '[class*="feedback" i]', '[itemprop="review"]', "article",
)
# Nodes that are never a review even when they sit inside a review container.
_STRUCTURAL_NOISE = re.compile(
    r"^\s*(sign in|log in|cookie|accept all|subscribe|newsletter|advertisement|"
    r"share this|read more|show more|helpful\?|report (abuse|this)|was this review)",
    re.I,
)


def extract_structural(tree, url: str) -> list[Candidate]:
    """Repeated sibling blocks that look like review cards, found by structure.

    This is the part that does not depend on class names. A page's review cards
    are siblings with near-identical internal structure, so once one is located,
    Scrapling's `find_similar()` returns the rest even when every class is a
    hashed build artefact. That is what makes it survive the markup drift the
    hand-written Trustpilot selectors documented as their known weakness.
    """
    best: list[Candidate] = []
    for hint in _REVIEW_CONTAINER_HINTS:
        try:
            seeds = tree.css(hint)
        except Exception:  # noqa: BLE001
            continue
        if not seeds:
            continue
        for seed in seeds[:4]:  # a few seeds; the first match is often a header
            group = [seed]
            try:
                similar = seed.find_similar(similarity_threshold=0.25)
                if similar:
                    group = [seed] + list(similar)
            except Exception:  # noqa: BLE001 — older/edge cases: just use the seed
                pass
            cands = _candidates_from_nodes(group, tree, url, f"structural:{hint}")
            # Prefer the richest grouping this page offers.
            if len(cands) > len(best):
                best = cands
            if len(best) >= MAX_RECORDS_PER_PAGE:
                return best[:MAX_RECORDS_PER_PAGE]
    return best


def _candidates_from_nodes(nodes, tree, url: str, strategy: str) -> list[Candidate]:
    out: list[Candidate] = []
    seen_texts: set[str] = set()
    for node in nodes:
        try:
            raw = node.get_all_text(separator="\n", strip=True)
        except Exception:  # noqa: BLE001
            continue
        text = textclean.clean(raw)
        if len(text) < MIN_TEXT_CHARS or _STRUCTURAL_NOISE.match(text):
            continue
        # Nested matches produce the same text twice (parent and child).
        key = text[:200]
        if key in seen_texts:
            continue
        seen_texts.add(key)

        rating, scale = _sniff_rating(node)
        date = _sniff_date(node)
        author = _sniff_author(node)
        # A card's full text includes its own byline and timestamp. Those are
        # already captured as fields, so leaving them in the body only adds noise
        # to the embedding and repeats itself on the dashboard.
        text = _strip_card_metadata(text, author)
        if len(text) < MIN_TEXT_CHARS:
            continue
        out.append(
            Candidate(
                text=textclean.truncate(text, MAX_TEXT_CHARS),
                source_url=url,
                # Only call it a review when the block itself carries review
                # evidence. Otherwise it is a mention, and counted as one.
                kind="review" if (rating is not None or author) else "mention",
                author=author,
                rating=rating,
                rating_scale=scale,
                created_at=date,
                strategy=strategy,
                evidence=_node_path(node),
            )
        )
        if len(out) >= MAX_RECORDS_PER_PAGE:
            break
    return out


# A standalone line that is only a date, a star count, or a "verified" badge.
_METADATA_LINE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}\s+\w{3,9}\s+\d{4}"                      # 4 Mar 2026
    r"|\w{3,9}\s+\d{1,2},?\s+\d{4}"                   # March 4, 2026
    r"|\d{4}-\d{2}-\d{2}[T\s0-9:.+Z-]*"               # 2026-03-04T08:12:00Z
    r"|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"                 # 04/03/2026
    r"|(?:a|an|\d+)\s+(?:second|minute|hour|day|week|month|year)s?\s+ago"
    r"|rated\s+\d(?:\.\d)?\s+out\s+of\s+\d"
    r"|\d(?:\.\d)?\s*(?:/\s*\d)?\s*stars?"
    r"|verified(?:\s+purchase|\s+review|\s+buyer)?"
    r"|helpful\??|\d+\s+(?:reviews?|comments?|likes?|upvotes?)"
    r")\s*$",
    re.I,
)


def _strip_card_metadata(text: str, author: str | None) -> str:
    """Remove byline/date/badge/boilerplate lines that are not the review."""
    lines = text.split("\n")
    author_key = (author or "").strip().casefold()
    kept = [
        ln for ln in lines
        if ln.strip()
        and ln.strip().casefold() != author_key
        and not _METADATA_LINE.match(ln)
        # Site chrome is never review content, whichever strategy found it. The
        # article fallback filtered these but the structural path did not, and
        # since `article` is one of the structural hints (Trustpilot wraps its
        # cards in <article>), a real article page went through structural
        # extraction and kept its cookie notice.
        and not _BOILERPLATE.search(ln)
    ]
    # If stripping removed nearly everything, the "metadata" was the content.
    cleaned = "\n".join(kept).strip()
    return cleaned if len(cleaned) >= MIN_TEXT_CHARS else text


def _sniff_rating(node) -> tuple[float | None, float | None]:
    """A rating the block states explicitly. Never inferred from wording."""
    for selector, attr in (
        ("[data-service-review-rating]", "data-service-review-rating"),
        ("[data-rating]", "data-rating"),
        ('[itemprop="ratingValue"]', "content"),
        ("[aria-label*='star' i]", "aria-label"),
        ("[aria-label*='rated' i]", "aria-label"),
        ("img[alt*='star' i]", "alt"),
        ("img[alt*='rated' i]", "alt"),
        ("meta[itemprop='ratingValue']", "content"),
    ):
        raw = _first_attr(node, selector, attr)
        if raw:
            rating, scale = parse_rating(raw)
            if rating is not None:
                return rating, scale
    return None, None


def _sniff_date(node) -> str | None:
    for selector in ("time[datetime]", "[datetime]", "[data-service-review-date-time-ago]"):
        raw = _first_attr(node, selector, "datetime") or _first_attr(
            node, selector, "data-service-review-date-time-ago"
        )
        if raw:
            return _clean_date(raw)
    return None


_AUTHOR_SELECTORS = (
    "[data-consumer-name-typography]",
    '[itemprop="author"]',
    '[class*="author" i]',
    '[class*="username" i]',
    '[class*="consumerName" i]',
    '[rel="author"]',
)


def _sniff_author(node) -> str | None:
    for selector in _AUTHOR_SELECTORS:
        value = _first_text(node, selector)
        # An "author" longer than a name is a bio or a quote; refuse it rather
        # than store a sentence in the author column.
        if value and 1 < len(value) <= 80 and "\n" not in value:
            return value
    return None


def _node_path(node) -> str:
    """A pointer back to where on the page this came from, for traceability."""
    try:
        return str(node.generate_css_selector)[:300]
    except Exception:  # noqa: BLE001
        try:
            return str(node.tag)
        except Exception:  # noqa: BLE001
            return ""


# --- 5. Article fallback -----------------------------------------------------

_BOILERPLATE = re.compile(
    r"(cookie|privacy policy|terms of (use|service)|all rights reserved|"
    r"sign up for our newsletter|accept all cookies|enable javascript)",
    re.I,
)


def extract_article(tree, url: str, title: str = "") -> list[Candidate]:
    """The page's readable text as a single MENTION (never a review).

    This is what the existing Firecrawl connector effectively produces, kept as
    the floor so a page with no review structure still contributes something. It
    is explicitly `kind="mention"`: counting a company's own About page as a
    customer review would be exactly the kind of quota-padding Phase 5 forbids.
    """
    for selector in ("article", "main", '[role="main"]', "#content", ".content", "body"):
        try:
            found = tree.css(selector)
        except Exception:  # noqa: BLE001
            continue
        if not found:
            continue
        try:
            raw = found[0].get_all_text(separator="\n", strip=True)
        except Exception:  # noqa: BLE001
            continue
        body = textclean.clean(raw)
        # Drop lines that are pure chrome, then re-check length.
        lines = [ln for ln in body.split("\n") if ln and not _BOILERPLATE.search(ln)]
        body = "\n".join(lines).strip()
        if len(body) < MIN_TEXT_CHARS:
            continue
        text = "\n\n".join(p for p in (textclean.clean(title), body) if p)
        return [
            Candidate(
                text=textclean.truncate(text, MAX_TEXT_CHARS),
                source_url=url,
                kind="mention",
                title=textclean.clean(title) or None,
                strategy="article",
                evidence=selector,
            )
        ]
    return []


# --- pagination --------------------------------------------------------------

_NEXT_SELECTORS = (
    'link[rel="next"]',
    'a[rel="next"]',
    'a[aria-label*="next" i]',
    'a[class*="next" i]',
    '[data-pagination-button-next-link]',
)
_PAGE_PARAM = re.compile(r"([?&](?:page|p|start|offset|pageNumber)=)(\d+)", re.I)


def find_next_urls(tree, url: str, limit: int = 1) -> list[str]:
    """Links the page itself declares as "next", policy-checked and canonical.

    Only follows links the page *marks* as next (rel=next, aria-label, data
    attribute). It does not synthesize `?page=N+1`: guessing page URLs is how a
    crawler ends up hammering a site with requests for pages that do not exist.
    """
    out: list[str] = []
    for selector in _NEXT_SELECTORS:
        href = _first_attr(tree, selector, "href")
        if not href:
            continue
        resolved = _resolve_url(tree, url, href)
        if not resolved or resolved == url:
            continue
        canonical = urlpolicy.canonical(resolved)
        if canonical and canonical not in out and urlpolicy.is_allowed(canonical, resolve=False):
            out.append(canonical)
        if len(out) >= limit:
            break
    return out


def _resolve_url(tree, base: str, href: str) -> str:
    """Absolute URL from a possibly relative href, and only if policy allows it."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    absolute = href
    if "://" not in href:
        try:
            absolute = tree.urljoin(href)
        except Exception:  # noqa: BLE001
            from urllib.parse import urljoin

            absolute = urljoin(base, href)
    # Syntactic check only: the fetch path resolves DNS when it actually fetches.
    return absolute if urlpolicy.is_allowed(absolute, resolve=False) else ""


def _clean_date(value: Any) -> str | None:
    """Keep a date string only if it looks like a date. No parsing, no guessing.

    `created_at` is a text column holding each source's own format; the dashboard
    reads an ISO prefix from it. A relative string ("6 years ago") is dropped
    rather than converted, because converting would invent a precision the page
    never gave us.

    >>> _clean_date("2026-03-04T10:00:00Z")
    '2026-03-04T10:00:00Z'
    >>> _clean_date("6 years ago") is None
    True
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or len(raw) > 64:
        return None
    return raw if re.match(r"^\s*\d{4}-\d{2}", raw) else None


# --- the entry point ---------------------------------------------------------

def extract(
    html: str,
    url: str,
    *,
    allow_article_fallback: bool = True,
    strategies: list[SiteStrategy] | None = None,
    find_pagination: bool = True,
) -> PageExtraction:
    """Run the extractors in precision order and return the first real yield.

    Returns a `PageExtraction` with `error` set rather than raising: one page
    failing to parse must never abort a collection.
    """
    result = PageExtraction(url=url)
    if not html or not html.strip():
        result.error = "empty response body"
        return result

    tree = parse(html, url)
    if tree is None:
        result.error = "scrapling is not installed (pip install scrapling)"
        return result

    try:
        result.title = textclean.clean(_first_text(tree, "title, h1"))
    except Exception:  # noqa: BLE001
        result.title = ""

    for name, runner in (
        ("jsonld", lambda: extract_jsonld(tree, url)),
        ("microdata", lambda: extract_microdata(tree, url)),
        ("site", lambda: _run_site(tree, url, strategies)),
        ("structural", lambda: extract_structural(tree, url)),
    ):
        try:
            candidates = runner()
        except Exception:  # noqa: BLE001
            log.exception("extractor %s failed on %s", name, url)
            continue
        if candidates:
            result.candidates = candidates[:MAX_RECORDS_PER_PAGE]
            result.strategy = candidates[0].strategy or name
            break

    if not result.candidates and allow_article_fallback:
        try:
            result.candidates = extract_article(tree, url, result.title)
            if result.candidates:
                result.strategy = "article"
        except Exception:  # noqa: BLE001
            log.exception("article fallback failed on %s", url)

    if not result.candidates and not result.error:
        result.error = "no extractable content"

    if find_pagination:
        try:
            result.next_urls = find_next_urls(tree, url)
        except Exception:  # noqa: BLE001
            result.next_urls = []

    return result


def _run_site(tree, url: str, strategies: list[SiteStrategy] | None) -> list[Candidate]:
    for strategy in strategies if strategies is not None else site_strategies():
        if strategy.matches(url):
            found = extract_with_strategy(tree, url, strategy)
            if found:
                return found
    return []
