"""Validation, deduplication and provenance for records on their way to storage.

Every engine now funnels through here before `loader.load()`. Three jobs.

**Validate.** Drop what should never reach the database: empty or malformed text,
ratings outside the documented 1-5 scale, citation URLs that are not usable
links, and — the one that matters most for a brand-reputation tool — text that
does not mention the brand at all. A keyword search for "Amadeus" returns the
composer, the film and an airline IT company; storing all three and letting the
LLM QC pass sort it out costs a model call per row and leaves the dashboard wrong
until it runs. A cheap deterministic check up front is both better and free.

**Deduplicate — carefully.** `stable_id` already makes re-scraping idempotent for
an *identical* capture. What it cannot catch is the same review read twice by two
engines that disagree slightly: Scrapling gets the full card text, the AI
extractor returns it without the trailing "Read more", and the two hash
differently. So there is a second, narrower pass.

The narrowness is deliberate and is the most important decision in this module.
Near-duplicate detection is scoped to **the same brand and the same canonical
URL**. Two reviews with near-identical text on *different* pages are two real
customers who both wrote "Great product, fast shipping" — and merging them would
silently delete genuine feedback and understate how often a sentiment appears.
Phase 6 asks us not to merge distinct reviews that happen to use similar
language, and a per-URL scope is how that guarantee is actually enforced rather
than hoped for.

**Record provenance.** The platform stays in `source`. How we got the row goes in
`extra["extraction"]`, alongside the validation verdict, so a row can always be
traced from the page through the database to a dashboard citation.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from . import text as textclean
from .models import NormalizedReview
from .scraping import urlpolicy

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = int(os.environ.get("QUALITY_MIN_TEXT_CHARS", "40"))
MAX_TEXT_CHARS = int(os.environ.get("QUALITY_MAX_TEXT_CHARS", "6000"))
#: Jaccard overlap of word 4-grams above which two records on the SAME URL are
#: treated as one review read twice.
NEAR_DUPLICATE_THRESHOLD = float(os.environ.get("QUALITY_NEAR_DUP_THRESHOLD", "0.82"))
#: Require the brand to be mentioned in the text. Off for sources where the row
#: is on-topic by construction (see `TRUSTED_SOURCES`).
REQUIRE_BRAND_MENTION = os.environ.get("QUALITY_REQUIRE_BRAND_MENTION", "1") != "0"

#: Rows from these sources were resolved by app id or brand page, not by a
#: keyword match, so they are on-topic without mentioning the brand by name —
#: an App Store review rarely repeats the app's name. Same list `qc.py` trusts.
TRUSTED_SOURCES = (
    "app_store", "google_play", "trustpilot", "google_maps", "yelp", "tripadvisor",
)

#: Words too generic to establish that a text is about a brand. "Coffee" alone
#: must not make a page about coffee count as a mention of "Blue Bottle Coffee".
#: Mirrors the set `api/rag.py` uses for the same reason.
_GENERIC_BRAND_WORDS = frozenset({
    "coffee", "inc", "co", "company", "ltd", "llc", "corp", "corporation",
    "group", "the", "brand", "app", "store", "shop", "online", "official",
    "limited", "holdings", "international", "global", "services", "solutions",
    "technologies", "tech", "labs", "studio", "studios", "media", "digital",
})

#: Boilerplate that is a page, not feedback. Checked against the WHOLE text, so a
#: review that happens to mention cookies is unaffected.
_BOILERPLATE_ONLY = re.compile(
    r"^(?:[^a-z]*)(?:"
    r"enable javascript|javascript is (?:required|disabled)|"
    r"access denied|403 forbidden|404 not found|page not found|"
    r"are you a robot|verify you are human|checking your browser|"
    r"this site uses cookies|we use cookies|cookie (?:policy|notice|settings)|"
    r"sign in to continue|please log in|create an account to|"
    r"subscribe to (?:read|continue)|your session has expired"
    r")",
    re.I,
)

_WORD = re.compile(r"[a-z0-9']+")


@dataclass
class Verdict:
    """Why a record was kept or dropped."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class QualityReport:
    """What a validation pass did, for metrics and for the run record."""

    kept: int = 0
    rejected: int = 0
    duplicates: int = 0
    repaired: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def duplicate(self, reason: str = "near-duplicate on the same URL") -> None:
        self.duplicates += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {
            "kept": self.kept,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "repaired": self.repaired,
            "reasons": dict(self.reasons),
        }


