"""Aggregations for the dashboard — all from MARTS.REVIEWS (no scraping cost).

Returns one JSON blob the dashboard page renders: totals, sentiment split,
source mix, per-brand counts, average rating, and the latest negative reviews.
Optional brand filter narrows everything except the brand list.
"""

from __future__ import annotations

import os

_WHERE = "(%(brand)s IS NULL OR brand ILIKE %(brand)s)"

# Whitelisted sort orders for the reviews list. Only these keys are accepted from
# the API, so the clause can be interpolated safely (no SQL injection surface).
_SORTS = {
    "recent": "captured_at DESC",
    "oldest": "captured_at ASC",
    "rating_high": "rating DESC NULLS LAST",
    "rating_low": "rating ASC NULLS LAST",
    "negative": "CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END, captured_at DESC",
    "positive": "CASE sentiment WHEN 'positive' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END, captured_at DESC",
}


def _connect():
    import snowflake.connector  # type: ignore

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD"),
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "REVIEWBOT"),
        schema="MARTS",
    )


def get_stats(brand: str | None = None) -> dict:
    b = {"brand": brand}
    conn = _connect()
    try:
        cur = conn.cursor()

        cur.execute(f"SELECT COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE}", b)
        total = cur.fetchone()[0]

        sentiment = {"positive": 0, "neutral": 0, "negative": 0}
        cur.execute(
            f"SELECT sentiment, COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE} GROUP BY sentiment", b
        )
        for s, c in cur.fetchall():
            if s in sentiment:
                sentiment[s] = c

        cur.execute(
            f"SELECT source, COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE} GROUP BY source ORDER BY 2 DESC",
            b,
        )
        by_source = [{"source": s, "count": c} for s, c in cur.fetchall()]

        cur.execute(
            f"SELECT AVG(rating) FROM MARTS.REVIEWS WHERE rating IS NOT NULL AND {_WHERE}", b
        )
        ar = cur.fetchone()[0]
        avg_rating = round(float(ar), 2) if ar is not None else None

        cur.execute(
            f"""
            SELECT text, source, source_url, author, rating, brand, captured_at
            FROM MARTS.REVIEWS
            WHERE sentiment = 'negative' AND {_WHERE}
            ORDER BY captured_at DESC
            LIMIT 12
            """,
            b,
        )
        cols = [c[0].lower() for c in cur.description]
        recent_negative = []
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            t = r.pop("text", "") or ""
            r["excerpt"] = (t[:240] + "…") if len(t) > 240 else t
            recent_negative.append(r)

        cur.close()
        return {
            "brand": brand,
            # Brand roster intentionally omitted: the per-browser brand list lives
            # in the client's localStorage, so the API never returns every brand.
            "brands": [],
            "total": total,
            "sentiment": sentiment,
            "by_source": by_source,
            "by_brand": [],
            "avg_rating": avg_rating,
            "recent_negative": recent_negative,
        }
    finally:
        conn.close()


def _brand_block(cur, brand: str) -> dict:
    """One brand's slice for the compare view: count, sentiment, rating, sources."""
    b = {"brand": brand}

    cur.execute(f"SELECT COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE}", b)
    total = cur.fetchone()[0]

    sentiment = {"positive": 0, "neutral": 0, "negative": 0}
    cur.execute(
        f"SELECT sentiment, COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE} GROUP BY sentiment", b
    )
    for s, c in cur.fetchall():
        if s in sentiment:
            sentiment[s] = c

    cur.execute(f"SELECT AVG(rating) FROM MARTS.REVIEWS WHERE rating IS NOT NULL AND {_WHERE}", b)
    ar = cur.fetchone()[0]
    avg_rating = round(float(ar), 2) if ar is not None else None

    cur.execute(
        f"SELECT source, COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE} GROUP BY source ORDER BY 2 DESC",
        b,
    )
    by_source = [{"source": s, "count": c} for s, c in cur.fetchall()]

    return {
        "brand": brand,
        "total": total,
        "sentiment": sentiment,
        "avg_rating": avg_rating,
        "by_source": by_source,
    }


def compare_stats(a: str | None = None, b: str | None = None) -> dict:
    """Two brands side by side, using ONLY the brands explicitly requested.

    We intentionally do not auto-fill from the database. The brand list is
    per-browser (localStorage), so an unspecified side stays empty rather than
    leaking whatever brands happen to be stored.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        left = _brand_block(cur, a) if a else None
        right = _brand_block(cur, b) if b else None
        cur.close()
        return {"brands": [], "a": left, "b": right}
    finally:
        conn.close()


def get_reviews(brand: str | None = None, sort: str = "recent", limit: int = 40) -> dict:
    """Reviews for the dashboard list, ordered by a whitelisted sort key."""
    order = _SORTS.get(sort, _SORTS["recent"])
    limit = max(1, min(int(limit), 200))
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT text, source, source_url, author, rating, brand, sentiment, captured_at
            FROM MARTS.REVIEWS
            WHERE {_WHERE}
            ORDER BY {order}
            LIMIT {limit}
            """,
            {"brand": brand},
        )
        cols = [c[0].lower() for c in cur.description]
        reviews = []
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            t = r.pop("text", "") or ""
            r["excerpt"] = (t[:240] + "…") if len(t) > 240 else t
            ca = r.get("captured_at")
            r["captured_at"] = ca.isoformat() if hasattr(ca, "isoformat") else (str(ca) if ca else None)
            reviews.append(r)
        cur.close()
        return {"brand": brand, "sort": sort if sort in _SORTS else "recent", "reviews": reviews}
    finally:
        conn.close()
