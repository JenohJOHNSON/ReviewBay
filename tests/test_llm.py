"""The LLM gateway: provider order, fallback, cooldowns, and failure classification.

No network. Providers are fakes keyed by model name; failures use the OpenAI
SDK's REAL exception classes (built with the SDK's own HTTP types), so the
classification is tested against what the SDK actually raises, not against
look-alikes that happen to share attribute names.
"""

from __future__ import annotations

import os
import types
import unittest
import unittest.mock

import openai
import openai._exceptions as _sdk_exceptions

from reviewbot import llm

_http = getattr(_sdk_exceptions, "httpx2", None) or getattr(_sdk_exceptions, "httpx")
_REQ = _http.Request("POST", "https://api.example/v1/chat/completions")


def sdk_error(status: int, message: str, code: str | None = None, headers: dict | None = None,
              type_: str | None = None):
    """A genuine openai exception for this HTTP status."""
    response = _http.Response(status, request=_REQ, headers=headers or {})
    body = {"message": message, "code": code} if code else {"message": message}
    if type_:
        body["type"] = type_
    cls = {
        400: openai.BadRequestError, 401: openai.AuthenticationError,
        403: openai.PermissionDeniedError, 404: openai.NotFoundError,
        429: openai.RateLimitError, 500: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    return cls(message, response=response, body=body)


QUOTA = lambda: sdk_error(429, "You exceeded your current quota", code="insufficient_quota")  # noqa: E731

# Verbatim from the production logs on 2026-10-20: the newer form of "out of
# credit", whose code is NOT insufficient_quota. The first version of classify()
# took it for a short rate limit.
NO_CREDITS = lambda: sdk_error(  # noqa: E731
    429,
    "You have no credits remaining. Add credits to continue using the API at "
    "https://platform.openai.com/settings/organization/billing/.",
    code="credit_balance_exhausted",
    type_="insufficient_quota",
)


class FakeClient:
    """Answers per model: a string is a reply, an exception is raised."""

    def __init__(self, provider: str, outcomes: dict, calls: list):
        self.provider, self.outcomes, self.calls = provider, outcomes, calls
        self.responses = types.SimpleNamespace(create=self._responses)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat))
        self.options = {}

    def with_options(self, **options):
        self.options = options
        return self

    def _outcome(self, api: str, kwargs: dict):
        model = kwargs["model"]
        self.calls.append((self.provider, api, model, kwargs))
        outcome = self.outcomes.get(model, "")
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(kwargs)
        return outcome

    def _responses(self, **kwargs):
        text = self._outcome("responses", kwargs)
        return types.SimpleNamespace(
            output_text=text, model=kwargs["model"],
            usage=types.SimpleNamespace(input_tokens=11, output_tokens=7),
        )

    def _chat(self, **kwargs):
        outcome = self._outcome("chat", kwargs)
        text, used = (outcome if isinstance(outcome, tuple) else (outcome, kwargs["model"]))
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
            model=used,
            usage=types.SimpleNamespace(prompt_tokens=13, completion_tokens=5),
        )


