"""Aggregations for the dashboard — all from MARTS.REVIEWS (no scraping cost).

Returns one JSON blob the dashboard page renders: totals, sentiment split,
source mix, per-brand counts, average rating, and the latest negative reviews.
Optional brand filter narrows everything except the brand list.

Two things to know before editing a query here:

* The brand filter is `brand ILIKE :brand`, so the value MUST go through
  `db.like_param()`. Unescaped, `?brand=%` matches every brand in the database.
* `created_at` is stored as text in mixed ISO shapes. The trend and alert
  aggregations therefore bucket on a regex-guarded `YYYY-MM-DD` prefix inside
  Postgres. The previous version pulled every matching row into Python to parse
  dates there, which meant a full table read on every dashboard load.
"""

from __future__ import annotations

from .. import db

# ::text so Postgres can infer the parameter type in the `IS NULL` branch.
# `relevant IS NOT FALSE` drops only reviews the QC pass flagged as off-topic
# (NULL = not yet checked, so nothing disappears until QC judges it), keeping the
# whole dashboard, sentiment, sources, trends, compare, to relevant reviews.
_BRAND = "(%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)"
_RELEVANT = "relevant IS NOT FALSE"
_WHERE = _BRAND + " AND " + _RELEVANT

# `created_at` as a real date, or NULL when the stored string is not ISO-ish
# (Google sometimes hands back "6 years ago"). Full-date strings keep their day;
# year-month strings are pinned to the 1st, which is what the Python parser
# effectively did too.
_CREATED_DATE = """
(CASE
   WHEN created_at ~ '^\\s*\\d{4}-\\d{2}-\\d{2}'
     THEN substring(created_at from '^\\s*(\\d{4}-\\d{2}-\\d{2})')
   WHEN created_at ~ '^\\s*\\d{4}-\\d{2}'
     THEN substring(created_at from '^\\s*(\\d{4}-\\d{2})') || '-01'
 END)::date
"""

# Whitelisted sort orders for the reviews list. Only these keys are accepted from
# the API, so the clause can be interpolated safely (no SQL injection surface).
_SORTS = {
    "recent": "captured_at DESC",
    "oldest": "captured_at ASC",
    "rating_high": "rating DESC NULLS LAST",
    "rating_low": "rating ASC NULLS LAST",
    "negative": "CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END, captured_at DESC",
    "positive": "CASE sentiment WHEN 'positive' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END, captured_at DESC",
    # Group by platform (source), then by sub-platform (the host of the URL, e.g.
    # which Mastodon instance or which website a "web" hit came from), then most
    # negative first so the worst of each surfaces. host = the bit between "://"
    # and the next "/".
    "platform": (
        "source ASC, "
        "split_part(split_part(source_url, '://', 2), '/', 1) ASC, "
        "CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END, captured_at DESC"
    ),
}


def _connect():
    return db.connect()


def _p(brand: str | None, **extra) -> dict:
    """Query parameters with the brand escaped for ILIKE."""
    return {"brand": db.like_param(brand), **extra}


def _empty_stats(brand: str | None, setup_required: str | None = None) -> dict:
    data = {
        "brand": brand,
        "brands": [],
        "total": 0,
        "flagged_offtopic": 0,
        "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
        "by_source": [],
        "by_category": [],
        "by_brand": [],
        "avg_rating": None,
        "recent_negative": [],
    }
    if setup_required:
        data["setup_required"] = setup_required
    return data


def _cols(cur) -> list[str]:
    cols = []
    for c in cur.description:
        name = getattr(c, "name", None)
        cols.append((name if name is not None else c[0]).lower())
    return cols


def get_stats(brand: str | None = None) -> dict:
    b = _p(brand)
    try:
        return _get_stats(brand, b)
    except db.DatabaseConfigError:
        return _empty_stats(brand, "database")