# --- brand relevance ---------------------------------------------------------

def brand_tokens(brand: str) -> list[str]:
    """The distinctive words of a brand name.

    >>> brand_tokens("Blue Bottle Coffee")
    ['blue', 'bottle']
    >>> brand_tokens("Nike")
    ['nike']

    A name made entirely of generic words keeps all of them, because dropping
    them all would leave nothing to match on and make every text a match:

    >>> brand_tokens("The Coffee Company")
    ['the', 'coffee', 'company']
    """
    tokens = [t for t in _WORD.findall((brand or "").lower()) if len(t) > 1]
    distinctive = [t for t in tokens if t not in _GENERIC_BRAND_WORDS]
    # A brand made entirely of generic words still needs something to match on.
    return distinctive or tokens


def mentions_brand(text: str, brand: str, keywords: list[str] | None = None) -> bool:
    """Whether `text` plausibly refers to `brand`.

    Deliberately permissive: this exists to drop the obvious keyword collisions
    cheaply, not to replace the LLM QC pass that judges the subtle ones. A single
    distinctive token is enough, and any configured keyword counts.

    >>> mentions_brand("The Blue Bottle on Mission is great", "Blue Bottle Coffee")
    True
    >>> mentions_brand("I love coffee in the morning", "Blue Bottle Coffee")
    False
    >>> mentions_brand("bluebottle delivery was late", "Blue Bottle Coffee")
    True
    """
    if not text:
        return False
    haystack = " ".join(_WORD.findall(text.lower()))
    if not haystack:
        return False
    # Also match a run-together form ("bluebottle"), which is how handles and
    # hashtags spell brand names.
    squashed = haystack.replace(" ", "")

    for term in [brand, *(keywords or [])]:
        tokens = brand_tokens(term)
        if not tokens:
            continue
        if all(t in haystack for t in tokens):
            return True
        joined = "".join(tokens)
        if len(joined) >= 5 and joined in squashed:
            return True
    return False


# --- validation --------------------------------------------------------------

def normalize_rating(rating) -> tuple[float | None, bool]:
    """A rating on the documented 1-5 scale, and whether it was repaired.

    `models.py` documents `rating` as "1-5 where the source has one". A value
    outside that is a parsing failure, and a wrong rating is worse than none, so
    it is dropped rather than clamped — except for a 0, which review platforms
    use for "no rating given".

    >>> normalize_rating(4.5)
    (4.5, False)
    >>> normalize_rating(0)
    (None, True)
    >>> normalize_rating(9)
    (None, True)
    >>> normalize_rating("4")
    (4.0, True)
    >>> normalize_rating(None)
    (None, False)
    """
    if rating is None:
        return None, False
    repaired = not isinstance(rating, float)
    try:
        value = float(rating)
    except (TypeError, ValueError):
        return None, True
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None, True
    if value <= 0 or value > 5:
        return None, True
    return round(value, 2), repaired


def normalize_created_at(value) -> tuple[str | None, bool]:
    """A `created_at` the dashboard can read, or None. Never a guessed date.

    `marts.reviews.created_at` is text holding each source's own format, and the
    trend query reads an ISO `YYYY-MM` prefix from it. So an ISO-prefixed string
    is kept as-is and anything else is dropped: converting "6 years ago" into a
    date would invent a precision the page never gave us.

    >>> normalize_created_at("2026-03-04T08:12:00Z")
    ('2026-03-04T08:12:00Z', False)
    >>> normalize_created_at("6 years ago")
    (None, True)
    >>> normalize_created_at(None)
    (None, False)
    """
    if value is None:
        return None, False
    raw = str(value).strip()
    if not raw:
        return None, False
    if len(raw) > 64:
        return None, True
    return (raw, False) if re.match(r"^\d{4}-\d{2}", raw) else (None, True)


