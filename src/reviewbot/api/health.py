"""Collection health: what the scraping layer actually did, for the dashboard.

The dashboard could already show how many reviews exist and how they feel. What
it could not show is whether *collection* is working — which sources produced
data, which came back empty, how long a run took, when the last successful one
was, and what the AI extraction cost. That is the difference between "we have
190 reviews" and "we have 190 reviews, Trustpilot has been failing for a week,
and the last run spent 14 cents".

Everything here is read from data the pipeline already records
(`marts.scrape_runs`, `marts.reviews`, `marts.engine_stats`), so the panel costs
three cheap queries and no scraping. Each section degrades independently: a
database without `postgres/002_scraping_engines.sql` applied simply returns the
sections it can, with `migration_required` set, rather than erroring.
"""

from __future__ import annotations

import logging

from .. import db, runs
from ..sources import category_for, label_for

log = logging.getLogger(__name__)


def _per_source(brand: str | None) -> list[dict]:
    """Per-source counts and recency from the stored reviews themselves.

    This is the ground truth: a source "works" if rows from it are in the
    database, regardless of what a run record says.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source,
                       COUNT(*)                                           AS total,
                       COUNT(*) FILTER (WHERE relevant IS NOT FALSE)      AS relevant,
                       COUNT(*) FILTER (WHERE relevant = FALSE)           AS off_topic,
                       MAX(captured_at)                                   AS last_captured,
                       AVG(rating) FILTER (WHERE rating IS NOT NULL)      AS avg_rating,
                       COUNT(*) FILTER (WHERE rating IS NOT NULL)         AS with_rating
                FROM marts.reviews
                WHERE (%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)
                GROUP BY source
                ORDER BY 2 DESC
                """,
                {"brand": db.like_param(brand)},
            )
            rows = cur.fetchall()

    out = []
    for source, total, relevant, off_topic, last_captured, avg_rating, with_rating in rows:
        out.append(
            {
                "source": source,
                "label": label_for(source),
                "category": category_for(source),
                "total": total or 0,
                "relevant": relevant or 0,
                "off_topic": off_topic or 0,
                "with_rating": with_rating or 0,
                "avg_rating": round(float(avg_rating), 2) if avg_rating is not None else None,
                "last_captured": (
                    last_captured.isoformat() if hasattr(last_captured, "isoformat")
                    else (str(last_captured) if last_captured else None)
                ),
                # A source whose rows the QC pass mostly rejected is a source
                # that is finding the wrong brand, which is worth surfacing.
                "precision": (
                    round((relevant or 0) / total, 3) if total else None
                ),
            }
        )
    return out


def _engine_rollup(brand: str | None) -> dict:
    """Per-engine totals from the most recent runs that recorded them."""
    detail = runs.get_run_detail(brand, limit=5)
    if not detail:
        return {"available": False, "engines": [], "runs": []}

    engines: dict[str, dict] = {}
    for run in detail:
        blob = run.get("by_engine") or {}
        for name, stats in (blob.get("engines") or {}).items():
            agg = engines.setdefault(
                name,
                {
                    "engine": name, "attempted": 0, "succeeded": 0, "failed": 0,
                    "items": 0, "valid": 0, "duplicates": 0, "rejected": 0,
                    "duration_ms": 0, "ai_calls": 0, "estimated_cost_usd": 0.0,
                    "skipped": {},
                },
            )
            for key in ("attempted", "succeeded", "failed", "items", "valid",
                        "duplicates", "rejected", "duration_ms", "ai_calls"):
                agg[key] += int(stats.get(key) or 0)
            agg["estimated_cost_usd"] += float(stats.get("estimated_cost_usd") or 0.0)
            for reason, count in (stats.get("skipped") or {}).items():
                agg["skipped"][reason] = agg["skipped"].get(reason, 0) + int(count or 0)

    for agg in engines.values():
        agg["success_rate"] = (
            round(agg["succeeded"] / agg["attempted"], 3) if agg["attempted"] else None
        )
        agg["valid_per_request"] = (
            round(agg["valid"] / agg["attempted"], 3) if agg["attempted"] else None
        )
        agg["estimated_cost_usd"] = round(agg["estimated_cost_usd"], 6)

    latest = detail[0]
    return {
        "available": True,
        "engines": sorted(engines.values(), key=lambda e: -e["valid"]),
        "latest": {
            "brand": latest.get("brand"),
            "status": latest.get("status"),
            "reviews_found": latest.get("reviews_found"),
            "duration_ms": latest.get("duration_ms"),
            "started_at": latest.get("started_at"),
            "engines": latest.get("engines"),
            "target": latest.get("target"),
            "floor": latest.get("floor"),
            "ai_calls": latest.get("ai_calls"),
            "ai_cost_usd": latest.get("ai_cost_usd"),
        },
        "runs": [
            {
                "brand": r.get("brand"),
                "status": r.get("status"),
                "reviews_found": r.get("reviews_found"),
                "duration_ms": r.get("duration_ms"),
                "started_at": r.get("started_at"),
                "engines": r.get("engines"),
                "ai_cost_usd": r.get("ai_cost_usd"),
            }
            for r in detail
        ],
    }


def _last_successful(brand: str | None) -> dict | None:
    """The most recent run that actually produced records."""
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT brand, reviews_found, duration_ms, started_at, sources
                    FROM marts.scrape_runs
                    WHERE status = 'ok' AND reviews_found > 0
                      AND (%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    {"brand": db.like_param(brand)},
                )
                row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    name, found, duration, started, sources = row
    return {
        "brand": name,
        "reviews_found": found,
        "duration_ms": duration,
        "started_at": started.isoformat() if hasattr(started, "isoformat") else str(started),
        "sources": sources,
    }


def collection_health(brand: str | None = None) -> dict:
    """The collection-health payload: sources, engines, recency, connectors."""
    from ..sources import connector_statuses

    try:
        per_source = _per_source(brand)
    except db.DatabaseConfigError:
        return {
            "brand": brand,
            "setup_required": "database",
            "sources": [],
            "engines": {"available": False, "engines": [], "runs": []},
            "connectors": connector_statuses(),
        }
    except Exception:  # noqa: BLE001
        log.exception("collection health: per-source query failed")
        per_source = []

    try:
        engines = _engine_rollup(brand)
    except Exception:  # noqa: BLE001
        log.exception("collection health: engine rollup failed")
        engines = {"available": False, "engines": [], "runs": []}

    totals = {
        "reviews": sum(s["total"] for s in per_source),
        "relevant": sum(s["relevant"] for s in per_source),
        "off_topic": sum(s["off_topic"] for s in per_source),
        "sources_with_data": sum(1 for s in per_source if s["total"]),
    }

    payload = {
        "brand": brand,
        "totals": totals,
        "sources": per_source,
        "engines": engines,
        "last_successful": _last_successful(brand),
        # Which connectors are configured at all, so an empty source is
        # distinguishable from an unconfigured one.
        "connectors": connector_statuses(),
    }
    if not engines.get("available"):
        # Two different situations, which used to share one message: the engine
        # columns are missing (the operator has a migration to run), or they
        # exist and no run has recorded engine metrics for this brand yet (a
        # brand refreshed only by the worker, or one whose first collection is
        # still going). Telling the second operator to "run the migration" sent
        # them to re-apply a migration that had already succeeded.
        if runs._extended_available():
            payload["engines_note"] = (
                "No collection run has recorded per-engine metrics for this brand yet. "
                "They appear after its next collection."
            )
        else:
            payload["migration_required"] = (
                "Run python scripts/migrate.py to record per-engine collection metrics."
            )
    return payload
