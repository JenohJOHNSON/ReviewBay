"""AI-assisted extraction: output validation, cost control, and the evidence gate.

The evidence gate is the important one. ReviewBay's product claim is that every
statement links back to a real person's words, so a model that paraphrases a
review into something the customer never wrote is not a cosmetic problem, it is
the product being wrong. Nothing an LLM returns is stored unless its text is
demonstrably on the page it came from.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from reviewbot import text as textclean
from reviewbot.connectors import scrapegraph_source as sg
from reviewbot.scraping import budget, evidence

FIXTURES = Path(__file__).parent / "fixtures"

PAGE = textclean.clean((FIXTURES / "structural_reviews.html").read_text(encoding="utf-8"))
VERBATIM = (
    "The baristas remember my order and the beans taste fresh every single time."
)
PARAPHRASE = "Staff recall what I usually get and the coffee is consistently fresh."
FABRICATED = "The store was filthy and the staff were openly rude to me the whole time."


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ev = evidence.SourceEvidence(PAGE)

    def test_a_verbatim_quote_is_supported(self) -> None:
        support = self.ev.supports(VERBATIM)
        self.assertTrue(support)
        self.assertEqual(support.score, 1.0)

    def test_a_paraphrase_is_refused(self) -> None:
        support = self.ev.supports(PARAPHRASE)
        self.assertFalse(support)
        self.assertIn("verbatim", support.reason)

    def test_an_invented_review_is_refused(self) -> None:
        self.assertFalse(self.ev.supports(FABRICATED))

    def test_a_summary_across_reviews_is_refused(self) -> None:
        self.assertFalse(
            self.ev.supports("Customers praise freshness but complain about mobile ordering.")
        )

    def test_a_real_quote_with_an_invented_sentence_appended_is_refused(self) -> None:
        """Partially-fabricated output must not pass on the strength of its real half."""
        mixed = VERBATIM + " Also the parking lot is always covered in broken glass."
        self.assertFalse(self.ev.supports(mixed))

    def test_a_truncated_verbatim_quote_is_supported(self) -> None:
        self.assertTrue(self.ev.supports(VERBATIM[:60]))

    def test_author_names_are_checked_against_the_page(self) -> None:
        self.assertTrue(self.ev.mentions("Priya K"))
        self.assertFalse(self.ev.mentions("Alexandra Fictional"))

    def test_empty_inputs_are_safe(self) -> None:
        self.assertFalse(self.ev.supports(""))
        self.assertFalse(evidence.SourceEvidence("").supports(VERBATIM))


class OutputParsingTests(unittest.TestCase):
    """Models wrap JSON in all sorts of ways; none of them should lose records."""

    def test_clean_json(self) -> None:
        self.assertEqual(len(sg._parse_items('{"reviews":[{"text":"a"},{"text":"b"}]}')), 2)

    def test_markdown_fenced_json(self) -> None:
        self.assertEqual(
            len(sg._parse_items('```json\n{"reviews":[{"text":"a"}]}\n```')), 1
        )

    def test_json_with_prose_around_it(self) -> None:
        raw = 'Sure, here you go:\n{"reviews":[{"text":"a"}]}\nHope that helps!'
        self.assertEqual(len(sg._parse_items(raw)), 1)

    def test_alternative_container_keys(self) -> None:
        for key in ("reviews", "items", "results", "data", "records", "customer_reviews"):
            with self.subTest(key=key):
                self.assertEqual(len(sg._parse_items('{"%s":[{"text":"a"}]}' % key)), 1)

    def test_a_single_bare_review_object(self) -> None:
        self.assertEqual(len(sg._parse_items('{"text":"a","rating":5}')), 1)

    def test_a_bare_array(self) -> None:
        self.assertEqual(len(sg._parse_items('[{"text":"a"},{"text":"b"}]')), 2)

    def test_an_empty_result_is_a_valid_answer(self) -> None:
        self.assertEqual(sg._parse_items('{"reviews":[]}'), [])

    def test_prose_with_no_json_yields_nothing_rather_than_raising(self) -> None:
        self.assertEqual(sg._parse_items("I could not find any reviews on this page."), [])

    def test_truncated_json_yields_nothing_rather_than_raising(self) -> None:
        self.assertEqual(sg._parse_items('{"reviews":[{"text":"a"'), [])

    def test_nested_braces_inside_strings_do_not_confuse_the_scanner(self) -> None:
        raw = 'Here: {"reviews":[{"text":"he said {weird} things"}]} done'
        items = sg._parse_items(raw)
        self.assertEqual(len(items), 1)
        self.assertIn("{weird}", items[0]["text"])


class ValidationTests(unittest.TestCase):
    def _validate(self, items):
        outcome = sg.ExtractionOutcome(backend="test")
        return sg.validate(items, PAGE, outcome), outcome

    def test_a_verbatim_record_is_kept_with_its_fields(self) -> None:
        kept, _ = self._validate(
            [{"text": VERBATIM, "author": "Priya K", "rating": 5, "created_at": "2026-03-04"}]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].author, "Priya K")
        self.assertEqual(kept[0].rating, 5.0)
        self.assertEqual(kept[0].created_at, "2026-03-04")

    def test_a_paraphrased_record_is_rejected_with_a_reason(self) -> None:
        kept, outcome = self._validate([{"text": PARAPHRASE}])
        self.assertEqual(kept, [])
        self.assertEqual(outcome.rejected, 1)
        self.assertIn("text not found verbatim on the page", outcome.rejections)

    def test_an_unverifiable_author_is_dropped_but_the_review_is_kept(self) -> None:
        """Prefer a missing value over a fabricated one."""
        kept, _ = self._validate([{"text": VERBATIM, "author": "Alexandra Fictional"}])
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0].author)

    def test_a_relative_date_is_dropped_not_converted(self) -> None:
        kept, _ = self._validate([{"text": VERBATIM, "created_at": "3 days ago"}])
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0].created_at)

    def test_a_rating_on_another_scale_is_converted(self) -> None:
        kept, _ = self._validate([{"text": VERBATIM, "rating": 8, "rating_scale": 10}])
        self.assertEqual(kept[0].rating, 4.0)

    def test_an_out_of_range_rating_is_dropped(self) -> None:
        kept, _ = self._validate([{"text": VERBATIM, "rating": 99}])
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0].rating)

    def test_short_records_are_rejected(self) -> None:
        kept, outcome = self._validate([{"text": "ok"}])
        self.assertEqual(kept, [])
        self.assertTrue(any("shorter" in r for r in outcome.rejections))

    def test_records_with_no_text_are_rejected(self) -> None:
        kept, outcome = self._validate([{"author": "Priya K", "rating": 5}])
        self.assertEqual(kept, [])
        self.assertIn("no text", outcome.rejections)

    def test_duplicates_within_one_response_are_collapsed(self) -> None:
        kept, outcome = self._validate([{"text": VERBATIM}, {"text": VERBATIM}])
        self.assertEqual(len(kept), 1)
        self.assertIn("duplicate of another record in the same response", outcome.rejections)

    def test_the_kind_field_is_constrained(self) -> None:
        kept, _ = self._validate([{"text": VERBATIM, "kind": "nonsense"}])
        self.assertEqual(kept[0].kind, "review")

    def test_the_record_count_is_capped(self) -> None:
        many = [{"text": VERBATIM} for _ in range(200)]
        kept, _ = self._validate(many)
        self.assertLessEqual(len(kept), sg.MAX_RECORDS)

    def test_a_record_claiming_a_different_page_is_rejected(self) -> None:
        """The evidence check is against the page the model was actually given."""
        kept, _ = self._validate([{"text": "Harbour Tools delivered the drill set next day."}])
        self.assertEqual(kept, [])


class BudgetTests(unittest.TestCase):
    def test_the_call_budget_is_enforced_before_the_call(self) -> None:
        spend = budget.SpendBudget(max_calls=2, max_cost_usd=10.0)
        spend.reserve(100)
        spend.reserve(100)
        with self.assertRaises(budget.BudgetExceeded) as caught:
            spend.reserve(100)
        self.assertIn("call budget", str(caught.exception))

    def test_the_cost_ceiling_is_enforced(self) -> None:
        spend = budget.SpendBudget(
            max_calls=100, max_cost_usd=0.0001, max_input_tokens_per_call=100_000
        )
        with self.assertRaises(budget.BudgetExceeded) as caught:
            spend.reserve(50_000)
        self.assertIn("cost ceiling", str(caught.exception))

    def test_an_oversized_page_is_refused_before_it_is_sent(self) -> None:
        spend = budget.SpendBudget(max_calls=10, max_cost_usd=10.0,
                                   max_input_tokens_per_call=1000)
        with self.assertRaises(budget.BudgetExceeded) as caught:
            spend.reserve(5000)
        self.assertIn("too large", str(caught.exception))

    def test_zero_means_none_allowed_not_unlimited(self) -> None:
        """Regression: `max_calls > 0 and ...` made 0 mean "no limit".

        Setting SCRAPEGRAPH_MAX_CALLS_PER_RUN=0 is the obvious way to say "spend
        nothing", and it used to grant unlimited calls instead.
        """
        spend = budget.SpendBudget(max_calls=0, max_cost_usd=1.0)
        with self.assertRaises(budget.BudgetExceeded):
            spend.reserve(10)
        self.assertEqual(spend.snapshot()["calls"], 0)

    def test_zero_cost_ceiling_allows_nothing(self) -> None:
        spend = budget.SpendBudget(max_calls=100, max_cost_usd=0.0)
        with self.assertRaises(budget.BudgetExceeded):
            spend.reserve(10)

    def test_a_negative_limit_means_unlimited(self) -> None:
        spend = budget.SpendBudget(
            max_calls=-1, max_cost_usd=-1, max_input_tokens_per_call=-1
        )
        for _ in range(50):
            spend.reserve(1_000_000)
        self.assertEqual(spend.snapshot()["calls"], 50)

    def test_refusals_are_recorded_for_the_run_report(self) -> None:
        spend = budget.SpendBudget(max_calls=0, max_cost_usd=1.0)
        with self.assertRaises(budget.BudgetExceeded):
            spend.reserve(10)
        snapshot = spend.snapshot()
        self.assertEqual(snapshot["refusals"], 1)
        self.assertIn("call budget", snapshot["last_refusal"])

    def test_cost_is_estimated_from_real_token_counts(self) -> None:
        spend = budget.SpendBudget(max_calls=5, max_cost_usd=10.0, model="gpt-4o-mini")
        spend.reserve(1000)
        spend.record(1000, 500)
        self.assertGreater(spend.snapshot()["estimated_cost_usd"], 0)

    def test_an_unknown_model_is_not_assumed_free(self) -> None:
        price_in, price_out = budget.model_price("some-new-model")
        self.assertGreater(price_in, 0)
        self.assertGreater(price_out, 0)

    def test_price_overrides_are_honoured(self) -> None:
        with patch.dict(
            "os.environ",
            {"SCRAPEGRAPH_PRICE_IN": "1.0", "SCRAPEGRAPH_PRICE_OUT": "2.0"},
            clear=False,
        ):
            self.assertEqual(budget.model_price("gpt-4o-mini"), (1.0, 2.0))

    def test_token_estimation_is_roughly_proportional(self) -> None:
        small = budget.estimate_tokens("a word " * 10)
        large = budget.estimate_tokens("a word " * 1000)
        self.assertGreater(large, small * 10)


class ExtractorTests(unittest.TestCase):
    """The AiExtractor, with the backend stubbed out."""

    def setUp(self) -> None:
        sg.clear_cache()

    def _extractor(self, **kw):
        defaults = dict(spend=budget.SpendBudget(max_calls=5, max_cost_usd=1.0), backend="native")
        defaults.update(kw)
        return sg.AiExtractor(**defaults)

    def test_a_successful_extraction_records_usage_and_cost(self) -> None:
        extractor = self._extractor()
        with patch.object(
            sg, "_extract_native",
            return_value=([{"text": VERBATIM, "author": "Priya K"}], 900, 120),
        ):
            outcome = extractor.extract(PAGE, "https://e.example/p")
        self.assertEqual(len(outcome.records), 1)
        self.assertEqual(outcome.prompt_tokens, 900)
        self.assertGreater(outcome.cost_usd, 0)
        self.assertEqual(extractor.spend.snapshot()["calls"], 1)

    def test_a_backend_failure_is_not_fatal(self) -> None:
        extractor = self._extractor()
        with patch.object(sg, "_extract_native", side_effect=RuntimeError("model down")):
            outcome = extractor.extract(PAGE, "https://e.example/p")
        self.assertEqual(outcome.records, [])
        self.assertIn("RuntimeError", outcome.error)

    def test_a_failed_call_still_charges_the_estimate(self) -> None:
        """It may well have cost money; pretending otherwise hides spend."""
        extractor = self._extractor()
        with patch.object(sg, "_extract_native", side_effect=RuntimeError("boom")):
            extractor.extract(PAGE, "https://e.example/p")
        self.assertGreater(extractor.spend.snapshot()["prompt_tokens"], 0)

    def test_a_cached_extraction_spends_nothing(self) -> None:
        extractor = self._extractor()
        with patch.object(
            sg, "_extract_native", return_value=([{"text": VERBATIM}], 900, 100)
        ) as call:
            first = extractor.extract(PAGE, "https://e.example/p")
            second = extractor.extract(PAGE, "https://e.example/p")
        self.assertEqual(call.call_count, 1)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(extractor.spend.snapshot()["calls"], 1)

    def test_the_cache_is_keyed_on_content_not_url(self) -> None:
        """The same page reached by two links is one extraction."""
        extractor = self._extractor()
        with patch.object(
            sg, "_extract_native", return_value=([{"text": VERBATIM}], 900, 100)
        ) as call:
            extractor.extract(PAGE, "https://a.example/p")
            extractor.extract(PAGE, "https://b.example/other")
        self.assertEqual(call.call_count, 1)

    def test_different_content_is_a_cache_miss(self) -> None:
        extractor = self._extractor()
        with patch.object(
            sg, "_extract_native", return_value=([{"text": VERBATIM}], 900, 100)
        ) as call:
            extractor.extract(PAGE, "https://e.example/p")
            extractor.extract(PAGE + " something else entirely", "https://e.example/q")
        self.assertEqual(call.call_count, 2)

    def test_an_over_budget_page_is_never_sent(self) -> None:
        extractor = self._extractor(
            spend=budget.SpendBudget(max_calls=0, max_cost_usd=1.0)
        )
        with patch.object(sg, "_extract_native", side_effect=AssertionError("must not call")):
            outcome = extractor.extract(PAGE, "https://e.example/p")
        self.assertIn("budget", outcome.error)

    def test_oversized_content_is_truncated_before_the_call(self) -> None:
        extractor = self._extractor()
        huge = "Harbour Tools is great. " * 5000
        captured = {}

        def fake(content, objective, model):
            captured["len"] = len(content)
            return [], 10, 10

        with patch.object(sg, "_extract_native", side_effect=fake):
            extractor.extract(huge, "https://e.example/p")
        self.assertLessEqual(captured["len"], sg.MAX_CONTENT_CHARS)

    def test_empty_content_short_circuits(self) -> None:
        extractor = self._extractor()
        with patch.object(sg, "_extract_native", side_effect=AssertionError("must not call")):
            outcome = extractor.extract("", "https://e.example/p")
        self.assertIn("empty", outcome.error)

    def test_no_backend_available_is_reported_not_raised(self) -> None:
        extractor = sg.AiExtractor(backend="")
        extractor.backend = ""
        outcome = extractor.extract(PAGE, "https://e.example/p")
        self.assertFalse(outcome.records)
        self.assertIn("no AI-extraction backend", outcome.error)


class ProvenanceTests(unittest.TestCase):
    def test_source_is_the_platform_not_the_ai_engine(self) -> None:
        record = sg.AiRecord(text=VERBATIM, author="Priya K", rating=5.0)
        outcome = sg.ExtractionOutcome(backend="native", model="gpt-4o-mini")
        review = sg.to_review(
            "Northwind Coffee", record, "https://www.trustpilot.com/review/x", outcome
        )
        self.assertEqual(review.source, "trustpilot")
        self.assertNotEqual(review.source, "scrapegraphai")

    def test_the_ai_engine_and_its_validation_are_recorded_in_extra(self) -> None:
        record = sg.AiRecord(text=VERBATIM)
        outcome = sg.ExtractionOutcome(backend="native", model="gpt-4o-mini")
        review = sg.to_review("Brand", record, "https://e.example/p", outcome)
        extraction = review.extra["extraction"]
        self.assertEqual(extraction["engine"], "scrapegraphai")
        self.assertEqual(extraction["backend"], "native")
        self.assertEqual(extraction["model"], "gpt-4o-mini")
        self.assertEqual(extraction["validation"], "verbatim-verified")
        self.assertEqual(extraction["strategy"], "ai")

    def test_a_record_with_no_usable_url_yields_nothing(self) -> None:
        record = sg.AiRecord(text=VERBATIM)
        outcome = sg.ExtractionOutcome()
        self.assertIsNone(sg.to_review("Brand", record, "javascript:alert(1)", outcome))


class BackendSelectionTests(unittest.TestCase):
    def test_the_connector_is_off_unless_explicitly_enabled(self) -> None:
        with patch.object(sg, "ENABLED", False):
            with self.assertRaises(KeyError):
                sg.ScrapeGraphConnector()

    def test_native_is_chosen_when_the_library_is_absent(self) -> None:
        with patch.object(sg, "BACKEND", "auto"), \
             patch.object(sg, "library_available", lambda: False), \
             patch.object(sg, "native_available", lambda: True):
            self.assertEqual(sg.resolve_backend(), "native")

    def test_the_library_is_preferred_when_installed(self) -> None:
        with patch.object(sg, "BACKEND", "auto"), \
             patch.object(sg, "library_available", lambda: True):
            self.assertEqual(sg.resolve_backend(), "scrapegraphai")

    def test_an_explicit_backend_that_is_unavailable_resolves_to_nothing(self) -> None:
        with patch.object(sg, "BACKEND", "scrapegraphai"), \
             patch.object(sg, "library_available", lambda: False):
            self.assertEqual(sg.resolve_backend(), "")

    def test_the_objective_states_the_rules_its_output_is_checked_against(self) -> None:
        """The prompt and the validator must not drift apart."""
        self.assertIn("VERBATIM", sg.OBJECTIVE)
        self.assertIn("verified to appear verbatim", sg.OBJECTIVE)
        self.assertIn("untrusted data", sg.OBJECTIVE)
        self.assertIn('{"reviews": []}', sg.OBJECTIVE)


if __name__ == "__main__":
    unittest.main()
