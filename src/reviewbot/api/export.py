"""CSV export of a brand's reviews.

Streams the full reviews (not the dashboard's truncated excerpts) as CSV so users
can pull the raw data into a spreadsheet or their own tooling.

Two safeguards that are easy to miss:

* The brand filter goes through `db.like_param()` like every other brand query,
  and the relevance predicate matches the dashboard, so an export cannot hand
  back rows the QC pass judged off-topic (or, worse, every brand's rows).
* Review text is attacker-controlled and the destination is a spreadsheet, so
  every field is passed through `_csv_safe()`. A review beginning `=`, `+`, `-`
  or `@` is a live formula in Excel, Sheets and LibreOffice.
"""

from __future__ import annotations

import csv
import io

from .. import db

_WHERE = "(%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text) AND relevant IS NOT FALSE"
_COLS = ["brand", "source", "source_url", "author", "rating", "sentiment",
         "created_at", "captured_at", "text"]

# Characters a spreadsheet treats as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Neutralize spreadsheet formula injection without altering the reading.

    Prefixing with a single quote is the convention spreadsheets understand as
    "this is literal text"; it is visible in the cell's formula bar but not in
    the rendered value.
    """
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def reviews_csv(brand: str | None = None, limit: int = 5000, source: str | None = None) -> str:
    limit = max(1, min(int(limit), 50000))
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_COLS)}
                FROM marts.reviews
                WHERE {_WHERE} AND (%(source)s::text IS NULL OR source = %(source)s::text)
                ORDER BY captured_at DESC
                LIMIT %(limit)s
                """,
                {"brand": db.like_param(brand), "limit": limit, "source": source or None},
            )
            rows = cur.fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_COLS)
    for row in rows:
        w.writerow([_csv_safe(v) for v in row])
    return buf.getvalue()


def _slug(brand: str | None) -> str:
    base = "".join(c if c.isalnum() else "-" for c in (brand or "all").lower()).strip("-")
    return base or "all"


def reviews_filename(brand: str | None) -> str:
    return f"reviewbay-{_slug(brand)}-reviews.csv"
