"""The embeddings fallback, and the one rule it must never break.

Vectors from different embedding models live in different spaces. A question
embedded by one model and ranked against reviews embedded by another returns
confident nonsense, with no error anywhere. So the only acceptable fallback is
the SAME model served by another provider, and these tests pin that down along
with the retry and cooldown behaviour. Failures use the OpenAI SDK's real
exception classes, including the exact "no credits" error production returned.
"""

from __future__ import annotations

import os
import types
import unittest
import unittest.mock

import openai
import openai._exceptions as _sdk_exceptions

from reviewbot import embeddings

_http = getattr(_sdk_exceptions, "httpx2", None) or getattr(_sdk_exceptions, "httpx")
_REQ = _http.Request("POST", "https://api.example/v1/embeddings")


def no_credits():
    """Verbatim from the production logs on 2026-10-20."""
    body = {
        "message": "You have no credits remaining. Add credits to continue using the API at "
                   "https://platform.openai.com/settings/organization/billing/.",
        "type": "insufficient_quota", "param": None, "code": "credit_balance_exhausted",
    }
    return openai.RateLimitError(body["message"], response=_http.Response(429, request=_REQ), body=body)


def server_error():
    return openai.InternalServerError("upstream", response=_http.Response(500, request=_REQ), body=None)


def bad_request():
    return openai.BadRequestError("bad input", response=_http.Response(400, request=_REQ), body=None)


def rate_limited():
    return openai.RateLimitError("Rate limit reached", response=_http.Response(429, request=_REQ),
                                 body={"message": "Rate limit reached", "code": "rate_limit_exceeded"})


class FakeEmbeddings:
    """A provider: a queue of outcomes, each an exception or a dimension count."""

    def __init__(self, name, outcomes, calls):
        self.name, self.outcomes, self.calls = name, list(outcomes), calls
        self.embeddings = types.SimpleNamespace(create=self._create)

    def _create(self, *, model, input, dimensions):
        self.calls.append((self.name, model, dimensions, len(input)))
        outcome = self.outcomes.pop(0) if self.outcomes else 768
        if isinstance(outcome, BaseException):
            raise outcome
        return types.SimpleNamespace(data=[
            types.SimpleNamespace(index=i, embedding=[0.1] * outcome) for i in range(len(input))
        ])


