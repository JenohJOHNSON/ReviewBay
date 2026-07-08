"""Normalize Airbyte Cloud staging rows into ReviewBay raw reviews.

Airbyte Cloud owns the Apify-backed extraction in the cloud plan. Its Postgres
destination writes connector rows into a staging schema; this module maps those
rows into the existing raw.reviews_raw contract so the rest of ReviewBay stays
unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from typing import Any

import yaml
from psycopg import sql
from psycopg.rows import dict_row

from .. import db
from ..connectors.apify_source import (
    _facebook_map,
    _gmaps_map,
    _instagram_map,
    _reddit_map,
    _tripadvisor_map,
    _yelp_map,
)
from ..connectors.google_search import GoogleSearchConnector
from ..ingestion import loader
from ..models import NormalizedReview

log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("AIRBYTE_SOURCE_MAP", "/app/config/airbyte_sources.yml")
IMPORT_LIMIT = int(os.environ.get("AIRBYTE_IMPORT_LIMIT", "500"))
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _as_obj(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return the source payload, hiding Airbyte metadata columns."""
    legacy = _as_obj(row.get("_airbyte_data"))
    if legacy:
        return legacy

    clean = {k: v for k, v in row.items() if not k.startswith("_airbyte_")}
    for key in ("data", "item", "record", "payload", "json"):
        nested = _as_obj(clean.get(key))
        if nested:
            return nested
    return clean


def _normalised_map(brand: str, source: str, item: dict[str, Any]) -> NormalizedReview | None:
    text = item.get("text") or item.get("review") or item.get("body") or item.get("description")
    url = item.get("source_url") or item.get("url") or item.get("reviewUrl") or item.get("link")
    if not text or not url:
        return None
    return NormalizedReview(
        brand=brand,
        source=source,
        source_url=str(url),
        text=str(text),
        author=item.get("author") or item.get("name") or item.get("userName"),
        rating=_as_float(item.get("rating") or item.get("stars")),
        created_at=item.get("created_at") or item.get("createdAt") or item.get("date"),
        extra={k: v for k, v in item.items() if k not in {"text", "source_url", "url", "author", "rating"}},
    )


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def map_airbyte_row(
    brand: str,
    source: str,
    mapper: str,
    row: dict[str, Any],
) -> list[NormalizedReview]:
    """Map one Airbyte row to zero or more normalized reviews.

    The `web` mapper handles both a single organic result and a full Google
    Search actor SERP row containing organicResults[].
    """
    item = _payload(row)
    mapper = (mapper or source or "normalized").strip().lower()
    mapped: list[NormalizedReview | None]

    if mapper == "google_maps":
        mapped = [_gmaps_map(brand, item)]
    elif mapper == "yelp":
        mapped = [_yelp_map(brand, item)]
    elif mapper == "reddit":
        mapped = [_reddit_map(brand, item)]
    elif mapper == "tripadvisor":
        mapped = [_tripadvisor_map(brand, item)]
    elif mapper == "instagram":
        mapped = [_instagram_map(brand, item)]
    elif mapper == "facebook":
        mapped = [_facebook_map(brand, item)]
    elif mapper == "web":
        results = item.get("organicResults")
        if isinstance(results, list):
            mapped = [GoogleSearchConnector._map_result(brand, r) for r in results if isinstance(r, dict)]
        else:
            mapped = [GoogleSearchConnector._map_result(brand, item)]
    elif mapper == "normalized":
        mapped = [_normalised_map(brand, source, item)]
    else:
        raise ValueError(f"unknown Airbyte mapper: {mapper}")

    return [r for r in mapped if r and r.source_url and r.text.strip()]


def _load_config(path: str = CONFIG_PATH) -> list[dict[str, Any]]:
    if not path or not os.path.exists(path):
        log.info("Airbyte source map %s not found; skipping Airbyte import", path)
        return []
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    sources = doc if isinstance(doc, list) else doc.get("sources", [])
    return [s for s in sources if isinstance(s, dict)]


def _split_table(name: str) -> tuple[str, str]:
    parts = (name or "").split(".")
    if len(parts) == 1:
        schema, table = "airbyte", parts[0]
    elif len(parts) == 2:
        schema, table = parts
    else:
        raise ValueError(f"invalid table name: {name!r}")
    if not (_IDENT.match(schema) and _IDENT.match(table)):
        raise ValueError(f"unsafe table name: {name!r}")
    return schema, table


def _rows_for_table(conn, schema: str, table: str, limit: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
        reg = cur.fetchone()
        reg_value = next(iter(reg.values())) if isinstance(reg, dict) else reg[0]
        if reg_value is None:
            log.warning("Airbyte staging table %s.%s does not exist; skipping", schema, table)
            return []

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        cols = {
            (r.get("column_name") if isinstance(r, dict) else r[0])
            for r in cur.fetchall()
        }
        order = (
            sql.SQL(" ORDER BY {} DESC").format(sql.Identifier("_airbyte_extracted_at"))
            if "_airbyte_extracted_at" in cols
            else sql.SQL("")
        )
        cur.execute(
            sql.SQL("SELECT * FROM {}{} LIMIT %s").format(sql.Identifier(schema, table), order),
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def normalize_sources(sources: Iterable[dict[str, Any]], limit: int = IMPORT_LIMIT) -> int:
    conn = db.connect(row_factory=dict_row)
    total = 0
    try:
        for spec in sources:
            table_name = spec.get("table")
            brand = spec.get("brand")
            source = spec.get("source")
            mapper = spec.get("mapper", source)
            if not table_name or not brand or not source:
                log.warning("invalid Airbyte source spec, expected table/brand/source: %s", spec)
                continue

            schema, table = _split_table(str(table_name))
            rows = _rows_for_table(conn, schema, table, limit)
            reviews: list[NormalizedReview] = []
            for row in rows:
                try:
                    reviews.extend(map_airbyte_row(str(brand), str(source), str(mapper), row))
                except Exception:  # noqa: BLE001
                    log.exception("could not map Airbyte row table=%s mapper=%s", table_name, mapper)
            total += loader.load(reviews)
            log.info("Airbyte table=%s mapped %d review(s)", table_name, len(reviews))
    finally:
        conn.close()
    return total


def normalize_from_config(path: str = CONFIG_PATH, limit: int = IMPORT_LIMIT) -> int:
    return normalize_sources(_load_config(path), limit=limit)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    normalize_from_config()


if __name__ == "__main__":
    main()
