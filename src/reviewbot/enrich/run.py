"""Enrich raw reviews into marts: add embeddings + sentiment in Python.

It reads reviews from raw.reviews_raw that aren't in marts.reviews yet, computes
each one's embedding and sentiment locally (see embeddings.py), and upserts them
into marts.reviews with the vector stored in a pgvector vector(768) column.

Run standalone (`python -m reviewbot.enrich.run`), from the ingestion loop, or
as an Airflow task.
"""

from __future__ import annotations

import json
import logging
import os

from psycopg.rows import dict_row

from .. import db
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
FROM raw.reviews_raw r
LEFT JOIN marts.reviews m ON m.id = r.id
WHERE m.id IS NULL OR m.text <> r.text
LIMIT %(batch)s
"""

_UPSERT = """
INSERT INTO marts.reviews (
    id, brand, source, source_url, author, rating, text, sentiment,
    created_at, captured_at, embedding
) VALUES (
    %(id)s, %(brand)s, %(source)s, %(source_url)s, %(author)s, %(rating)s,
    %(text)s, %(sentiment)s, %(created_at)s, %(captured_at)s, (%(embedding)s)::vector
)
ON CONFLICT (id) DO UPDATE SET
    text = EXCLUDED.text,
    rating = EXCLUDED.rating,
    sentiment = EXCLUDED.sentiment,
    embedding = EXCLUDED.embedding,
    captured_at = EXCLUDED.captured_at
"""


def _connect():
    return db.connect(row_factory=dict_row)


def enrich() -> int:
    """Embed + score pending reviews into marts. Returns rows written.

    Loops in batches until raw is fully caught up.
    """
    conn = _connect()
    total = 0
    new_negatives: list[dict] = []
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute(_SELECT_PENDING, {"batch": BATCH})
                rows = [dict(r) for r in cur.fetchall()]
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
                cur.executemany(_UPSERT, params)
            conn.commit()
            total += len(params)
            log.info("enriched %d reviews (running total %d)", len(params), total)

            if len(rows) < BATCH:
                break
    finally:
        conn.close()

    log.info("enrichment complete: %d reviews into marts.reviews", total)

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
