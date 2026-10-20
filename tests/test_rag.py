from __future__ import annotations

import os
import types
import unittest
from unittest.mock import patch

from reviewbot import llm, prompting
from reviewbot.api import rag


SOURCES = [
    rag.Source(
        text="The app crashes on login but pickup is fast.",
        source="google_play",
        source_url="https://play.example/review/1",
        author="Ava",
        rating=2,
        brand="Blue Bottle",
        sentiment="negative",
        score=0.9,
    )
]

NO_KEYS = {"OPENAI_API_KEY": "", "OPENROUTER_API_KEY": ""}


class FakeResponsesClient:
    """Stands in for the OpenAI client the gateway builds."""

    def __init__(self, captured: dict, text="People mention crashes [1].", error=None):
        self.captured, self.text, self.error = captured, text, error
        self.responses = types.SimpleNamespace(create=self._create)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat))

    def with_options(self, **_kw):
        return self

    def _create(self, **payload):
        self.captured.update(payload)
        if self.error:
            raise self.error
        return types.SimpleNamespace(output_text=self.text, model=payload["model"], usage=None)

    def _chat(self, **payload):
        self.captured.setdefault("openrouter_calls", []).append(payload)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="Free answer [1]."))],
            model=payload["model"], usage=None,
        )


class RagTests(unittest.TestCase):
    def setUp(self):
        llm.reset_state()
        self.addCleanup(llm.reset_state)

    def test_missing_keys_use_extractive_fallback(self) -> None:
        with patch.dict(os.environ, NO_KEYS, clear=False), patch.object(
            rag, "retrieve", return_value=SOURCES
        ):
            result = rag.answer("What breaks?", brand="Blue Bottle")

        self.assertIn("AI generation is not available", result.answer)
        self.assertIn("[1]", result.answer)
        self.assertEqual(result.sources, SOURCES)
        self.assertTrue(result.generated_by["extractive"])

    def test_openai_response_path(self) -> None:
        captured: dict = {}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test", "OPENROUTER_API_KEY": ""}), \
                patch.object(llm, "_make_client", lambda provider: FakeResponsesClient(captured)), \
                patch.object(rag, "retrieve", return_value=SOURCES):
            result = rag.answer("What breaks?", brand="Blue Bottle")

        self.assertEqual(result.answer, "People mention crashes [1].")
        self.assertEqual(captured["model"], rag.OPENAI_MODEL)
        self.assertIn("instructions", captured)
        self.assertEqual(captured["max_output_tokens"], 1500)
        self.assertEqual(result.generated_by["provider"], "openai")
        self.assertFalse(result.generated_by["fallback"])

    def test_an_exhausted_openai_quota_is_answered_by_a_free_model(self) -> None:
        import openai
        import openai._exceptions as sdk

        http = getattr(sdk, "httpx2", None) or getattr(sdk, "httpx")
        quota = openai.RateLimitError(
            "You exceeded your current quota",
            response=http.Response(429, request=http.Request("POST", "https://x")),
            body={"code": "insufficient_quota", "message": "You exceeded your current quota"},
        )
        captured: dict = {}

        def make(provider):
            return FakeResponsesClient(captured, error=quota if provider == "openai" else None)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test", "OPENROUTER_API_KEY": "or-test",
                                     "OPENROUTER_MODELS": "free/a:free", "OPENROUTER_TASKS": ""}), \
                patch.object(llm, "_make_client", make), \
                patch.object(rag, "retrieve", return_value=SOURCES):
            result = rag.answer("What breaks?", brand="Blue Bottle")

        self.assertEqual(result.answer, "Free answer [1].")
        self.assertEqual(result.generated_by["provider"], "openrouter")
        self.assertTrue(result.generated_by["fallback"])
        self.assertTrue(result.generated_by["free"])
        # The free model gets the same fenced, citation-numbered prompt.
        sent = captured["openrouter_calls"][0]["messages"]
        self.assertIn("<review", sent[1]["content"])
        # ...and the same prompt-injection rule as the primary model.
        self.assertIn(prompting.UNTRUSTED_CONTENT_RULE, sent[0]["content"])

    def test_every_provider_failing_still_answers_extractively(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test", "OPENROUTER_API_KEY": ""}), \
                patch.object(llm, "_make_client",
                             lambda p: FakeResponsesClient({}, error=RuntimeError("down"))), \
                patch.object(rag, "retrieve", return_value=SOURCES):
            result = rag.answer("What breaks?", brand="Blue Bottle")
        self.assertIn("AI generation is not available", result.answer)
        self.assertTrue(result.generated_by["extractive"])


class KeywordRetrievalTests(unittest.TestCase):
    """When no embeddings provider works, search by keyword instead of stopping."""

    def test_an_embedding_failure_falls_back_to_keyword_search(self) -> None:
        keyword = [rag.Source(**{**SOURCES[0].__dict__, "retrieval": "keyword", "score": 0.1})]
        with patch.object(rag.db, "database_url", return_value="postgres://x"), \
                patch.object(rag.embeddings, "embed_one", side_effect=RuntimeError("quota")), \
                patch.object(rag, "keyword_retrieve", return_value=keyword) as kw:
            found = rag.retrieve("crashes on login", brand="Blue Bottle")
        kw.assert_called_once()
        self.assertEqual(found[0].retrieval, "keyword")

    def test_a_missing_database_is_still_reported_as_setup(self) -> None:
        with patch.object(rag.db, "database_url",
                          side_effect=rag.db.DatabaseConfigError("DATABASE_URL is not set")):
            with self.assertRaises(rag.db.DatabaseConfigError):
                rag.retrieve("anything")

    def test_keyword_results_never_claim_high_confidence(self) -> None:
        keyword = [rag.Source(**{**SOURCES[0].__dict__, "retrieval": "keyword", "score": 9.0})
                   for _ in range(6)]
        confidence, evidence = rag._confidence_and_evidence(keyword)
        self.assertEqual(confidence, "Medium")
        self.assertEqual(evidence["retrieval"], "keyword")
        recent = [rag.Source(**{**SOURCES[0].__dict__, "retrieval": "recent"})] * 6
        self.assertEqual(rag._confidence_and_evidence(recent)[0], "Low")

    def test_vector_results_are_graded_as_before(self) -> None:
        strong = [rag.Source(**{**SOURCES[0].__dict__, "score": 0.8}) for _ in range(3)]
        confidence, evidence = rag._confidence_and_evidence(strong)
        self.assertEqual(confidence, "High")
        self.assertNotIn("retrieval", evidence)

    def test_the_tsquery_is_built_from_content_words_only(self) -> None:
        self.assertEqual(
            rag._tsquery("What do people complain about with DELIVERY times?"),
            "complain | delivery | times",
        )

    def test_tsquery_syntax_in_a_question_cannot_reach_postgres(self) -> None:
        # & | ! : ( ) and quotes are tsquery operators; only [a-z0-9] survives.
        query = rag._tsquery("crash' & !(refund):* | \\ 'x")
        self.assertEqual(query, "crash | refund")
        for ch in "&!():*'\\":
            self.assertNotIn(ch, query)

    def test_a_question_of_only_stopwords_has_no_tsquery(self) -> None:
        self.assertIsNone(rag._tsquery("what do they say about it?"))


if __name__ == "__main__":
    unittest.main()
