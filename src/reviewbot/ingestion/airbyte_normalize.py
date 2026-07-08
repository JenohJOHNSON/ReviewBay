"""Normalize Airbyte-landed Apify datasets into raw.reviews_raw.

Airbyte Cloud syncs each "Apify Dataset" source into its own table in the Neon
`airbyte` schema. This reshapes those rows into our NormalizedReview contract
using the SAME per-source mappers the direct Apify connectors use
(connectors/apify_source.py), then upserts them into raw.reviews_raw, so
everything downstream (enrich -> marts -> chat) stays source-agnostic.

`config/airbyte_sources.yml` (or $AIRBYTE_SOURCE_MAP) maps each staging table to
{brand, source}. App Store and Google Play stay on the free Python connectors and
are not part of this path.
"""

from __future__ import annotations

import logging
import os
import re

import yaml

from ..connectors.apify_source import (
    FACEBOOK,
    GOOGLE_MAPS,
    INSTAGRAM,
    REDDIT,
    TRIPADVISOR,
    YELP,
)
from ..db import connect
from . import loader

log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("AIRBYTE_SOURCE_MAP", "/app/config/airbyte_sources.yml")

# Reuse the exact field mappings the live Apify connectors use, so an Airbyte-fed
# row and a directly-scraped row become the identical NormalizedReview.
_MAPPERS = {
    "google_maps": GOOGLE_MAPS.map_item,
    "yelp": YELP.map_item,
    "tripadvisor": TRIPADVISOR.map_item,
    "reddit": REDDIT.map_item,
    "instagram": INSTAGRAM.map_item,
    "facebook": FACEBOOK.map_item,
}

# schema.table of safe identifier chars only (config is trusted, but validate
# before interpolating into the SELECT).
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


def _read_items(table: str) -> list[dict]:
    """Read an Airbyte staging table, returning each record as the original Apify
    item dict. Handles both Postgres-destination shapes: a `_airbyte_data` jsonb
    column, or typed columns (everything not prefixed with `_airbyte`)."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table}")  # table validated by _TABLE_RE
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()

    items: list[dict] = []
    for row in rows:
        rec = dict(zip(cols, row))
        data = rec.get("_airbyte_data")
        if isinstance(data, dict):
            items.append(data)
        else:
            items.append({k: v for k, v in rec.items() if not k.startswith("_airbyte")})
    return items


def _load_config(path: str | None = None) -> list[dict]:
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    return doc.get("sources") or []


def normalize(path: str | None = None) -> int:
    """Reshape every configured Airbyte staging table into raw.reviews_raw.

    Returns rows written. A table that has not been synced yet (does not exist) is
    logged and skipped, so this is safe to run before Airbyte is configured.
    """
    entries = _load_config(path)
    if not entries:
        log.info("airbyte normalize: no sources configured; nothing to do")
        return 0

    total = 0
    for entry in entries:
        table = entry.get("table")
        brand = entry.get("brand")
        source = entry.get("source")
        mapper = _MAPPERS.get(source)
        if not (table and brand and mapper) or not _TABLE_RE.match(table or ""):
            log.warning("airbyte normalize: skipping invalid entry %r", entry)
            continue

        try:
            items = _read_items(table)
        except Exception as ex:  # noqa: BLE001 (usually: table not synced yet)
            log.warning("airbyte normalize: cannot read %s (%s); skipping", table, ex)
            continue

        reviews = []
        for item in items:
            try:
                review = mapper(brand, item)
            except Exception:  # noqa: BLE001
                log.exception("airbyte normalize: bad %s item in %s", source, table)
                review = None
            if review and review.source_url and (review.text or "").strip():
                reviews.append(review)

        written = loader.load(reviews)
        log.info("airbyte normalize: %s -> %d rows (brand=%s)", table, written, brand)
        total += written
    return total