class GatewayTestCase(unittest.TestCase):
    MODELS = "free/a:free,free/b:free,openrouter/free"

    def setUp(self):
        llm.reset_state()
        self.addCleanup(llm.reset_state)
        env = unittest.mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test-openai",
            "OPENROUTER_API_KEY": "sk-or-test",
            "OPENROUTER_MODELS": self.MODELS,
            "OPENROUTER_TASKS": "",
            "LLM_PRIMARY": "auto",
            "OPENROUTER_DATA_COLLECTION": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.calls: list = []
        self.outcomes = {"openai": {}, "openrouter": {}}
        patcher = unittest.mock.patch.object(
            llm, "_make_client",
            lambda provider: FakeClient(provider, self.outcomes[provider], self.calls),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def chat(self, **kw):
        defaults = dict(system="sys", user="question", max_tokens=1500,
                        openai_model="gpt-4o-mini", openai_api="responses")
        defaults.update(kw)
        return llm.complete(defaults.pop("task", "chat"), **defaults)

    def models_called(self):
        return [(p, m) for p, _api, m, _kw in self.calls]


class PlanTests(GatewayTestCase):
    def test_openai_first_then_openrouter_models_in_order(self):
        plan = llm._plan("chat", "gpt-4o-mini")
        self.assertEqual(
            [(a.provider, a.model) for a in plan],
            [("openai", "gpt-4o-mini"), ("openrouter", "free/a:free"),
             ("openrouter", "free/b:free"), ("openrouter", "openrouter/free")],
        )

    def test_llm_primary_openrouter_puts_openai_last(self):
        os.environ["LLM_PRIMARY"] = "openrouter"
        plan = llm._plan("chat", "gpt-4o-mini")
        self.assertEqual(plan[0].provider, "openrouter")
        self.assertEqual(plan[-1].provider, "openai")

    def test_openrouter_alone_is_enough(self):
        del os.environ["OPENAI_API_KEY"]
        self.assertTrue(llm.available("chat"))
        self.assertEqual({a.provider for a in llm._plan("chat", "gpt-4o-mini")}, {"openrouter"})

    def test_background_tasks_do_not_use_openrouter_by_default(self):
        # The free tier's 50 requests a day must not be eaten by the QC loop.
        self.assertEqual({a.provider for a in llm._plan("qc", "gpt-4o-mini")}, {"openai"})
        self.assertEqual({a.provider for a in llm._plan("extraction", "gpt-4o-mini")}, {"openai"})
        del os.environ["OPENAI_API_KEY"]
        self.assertFalse(llm.available("qc"))

    def test_openrouter_tasks_all_opens_every_task(self):
        os.environ["OPENROUTER_TASKS"] = "all"
        for task in llm.TASKS:
            self.assertIn("openrouter", {a.provider for a in llm._plan(task, "m")}, task)

    def test_nothing_configured_means_unavailable(self):
        del os.environ["OPENAI_API_KEY"]
        del os.environ["OPENROUTER_API_KEY"]
        self.assertFalse(llm.available("chat"))
        with self.assertRaises(llm.LLMUnavailable):
            self.chat()

    def test_default_models_end_with_the_free_router(self):
        self.assertEqual(llm.DEFAULT_OPENROUTER_MODELS[-1], "openrouter/free")
        self.assertTrue(all(llm.is_free_model(m) for m in llm.DEFAULT_OPENROUTER_MODELS))

    def test_unknown_task_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            llm.complete("poetry", system="s", user="u")


class PrimaryPathTests(GatewayTestCase):
    """The OpenAI request must be exactly what each call site sent before."""

    def test_responses_api_payload_is_unchanged(self):
        self.outcomes["openai"]["gpt-4o-mini"] = "An answer [1]."
        result = self.chat()
        _p, api, _m, kwargs = self.calls[0]
        self.assertEqual(api, "responses")
        self.assertEqual(kwargs, {"model": "gpt-4o-mini", "instructions": "sys",
                                  "input": "question", "max_output_tokens": 1500})
        self.assertEqual((result.provider, result.fallback), ("openai", False))
        self.assertEqual(result.usage, {"prompt_tokens": 11, "completion_tokens": 7})

    def test_reasoning_models_get_a_reasoning_effort(self):
        self.outcomes["openai"]["gpt-5-mini"] = "ok"
        self.chat(openai_model="gpt-5-mini")
        self.assertEqual(self.calls[0][3]["reasoning"], {"effort": "low"})

    def test_chat_api_payload_for_qc(self):
        self.outcomes["openai"]["gpt-4o-mini"] = '{"items": []}'
        self.chat(task="qc", openai_api="chat", json_mode=True, temperature=0, max_tokens=None)
        _p, api, _m, kwargs = self.calls[0]
        self.assertEqual(api, "chat")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["temperature"], 0)
        self.assertNotIn("max_tokens", kwargs)  # QC never set one before


