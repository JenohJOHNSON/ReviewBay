"""Aggregations for the dashboard — all from MARTS.REVIEWS (no scraping cost).

Returns one JSON blob the dashboard page renders: totals, sentiment split,
source mix, per-brand counts, average rating, and the latest negative reviews.
Optional brand filter narrows everything except the brand list.
"""

from __future__ import annotations

from .. import db

# ::text so Postgres can infer the parameter type in the `IS NULL` branch.
# `relevant IS NOT FALSE` drops only reviews the QC pass flagged as off-topic
# (NULL = not yet checked, so nothing disappears until QC judges it), keeping the
# whole dashboard, sentiment, sources, trends, compare, to relevant reviews.
_BRAND = "(%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)"
_WHERE = _BRAND + " AND relevant IS NOT FALSE"

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
    b = {"brand": brand}
    try:
        conn = _connect()
    except db.DatabaseConfigError:
        return _empty_stats(brand, "database")
    try:
        cur = conn.cursor()

        cur.execute(f"SELECT COUNT(*) FROM MARTS.REVIEWS WHERE {_WHERE}", b)
        total = cur.fetchone()[0]

        # How many the QC pass filtered out as off-topic (shown as a note).
        cur.execute(f"SELECT COUNT(*) FROM MARTS.REVIEWS WHERE {_BRAND} AND relevant = FALSE", b)
        flagged_offtopic = cur.fetchone()[0]

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
        from ..sources import group_by_category

        by_category = group_by_category(by_source)

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
            "total": total,
            "flagged_offtopic": flagged_offtopic,
            "sentiment": sentiment,
            "by_source": by_source,
            "by_category": by_category,
            "by_brand": [],
            "avg_rating": avg_rating,
            "recent_negative": recent_negative,
        }
    finally:
        conn.close()


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
        "health": health_score(sentiment, avg_rating),
    }


def compare_stats(a: str | None = None, b: str | None = None) -> dict:
    """Two brands side by side, using ONLY the brands explicitly requested.

    We intentionally do not auto-fill from the database. The brand list is
    per-browser (localStorage), so an unspecified side stays empty rather than
    leaking whatever brands happen to be stored.
    """
    try:
        conn = _connect()
    except db.DatabaseConfigError:
        return {"brands": [], "a": None, "b": None, "setup_required": "database"}
    try:
        cur = conn.cursor()
        left = _brand_block(cur, a) if a else None
        right = _brand_block(cur, b) if b else None
        cur.close()
        return {"brands": [], "a": left, "b": right}
    finally:
        conn.close()


def _parse_dt(s: str | None):
    """Parse a review's created_at (mixed ISO shapes) to a UTC-aware datetime,
    else None. Naive timestamps are assumed UTC, so aware and naive values stay
    comparable (some sources include an offset, some do not)."""
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
    fmt = "%Y" if bucket == "year" else "%Y-%m"
    try:
        conn = _connect()
    except db.DatabaseConfigError:
        return {"brand": brand, "bucket": bucket, "buckets": [], "setup_required": "database"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT created_at, sentiment FROM marts.reviews "
                f"WHERE {_WHERE} AND created_at IS NOT NULL",
                {"brand": brand},
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    from collections import defaultdict

    buckets: dict = defaultdict(lambda: {"positive": 0, "neutral": 0, "negative": 0})
    for created, sentiment in rows:
        dt = _parse_dt(created)
        if not dt:
            continue
        key = dt.strftime(fmt)
        if sentiment in buckets[key]:
            buckets[key][sentiment] += 1
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        out.append({"period": k, **b, "total": b["positive"] + b["neutral"] + b["negative"]})
    return {"brand": brand, "bucket": bucket, "buckets": out}


def get_sentiment_alert(brand: str | None = None, recent_n: int = 30) -> dict:
    """Flag whether sentiment is dropping: compare the negative rate of the most
    recent reviews (by post date) against the older baseline. Deterministic and
    in-app, no external notifications.
    """
    try:
        conn = _connect()
    except db.DatabaseConfigError:
        return {
            "status": "setup_required",
            "message": "Database is not configured yet.",
            "setup_required": "database",
        }
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT created_at, sentiment FROM marts.reviews "
                f"WHERE {_WHERE} AND created_at IS NOT NULL",
                {"brand": brand},
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    dated = [(dt, s) for (c, s) in rows if (dt := _parse_dt(c))]
    dated.sort(key=lambda x: x[0], reverse=True)
    if len(dated) < recent_n + 10:
        return {"status": "insufficient", "message": "Not enough dated reviews yet to spot a trend."}

    def neg_pct(group):
        return round(100 * sum(1 for _, s in group if s == "negative") / len(group)) if group else 0

    recent, baseline = dated[:recent_n], dated[recent_n:]
    r, b = neg_pct(recent), neg_pct(baseline)
    delta = r - b
    if delta >= 15:
        status, msg = "drop", f"Negative reviews are up sharply lately ({b}% to {r}%)."
    elif delta >= 7:
        status, msg = "watch", f"Negative reviews are ticking up ({b}% to {r}%)."
    else:
        status, msg = "ok", f"Sentiment is steady (recent negatives {r}%, baseline {b}%)."
    return {"status": status, "message": msg, "recent_negative_pct": r,
            "baseline_negative_pct": b, "delta": delta, "recent_n": len(recent)}


def get_reviews(
    brand: str | None = None, sort: str = "recent", limit: int = 40, source: str | None = None
) -> dict:
    """Reviews for the dashboard list, ordered by a whitelisted sort key and
    optionally filtered to one platform (source)."""
    order = _SORTS.get(sort, _SORTS["recent"])
    limit = max(1, min(int(limit), 200))
    src = source or None
    try:
        conn = _connect()
    except db.DatabaseConfigError:
        return {
            "brand": brand,
            "sort": sort if sort in _SORTS else "recent",
            "source": src,
            "reviews": [],
            "setup_required": "database",
        }
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT text, source, source_url, author, rating, brand, sentiment, captured_at
            FROM MARTS.REVIEWS
            WHERE {_WHERE} AND (%(source)s::text IS NULL OR source = %(source)s::text)
            ORDER BY {order}
            LIMIT {limit}
            """,
            {"brand": brand, "source": src},
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
    finally:
        conn.close()
