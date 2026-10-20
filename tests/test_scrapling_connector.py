"""The Scrapling depth engine: gating, the real fetch path, and provenance.

The fetch-path tests run against `tests/fixture_server.py` rather than a mocked
`requests`, because the fetch path is where the redirect re-validation, the
content-type check, the byte cap and the politeness controls live. Mocking the
transport would test everything except the parts most worth testing.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fixture_server import FixtureServer, dev_fetch_env

from reviewbot.connectors import scrapling_source as ss
from reviewbot.scraping import budget, extract as extractor, metrics, robots

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CapabilityTests(unittest.TestCase):
    def test_the_parser_is_available_in_the_base_install(self) -> None:
        """Scrapling's light core is a base dependency; the browser is not."""
        self.assertTrue(ss.ScraplingEngine.parser_available())

    def test_the_tier_falls_back_to_what_is_installed(self) -> None:
        engine = ss.ScraplingEngine()
        self.assertIn(engine.resolved_tier(), ("requests", "scrapling", "browser"))

    def test_asking_for_a_missing_tier_degrades_instead_of_failing(self) -> None:
        engine = ss.ScraplingEngine(tier="browser")
        with patch.object(ss.ScraplingEngine, "browser_available", staticmethod(lambda: False)):
            self.assertIn(engine.resolved_tier(), ("requests", "scrapling"))

    def test_the_connector_disables_cleanly_when_turned_off(self) -> None:
        """build_connectors treats KeyError as "not configured"."""
        with patch.object(ss, "ENABLED", False):
            with self.assertRaises(KeyError):
                ss.ScraplingConnector()


class GatingTests(unittest.TestCase):
    """What the engine refuses to fetch, and why."""

    def setUp(self) -> None:
        self.engine = ss.ScraplingEngine()

    def test_platforms_with_a_dedicated_connector_are_skipped(self) -> None:
        for url in (
            "https://apps.apple.com/us/app/id123",
            "https://play.google.com/store/apps/details?id=com.x",
            "https://news.ycombinator.com/item?id=1",
        ):
            with self.subTest(url=url):
                reason = self.engine.should_skip(url)
                self.assertIn("dedicated connector", reason)

    def test_platforms_that_do_not_permit_fetching_are_skipped(self) -> None:
        for url in (
            "https://www.instagram.com/explore/tags/x/",
            "https://www.facebook.com/somepage",
            "https://www.linkedin.com/company/x",
            "https://x.com/someone/status/1",
        ):
            with self.subTest(url=url):
                self.assertTrue(self.engine.should_skip(url))

    def test_blocked_urls_are_skipped_by_policy(self) -> None:
        self.assertIn("url policy", self.engine.should_skip("http://169.254.169.254/"))
        self.assertIn("url policy", self.engine.should_skip("file:///etc/passwd"))

    def test_a_declined_domain_is_not_asked_again(self) -> None:
        self.engine.declined_domains.add("example.com")
        self.assertIn("declined", self.engine.should_skip("https://example.com/reviews"))


class ProvenanceTests(unittest.TestCase):
    """`source` must stay the platform. The engine is how we read, not who said it."""

    def _candidate(self):
        page = extractor.extract(
            fixture("structural_reviews.html"),
            "https://www.trustpilot.com/review/northwind.example?utm_source=x",
        )
        return page.candidates[0]

    def test_source_is_the_platform_not_the_engine(self) -> None:
        review = ss.to_review("Northwind Coffee", self._candidate())
        self.assertEqual(review.source, "trustpilot")
        self.assertNotEqual(review.source, "scrapling")

    def test_the_engine_is_recorded_in_extra_instead(self) -> None:
        review = ss.to_review("Northwind Coffee", self._candidate(), discovered_via="https://s.example/q")
        extraction = review.extra["extraction"]
        self.assertEqual(extraction["engine"], "scrapling")
        self.assertEqual(extraction["kind"], "review")
        self.assertEqual(extraction["parser_version"], ss.PARSER_VERSION)
        self.assertEqual(extraction["discovered_via"], "https://s.example/q")
        self.assertTrue(extraction["extracted_at"])
        self.assertTrue(extraction["evidence"])

    def test_the_citation_url_is_canonicalized(self) -> None:
        review = ss.to_review("Northwind Coffee", self._candidate())
        self.assertNotIn("utm_source", review.source_url)

    def test_a_candidate_with_no_text_yields_nothing(self) -> None:
        candidate = extractor.Candidate(text="   ", source_url="https://e.example/1")
        self.assertIsNone(ss.to_review("B", candidate))

    def test_a_candidate_with_no_url_yields_nothing(self) -> None:
        candidate = extractor.Candidate(text="A long enough review body to be stored." * 2,
                                        source_url="")
        self.assertIsNone(ss.to_review("B", candidate))


