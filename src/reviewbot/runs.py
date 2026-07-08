"""Scrape run history: record and read one row per collection run.

A collection run (run_brand) records how it went into marts.scrape_runs, so the
dashboard can show a run history (date, brand, sources, reviews found, status,
duration). Recording is best-effort and never blocks or fails a scrape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _connect():
    from .db import connect

    return connect()


def record_run(
    brand: str,
    sources: list[str] | None,
    reviews_found: int,
    status: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Insert one run row. Best-effort: log and swallow on any failure."""
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO marts.scrape_runs
                        (brand, sources, reviews_found, status, duration_ms, started_at, finished_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        brand,
                        ", ".join(sources or []),
                        int(reviews_found or 0),
                        status,
                        duration_ms,
                        started_at,
                        finished_at,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — observability must never break a scrape
        log.exception("could not record scrape run for brand=%s", brand)


def list_runs(brand: str | None = None, limit: int = 50) -> list[dict]:
    """Recent runs, newest first, optionally filtered to one brand."""
    limit = max(1, min(int(limit), 200))
    where = "WHERE brand ILIKE %(brand)s" if brand else ""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT brand, sources, reviews_found, status, duration_ms, started_at
                FROM marts.scrape_runs
                {where}
                ORDER BY started_at DESC
                LIMIT %(limit)s
                """,
                {"brand": brand, "limit": limit},
            )
            cols = [c[0].lower() for c in cur.description]
            out = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                sa = r.get("started_at")
                r["started_at"] = sa.isoformat() if hasattr(sa, "isoformat") else (str(sa) if sa else None)
                out.append(r)
            return out
    finally:
        conn.close()


def now() -> datetime:
    return datetime.now(timezone.utc)
