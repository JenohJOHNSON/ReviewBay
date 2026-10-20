"""Scrape run history: record and read one row per collection run.

A collection run records how it went into `marts.scrape_runs`, so the dashboard
can show a run history (date, brand, sources, reviews found, status, duration)
and so the collection-health panel can show which engines did the work.

Recording is best-effort and never blocks or fails a scrape.

The engine columns (`engines`, `by_engine`, `target`, `floor`, `ai_calls`,
`ai_cost_usd`) arrive with `postgres/002_scraping_engines.sql`. They are written
only when they exist: a database that has not been migrated keeps working with
the original column set, and `record_run` falls back to it automatically rather
than failing the run over observability.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from . import db

log = logging.getLogger(__name__)

_EXTENDED_COLUMNS = ("engines", "by_engine", "target", "floor", "ai_calls", "ai_cost_usd")
#: None = not probed yet. A positive answer is cached for good: columns are
#: never removed. A negative one is re-checked every _REPROBE_SECONDS, because
#: the migration is usually run against a database the API is ALREADY serving,
#: and caching "no" for the life of the process meant the dashboard kept saying
#: "run the migration" after it had been run, until someone restarted the API.
_HAS_EXTENDED: bool | None = None
_PROBED_AT = 0.0
_REPROBE_SECONDS = 60.0
_PROBE_LOCK = threading.Lock()


def _extended_available() -> bool:
    global _HAS_EXTENDED, _PROBED_AT

    def fresh() -> bool:
        return _HAS_EXTENDED is True or (
            _HAS_EXTENDED is False and time.monotonic() - _PROBED_AT < _REPROBE_SECONDS
        )

    if fresh():
        return bool(_HAS_EXTENDED)
    with _PROBE_LOCK:
        if fresh():
            return bool(_HAS_EXTENDED)
        _PROBED_AT = time.monotonic()
        try:
            with db.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema = 'marts' AND table_name = 'scrape_runs' "
                        "AND column_name = ANY(%s)",
                        (list(_EXTENDED_COLUMNS),),
                    )
                    found = cur.fetchone()[0]
            _HAS_EXTENDED = found == len(_EXTENDED_COLUMNS)
            if not _HAS_EXTENDED:
                log.info(
                    "runs: marts.scrape_runs is missing the engine columns "
                    "(%d/%d) — run postgres/002_scraping_engines.sql to record "
                    "per-engine collection metrics",
                    found, len(_EXTENDED_COLUMNS),
                )
        except Exception:  # noqa: BLE001
            log.debug("runs: could not probe scrape_runs columns", exc_info=True)
            _HAS_EXTENDED = False
        return _HAS_EXTENDED


def reset_schema_probe() -> None:
    """Forget the cached column probe (tests, after a migration)."""
    global _HAS_EXTENDED
    with _PROBE_LOCK:
        _HAS_EXTENDED = None


_INSERT_BASIC = """
INSERT INTO marts.scrape_runs
    (brand, sources, reviews_found, status, duration_ms, started_at, finished_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_EXTENDED = """
INSERT INTO marts.scrape_runs
    (brand, sources, reviews_found, status, duration_ms, started_at, finished_at,
     engines, by_engine, target, floor, ai_calls, ai_cost_usd)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
"""


def record_run(
    brand: str,
    sources: list[str] | None,
    reviews_found: int,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    *,
    engines: list[str] | None = None,
    by_engine: dict | None = None,
    target: int | None = None,
    floor: int | None = None,
    ai_calls: int | None = None,
    ai_cost_usd: float | None = None,
) -> None:
    """Insert one run row. Best-effort: log and swallow on any failure."""
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    basic = (
        brand,
        ", ".join(sources or []),
        int(reviews_found or 0),
        status,
        duration_ms,
        started_at,
        finished_at,
    )
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                if _extended_available():
                    cur.execute(
                        _INSERT_EXTENDED,
                        basic
                        + (
                            ", ".join(engines or []),
                            json.dumps(by_engine or {}),
                            int(target) if target is not None else None,
                            int(floor) if floor is not None else None,
                            int(ai_calls or 0),
                            float(ai_cost_usd or 0.0),
                        ),
                    )
                else:
                    cur.execute(_INSERT_BASIC, basic)
            conn.commit()
    except Exception:  # noqa: BLE001 — observability must never break a scrape
        log.exception("could not record scrape run for brand=%s", brand)


def list_runs(brand: str | None = None, limit: int = 50) -> list[dict]:
    """Recent runs, newest first, optionally filtered to one brand."""
    limit = max(1, min(int(limit), 200))
    where = "WHERE brand ILIKE %(brand)s" if brand else ""
    extended = _extended_available()
    columns = "brand, sources, reviews_found, status, duration_ms, started_at"
    if extended:
        columns += ", engines, target, floor, ai_calls, ai_cost_usd"
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {columns}
                FROM marts.scrape_runs
                {where}
                ORDER BY started_at DESC
                LIMIT %(limit)s
                """,
                {"brand": db.like_param(brand), "limit": limit},
            )
            cols = [c[0].lower() for c in cur.description]
            out = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                sa = r.get("started_at")
                r["started_at"] = (
                    sa.isoformat() if hasattr(sa, "isoformat") else (str(sa) if sa else None)
                )
                out.append(r)
            return out


def get_run_detail(brand: str | None = None, limit: int = 1) -> list[dict]:
    """Recent runs including the full per-engine metrics blob.

    Separate from `list_runs` because `by_engine` is large and the history table
    does not need it on every row.
    """
    if not _extended_available():
        return []
    limit = max(1, min(int(limit), 20))
    where = "WHERE brand ILIKE %(brand)s" if brand else ""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT brand, status, reviews_found, duration_ms, started_at,
                       engines, by_engine, target, floor, ai_calls, ai_cost_usd
                FROM marts.scrape_runs
                {where}
                ORDER BY started_at DESC
                LIMIT %(limit)s
                """,
                {"brand": db.like_param(brand), "limit": limit},
            )
            cols = [c[0].lower() for c in cur.description]
            out = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                sa = r.get("started_at")
                r["started_at"] = (
                    sa.isoformat() if hasattr(sa, "isoformat") else (str(sa) if sa else None)
                )
                out.append(r)
            return out


def now() -> datetime:
    return datetime.now(timezone.utc)