class LiveFetchPathTests(unittest.TestCase):
    """The real transport, against the local fixture server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = {k: os.environ.get(k) for k in dev_fetch_env()}
        os.environ.update(dev_fetch_env())
        cls.server = FixtureServer()
        cls.base = cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        for key, value in cls._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self) -> None:
        robots.reset_cache()

    def _engine(self, **kw):
        defaults = dict(max_pages=20, max_pages_per_domain=20, request_timeout=5,
                        deadline_seconds=60)
        defaults.update(kw)
        return ss.ScraplingEngine(
            crawl_budget=budget.CrawlBudget(**defaults), metrics=metrics.RunMetrics()
        )

    def test_a_normal_page_is_fetched_and_extracted(self) -> None:
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/structural_reviews.html")
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.status, 200)
            page = extractor.extract(result.html, result.url)
            self.assertEqual(len(page.candidates), 3)
        finally:
            engine.close()

    def test_a_redirect_is_followed(self) -> None:
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/redirect-once")
            self.assertTrue(result.ok, result.error)
            self.assertIn("Northwind", result.html)
        finally:
            engine.close()

    def test_a_redirect_to_a_blocked_address_is_refused(self) -> None:
        """THE SSRF case: a policy-clean host redirecting to the metadata endpoint.

        Validating only the URL the user supplied is not enough, because the
        redirect target is chosen by the server being fetched.
        """
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/redirect-to-metadata")
            self.assertFalse(result.ok)
            self.assertIn("url policy", result.error)
            self.assertNotIn("meta-data", result.html)
        finally:
            engine.close()

    def test_a_redirect_loop_is_bounded(self) -> None:
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/redirect-loop")
            self.assertFalse(result.ok)
            self.assertIn("too many redirects", result.error)
        finally:
            engine.close()

    def test_a_rate_limit_quarantines_the_whole_domain(self) -> None:
        """429 is about the host, so stop asking the host."""
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/rate-limited")
            self.assertTrue(result.declined)
            self.assertIn("127.0.0.1", engine.declined_domains)
        finally:
            engine.close()

    def test_a_403_on_one_page_does_not_quarantine_the_domain(self) -> None:
        """A login-gated page says nothing about the rest of a public site."""
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/forbidden")
            self.assertTrue(result.declined)
            self.assertEqual(engine.declined_domains, set())
            # ...and the rest of the site is still readable.
            ok = engine.fetch(self.base + "/structural_reviews.html")
            self.assertTrue(ok.ok, ok.error)
        finally:
            engine.close()

    def test_a_declined_page_is_never_retried(self) -> None:
        engine = self._engine()
        try:
            engine.fetch(self.base + "/forbidden")
            self.assertEqual(engine.metrics.engine(ss.ENGINE).retries, 0)
        finally:
            engine.close()

    def test_a_non_html_response_is_rejected_by_content_type(self) -> None:
        engine = self._engine()
        try:
            result = engine.fetch(self.base + "/not-html")
            self.assertFalse(result.ok)
            self.assertIn("not a web page", result.error)
        finally:
            engine.close()

    def test_an_oversized_page_is_truncated_to_the_cap(self) -> None:
        engine = self._engine(max_bytes_per_page=40_000)
        try:
            result = engine.fetch(self.base + "/huge.html")
            self.assertTrue(result.ok, result.error)
            self.assertLessEqual(len(result.html), 40_000)
        finally:
            engine.close()

    def test_robots_disallow_is_honoured(self) -> None:
        engine = self._engine()
        try:
            reason = engine.should_skip(self.base + "/private/secret.html")
            self.assertIn("robots.txt", reason)
        finally:
            engine.close()

    def test_robots_allow_lets_a_page_through(self) -> None:
        engine = self._engine()
        try:
            self.assertEqual(engine.should_skip(self.base + "/structural_reviews.html"), "")
        finally:
            engine.close()

    def test_the_crawl_budget_is_enforced(self) -> None:
        engine = self._engine(max_pages=2, max_pages_per_domain=2)
        try:
            self.assertTrue(engine.fetch(self.base + "/empty.html").ok)
            self.assertTrue(engine.fetch(self.base + "/empty.html").ok)
            third = engine.fetch(self.base + "/empty.html")
            self.assertFalse(third.ok)
            self.assertIn("budget", third.error)
        finally:
            engine.close()

    def test_harvest_follows_declared_pagination(self) -> None:
        engine = self._engine()
        try:
            candidates = engine.harvest(self.base + "/paginated_page1.html")
            # 3 on page 1, 2 on page 2, 1 on page 3.
            self.assertEqual(len(candidates), 6)
        finally:
            engine.close()

    def test_harvest_on_an_unavailable_url_returns_nothing_quietly(self) -> None:
        engine = self._engine()
        try:
            self.assertEqual(engine.harvest(self.base + "/missing.html"), [])
        finally:
            engine.close()

    def test_metrics_record_what_happened(self) -> None:
        engine = self._engine()
        try:
            engine.fetch(self.base + "/structural_reviews.html")
            engine.fetch(self.base + "/missing.html")
            tally = engine.metrics.engine(ss.ENGINE)
            self.assertEqual(tally.succeeded, 1)
            self.assertEqual(tally.failed, 1)
            self.assertGreater(tally.bytes_fetched, 0)
        finally:
            engine.close()

    def test_the_connector_yields_normalized_reviews_from_supplied_urls(self) -> None:
        engine = self._engine()
        connector = ss.ScraplingConnector(
            urls=[self.base + "/structural_reviews.html"], engine=engine
        )
        try:
            reviews = list(connector.fetch("Northwind Coffee", ["Northwind Coffee"], 10))
            self.assertEqual(len(reviews), 3)
            self.assertTrue(all(r.brand == "Northwind Coffee" for r in reviews))
            self.assertTrue(all(r.source_url.startswith("http") for r in reviews))
            self.assertTrue(all(r.extra["extraction"]["engine"] == "scrapling" for r in reviews))
        finally:
            connector.close()

    def test_the_connector_honours_its_limit(self) -> None:
        engine = self._engine()
        connector = ss.ScraplingConnector(
            urls=[self.base + "/structural_reviews.html"], engine=engine
        )
        try:
            reviews = list(connector.fetch("Northwind Coffee", ["Northwind Coffee"], 2))
            self.assertEqual(len(reviews), 2)
        finally:
            connector.close()

    def test_the_connector_with_no_urls_yields_nothing(self) -> None:
        """It reads pages the search sources found; it does not search."""
        connector = ss.ScraplingConnector(urls=[], engine=self._engine())
        try:
            self.assertEqual(list(connector.fetch("Anything", ["Anything"], 10)), [])
        finally:
            connector.close()


class BrowserEscalationTests(unittest.TestCase):
    def test_no_escalation_when_content_was_extracted(self) -> None:
        engine = ss.ScraplingEngine()
        engine.allow_browser = True
        result = ss.FetchResult(url="https://e.example/x", status=200,
                                html=fixture("structural_reviews.html"), tier="requests")
        page = extractor.extract(result.html, result.url)
        self.assertFalse(engine.needs_browser(result, page))

    def test_no_escalation_when_no_browser_is_installed(self) -> None:
        engine = ss.ScraplingEngine()
        engine.allow_browser = True
        result = ss.FetchResult(url="https://e.example/x", status=200,
                                html='<html><body><div id="root"></div></body></html>',
                                tier="requests")
        with patch.object(ss.ScraplingEngine, "browser_available", staticmethod(lambda: False)):
            self.assertFalse(engine.needs_browser(result, None))

    def test_escalation_for_a_client_rendered_shell(self) -> None:
        engine = ss.ScraplingEngine()
        engine.allow_browser = True
        result = ss.FetchResult(
            url="https://e.example/x", status=200, tier="requests",
            html='<html><body><div id="root"></div>'
                 '<script>window.__INITIAL_STATE__={}</script></body></html>',
        )
        page = extractor.extract(result.html, result.url)
        with patch.object(ss.ScraplingEngine, "browser_available", staticmethod(lambda: True)):
            self.assertTrue(engine.needs_browser(result, page))

    def test_no_escalation_when_the_browser_already_ran(self) -> None:
        engine = ss.ScraplingEngine()
        engine.allow_browser = True
        result = ss.FetchResult(url="https://e.example/x", status=200, html="<html></html>",
                                tier="browser")
        with patch.object(ss.ScraplingEngine, "browser_available", staticmethod(lambda: True)):
            self.assertFalse(engine.needs_browser(result, None))


class ComplianceTests(unittest.TestCase):
    """Guards against anti-bot evasion creeping in."""

    def test_the_user_agent_identifies_the_project(self) -> None:
        self.assertIn("ReviewBay", robots.USER_AGENT)
        self.assertIn("http", robots.USER_AGENT)

    def test_cloudflare_solving_is_never_requested(self) -> None:
        source = Path(ss.__file__).read_text(encoding="utf-8")
        self.assertIn("solve_cloudflare=False", source)
        self.assertNotIn("solve_cloudflare=True", source)

    def test_tls_impersonation_is_not_used(self) -> None:
        source = Path(ss.__file__).read_text(encoding="utf-8")
        self.assertNotIn("impersonate=", source)

    def test_stealthy_headers_are_disabled(self) -> None:
        source = Path(ss.__file__).read_text(encoding="utf-8")
        self.assertIn("stealthy_headers=False", source)


if __name__ == "__main__":
    unittest.main()