def _get_stats(brand: str | None, b: dict) -> dict:
    with db.connection() as conn:
        cur = conn.cursor()

        # Totals, off-topic count, sentiment split and average rating in ONE pass
        # over the brand's rows instead of four separate scans.
        cur.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE {_RELEVANT})                      AS total,
                COUNT(*) FILTER (WHERE relevant = FALSE)                 AS flagged_offtopic,
                COUNT(*) FILTER (WHERE {_RELEVANT} AND sentiment = 'positive') AS positive,
                COUNT(*) FILTER (WHERE {_RELEVANT} AND sentiment = 'neutral')  AS neutral,
                COUNT(*) FILTER (WHERE {_RELEVANT} AND sentiment = 'negative') AS negative,
                AVG(rating) FILTER (WHERE {_RELEVANT} AND rating IS NOT NULL)  AS avg_rating
            FROM MARTS.REVIEWS
            WHERE {_BRAND}
            """,
            b,
        )
        total, flagged_offtopic, pos, neu, neg, ar = cur.fetchone()
        sentiment = {"positive": pos or 0, "neutral": neu or 0, "negative": neg or 0}
        avg_rating = round(float(ar), 2) if ar is not None else None

        cur.execute(
            f"SELECT source, COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE} GROUP BY source ORDER BY 2 DESC",
            b,
        )
        by_source = [{"source": s, "count": c} for s, c in cur.fetchall()]
        from ..sources import group_by_category

        by_category = group_by_category(by_source)

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
        cols = _cols(cur)
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
            "total": total or 0,
            "flagged_offtopic": flagged_offtopic or 0,
            "sentiment": sentiment,
            "by_source": by_source,
            "by_category": by_category,
            "by_brand": [],
            "avg_rating": avg_rating,
            "recent_negative": recent_negative,
        }


def health_score(sentiment: dict, avg_rating: float | None) -> dict:
    """0-100 brand health from sentiment mix blended with rating (shared by the
    report and the compare view)."""
    pos, neu, neg = sentiment.get("positive", 0), sentiment.get("neutral", 0), sentiment.get("negative", 0)
    n = pos + neu + neg
    if not n:
        return {"score": None, "label": "No data"}
    sent_score = 100 * (pos + 0.5 * neu) / n
    score = round(0.6 * sent_score + 0.4 * (100 * avg_rating / 5)) if avg_rating is not None else round(sent_score)
    label = (
        "Excellent" if score >= 80 else "Good" if score >= 65
        else "Mixed" if score >= 45 else "At risk"
    )
    return {"score": score, "label": label}


def _brand_block(cur, brand: str) -> dict:
    """One brand's slice for the compare view: count, sentiment, rating, sources."""
    b = _p(brand)

    cur.execute(
        f"""
        SELECT
            COUNT(*)                                            AS total,
            COUNT(*) FILTER (WHERE sentiment = 'positive')       AS positive,
            COUNT(*) FILTER (WHERE sentiment = 'neutral')        AS neutral,
            COUNT(*) FILTER (WHERE sentiment = 'negative')       AS negative,
            AVG(rating) FILTER (WHERE rating IS NOT NULL)        AS avg_rating
        FROM MARTS.REVIEWS
        WHERE {_WHERE}
        """,
        b,
    )
    total, pos, neu, neg, ar = cur.fetchone()
    sentiment = {"positive": pos or 0, "neutral": neu or 0, "negative": neg or 0}
    avg_rating = round(float(ar), 2) if ar is not None else None

    cur.execute(
        f"SELECT source, COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE} GROUP BY source ORDER BY 2 DESC",
        b,
    )
    by_source = [{"source": s, "count": c} for s, c in cur.fetchall()]

    return {
        "brand": brand,
        "total": total or 0,
        "sentiment": sentiment,
        "avg_rating": avg_rating,
        "by_source": by_source,
        "health": health_score(sentiment, avg_rating),
    }


def compare_stats(a: str | None = None, b: str | None = None) -> dict:
    """Two brands side by side, using ONLY the brands explicitly requested.

    We intentionally do not auto-fill from the database. The brand list is
    per-browser (localStorage), so an unspecified side stays empty rather than
    leaking whatever brands happen to be stored.
    """
    try:
        with db.connection() as conn:
            cur = conn.cursor()
            left = _brand_block(cur, a) if a else None
            right = _brand_block(cur, b) if b else None
            cur.close()
            return {"brands": [], "a": left, "b": right}
    except db.DatabaseConfigError:
        return {"brands": [], "a": None, "b": None, "setup_required": "database"}


def _parse_dt(s: str | None):
    """Parse a review's created_at (mixed ISO shapes) to a UTC-aware datetime,
    else None. Naive timestamps are assumed UTC, so aware and naive values stay
    comparable (some sources include an offset, some do not).

    Kept for callers outside the dashboard queries; the trend and alert
    aggregations now do their date handling in SQL (see `_CREATED_DATE`).
    """
    if not s:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001 — unparseable/relative dates are just skipped
        return None


