"""Enrich RAW reviews into MARTS: add real embeddings + sentiment (in Python).

It reads reviews from raw.reviews_raw that aren't in marts.reviews yet, computes
each one's embedding and sentiment locally (see embeddings.py), and upserts them
into marts with the vector stored in the pgvector vector(768) column. The chatbot
then searches marts by cosine distance (embedding <=> query_vector).

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
# the process OOM-killed on a memory-constrained host (the embedding model already
# needs ~1 GB just to load). Default kept low for small cloud instances; raise it
# via env on machines with more RAM if you want faster enrichment.
BATCH = int(os.environ.get("ENRICH_BATCH", "16"))

# Pull reviews that still need enriching (new, or whose text changed).
_SELECT_PENDING = """
SELECT r.id, r.brand, r.source, r.source_url, r.author, r.rating, r.text,
       r.created_at, r.captured_at, m.id AS existing_id
FROM RAW.REVIEWS_RAW r
LEFT JOIN MARTS.REVIEWS m ON m.id = r.id
WHERE m.id IS NULL OR m.text <> r.text
LIMIT %(batch)s
"""

# Write the vector with an explicit ::vector cast. json.dumps of a float list is
# already valid pgvector text input, so no extra adapter is needed.
_MERGE = """
INSERT INTO marts.reviews
    (id, brand, source, source_url, author, rating, text, sentiment,
     created_at, captured_at, embedding)
VALUES
    (%(id)s, %(brand)s, %(source)s, %(source_url)s, %(author)s, %(rating)s,
     %(text)s, %(sentiment)s, %(created_at)s, %(captured_at)s, %(embedding)s::vector)
ON CONFLICT (id) DO UPDATE SET
    text = EXCLUDED.text, rating = EXCLUDED.rating, sentiment = EXCLUDED.sentiment,
    embedding = EXCLUDED.embedding, captured_at = EXCLUDED.captured_at
"""


def _connect():
    from ..db import connect

    return connect()


def _read_batch() -> list[dict]:
    """Read one batch of pending rows with a short-lived connection."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_PENDING, {"batch": BATCH})
            cols = [c[0].lower() for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _write_batch(params: list[dict]) -> None:
    """Write one enriched batch with a short-lived connection."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(_MERGE, params)
        conn.commit()
    finally:
        conn.close()


def enrich() -> int:
    """Embed + score pending reviews into MARTS. Returns rows written.

    Loops in batches until RAW is fully caught up. Crucially, the Neon connection
    is NOT held open while embeddings are computed: the local model load + embed
    can take tens of seconds, and a connection left idle that long gets dropped by
    Neon ("SSL error: unexpected eof"). So we read with one short-lived
    connection, embed with NO connection held, then write with a fresh one.
    """
    total = 0
    new_negatives: list[dict] = []
    while True:
        rows = _read_batch()
        if not rows:
            break

        # Slow part (model load + embedding): no DB connection is held here.
        vectors = embeddings.embed([r["text"] for r in rows])
        params = []
        for row, vec in zip(rows, vectors):
            is_new = row.pop("existing_id", None) is None
            sentiment = embeddings.sentiment_bucket(row["text"], row.get("rating"))
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

        _write_batch(params)
        total += len(params)
        log.info("enriched %d reviews (running total %d)", len(params), total)

        if len(rows) < BATCH:
            break

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