class FallbackTests(GatewayTestCase):
    def test_an_exhausted_openai_quota_falls_back_to_a_free_model(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = "Free answer [1]."
        result = self.chat()
        self.assertEqual(result.text, "Free answer [1].")
        self.assertEqual((result.provider, result.model, result.fallback), ("openrouter", "free/a:free", True))
        self.assertEqual(result.failures[0]["kind"], "quota")
        self.assertTrue(result.label()["free"])

    def test_the_production_no_credits_error_sets_openai_aside_for_half_an_hour(self):
        self.outcomes["openai"]["gpt-4o-mini"] = NO_CREDITS()
        self.outcomes["openrouter"]["free/a:free"] = "Free answer."
        self.assertEqual(self.chat().provider, "openrouter")
        cooling = [c for c in llm.status()["cooling_down"] if c["provider"] == "openai"]
        self.assertEqual(cooling[0]["kind"], "quota")
        self.assertGreater(cooling[0]["seconds_left"], 25 * 60)

    def test_the_exhausted_provider_is_skipped_on_the_next_request(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = "Free answer."
        self.chat()
        self.calls.clear()
        result = self.chat()
        # Without the cooldown every message would first wait on a provider
        # already known to be refusing.
        self.assertNotIn(("openai", "gpt-4o-mini"), self.models_called())
        self.assertEqual(result.failures[0]["kind"], "cooling_down")

    def test_a_rate_limited_model_is_set_aside_for_its_retry_after(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = sdk_error(
            429, "Rate limit exceeded", headers={"retry-after": "12"})
        self.outcomes["openrouter"]["free/b:free"] = "B answers."
        self.assertEqual(self.chat().model, "free/b:free")
        cooling = {(c["provider"], c["model"]): c for c in llm.status()["cooling_down"]}
        self.assertIn(("openrouter", "free/a:free"), cooling)
        self.assertLessEqual(cooling[("openrouter", "free/a:free")]["seconds_left"], 12)
        self.assertEqual(cooling[("openrouter", "free/a:free")]["kind"], "rate_limit")

    def test_the_free_daily_allowance_sets_aside_every_openrouter_model(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = sdk_error(
            429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000")
        with self.assertRaises(llm.LLMUnavailable) as caught:
            self.chat()
        kinds = [f["kind"] for f in caught.exception.failures]
        self.assertEqual(kinds[:2], ["quota", "daily_limit"])
        # Every later OpenRouter model was skipped, not called: the allowance is
        # per account, so trying each one would just collect the same refusal.
        self.assertNotIn(("openrouter", "free/b:free"), self.models_called())
        self.assertTrue(all(k == "cooling_down" for k in kinds[2:]))

    def test_an_empty_answer_moves_on_to_the_next_model(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = "   "
        self.outcomes["openrouter"]["free/b:free"] = "Real answer."
        result = self.chat()
        self.assertEqual(result.model, "free/b:free")
        self.assertEqual([f["kind"] for f in result.failures], ["quota", "empty"])

    def test_the_free_router_reports_the_model_it_actually_used(self):
        os.environ["OPENROUTER_MODELS"] = "openrouter/free"
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["openrouter/free"] = ("Routed answer.", "some/model:free")
        result = self.chat()
        self.assertEqual(result.model, "some/model:free")
        self.assertEqual(result.requested_model, "openrouter/free")

    def test_a_retired_model_is_skipped_for_hours_not_seconds(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = sdk_error(404, "Model not found")
        self.outcomes["openrouter"]["free/b:free"] = "ok"
        self.chat()
        cooling = {c["model"]: c for c in llm.status()["cooling_down"]}
        self.assertGreater(cooling["free/a:free"]["seconds_left"], 3600)

    def test_a_data_policy_refusal_points_at_the_privacy_settings(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = sdk_error(
            404, "No endpoints found matching your data policy (Free model publication)")
        self.outcomes["openrouter"]["free/b:free"] = "ok"
        with self.assertLogs("reviewbot.llm", "WARNING") as logs:
            self.chat()
        self.assertIn("openrouter.ai/settings/privacy", " ".join(logs.output))

    def test_everything_failing_raises_with_the_full_story(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        for model in ("free/a:free", "free/b:free", "openrouter/free"):
            self.outcomes["openrouter"][model] = sdk_error(500, "upstream exploded")
        with self.assertRaises(llm.LLMUnavailable) as caught:
            self.chat()
        self.assertEqual([f["kind"] for f in caught.exception.failures],
                         ["quota", "server", "server", "server"])

    def test_transient_failures_are_not_remembered(self):
        self.outcomes["openai"]["gpt-4o-mini"] = sdk_error(500, "blip")
        self.outcomes["openrouter"]["free/a:free"] = "ok"
        self.chat()
        self.outcomes["openai"]["gpt-4o-mini"] = "back"
        self.assertEqual(self.chat().provider, "openai")

    def test_the_deadline_stops_further_attempts(self):
        def slow_failure(kwargs):
            raise sdk_error(500, "slow")
        self.outcomes["openai"]["gpt-4o-mini"] = slow_failure
        with unittest.mock.patch.object(llm.time, "monotonic", side_effect=[0.0, 0.0, 500.0, 500.0, 500.0]):
            with self.assertRaises(llm.LLMUnavailable) as caught:
                self.chat(deadline=60)
        self.assertEqual(caught.exception.failures[-1]["kind"], "deadline")


class OpenRouterRequestTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        os.environ["LLM_PRIMARY"] = "openrouter"

    def test_free_models_get_room_to_reason(self):
        self.outcomes["openrouter"]["free/a:free"] = "ok"
        self.chat(max_tokens=1500)
        self.assertGreaterEqual(self.calls[0][3]["max_tokens"], 4096)

    def test_json_mode_is_requested_from_openrouter_too(self):
        os.environ["OPENROUTER_TASKS"] = "all"
        self.outcomes["openrouter"]["free/a:free"] = "{}"
        self.chat(task="qc", openai_api="chat", json_mode=True)
        self.assertEqual(self.calls[0][3]["response_format"], {"type": "json_object"})

    def test_data_collection_can_be_denied(self):
        os.environ["OPENROUTER_DATA_COLLECTION"] = "deny"
        self.outcomes["openrouter"]["free/a:free"] = "ok"
        self.chat()
        self.assertEqual(self.calls[0][3]["extra_body"], {"provider": {"data_collection": "deny"}})


class RealClientConstructionTests(unittest.TestCase):
    """Builds the real SDK clients (no request is sent) to check their settings."""

    def test_openrouter_client_settings(self):
        with unittest.mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test",
                                                   "OPENROUTER_SITE_URL": "https://reviewbay.example"}):
            client = llm._make_client("openrouter")
        self.assertEqual(str(client.base_url).rstrip("/"), llm.OPENROUTER_BASE_URL.rstrip("/"))
        self.assertEqual(client.max_retries, 0)
        headers = client.default_headers
        self.assertEqual(headers.get("X-Title"), "ReviewBay")
        self.assertEqual(headers.get("HTTP-Referer"), "https://reviewbay.example")

    def test_openai_client_retries_once(self):
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            self.assertEqual(llm._make_client("openai").max_retries, 1)

    def test_with_options_exists_on_the_real_client(self):
        with unittest.mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            client = llm._make_client("openai").with_options(timeout=5)
        self.assertEqual(client.timeout, 5)


class ClassificationTests(unittest.TestCase):
    """Against the SDK's real exception classes."""

    CASES = [
        (QUOTA(), "openai", "quota"),
        (NO_CREDITS(), "openai", "quota"),
        (sdk_error(429, "Rate limit reached for requests"), "openai", "rate_limit"),
        (sdk_error(429, "Rate limit exceeded: free-models-per-day"), "openrouter", "daily_limit"),
        (sdk_error(402, "Insufficient credits"), "openrouter", "credits"),
        (sdk_error(401, "Incorrect API key provided"), "openai", "auth"),
        (sdk_error(403, "Your input was flagged by moderation"), "openrouter", "moderation"),
        (sdk_error(404, "No endpoints found matching your data policy"), "openrouter", "data_policy"),
        (sdk_error(404, "The model does not exist"), "openrouter", "model_unavailable"),
        (sdk_error(400, "This model's maximum context length is 8192 tokens"), "openai", "context"),
        (sdk_error(400, "Unsupported parameter"), "openai", "bad_request"),
        (sdk_error(500, "Internal error"), "openai", "server"),
        (openai.APITimeoutError(request=_REQ), "openai", "unreachable"),
        (openai.APIConnectionError(request=_REQ), "openai", "unreachable"),
        (RuntimeError("something else"), "openai", "error"),
    ]

    def test_each_failure_is_named_correctly(self):
        for exc, provider, expected in self.CASES:
            with self.subTest(expected=expected, error=str(exc)[:40]):
                self.assertEqual(llm.classify(exc, provider), expected)


class StatusTests(GatewayTestCase):
    def test_status_and_summary_never_contain_a_key(self):
        self.outcomes["openai"]["gpt-4o-mini"] = QUOTA()
        self.outcomes["openrouter"]["free/a:free"] = "ok"
        self.chat()
        text = repr(llm.status()) + llm.summary()
        self.assertNotIn("sk-test-openai", text)
        self.assertNotIn("sk-or-test", text)
        self.assertEqual(llm.summary(), "openai+openrouter(3)")
        models = {(m["provider"], m["model"]): m for m in llm.status()["models"]}
        self.assertEqual(models[("openrouter", "free/a:free")]["successes"], 1)
        self.assertEqual(models[("openai", "gpt-4o-mini")]["failures"], {"quota": 1})

    def test_a_paid_model_in_the_list_is_warned_about_once(self):
        os.environ["OPENROUTER_MODELS"] = "anthropic/some-paid-model,free/a:free"
        with self.assertLogs("reviewbot.llm", "WARNING") as logs:
            llm._plan("chat", "gpt-4o-mini")
            llm._plan("chat", "gpt-4o-mini")
        warnings = [line for line in logs.output if "not a free model" in line]
        self.assertEqual(len(warnings), 1)

    def test_is_free_model(self):
        self.assertTrue(llm.is_free_model("google/gemma-4-31b-it:free"))
        self.assertTrue(llm.is_free_model("openrouter/free"))
        self.assertFalse(llm.is_free_model("google/lyria-3-pro-preview"))  # $0.08 a song


if __name__ == "__main__":
    unittest.main()
