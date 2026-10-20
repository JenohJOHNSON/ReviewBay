"""Enrich RAW reviews into MARTS: add real embeddings + sentiment.

It reads reviews from raw.reviews_raw that aren't in marts.reviews yet, computes
each one's OpenAI embedding and local VADER sentiment (see embeddings.py), and
upserts them into marts with the vector stored in the pgvector vector(768)
column. The chatbot then searches marts by cosine distance (embedding <=>
query_vector).

Run standalone (`python -m reviewbot.enrich.run`), from the cloud worker, or from
another scheduler.

**When nothing can embed, reviews are saved anyway, without a vector.** An empty
OpenAI account used to keep every new review off the dashboard: a freshly
searched brand showed 0 reviews while all of them waited in raw.reviews_raw.
Nothing on the dashboard needs the vector (counts, ratings and charts read the
row, and sentiment is computed locally), and chat finds a review without one by
keyword (`api/rag.py`). Each later pass gives those reviews their vector once a
provider can embed again (`backfill()`), always with the same model, so vectors
from different models never mix.

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
# much: 96 reviews of text plus 96 x 768 floats is a few MB.
BATCH = int(os.environ.get("ENRICH_BATCH", "96"))

# How many batches of missing vectors one pass fills in. A long outage can leave
# thousands, and a brand search runs a pass: it should not wait on all of them.
BACKFILL_MAX_BATCHES = 10

# Failures that are about the batch, not the provider: that batch goes without
# vectors and the next one is still tried. Anything else means the provider
# cannot embed right now, and asking again during this pass would only wait.
_BAD_INPUT = frozenset({"bad_request", "context", "moderation"})

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
# already valid pgvector text input, so no extra adapter is needed. A review
# saved while nothing could embed gets embedding NULL.
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

# Reviews still waiting for a vector, in id order. Paging by id rather than
# re-reading from the start steps over a batch the provider refused.
_SELECT_UNEMBEDDED = """
SELECT id, text
FROM marts.reviews
WHERE embedding IS NULL AND id > %(after)s
ORDER BY id
LIMIT %(batch)s
"""

# Only while the text is still the text that was embedded, and only if no
# other pass got there first.
_SET_VECTOR = """
UPDATE marts.reviews SET embedding = %(embedding)s::vector
WHERE id = %(id)s AND text = %(text)s AND embedding IS NULL
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


def _read_unembedded(after: str) -> list[tuple[str, str]]:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_UNEMBEDDED, {"after": after, "batch": BATCH})
            return [(row[0], row[1]) for row in cur.fetchall()]


def _write_vectors(rows: list[tuple[str, str]], vectors: list[list[float]]) -> None:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(_SET_VECTOR, [
                {"id": rid, "text": text, "embedding": json.dumps(vec)}
                for (rid, text), vec in zip(rows, vectors)
            ])
        conn.commit()


def _failure(exc: BaseException) -> str:
    """Name an embeddings failure. Logged instead of the provider's own message."""
    if isinstance(exc, embeddings.EmbeddingsUnavailable):
        return "unavailable"
    from ..llm import classify

    return classify(exc, "embeddings")


def backfill() -> int:
    """Give a vector to reviews saved without one. Returns how many were filled.

    Stops at the first sign that no provider can embed (that will not change by
    the next batch), steps over a batch refused for its content, and does at
    most BACKFILL_MAX_BATCHES batches per call.
    """
    if not embeddings.providers():
        return 0
    filled, after = 0, ""
    for _ in range(BACKFILL_MAX_BATCHES):
        rows = _read_unembedded(after)
        if not rows:
            break
        after = rows[-1][0]
        try:
            vectors = embeddings.embed([text for _rid, text in rows])
        except Exception as exc:  # noqa: BLE001 - classified below
            kind = _failure(exc)
            if kind in _BAD_INPUT:
                log.warning("enrich: a batch of %d review(s) was refused for search vectors "
                            "(%s); skipping it", len(rows), kind)
                continue
            log.info("enrich: search vectors still unavailable (%s, %s); reviews without "
                     "one stay searchable by keyword", type(exc).__name__, kind)
            break
        _write_vectors(rows, vectors)
        filled += len(rows)
        if len(rows) < BATCH:
            break
    if filled:
        log.info("enrich: added search vectors to %d review(s) saved without one", filled)
    return filled


def enrich() -> int:
    """Embed + score pending reviews into MARTS. Returns rows written.

    Loops in batches until RAW is fully caught up. Crucially, the Neon connection
    is NOT held open while embeddings are computed: the embedding round-trip can
    take tens of seconds, and a connection left idle that long gets dropped by
    Neon ("SSL error: unexpected eof"). So we read with one short-lived
    connection, embed with NO connection held, then write with a fresh one.

    A batch that cannot be embedded is saved without vectors rather than left
    pending, so it reaches the dashboard at once; `backfill()` adds the vectors
    later. After the first sign that no provider can embed, the rest of the pass
    does not ask again: every batch would wait on the same refusal.
    """
    total = 0
    without_vector = 0
    embeddings_down = False
    new_negatives: list[dict] = []
    seen_ids: set[str] = set()
    while True:
        rows = _read_batch()
        if not rows:
            break

        # Guard against re-reading rows that were just written: the pending
        # query has no ORDER BY, so a write that silently missed would
        # otherwise loop forever.
        batch_ids = {r["id"] for r in rows}
        if batch_ids and batch_ids <= seen_ids:
            log.error(
                "enrich: the same %d pending row(s) came back after being written; "
                "stopping to avoid a spin. Fix the underlying error and re-run.",
                len(batch_ids),
            )
            break
        seen_ids |= batch_ids

        # Slow part (the embeddings API call): no DB connection is held here.
        vectors = None
        if not embeddings_down:
            try:
                vectors = embeddings.embed([r["text"] for r in rows])
            except Exception as exc:  # noqa: BLE001 - saved without vectors instead
                kind = _failure(exc)
                embeddings_down = kind not in _BAD_INPUT
                log.warning(
                    "enrich: no search vectors for a batch of %d (%s, %s); saving the "
                    "reviews without them, to be filled in later",
                    len(rows), type(exc).__name__, kind,
                )
        if vectors is None:
            vectors = [None] * len(rows)
            without_vector += len(rows)

        params = []
        for row, vec in zip(rows, vectors):
            is_new = row.pop("existing_id", None) is None
            sentiment = embeddings.sentiment_bucket(row["text"], row.get("rating"))
            params.append({**row, "sentiment": sentiment,
                           "embedding": None if vec is None else json.dumps(vec)})
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

    if without_vector:
        log.warning("enrichment complete: %d reviews into MARTS, %d of them without a "
                    "search vector yet (chat finds those by keyword)", total, without_vector)
    else:
        log.info("enrichment complete: %d reviews into MARTS", total)

    # Vectors for reviews saved without one, in this pass or an earlier one.
    # Skipped when this pass has just found that nothing can embed.
    if not embeddings_down:
        try:
            backfill()
        except Exception:  # noqa: BLE001 - best effort: the reviews are already saved
            log.exception("enrich: filling in missing search vectors failed (non-fatal)")

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
