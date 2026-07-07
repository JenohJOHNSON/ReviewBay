"""Loads normalized reviews into Neon Postgres via INSERT ... ON CONFLICT.

The stable review id dedupes re-scrapes, so each poll updates the latest scrape
metadata instead of appending duplicates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from .. import db
from ..models import NormalizedReview

log = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO raw.reviews_raw (
    id, brand, source, source_url, author, rating, text, created_at, captured_at, extra
) VALUES (
    %(id)s, %(brand)s, %(source)s, %(source_url)s, %(author)s, %(rating)s,
    %(text)s, %(created_at)s, %(captured_at)s, (%(extra)s)::jsonb
)
ON CONFLICT (id) DO UPDATE SET
    captured_at = EXCLUDED.captured_at,
    rating = EXCLUDED.rating,
    extra = EXCLUDED.extra
"""


def load(reviews: Iterable[NormalizedReview]) -> int:
    """Upsert reviews. Returns the count written."""
    rows = [r.to_row() for r in reviews]
    if not rows:
        return 0

    for row in rows:
        row["extra"] = json.dumps(row.get("extra") or {})

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, rows)
        conn.commit()
        log.info("upserted %d reviews into raw.reviews_raw", len(rows))
        return len(rows)
    finally:
        conn.close()
