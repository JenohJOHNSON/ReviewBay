"""Loads normalized reviews into Snowflake RAW.REVIEWS_RAW via MERGE.

MERGE (not INSERT) means re-scraping the same review is idempotent — the stable
id dedupes on the way in. Reads all connection settings from SNOWFLAKE_* env
vars so nothing is hard-coded.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable

from ..models import NormalizedReview

log = logging.getLogger(__name__)

_MERGE = """
MERGE INTO RAW.REVIEWS_RAW AS tgt
USING (
    SELECT
        %(id)s          AS id,
        %(brand)s       AS brand,
        %(source)s      AS source,
        %(source_url)s  AS source_url,
        %(author)s      AS author,
        %(rating)s      AS rating,
        %(text)s        AS text,
        %(created_at)s  AS created_at,
        %(captured_at)s AS captured_at,
        PARSE_JSON(%(extra)s) AS extra
) AS src
ON tgt.id = src.id
WHEN MATCHED THEN UPDATE SET
    captured_at = src.captured_at,
    rating      = src.rating,
    extra       = src.extra
WHEN NOT MATCHED THEN INSERT
    (id, brand, source, source_url, author, rating, text, created_at, captured_at, extra)
    VALUES
    (src.id, src.brand, src.source, src.source_url, src.author, src.rating,
     src.text, src.created_at, src.captured_at, src.extra);
"""


def _connect():
    import snowflake.connector  # type: ignore

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD"),
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "REVIEWBOT"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "RAW"),
    )


def load(reviews: Iterable[NormalizedReview]) -> int:
    """Upsert reviews. Returns the count written."""
    rows = [r.to_row() for r in reviews]
    if not rows:
        return 0

    for row in rows:
        row["extra"] = json.dumps(row.get("extra") or {})

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(_MERGE, rows)
        conn.commit()
        log.info("merged %d reviews into RAW.REVIEWS_RAW", len(rows))
        return len(rows)
    finally:
        conn.close()
