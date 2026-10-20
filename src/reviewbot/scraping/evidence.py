"""Proving that an extracted record is actually on the page it claims.

A deterministic extractor cannot invent a review: it copies a DOM node's text.
An LLM extractor can, and will — a model asked to "extract the reviews" from a
page with no reviews often produces plausible ones, and a model summarizing a
long review will paraphrase it into something the customer never wrote.

ReviewBay's entire product claim is that every statement links back to a real
person's words, so a paraphrase stored as a quotation is not a cosmetic problem,
it is the product being wrong. This module is the gate: an AI-extracted record is
kept only if its text is **demonstrably present in the fetched content**.

The check is n-gram containment, not similarity. Overlap scores are easy to game
with a fluent paraphrase; word-sequence containment is not. A record passes when
enough of its word 5-grams appear verbatim in the page, which a copy satisfies
trivially and a paraphrase fails.

Fields are checked individually, and the rule is "drop the field, keep the
record": an unverifiable author becomes `None` rather than disqualifying a review
whose text checks out. Phase 6's instruction is to prefer missing values over
fabricated ones, and this is where that happens.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Fraction of a record's word n-grams that must appear verbatim in the source.
#: 0.75 accepts a copy with minor whitespace or entity differences and rejects a
#: paraphrase. Raise toward 1.0 for stricter, lower for more recall.
SUPPORT_THRESHOLD = float(os.environ.get("EXTRACTION_SUPPORT_THRESHOLD", "0.75"))
#: n-gram width. 5 words is long enough that matching by chance is negligible and
#: short enough to survive a dropped word.
NGRAM = int(os.environ.get("EXTRACTION_SUPPORT_NGRAM", "5"))

_WORD = re.compile(r"[a-z0-9']+")


def _words(value: str) -> list[str]:
    return _WORD.findall((value or "").lower())


def _ngrams(words: list[str], n: int) -> list[tuple[str, ...]]:
    if len(words) < n:
        return [tuple(words)] if words else []
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


@dataclass(frozen=True)
class Support:
    """How well a piece of text is supported by the source content."""

    supported: bool
    score: float
    reason: str = ""

    def __bool__(self) -> bool:
        return self.supported


class SourceEvidence:
    """The page's text, indexed once so many records can be checked cheaply.

    Building the n-gram set is O(page); each check is then O(record), which
    matters because one page produces many records.
    """

    def __init__(self, content: str, n: int = NGRAM) -> None:
        self.n = max(2, n)
        self._words = _words(content)
        self._grams = set(_ngrams(self._words, self.n))
        # A flattened copy for cheap substring checks of short fields (an author
        # name is usually shorter than one n-gram).
        self._flat = " ".join(self._words)

    @property
    def word_count(self) -> int:
        return len(self._words)

    def supports(self, text: str, threshold: float = SUPPORT_THRESHOLD) -> Support:
        """Whether `text` is present in the source content.

        A short record (fewer words than one n-gram) falls back to substring
        containment, which is the correct check at that length.
        """
        words = _words(text)
        if not words:
            return Support(False, 0.0, "empty text")
        if not self._grams and not self._flat:
            return Support(False, 0.0, "no source content to check against")

        if len(words) < self.n:
            present = " ".join(words) in self._flat
            return Support(
                present, 1.0 if present else 0.0,
                "" if present else "short text not found verbatim in the page",
            )

        grams = _ngrams(words, self.n)
        hits = sum(1 for g in grams if g in self._grams)
        score = hits / len(grams)
        if score >= threshold:
            return Support(True, round(score, 4))
        return Support(
            False, round(score, 4),
            f"only {score:.0%} of the text appears verbatim in the page "
            f"(need {threshold:.0%}) — likely paraphrased or invented",
        )

    def mentions(self, value: str) -> bool:
        """Whether a short field (an author name, a date string) is on the page."""
        words = _words(value)
        if not words:
            return False
        return " ".join(words) in self._flat


def verify_text(content: str, text: str, threshold: float = SUPPORT_THRESHOLD) -> Support:
    """One-shot check, for a caller with a single record."""
    return SourceEvidence(content).supports(text, threshold)