def validate(
    review: NormalizedReview,
    *,
    keywords: list[str] | None = None,
    require_brand_mention: bool | None = None,
) -> Verdict:
    """Whether this record may be stored. Mutates nothing; see `clean_review()`."""
    if not review.brand or not review.brand.strip():
        return Verdict(False, "no brand")

    text = (review.text or "").strip()
    if not text:
        return Verdict(False, "empty text")
    if len(text) < MIN_TEXT_CHARS:
        return Verdict(False, f"text shorter than {MIN_TEXT_CHARS} characters")
    if _BOILERPLATE_ONLY.match(text):
        return Verdict(False, "page boilerplate, not feedback")
    # A "review" that is one word repeated is a scraping artefact.
    words = _WORD.findall(text.lower())
    if words and len(set(words)) <= 2 and len(words) > 6:
        return Verdict(False, "degenerate repeated text")

    url = (review.source_url or "").strip()
    if not url:
        return Verdict(False, "no source_url, so the row could not be cited")
    if not urlpolicy.is_allowed(url, resolve=False):
        return Verdict(False, "source_url is not a usable public http(s) link")

    should_check = (
        REQUIRE_BRAND_MENTION if require_brand_mention is None else require_brand_mention
    )
    if should_check and review.source not in TRUSTED_SOURCES:
        if not mentions_brand(text, review.brand, keywords):
            return Verdict(False, "text does not mention the brand")

    return Verdict(True)


def clean_review(review: NormalizedReview, report: QualityReport | None = None) -> NormalizedReview:
    """Return a normalized copy: clean text, sane rating, canonical URL.

    Does not decide whether to keep it — `validate()` does that, and should run
    on the cleaned copy so it judges what would actually be stored.
    """
    text = textclean.clean(review.text)
    if len(text) > MAX_TEXT_CHARS:
        text = textclean.truncate(text, MAX_TEXT_CHARS)

    rating, _ = normalize_rating(review.rating)
    created_at, _ = normalize_created_at(review.created_at)

    author = textclean.clean(review.author or "") or None
    author_dropped = False
    if author and len(author) > 120:
        # An "author" this long is a bio or a quote that landed in the wrong
        # field. Drop it rather than store a paragraph as a person's name.
        author = None
        author_dropped = True

    url = urlpolicy.canonical(review.source_url) or (review.source_url or "").strip()

    # Record only what was actually LOST. A value that merely changed type
    # (int 4 -> 4.0) was not dropped, and saying it was would make the audit
    # trail lie about the data.
    rating_dropped = review.rating is not None and rating is None
    date_dropped = review.created_at not in (None, "") and created_at is None

    if report is not None and (rating_dropped or date_dropped or author_dropped):
        report.repaired += 1

    extra = dict(review.extra or {})
    validation = {
        "status": "validated",
        "min_chars": MIN_TEXT_CHARS,
    }
    if rating_dropped:
        validation["rating_dropped"] = review.rating
    if date_dropped:
        validation["created_at_dropped"] = review.created_at
    if author_dropped:
        validation["author_dropped"] = True
    extraction = dict(extra.get("extraction") or {})
    extraction.setdefault("engine", "connector")
    extraction["validation"] = validation
    extra["extraction"] = extraction

    return NormalizedReview(
        brand=review.brand.strip(),
        source=review.source,
        source_url=url,
        text=text,
        author=author,
        rating=rating,
        captured_at=review.captured_at,
        created_at=created_at,
        extra=extra,
    )


# --- deduplication -----------------------------------------------------------

