"""The normalized record every connector produces.

This single shape is the contract that makes the whole system work: it is what
lands in Postgres, what gets embedded, and (crucially) what powers the citations
the chatbot returns. Every field that identifies *where* a piece of feedback came
from (source, source_url, author) is carried end-to-end.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def stable_id(brand: str, source: str, source_url: str, text: str) -> str:
    """Deterministic id so re-scraping the same review does not duplicate it.

    Keyed on (brand, source, url, text) rather than a random uuid, which is what
    lets the Postgres upsert (ON CONFLICT) update instead of append on every poll.
    Brand is part of the key so the SAME page found under two different brands
    (e.g. an "A vs B" article surfaced by both brand searches) becomes two
    correctly-attributed rows instead of one row whose brand flips, keeping every
    brand's data cleanly separated.
    """
    digest = hashlib.sha256(f"{brand}|{source}|{source_url}|{text}".encode("utf-8"))
    return digest.hexdigest()


@dataclass
class NormalizedReview:
    """One piece of user feedback, source-agnostic."""

    brand: str
    source: str  # "reddit", "google_maps", "yelp", "tripadvisor", ...
    source_url: str  # the link back to the original — the citation
    text: str
    author: str | None = None
    rating: float | None = None  # 1-5 where the source has one; None otherwise
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    created_at: str | None = None  # when the review itself was posted, if known
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return stable_id(self.brand, self.source, self.source_url, self.text)

    def to_row(self) -> dict[str, Any]:
        """Flatten to the shape the loader expects."""
        d = asdict(self)
        d["id"] = self.id
        d["extra"] = d.get("extra") or {}
        return d