def get_trend(brand: str | None = None, bucket: str = "month") -> dict:
    """Sentiment mix over time, bucketed by the review's own post date.

    Uses created_at (when the review was written), not captured_at (when we
    scraped it), so the trend reflects real history. Reviews without a parseable
    date are excluded. Buckets with no reviews are omitted.
    """
    fmt = "YYYY" if bucket == "year" else "YYYY-MM"
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH dated AS (
                        SELECT sentiment, {_CREATED_DATE} AS d
                        FROM marts.reviews
                        WHERE {_WHERE} AND created_at IS NOT NULL
                    )
                    SELECT to_char(d, %(fmt)s) AS period,
                           COUNT(*) FILTER (WHERE sentiment = 'positive') AS positive,
                           COUNT(*) FILTER (WHERE sentiment = 'neutral')  AS neutral,
                           COUNT(*) FILTER (WHERE sentiment = 'negative') AS negative,
                           COUNT(*) AS total
                    FROM dated
                    WHERE d IS NOT NULL
                    GROUP BY 1
                    ORDER BY 1
                    """,
                    _p(brand, fmt=fmt),
                )
                rows = cur.fetchall()
    except db.DatabaseConfigError:
        return {"brand": brand, "bucket": bucket, "buckets": [], "setup_required": "database"}

    buckets = [
        {
            "period": period,
            "positive": pos,
            "neutral": neu,
            "negative": neg,
            # `total` counts every sentiment value including NULL/unknown, while
            # the three buckets only count the known ones. Keep the shape the UI
            # expects (total == sum of the three) rather than the raw count.
            "total": pos + neu + neg,
        }
        for period, pos, neu, neg, total in rows
    ]
    return {"brand": brand, "bucket": bucket, "buckets": buckets}


def get_sentiment_alert(brand: str | None = None, recent_n: int = 30) -> dict:
    """Flag whether sentiment is dropping: compare the negative rate of the most
    recent reviews (by post date) against the older baseline. Deterministic and
    in-app, no external notifications.
    """
    recent_n = max(1, min(int(recent_n), 500))
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH dated AS (
                        SELECT sentiment, {_CREATED_DATE} AS d, captured_at
                        FROM marts.reviews
                        WHERE {_WHERE} AND created_at IS NOT NULL
                    ),
                    ranked AS (
                        SELECT sentiment,
                               row_number() OVER (ORDER BY d DESC, captured_at DESC) AS rn
                        FROM dated
                        WHERE d IS NOT NULL
                    )
                    SELECT
                        COUNT(*) FILTER (WHERE rn <= %(n)s)                                AS recent_total,
                        COUNT(*) FILTER (WHERE rn <= %(n)s AND sentiment = 'negative')     AS recent_neg,
                        COUNT(*) FILTER (WHERE rn >  %(n)s)                                AS base_total,
                        COUNT(*) FILTER (WHERE rn >  %(n)s AND sentiment = 'negative')     AS base_neg
                    FROM ranked
                    """,
                    _p(brand, n=recent_n),
                )
                recent_total, recent_neg, base_total, base_neg = cur.fetchone()
    except db.DatabaseConfigError:
        return {
            "status": "setup_required",
            "message": "Database is not configured yet.",
            "setup_required": "database",
        }

    recent_total = recent_total or 0
    base_total = base_total or 0
    # Same bar as before: enough recent reviews to fill the window, plus a
    # baseline of at least 10 to compare against.
    if recent_total < recent_n or base_total < 10:
        return {"status": "insufficient", "message": "Not enough dated reviews yet to spot a trend."}

    r = round(100 * (recent_neg or 0) / recent_total)
    b = round(100 * (base_neg or 0) / base_total)
    delta = r - b
    if delta >= 15:
        status, msg = "drop", f"Negative reviews are up sharply lately ({b}% to {r}%)."
    elif delta >= 7:
        status, msg = "watch", f"Negative reviews are ticking up ({b}% to {r}%)."
    else:
        status, msg = "ok", f"Sentiment is steady (recent negatives {r}%, baseline {b}%)."
    return {"status": status, "message": msg, "recent_negative_pct": r,
            "baseline_negative_pct": b, "delta": delta, "recent_n": recent_total}


def get_reviews(
    brand: str | None = None, sort: str = "recent", limit: int = 40, source: str | None = None
) -> dict:
    """Reviews for the dashboard list, ordered by a whitelisted sort key and
    optionally filtered to one platform (source)."""
    order = _SORTS.get(sort, _SORTS["recent"])
    limit = max(1, min(int(limit), 200))
    src = source or None
    try:
        return _get_reviews(brand, sort, order, limit, src)
    except db.DatabaseConfigError:
        return {
            "brand": brand,
            "sort": sort if sort in _SORTS else "recent",
            "source": src,
            "reviews": [],
            "setup_required": "database",
        }


def _get_reviews(brand, sort, order, limit, src) -> dict:
    with db.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT text, source, source_url, author, rating, brand, sentiment, captured_at
            FROM MARTS.REVIEWS
            WHERE {_WHERE} AND (%(source)s::text IS NULL OR source = %(source)s::text)
            ORDER BY {order}
            LIMIT %(limit)s
            """,
            _p(brand, source=src, limit=limit),
        )
        cols = _cols(cur)
        reviews = []
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            t = r.pop("text", "") or ""
            r["excerpt"] = (t[:240] + "…") if len(t) > 240 else t
            ca = r.get("captured_at")
            r["captured_at"] = ca.isoformat() if hasattr(ca, "isoformat") else (str(ca) if ca else None)
            reviews.append(r)
        cur.close()
        return {"brand": brand, "sort": sort if sort in _SORTS else "recent",
                "source": src, "reviews": reviews}
