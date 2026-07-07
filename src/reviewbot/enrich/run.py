"""Enrich RAW reviews into MARTS: add real embeddings + sentiment (in Python).

This replaces the old Cortex-based transform.sql. It reads reviews from
RAW.REVIEWS_RAW that aren't in MARTS.REVIEWS yet, computes each one's embedding
and sentiment locally (see embeddings.py), and MERGEs them into MARTS with the
vector stored in the VECTOR(FLOAT, 768) column. The chatbot then searches MARTS
with VECTOR_COSINE_SIMILARITY (which works on Snowflake trials).

Run standalone (`python -m reviewbot.enrich.run`), from the ingestion loop, or
as an Airflow task.
"""

from __future__ import annotations

import json
import logging
import os

from .. import embeddings

log = logging.getLogger(__name__)

# Small batch keeps peak memory low. Embedding a large batch (e.g. 200) can get
# the process OOM-killed on a memory-constrained Docker (around 4 GB). Raise this
# via env on machines with more RAM if you want faster enrichment.
BATCH = int(os.environ.get("ENRICH_BATCH", "32"))

# Pull reviews that still need enriching (new, or whose text changed).
_SELECT_PENDING = """
SELECT r.id, r.brand, r.source, r.source_url, r.author, r.rating, r.text,
       r.created_at, r.captured_at, m.id AS existing_id
FROM RAW.REVIEWS_RAW r
LEFT JOIN MARTS.REVIEWS m ON m.id = r.id
WHERE m.id IS NULL OR m.text <> r.text
LIMIT %(batch)s
"""

# Store the vector by parsing a JSON array and casting to VECTOR. This avoids any
# Cortex call — Snowflake just accepts the numbers we computed in Python.
_MERGE = """
MERGE INTO MARTS.REVIEWS AS tgt
USING (
    SELECT
        %(id)s AS id, %(brand)s AS brand, %(source)s AS source,
        %(source_url)s AS source_url, %(author)s AS author, %(rating)s AS rating,
        %(text)s AS text, %(sentiment)s AS sentiment,
        %(created_at)s AS created_at, %(captured_at)s AS captured_at,
        PARSE_JSON(%(embedding)s)::VECTOR(FLOAT, 768) AS embedding
) AS src
ON tgt.id = src.id
WHEN MATCHED THEN UPDATE SET
    text = src.text, rating = src.rating, sentiment = src.sentiment,
    embedding = src.embedding, captured_at = src.captured_at
WHEN NOT MATCHED THEN INSERT
    (id, brand, source, source_url, author, rating, text, sentiment,
     created_at, captured_at, embedding)
    VALUES
    (src.id, src.brand, src.source, src.source_url, src.author, src.rating,
     src.text, src.sentiment, src.created_at, src.captured_at, src.embedding);
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
    )


def enrich() -> int:
    """Embed + score pending reviews into MARTS. Returns rows written.

    Loops in batches until RAW is fully caught up.
    """
    conn = _connect()
    total = 0
    new_negatives: list[dict] = []
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute(_SELECT_PENDING, {"batch": BATCH})
                cols = [c[0].lower() for c in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            if not rows:
                break

            vectors = embeddings.embed([r["text"] for r in rows])
            params = []
            for row, vec in zip(rows, vectors):
                is_new = row.pop("existing_id", None) is None
                sentiment = embeddings.sentiment_bucket(row["text"])
                params.append({**row, "sentiment": sentiment, "embedding": json.dumps(vec)})
                if is_new and sentiment == "negative":
                    new_negatives.append(
                        {
                            "id": row.get("id"),
                            "brand": row.get("brand"),
                            "source": row.get("source"),
                            "source_url": row.get("source_url"),
                            "rating": row.get("rating"),
                            "text": row.get("text"),
                            "sentiment": sentiment,
                        }
                    )

            with conn.cursor() as cur:
                cur.executemany(_MERGE, params)
            conn.commit()
            total += len(params)
            log.info("enriched %d reviews (running total %d)", len(params), total)

            if len(rows) < BATCH:
                break
    finally:
        conn.close()

    log.info("enrichment complete: %d reviews into MARTS", total)

    if new_negatives:  # push alerts for brand-new negatives (non-fatal; off unless configured)
        try:
            from .. import alerts

            n = alerts.notify_new_negatives(new_negatives)
            if n:
                log.info("sent %d negative-review alert(s)", n)
        except Exception:  # noqa: BLE001
            log.exception("alerting failed (non-fatal)")

    return total


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    enrich()


if __name__ == "__main__":
    main()
