"""Loads normalized reviews into Postgres ``raw.reviews_raw`` via upsert.

``INSERT ... ON CONFLICT (id) DO UPDATE`` makes re-scraping idempotent: the stable
id dedupes on the way in. The connection comes from ``DATABASE_URL`` (see
``reviewbot.db``), reused from the idle set rather than opened per call — this
runs once per source per collection stage, so it was ~8+ Neon handshakes per
onboard.

``load()`` returns the number of rows it *sent*, which is not the number of new
rows: ``ON CONFLICT DO UPDATE`` does not distinguish an insert from an update.
Use ``load_counted()`` when the caller needs to report real novelty to a user.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from .. import db
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

# `xmax = 0` is true only for a freshly inserted tuple, so RETURNING it tells an
# insert apart from an ON CONFLICT update. That is what lets the UI say "47 new"
# instead of counting rows it already had from a previous run.
_UPSERT_COUNTED = _UPSERT + "\nRETURNING (xmax = 0) AS inserted"

# Rows per statement. Large enough to amortize the round trip, small enough that
# one failure does not roll back a whole collection.
_CHUNK = 500


def _rows(reviews: Iterable[NormalizedReview]) -> list[dict]:
    rows = [r.to_row() for r in reviews]
    for row in rows:
        row["extra"] = json.dumps(row.get("extra") or {})
    return rows


def load(reviews: Iterable[NormalizedReview]) -> int:
    """Upsert reviews. Returns the count written (sent, not necessarily new)."""
    rows = _rows(reviews)
    if not rows:
        return 0

    with db.connection() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), _CHUNK):
                cur.executemany(_UPSERT, rows[i : i + _CHUNK])
        conn.commit()
    log.info("merged %d reviews into raw.reviews_raw", len(rows))
    return len(rows)


def load_counted(reviews: Iterable[NormalizedReview]) -> tuple[int, int]:
    """Upsert reviews. Returns (rows_sent, rows_newly_inserted).

    The second number is the honest one to show a user as "collected": re-running
    a collection upserts the same rows, and counting those as fresh finds is how
    a repeat onboard appeared to collect a full set again.
    """
    rows = _rows(reviews)
    if not rows:
        return 0, 0

    inserted = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), _CHUNK):
                chunk = rows[i : i + _CHUNK]
                try:
                    cur.executemany(_UPSERT_COUNTED, chunk, returning=True)
                except TypeError:
                    # psycopg without executemany(returning=...): fall back to
                    # per-row execution for this chunk so the count stays real.
                    for row in chunk:
                        cur.execute(_UPSERT_COUNTED, row)
                        got = cur.fetchone()
                        if got and got[0]:
                            inserted += 1
                    continue
                # executemany(returning=True) exposes one result set per row.
                while True:
                    got = cur.fetchone()
                    if got and got[0]:
                        inserted += 1
                    if not cur.nextset():
                        break
        conn.commit()
    log.info(
        "merged %d reviews into raw.reviews_raw (%d new, %d already known)",
        len(rows), inserted, len(rows) - inserted,
    )
    return len(rows), inserted
