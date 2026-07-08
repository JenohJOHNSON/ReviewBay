"""CSV export of a brand's reviews.

Streams the full reviews (not the dashboard's truncated excerpts) as CSV so users
can pull the raw data into a spreadsheet or their own tooling.
"""

from __future__ import annotations

import csv
import io

_WHERE = "(%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)"
_COLS = ["brand", "source", "source_url", "author", "rating", "sentiment",
         "created_at", "captured_at", "text"]


def _connect():
    from ..db import connect

    return connect()


def reviews_csv(brand: str | None = None, limit: int = 5000, source: str | None = None) -> str:
    limit = max(1, min(int(limit), 50000))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_COLS)}
                FROM marts.reviews
                WHERE {_WHERE} AND (%(source)s::text IS NULL OR source = %(source)s::text)
                ORDER BY captured_at DESC
                LIMIT %(limit)s
                """,
                {"brand": brand, "limit": limit, "source": source or None},
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_COLS)
    for row in rows:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def _slug(brand: str | None) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (brand or "all").lower()).strip("-")
    return base or "all"


def reviews_filename(brand: str | None) -> str:
    return f"reviewbay-{_slug(brand)}-reviews.csv"
