"""Enrich RAW reviews into MARTS: add real embeddings + sentiment.

It reads reviews from raw.reviews_raw that aren't in marts.reviews yet, computes
each one's OpenAI embedding and local VADER sentiment (see embeddings.py), and
upserts them into marts with the vector stored in the pgvector vector(768)
column. The chatbot then searches marts by cosine distance (embedding <=>
query_vector).

Run standalone (`python -m reviewbot.enrich.run`), from the cloud worker, or from
another scheduler.

**On batch size.** This used to read 16 rows at a time and loop the pending-rows
query until it came back empty. The query is a LEFT JOIN of raw against marts,
so each iteration re-scanned raw to produce 16 rows: enriching 3,000 rows ran
~188 full joins *and* made 188 separate embedding requests, where the connector
already batches up to 96 inputs per call. `ENRICH_BATCH` now defaults to 96 to
match, and `postgres/002_scraping_engines.sql` adds the indexes the join needs.
Lower it if a very small instance is memory constrained; 16 reproduces the old
behaviour exactly.
"""

from __future__ import annotations

import json
import logging
import os

from .. import db, embeddings

log = logging.getLogger(__name__)

# One batch = one embeddings request (the connector chunks at 96 inputs/call), so
# matching the two removes a round trip per 16 rows without raising peak memory
# much: 96 reviews of text plus 96 × 768 floats is a few MB.
BATCH = int(os.environ.get("ENRICH_BATCH", "96"))

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


def _read_batch() -> list[dict]:
    """Read one batch of pending rows, holding the connection only for the read."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_PENDING, {"batch": BATCH})
            cols = [c[0].lower() for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def _write_batch(params: list[dict]) -> None:
    """Write one enriched batch."""
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_MERGE, params)
        conn.commit()


def enrich() -> int:
    """Embed + score pending reviews into MARTS. Returns rows written.

    Loops in batches until RAW is fully caught up. Crucially, the Neon connection
    is NOT held open while embeddings are computed: the embedding round-trip can
    take tens of seconds, and a connection left idle that long gets dropped by
    Neon ("SSL error: unexpected eof"). So we read with one short-lived
    connection, embed with NO connection held, then write with a fresh one.

    A batch whose embedding call fails is skipped rather than fatal: the rows
    stay pending and the next pass retries them. Without that, one bad batch
    (a rate limit, a single oversized input) stalled the whole backlog, and
    because the pending query is unordered it could stall it indefinitely.
    """
    total = 0
    new_negatives: list[dict] = []
    failed_batches = 0
    seen_ids: set[str] = set()
    while True:
        rows = _read_batch()
        if not rows:
            break

        # Guard against a batch we have already tried and failed to write: the
        # pending query has no ORDER BY, so a permanently-failing batch could
        # otherwise be re-read forever.
        batch_ids = {r["id"] for r in rows}
        if batch_ids and batch_ids <= seen_ids:
            log.error(
                "enrich: the same %d pending row(s) came back after a failure — "
                "stopping to avoid a spin. Fix the underlying error and re-run.",
                len(batch_ids),
            )
            break
        seen_ids |= batch_ids

        # Slow part (the embeddings API call): no DB connection is held here.
        try:
            vectors = embeddings.embed([r["text"] for r in rows])
        except Exception:  # noqa: BLE001
            log.exception(
                "enrich: embedding failed for a batch of %d; leaving them pending", len(rows)
            )
            failed_batches += 1
            if failed_batches >= 3:
                log.error("enrich: %d consecutive batch failures — stopping", failed_batches)
                break
            continue

        failed_batches = 0
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