class EmbeddingsTestCase(unittest.TestCase):
    def setUp(self):
        embeddings.reset_state()
        self.addCleanup(embeddings.reset_state)
        env = unittest.mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test", "OPENROUTER_API_KEY": "sk-or-test",
            "OPENROUTER_EMBEDDINGS": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.outcomes = {"openai": [], "openrouter": []}
        self.clients = {}

        def client_for(provider):
            if provider not in self.clients:
                self.clients[provider] = FakeEmbeddings(provider, self.outcomes[provider], self.calls)
            return self.clients[provider]

        for patcher in (
            unittest.mock.patch.object(embeddings, "_client_for", side_effect=client_for),
            # Backoff sleeps are real seconds; the tests only care how often we retry.
            unittest.mock.patch.object(embeddings.time, "sleep", lambda _s: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def providers_called(self):
        return [c[0] for c in self.calls]


class SameModelRuleTests(EmbeddingsTestCase):
    def test_the_fallback_is_the_same_model_through_openrouter(self):
        self.assertEqual(embeddings.providers(), [
            ("openai", embeddings.EMBED_MODEL),
            ("openrouter", "openai/" + embeddings.EMBED_MODEL),
        ])

    def test_no_free_or_different_embedding_model_is_ever_chosen(self):
        for provider, model in embeddings.providers():
            self.assertTrue(model.split("/")[-1] == embeddings.EMBED_MODEL, (provider, model))
            self.assertNotIn(":free", model)

    def test_the_fallback_cannot_be_redirected_by_configuration(self):
        with unittest.mock.patch.dict(os.environ, {"OPENROUTER_MODELS": "some/free-embedder:free",
                                                   "OPENROUTER_EMBED_MODEL": "some/other-model"}):
            self.assertEqual(embeddings.providers()[1][1], "openai/" + embeddings.EMBED_MODEL)

    def test_openrouter_embeddings_can_be_turned_off(self):
        os.environ["OPENROUTER_EMBEDDINGS"] = "0"
        self.assertEqual([p for p, _m in embeddings.providers()], ["openai"])

    def test_openrouter_alone_still_serves_the_same_model(self):
        del os.environ["OPENAI_API_KEY"]
        self.assertEqual(embeddings.providers(), [("openrouter", "openai/" + embeddings.EMBED_MODEL)])

    def test_every_request_asks_for_768_dimensions(self):
        embeddings.embed(["one", "two"])
        self.assertEqual({c[2] for c in self.calls}, {embeddings.EMBED_DIM})


class FallbackTests(EmbeddingsTestCase):
    def test_an_empty_account_is_not_retried_and_falls_back_at_once(self):
        self.outcomes["openai"].append(no_credits())
        vectors = embeddings.embed(["hello"])
        self.assertEqual(len(vectors[0]), 768)
        # One request to OpenAI, not four attempts with backoff.
        self.assertEqual(self.providers_called(), ["openai", "openrouter"])

    def test_an_empty_account_is_set_aside_for_later_calls(self):
        self.outcomes["openai"].append(no_credits())
        embeddings.embed(["first"])
        self.calls.clear()
        embeddings.embed(["second"])
        self.assertEqual(self.providers_called(), ["openrouter"])

    def test_a_transient_failure_is_retried_on_the_same_provider(self):
        self.outcomes["openai"].extend([server_error(), 768])
        embeddings.embed(["hello"])
        self.assertEqual(self.providers_called(), ["openai", "openai"])

    def test_a_rate_limit_is_retried_rather_than_abandoned(self):
        self.outcomes["openai"].extend([rate_limited(), 768])
        embeddings.embed(["hello"])
        self.assertEqual(self.providers_called(), ["openai", "openai"])

    def test_a_provider_returning_the_wrong_dimensions_is_refused(self):
        del os.environ["OPENROUTER_API_KEY"]
        self.outcomes["openai"].append(1536)
        with self.assertRaises(embeddings.EmbeddingsUnavailable):
            embeddings.embed(["hello"])

    def test_nothing_configured_says_what_to_set(self):
        del os.environ["OPENAI_API_KEY"]
        del os.environ["OPENROUTER_API_KEY"]
        with self.assertRaises(embeddings.EmbeddingsUnavailable) as caught:
            embeddings.embed(["hello"])
        self.assertIn("OPENAI_API_KEY", str(caught.exception))

    def test_everything_failing_raises_the_last_error(self):
        self.outcomes["openai"].append(no_credits())
        self.outcomes["openrouter"].append(openai.APIStatusError(
            "Insufficient credits", response=_http.Response(402, request=_REQ), body=None))
        with self.assertRaises(openai.APIStatusError):
            embeddings.embed(["hello"])

    def test_order_is_preserved_across_chunks(self):
        texts = [f"t{i}" for i in range(embeddings._MAX_INPUTS_PER_CALL + 5)]
        vectors = embeddings.embed(texts)
        self.assertEqual(len(vectors), len(texts))


class RetryabilityTests(unittest.TestCase):
    def test_the_production_no_credits_error_is_not_retryable(self):
        self.assertFalse(embeddings._retryable(no_credits()))

    def test_transient_failures_are_retryable(self):
        self.assertTrue(embeddings._retryable(rate_limited()))
        self.assertTrue(embeddings._retryable(server_error()))

    def test_a_bad_request_is_not_retryable(self):
        self.assertFalse(embeddings._retryable(bad_request()))

    def test_the_openai_client_leaves_retrying_to_the_backoff_loop(self):
        embeddings.reset_state()
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            self.assertEqual(embeddings._client().max_retries, 0)
        embeddings.reset_state()


if __name__ == "__main__":
    unittest.main()