def _shingles(text: str, n: int = 4) -> frozenset[tuple[str, ...]]:
    words = _WORD.findall((text or "").lower())
    if len(words) < n:
        return frozenset([tuple(words)]) if words else frozenset()
    return frozenset(tuple(words[i : i + n]) for i in range(len(words) - n + 1))


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of word 4-grams.

    >>> similarity("the app keeps crashing on login", "the app keeps crashing on login")
    1.0
    >>> round(similarity("the app keeps crashing on login", "shipping was very fast"), 2)
    0.0
    """
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return round(inter / union, 4) if union else 0.0


class Deduplicator:
    """Tracks what has been seen so the same review is stored once.

    Two levels:

    * `stable_id` — the existing deterministic identity. Unchanged, and still the
      database's primary key.
    * near-duplicate, **scoped to one (brand, canonical URL)** — catches the same
      review read by two engines with slightly different text. The scope is the
      safety property: it is structurally incapable of merging two customers who
      independently wrote the same short sentence on different pages.
    """

    def __init__(self, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> None:
        self.threshold = threshold
        self._ids: set[str] = set()
        #: (brand, url) -> list of texts already accepted there
        self._by_location: dict[tuple[str, str], list[str]] = {}

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> set[str]:
        return self._ids

    def seen_id(self, review: NormalizedReview) -> bool:
        return review.id in self._ids

    @staticmethod
    def _kind(review: NormalizedReview) -> str:
        return ((review.extra or {}).get("extraction") or {}).get("kind") or "review"

    def is_duplicate(self, review: NormalizedReview) -> tuple[bool, str]:
        """Whether this record duplicates one already accepted, and how."""
        if review.id in self._ids:
            return True, "identical capture (same stable id)"

        key = (review.brand.strip().lower(), urlpolicy.canonical(review.source_url))
        existing = self._by_location.get(key)
        if not existing:
            return False, ""

        kind = self._kind(review)
        normalized = " ".join(_WORD.findall((review.text or "").lower()))
        for other_text, other_kind in existing:
            other_normalized = " ".join(_WORD.findall(other_text.lower()))
            if normalized == other_normalized:
                return True, "same text on the same URL"
            score = similarity(review.text, other_text)
            if score >= self.threshold:
                return True, f"near-duplicate on the same URL (overlap {score:.0%})"

            # One engine returning a truncated version of what another returned
            # in full is the common cross-engine case, and containment catches it
            # where overlap does not (a short substring has low Jaccard).
            #
            # But containment must NOT fire across kinds. A whole-page "mention"
            # contains every individual review quote on that page by definition,
            # so without this guard the article-level fallback would swallow the
            # specific reviews that an engine later extracted from the same page
            # — deleting the better data in favour of the worse.
            if kind != other_kind:
                continue
            short, long = sorted((normalized, other_normalized), key=len)
            if len(short) >= 60 and short in long:
                return True, "truncated copy of a record already stored for this URL"
        return False, ""

    def add(self, review: NormalizedReview) -> None:
        self._ids.add(review.id)
        key = (review.brand.strip().lower(), urlpolicy.canonical(review.source_url))
        bucket = self._by_location.setdefault(key, [])
        # Bound the per-URL memory: a page with hundreds of reviews would
        # otherwise hold every text for the whole run.
        if len(bucket) < 400:
            bucket.append((review.text, self._kind(review)))

    def forget_location(self, review: NormalizedReview) -> None:
        """Drop a record so a better one for the same URL can replace it.

        Used when a page yielded only a whole-page mention and an engine then
        extracted the real reviews from it: the mention is superseded, and
        keeping both would double-count the page's content.
        """
        self._ids.discard(review.id)
        key = (review.brand.strip().lower(), urlpolicy.canonical(review.source_url))
        bucket = self._by_location.get(key)
        if not bucket:
            return
        self._by_location[key] = [
            entry for entry in bucket if entry[0] != review.text
        ]

    def seed(self, ids) -> None:
        """Pre-load known ids (e.g. a run's already-loaded set)."""
        self._ids.update(ids)


# --- the pipeline entry point ------------------------------------------------

def prepare(
    reviews,
    *,
    keywords: list[str] | None = None,
    dedup: Deduplicator | None = None,
    report: QualityReport | None = None,
    require_brand_mention: bool | None = None,
) -> list[NormalizedReview]:
    """Clean, validate and deduplicate records on their way to the loader.

    The one function every engine's output should pass through. Returns the
    records worth storing, in order, and records why the rest were dropped.
    """
    dedup = dedup if dedup is not None else Deduplicator()
    report = report if report is not None else QualityReport()
    out: list[NormalizedReview] = []

    for review in reviews:
        try:
            cleaned = clean_review(review, report)
        except Exception:  # noqa: BLE001 — one malformed record must not stop a batch
            log.exception("quality: could not clean a record; dropping it")
            report.reject("could not be normalized")
            continue

        verdict = validate(
            cleaned, keywords=keywords, require_brand_mention=require_brand_mention
        )
        if not verdict:
            report.reject(verdict.reason)
            continue

        duplicate, how = dedup.is_duplicate(cleaned)
        if duplicate:
            report.duplicate(how)
            continue

        dedup.add(cleaned)
        report.kept += 1
        out.append(cleaned)

    return out
