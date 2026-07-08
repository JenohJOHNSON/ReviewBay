"""Loads normalized reviews into Postgres ``raw.reviews_raw`` via upsert.

``INSERT ... ON CONFLICT (id) DO UPDATE`` makes re-scraping idempotent: the stable
id dedupes on the way in. The connection comes from ``DATABASE_URL`` (see
``reviewbot.db``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from ..db import connect
from ..models import NormalizedReview

log = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO raw.reviews_raw
    (id, brand, source, source_url, author, rating, text, created_at, captured_at, extra)
VALUES
    (%(id)s, %(brand)s, %(source)s, %(source_url)s, %(author)s, %(rating)s,
     %(text)s, %(created_at)s, %(captured_at)s, %(extra)s::jsonb)
ON CONFLICT (id) DO UPDATE SET
    captured_at = EXCLUDED.captured_at,
    rating      = EXCLUDED.rating,
    extra       = EXCLUDED.extra
"""


def load(reviews: Iterable[NormalizedReview]) -> int:
    """Upsert reviews. Returns the count written."""
    rows = [r.to_row() for r in reviews]
    if not rows:
        return 0

    for row in rows:
        row["extra"] = json.dumps(row.get("extra") or {})

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, rows)
        conn.commit()
        log.info("merged %d reviews into raw.reviews_raw", len(rows))
        return len(rows)
    finally:
        conn.close()
