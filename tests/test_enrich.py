"""Enrichment when nothing can embed, and filling the vectors in later.

An empty OpenAI account used to keep new reviews off the dashboard entirely: a
batch that could not be embedded stayed behind in raw.reviews_raw, so a newly
searched brand showed 0 reviews. Now such reviews are saved without a vector and
given one by a later pass. The database calls are replaced by recorders here;
the SQL itself runs against real Postgres in test_postgres_e2e.py.
"""

from __future__ import annotations

import json
import unittest
import unittest.mock

import openai
import openai._exceptions as _sdk_exceptions

from reviewbot import alerts
from reviewbot.enrich import run

_http = getattr(_sdk_exceptions, "httpx2", None) or getattr(_sdk_exceptions, "httpx")
_REQ = _http.Request("POST", "https://api.example/v1/embeddings")


def no_credits():
    body = {"message": "You have no credits remaining.", "type": "insufficient_quota",
            "param": None, "code": "credit_balance_exhausted"}
    return openai.RateLimitError(body["message"], response=_http.Response(429, request=_REQ),
                                 body=body)


def bad_request():
    return openai.BadRequestError("input rejected", response=_http.Response(400, request=_REQ),
                                  body=None)


def raw_row(i, rating=None):
    return {"id": f"r{i:03d}", "brand": "Acme", "source": "web",
            "source_url": f"https://example.com/{i}", "author": None, "rating": rating,
            "text": f"review number {i}", "created_at": None, "captured_at": "2026-10-20",
            "existing_id": None}


class EnrichTestCase(unittest.TestCase):
    """BATCH is 2, the database is a few lists, embed() follows self.outcomes."""

    def setUp(self):
        self.pending = []      # batches _read_batch hands out, in order
        self.written = []      # the params of each _write_batch call
        self.unembedded = []   # (id, text) rows saved without a vector
        self.filled = {}       # id -> vector written by backfill
        self.embed_calls = []
        self.outcomes = []     # per embed() call: an exception to raise, or None

        def read_batch():
            return self.pending.pop(0) if self.pending else []

        def read_unembedded(after):
            rows = [r for r in self.unembedded if r[0] > after and r[0] not in self.filled]
            return rows[: run.BATCH]

        def write_vectors(rows, vectors):
            for (rid, _text), vec in zip(rows, vectors):
                self.filled[rid] = vec

        def embed(texts):
            self.embed_calls.append(list(texts))
            outcome = self.outcomes.pop(0) if self.outcomes else None
            if outcome is not None:
                raise outcome
            return [[0.25] * 768 for _ in texts]

        for target, attr, new in (
            (run, "BATCH", 2),
            (run, "_read_batch", read_batch),
            (run, "_write_batch", self.written.append),
            (run, "_read_unembedded", read_unembedded),
            (run, "_write_vectors", write_vectors),
            (run.embeddings, "embed", embed),
            (run.embeddings, "providers", lambda: [("openai", "text-embedding-3-small")]),
            (alerts, "notify_new_negatives", lambda rows: 0),
        ):
            patcher = unittest.mock.patch.object(target, attr, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def vectors_written(self):
        return [p["embedding"] for batch in self.written for p in batch]


class SavedWithoutVectorsTests(EnrichTestCase):
    def test_reviews_reach_marts_when_nothing_can_embed(self):
        self.pending = [[raw_row(1, rating=5), raw_row(2, rating=1)]]
        self.outcomes = [no_credits()]
        self.assertEqual(run.enrich(), 2)
        self.assertEqual(self.vectors_written(), [None, None])
        # Sentiment is computed locally, so the dashboard still gets it.
        self.assertEqual([p["sentiment"] for p in self.written[0]], ["positive", "negative"])

    def test_one_refusal_stops_asking_for_the_rest_of_the_pass(self):
        self.pending = [[raw_row(1), raw_row(2)], [raw_row(3)]]
        self.outcomes = [no_credits()]
        self.assertEqual(run.enrich(), 3)
        self.assertEqual(len(self.embed_calls), 1)
        self.assertEqual(self.vectors_written(), [None] * 3)

    def test_a_pass_that_found_no_provider_does_not_backfill(self):
        self.pending = [[raw_row(1)]]
        self.unembedded = [("old", "an older review")]
        self.outcomes = [no_credits()]
        run.enrich()
        self.assertEqual(self.filled, {})
        self.assertEqual(len(self.embed_calls), 1)

    def test_a_refused_batch_does_not_stop_the_next_one(self):
        self.pending = [[raw_row(1), raw_row(2)], [raw_row(3)]]
        self.outcomes = [bad_request()]
        run.enrich()
        first, second = self.written
        self.assertEqual([p["embedding"] for p in first], [None, None])
        self.assertEqual(json.loads(second[0]["embedding"]), [0.25] * 768)

    def test_reviews_that_can_be_embedded_get_their_vector(self):
        self.pending = [[raw_row(1)]]
        self.assertEqual(run.enrich(), 1)
        self.assertEqual(len(json.loads(self.written[0][0]["embedding"])), 768)


class BackfillTests(EnrichTestCase):
    def test_reviews_saved_without_a_vector_get_one_later(self):
        self.unembedded = [("a", "one"), ("b", "two"), ("c", "three")]
        self.assertEqual(run.backfill(), 3)
        self.assertEqual(set(self.filled), {"a", "b", "c"})
        self.assertEqual(len(self.embed_calls), 2)

    def test_a_pass_with_nothing_new_still_fills_vectors_in(self):
        self.unembedded = [("a", "one")]
        self.assertEqual(run.enrich(), 0)
        self.assertEqual(set(self.filled), {"a"})

    def test_it_stops_at_the_first_sign_that_nothing_can_embed(self):
        self.unembedded = [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")]
        self.outcomes = [no_credits()]
        self.assertEqual(run.backfill(), 0)
        self.assertEqual(len(self.embed_calls), 1)

    def test_it_steps_over_a_refused_batch(self):
        self.unembedded = [("a", "1"), ("b", "2"), ("c", "3")]
        self.outcomes = [bad_request()]
        self.assertEqual(run.backfill(), 1)
        self.assertEqual(set(self.filled), {"c"})

    def test_it_does_not_query_without_a_provider(self):
        self.unembedded = [("a", "1")]
        with unittest.mock.patch.object(run.embeddings, "providers", lambda: []), \
                unittest.mock.patch.object(run, "_read_unembedded",
                                           side_effect=AssertionError("queried")):
            self.assertEqual(run.backfill(), 0)
        self.assertEqual(self.embed_calls, [])

    def test_one_call_is_bounded(self):
        self.unembedded = [(f"r{i:03d}", str(i)) for i in range(50)]
        with unittest.mock.patch.object(run, "BACKFILL_MAX_BATCHES", 3):
            self.assertEqual(run.backfill(), 6)

    def test_a_backfill_failure_does_not_fail_the_pass(self):
        self.pending = [[raw_row(1)]]
        with unittest.mock.patch.object(run, "backfill", side_effect=RuntimeError("db gone")):
            self.assertEqual(run.enrich(), 1)


if __name__ == "__main__":
    unittest.main()
