"""Engine routing: the rules, and the evidence they use.

The brief was explicit that this must be rules rather than an LLM deciding which
scraper to run, and that every decision should be inspectable. So each test here
asserts both the decision and the reason, because the reason is what an operator
reads in the run record.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from reviewbot.ingestion import routing


class ConnectorPriorityTests(unittest.TestCase):
    """Rule 1: never duplicate a connector that reads an official feed."""

    def setUp(self) -> None:
        self.router = routing.Router()

    def test_app_store_urls_go_to_the_app_store_connector(self) -> None:
        decision = self.router.route("https://apps.apple.com/us/app/id123?see-all=reviews")
        self.assertIs(decision.engine, routing.Engine.CONNECTOR)
        self.assertEqual(decision.connector, "app_store")

    def test_play_store_urls_go_to_the_google_play_connector(self) -> None:
        decision = self.router.route("https://play.google.com/store/apps/details?id=com.x")
        self.assertIs(decision.engine, routing.Engine.CONNECTOR)
        self.assertEqual(decision.connector, "google_play")

    def test_hacker_news_urls_go_to_the_algolia_connector(self) -> None:
        decision = self.router.route("https://news.ycombinator.com/item?id=1")
        self.assertIs(decision.engine, routing.Engine.CONNECTOR)
        self.assertEqual(decision.connector, "hackernews")

    def test_the_reason_explains_the_choice(self) -> None:
        decision = self.router.route("https://apps.apple.com/us/app/id1")
        self.assertIn("already covers", decision.reason)


class ProhibitedPlatformTests(unittest.TestCase):
    """Rule 2: platforms we do not fetch directly."""

    def test_social_platforms_are_not_fetched_directly(self) -> None:
        router = routing.Router()
        for url in (
            "https://www.facebook.com/brand",
            "https://www.instagram.com/p/abc/",
            "https://www.linkedin.com/company/x",
            "https://x.com/user/status/1",
            "https://www.tiktok.com/@user/video/1",
            "https://www.youtube.com/watch?v=1",
        ):
            with self.subTest(url=url):
                decision = router.route(url)
                self.assertIsNot(decision.engine, routing.Engine.SCRAPLING)

    def test_thorough_strategy_routes_them_to_a_vendor_instead_of_skipping(self) -> None:
        router = routing.Router(strategy="thorough")
        decision = router.route("https://www.instagram.com/p/abc/")
        self.assertIs(decision.engine, routing.Engine.VENDOR)

    def test_search_result_pages_are_skipped(self) -> None:
        router = routing.Router()
        decision = router.route("https://www.google.com/search?q=brand+reviews")
        self.assertIs(decision.engine, routing.Engine.SKIP)
        self.assertIn("not a review page", decision.reason)


class PolicyTests(unittest.TestCase):
    def test_blocked_urls_are_skipped_before_anything_else(self) -> None:
        router = routing.Router()
        for url in ("http://169.254.169.254/", "file:///etc/passwd", "http://localhost/"):
            with self.subTest(url=url):
                decision = router.route(url)
                self.assertIs(decision.engine, routing.Engine.SKIP)
                self.assertIn("url policy", decision.reason)


class DefaultRouteTests(unittest.TestCase):
    """Rule 3: deterministic extraction first, for an ordinary public page."""

    def test_an_ordinary_page_goes_to_scrapling(self) -> None:
        decision = routing.Router().route("https://blog.example/brand-review")
        self.assertIs(decision.engine, routing.Engine.SCRAPLING)
        self.assertIn("deterministic", decision.reason)

    def test_the_browser_tier_is_only_chosen_for_configured_js_hosts(self) -> None:
        with patch.object(routing, "JS_HEAVY_HINTS", ("spa.example",)):
            router = routing.Router(browser_enabled=True)
            self.assertEqual(router.route("https://spa.example/reviews").tier, "browser")
            self.assertEqual(router.route("https://static.example/reviews").tier, "auto")

    def test_no_browser_tier_when_the_browser_is_disabled(self) -> None:
        with patch.object(routing, "JS_HEAVY_HINTS", ("spa.example",)):
            router = routing.Router(browser_enabled=False)
            self.assertEqual(router.route("https://spa.example/reviews").tier, "auto")


class HistoryTests(unittest.TestCase):
    """Rule 4: stop paying for a fetch that keeps failing."""

    @staticmethod
    def _history(domain, attempts, successes):
        return {
            (domain, routing.Engine.SCRAPLING.value): routing.DomainHistory(
                attempts=attempts, successes=successes
            )
        }

    def test_a_consistently_failing_domain_is_demoted(self) -> None:
        router = routing.Router(history=self._history("bad.example", 10, 0))
        decision = router.route("https://bad.example/reviews")
        self.assertIsNot(decision.engine, routing.Engine.SCRAPLING)
        self.assertIn("succeeds on bad.example", decision.reason)

    def test_a_failing_domain_falls_through_to_ai_when_enabled(self) -> None:
        router = routing.Router(history=self._history("bad.example", 10, 0), ai_enabled=True)
        self.assertIs(
            router.route("https://bad.example/reviews").engine, routing.Engine.SCRAPEGRAPH
        )

    def test_a_failing_domain_is_skipped_when_there_is_no_fallback(self) -> None:
        router = routing.Router(history=self._history("bad.example", 10, 0), ai_enabled=False)
        self.assertIs(router.route("https://bad.example/reviews").engine, routing.Engine.SKIP)

    def test_too_little_history_is_not_judged(self) -> None:
        """One or two failures is noise, not evidence."""
        router = routing.Router(history=self._history("new.example", 2, 0))
        self.assertIs(
            router.route("https://new.example/reviews").engine, routing.Engine.SCRAPLING
        )

    def test_a_healthy_domain_keeps_its_route(self) -> None:
        router = routing.Router(history=self._history("good.example", 20, 18))
        self.assertIs(
            router.route("https://good.example/reviews").engine, routing.Engine.SCRAPLING
        )

    def test_history_subdomains_fold_to_the_registrable_domain(self) -> None:
        router = routing.Router(history=self._history("bad.example", 10, 0), ai_enabled=False)
        self.assertIs(
            router.route("https://shop.bad.example/reviews").engine, routing.Engine.SKIP
        )

    def test_unavailable_history_falls_back_to_the_static_rules(self) -> None:
        """An un-migrated database must not break routing."""
        with patch("reviewbot.db.connection", side_effect=RuntimeError("no table")):
            self.assertEqual(routing.load_history(["x.example"]), {})


class EscalationTests(unittest.TestCase):
    """When a fetched page is worth an AI call."""

    def setUp(self) -> None:
        self.router = routing.Router(ai_enabled=True)

    def test_no_escalation_when_real_reviews_were_extracted(self) -> None:
        decision = self.router.may_escalate_to_ai(
            "https://e.example/p", had_content=True, extracted=5, reviews=5, strategy="jsonld"
        )
        self.assertIs(decision.engine, routing.Engine.SKIP)
        self.assertIn("already produced 5 review", decision.reason)

    def test_escalation_when_nothing_was_extracted(self) -> None:
        decision = self.router.may_escalate_to_ai(
            "https://e.example/p", had_content=True, extracted=0, reviews=0
        )
        self.assertIs(decision.engine, routing.Engine.SCRAPEGRAPH)

    def test_escalation_when_only_a_page_level_mention_was_extracted(self) -> None:
        """The flaw the benchmark exposed.

        `extract_article` returns a single whole-page mention for almost any
        page, so judging on "did we get any record" meant a page of reviews
        written as prose — the exact case AI extraction exists for — was never
        offered to it.
        """
        decision = self.router.may_escalate_to_ai(
            "https://e.example/p", had_content=True, extracted=1, reviews=0, strategy="article"
        )
        self.assertIs(decision.engine, routing.Engine.SCRAPEGRAPH)
        self.assertIn("whole-page mention", decision.reason)

    def test_no_escalation_without_content(self) -> None:
        decision = self.router.may_escalate_to_ai(
            "https://e.example/p", had_content=False, extracted=0, reviews=0
        )
        self.assertIs(decision.engine, routing.Engine.SKIP)
        self.assertIn("nothing was fetched", decision.reason)

    def test_no_escalation_when_ai_is_disabled(self) -> None:
        router = routing.Router(ai_enabled=False)
        decision = router.may_escalate_to_ai(
            "https://e.example/p", had_content=True, extracted=0, reviews=0
        )
        self.assertIs(decision.engine, routing.Engine.SKIP)
        self.assertIn("disabled", decision.reason)

    def test_cheap_strategy_never_escalates(self) -> None:
        router = routing.Router(ai_enabled=True, strategy="cheap")
        decision = router.may_escalate_to_ai(
            "https://e.example/p", had_content=True, extracted=0, reviews=0
        )
        self.assertIs(decision.engine, routing.Engine.SKIP)
        self.assertIn("cheap", decision.reason)


class StoppingTests(unittest.TestCase):
    """The target is a target, not a quota."""

    def setUp(self) -> None:
        self.router = routing.Router()

    def test_stop_at_the_target(self) -> None:
        keep_going, why = self.router.should_continue(200, 200, 150)
        self.assertFalse(keep_going)
        self.assertIn("target reached", why)

    def test_keep_going_below_the_floor(self) -> None:
        keep_going, _ = self.router.should_continue(40, 200, 150)
        self.assertTrue(keep_going)

    def test_paid_work_stops_at_the_floor(self) -> None:
        """Spending money to turn 160 genuine records into 200 is not the point."""
        keep_going, why = self.router.should_continue(160, 200, 150, paid=True)
        self.assertFalse(keep_going)
        self.assertIn("floor met", why)

    def test_free_work_continues_above_the_floor(self) -> None:
        keep_going, _ = self.router.should_continue(160, 200, 150, paid=False)
        self.assertTrue(keep_going)


class SnapshotTests(unittest.TestCase):
    def test_decisions_are_counted_for_the_run_record(self) -> None:
        router = routing.Router()
        router.route("https://a.example/1")
        router.route("https://b.example/2")
        router.route("https://apps.apple.com/us/app/id1")
        snapshot = router.snapshot()
        self.assertEqual(snapshot["strategy"], router.strategy)
        self.assertTrue(snapshot["decisions"])
        self.assertEqual(sum(snapshot["decisions"].values()), 3)

    def test_recording_history_never_raises(self) -> None:
        from reviewbot.scraping import metrics

        tally = metrics.RunMetrics()
        tally.attempt("scrapling", "x.example")
        tally.records("scrapling", valid=3, domain="x.example")
        with patch("reviewbot.db.connection", side_effect=RuntimeError("no table")):
            routing.record_history(tally)  # must not raise


if __name__ == "__main__":
    unittest.main()
