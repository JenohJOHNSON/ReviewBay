"""The collection orchestrator, end to end.

This is the test that proves the pieces fit: a fake search connector discovers
URLs on the fixture server, the depth pass fetches and extracts those pages, the
quality layer validates and deduplicates, the loader is called, and the run is
recorded — with the database stubbed so the suite needs no Postgres.

It also covers the failure modes the brief asks about explicitly: a source that
raises, a loader that fails, AI extraction disabled, and credentials absent.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fixture_server import FixtureServer, dev_fetch_env

from reviewbot.ingestion import collect
from reviewbot.models import NormalizedReview

BRAND = "Northwind Coffee"


class FakeSearchConnector:
    """Stands in for Tavily/Apify: returns SERP-shaped rows pointing at URLs.

    Deliberately snippet-shaped (title plus a short line, no rating, no author),
    because that is what a search API actually returns and what the depth pass
    exists to improve on.
    """

    source_name = "web"

    def __init__(self, urls: list[str], brand: str = BRAND) -> None:
        self.urls = urls
        self.brand = brand
        self.calls = 0

    def fetch(self, brand, keywords, limit, website=None):
        self.calls += 1
        for url in self.urls:
            yield NormalizedReview(
                brand=brand,
                source="web",
                source_url=url,
                text=(
                    f"Reviews of {brand} - customers rate the roastery and discuss "
                    f"the mobile ordering experience in detail on this page."
                ),
                extra={"engine": "fake-search"},
            )


class EmptyConnector:
    source_name = "hackernews"

    def fetch(self, brand, keywords, limit, website=None):
        return iter(())


class ExplodingConnector:
    source_name = "mastodon"

    def fetch(self, brand, keywords, limit, website=None):
        raise RuntimeError("source is down")
        yield  # pragma: no cover — makes this a generator


class CollectIntegrationBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = {
            k: os.environ.get(k)
            for k in list(dev_fetch_env()) + [
                "SCRAPLING_ENABLED", "SCRAPEGRAPH_ENABLED",
                "QUALITY_REQUIRE_BRAND_MENTION", "COLLECT_TARGET", "COLLECT_FLOOR",
            ]
        }
        os.environ.update(dev_fetch_env())
        os.environ["SCRAPLING_ENABLED"] = "1"
        os.environ["SCRAPEGRAPH_ENABLED"] = "0"
        os.environ["QUALITY_REQUIRE_BRAND_MENTION"] = "0"
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

    #: Most tests here exercise orchestration, not brand matching, and the
    #: fixtures are about a different brand than the one being collected. The
    #: brand filter reads its env var at import time, so it has to be patched on
    #: the module rather than set in the environment. `test_quality.py` and
    #: `QualityIntegrationTests` cover the filter itself.
    require_brand_mention = False

    def setUp(self) -> None:
        from reviewbot.scraping import robots

        robots.reset_cache()
        self.loaded: list[NormalizedReview] = []
        self.recorded: list[dict] = []

    def _run(self, connectors_by_source, *, target=200, floor=150, website=None,
             load_counted=None, on_progress=None):
        """Drive collect_until with fake connectors and a stubbed database."""
        def build(enabled):
            out = []
            for name in enabled:
                connector = connectors_by_source.get(name)
                if connector is not None:
                    out.append(connector)
            return out

        def fake_load_counted(reviews):
            rows = list(reviews)
            self.loaded.extend(rows)
            return len(rows), len(rows)

        def fake_record_run(brand, sources, found, status, started, finished, **kw):
            self.recorded.append(
                {"brand": brand, "sources": sources, "found": found, "status": status, **kw}
            )

        brand_cfg = {"name": BRAND, "keywords": [BRAND], "limit": 50}
        if website:
            brand_cfg["website"] = website

        with patch.object(collect, "build_connectors", build), \
             patch.object(collect.loader, "load_counted",
                          load_counted or fake_load_counted), \
             patch.object(collect.runs, "record_run", fake_record_run), \
             patch.object(collect.routing, "load_history", lambda domains=None: {}), \
             patch.object(collect.routing, "record_history", lambda metrics: None), \
             patch.object(collect.quality, "REQUIRE_BRAND_MENTION",
                          self.require_brand_mention):
            return collect.collect_until(
                brand_cfg, target=target, floor=floor, on_progress=on_progress
            )


class DepthPassTests(CollectIntegrationBase):
    def test_the_depth_pass_reads_the_pages_search_discovered(self) -> None:
        """The core claim: search finds pages, the depth engine reads them."""
        search = FakeSearchConnector([
            self.base + "/structural_reviews.html",
            self.base + "/jsonld_reviews.html",
        ])
        result = self._run({"web": search})

        engines = result["by_engine"]["engines"]
        self.assertIn("scrapling", engines)
        self.assertGreater(engines["scrapling"]["valid"], 0)

        # The search connector contributed 2 snippet rows; the depth pass turned
        # those same 2 URLs into the individual reviews on them.
        from_depth = [
            r for r in self.loaded
            if (r.extra.get("extraction") or {}).get("engine") == "scrapling"
        ]
        self.assertEqual(len(from_depth), 5)  # 3 cards + 2 json-ld reviews

    def test_depth_records_carry_ratings_authors_and_platform_provenance(self) -> None:
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        self._run({"web": search})
        from_depth = [
            r for r in self.loaded
            if (r.extra.get("extraction") or {}).get("engine") == "scrapling"
        ]
        self.assertTrue(all(r.rating is not None for r in from_depth))
        self.assertTrue(all(r.author for r in from_depth))
        # 127.0.0.1 is not a known platform host, so it stays "web" — the point
        # is that it is never "scrapling".
        self.assertTrue(all(r.source != "scrapling" for r in from_depth))

    def test_pagination_is_followed(self) -> None:
        search = FakeSearchConnector([self.base + "/paginated_page1.html"])
        self._run({"web": search})
        from_depth = [
            r for r in self.loaded
            if (r.extra.get("extraction") or {}).get("engine") == "scrapling"
        ]
        self.assertGreaterEqual(len(from_depth), 4)

    def test_the_brand_website_is_included_as_a_target(self) -> None:
        search = FakeSearchConnector([])
        result = self._run(
            {"web": search}, website=self.base + "/structural_reviews.html"
        )
        self.assertGreater(result["by_engine"]["engines"]["scrapling"]["valid"], 0)

    def test_an_invalid_website_does_not_break_the_run(self) -> None:
        search = FakeSearchConnector([self.base + "/jsonld_reviews.html"])
        result = self._run({"web": search}, website="http://169.254.169.254/")
        self.assertGreater(result["total"], 0)


class ResilienceTests(CollectIntegrationBase):
    def test_a_source_that_raises_does_not_abort_the_run(self) -> None:
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search, "mastodon": ExplodingConnector()})
        self.assertGreater(result["total"], 0)
        self.assertEqual(result["by_engine"]["engines"]["mastodon"]["failed"], 1)

    def test_an_empty_source_is_not_an_error(self) -> None:
        search = FakeSearchConnector([self.base + "/jsonld_reviews.html"])
        result = self._run({"web": search, "hackernews": EmptyConnector()})
        self.assertGreater(result["total"], 0)

    def test_a_loader_failure_does_not_count_rows_as_collected(self) -> None:
        """Rows that were never stored must not be reported as collected."""
        def failing_load(reviews):
            raise RuntimeError("neon is unreachable")

        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search}, load_counted=failing_load)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["collected_new"], 0)

    def test_no_sources_at_all_returns_an_empty_result_not_an_exception(self) -> None:
        result = self._run({})
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["floor_met"])
        self.assertEqual(result["by_source"], {})

    def test_unavailable_and_declined_pages_are_handled(self) -> None:
        search = FakeSearchConnector([
            self.base + "/missing.html",
            self.base + "/forbidden",
            self.base + "/structural_reviews.html",
        ])
        result = self._run({"web": search})
        self.assertGreater(result["total"], 0)


class QualityIntegrationTests(CollectIntegrationBase):
    def test_duplicate_urls_across_stages_do_not_inflate_the_count(self) -> None:
        """The same URL found by two passes must not be counted twice."""
        url = self.base + "/structural_reviews.html"
        search = FakeSearchConnector([url, url, url])
        result = self._run({"web": search})
        urls = [r.source_url for r in self.loaded]
        self.assertEqual(len(urls), len(set(f"{u}|{r.text}" for u, r in zip(urls, self.loaded))))
        self.assertEqual(result["total"], len(set(r.id for r in self.loaded)))

    def test_the_quality_report_is_part_of_the_result(self) -> None:
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search})
        report = result["quality"]
        self.assertIn("kept", report)
        self.assertIn("rejected", report)
        self.assertIn("duplicates", report)
        self.assertEqual(report["kept"], result["total"])

    def test_off_brand_records_are_rejected_when_the_check_is_on(self) -> None:
        """The brand filter drops a keyword collision before it is stored.

        The paginated fixture is about "Harbour Tools" while this run collects
        for "Northwind Coffee", so every record it yields is off-brand.
        """
        self.require_brand_mention = True
        try:
            search = FakeSearchConnector([self.base + "/paginated_page1.html"])
            result = self._run({"web": search})
        finally:
            self.require_brand_mention = False
        self.assertGreater(result["quality"]["rejected"], 0)
        self.assertIn(
            "text does not mention the brand", result["quality"]["reasons"]
        )


class ReportingTests(CollectIntegrationBase):
    def test_the_run_is_recorded_with_engine_detail(self) -> None:
        """The onboarding path never recorded a run, so history was always empty."""
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        self._run({"web": search})
        self.assertEqual(len(self.recorded), 1)
        record = self.recorded[0]
        self.assertEqual(record["brand"], BRAND)
        self.assertIn("scrapling", record["engines"])
        self.assertEqual(record["target"], 200)
        self.assertEqual(record["floor"], 150)
        self.assertIn("engines", record["by_engine"])

    def test_progress_is_reported_per_source(self) -> None:
        seen = []
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        self._run({"web": search}, on_progress=lambda s, w, t: seen.append((s, w, t)))
        self.assertTrue(seen)
        self.assertIn("scrapling", [s for s, _w, _t in seen])
        # The running total must never go backwards.
        totals = [t for _s, _w, t in seen]
        self.assertEqual(totals, sorted(totals))

    def test_collected_new_distinguishes_novelty_from_re_found_rows(self) -> None:
        def already_known(reviews):
            rows = list(reviews)
            self.loaded.extend(rows)
            return len(rows), 0  # sent, but nothing was new

        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search}, load_counted=already_known)
        self.assertGreater(result["total"], 0)
        self.assertEqual(result["collected_new"], 0)

    def test_routing_decisions_are_reported(self) -> None:
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search})
        self.assertIn("routing", result)
        self.assertTrue(result["routing"]["decisions"])

    def test_the_target_contract_is_preserved(self) -> None:
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search}, target=200, floor=150)
        self.assertEqual(result["target"], 200)
        self.assertEqual(result["floor"], 150)
        self.assertFalse(result["reached_target"])
        self.assertFalse(result["floor_met"])

    def test_collection_stops_once_the_target_is_met(self) -> None:
        """A met target skips later stages entirely rather than padding a number."""
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        result = self._run({"web": search}, target=1, floor=1)
        self.assertTrue(result["reached_target"])
        # The first source already satisfied the target, so the depth pass must
        # not have fetched anything at all.
        attempted = result["by_engine"]["engines"].get("scrapling", {}).get("attempted", 0)
        self.assertEqual(attempted, 0)
        self.assertEqual(result["total"], 1)

    def test_the_depth_pass_stops_mid_way_once_the_target_is_met(self) -> None:
        """With many URLs and a small target, it must stop early, not finish."""
        urls = [
            self.base + "/structural_reviews.html",
            self.base + "/jsonld_reviews.html",
            self.base + "/paginated_page1.html",
            self.base + "/article_only.html",
        ]
        search = FakeSearchConnector(urls)
        # 4 search rows land first, so a target of 6 leaves room for 2 more.
        result = self._run({"web": search}, target=6, floor=6)
        self.assertTrue(result["reached_target"])
        attempted = result["by_engine"]["engines"]["scrapling"]["attempted"]
        self.assertGreater(attempted, 0)
        self.assertLess(attempted, len(urls))


class DisabledEngineTests(CollectIntegrationBase):
    def test_collection_works_with_the_depth_engine_disabled(self) -> None:
        with patch.dict("os.environ", {"SCRAPLING_ENABLED": "0"}, clear=False):
            search = FakeSearchConnector([self.base + "/structural_reviews.html"])
            result = self._run({"web": search})
        self.assertNotIn("scrapling", result["by_engine"]["engines"])
        # The search rows still land, so the pipeline still produces data.
        self.assertGreater(result["total"], 0)

    def test_collection_works_with_scrapling_absent_entirely(self) -> None:
        with patch.object(collect, "_scrapling_ready", lambda: False):
            search = FakeSearchConnector([self.base + "/structural_reviews.html"])
            result = self._run({"web": search})
        self.assertGreater(result["total"], 0)

    def test_ai_extraction_is_off_by_default(self) -> None:
        search = FakeSearchConnector([self.base + "/irregular.html"])
        result = self._run({"web": search})
        engines = result["by_engine"]["engines"]
        self.assertEqual(engines.get("scrapegraphai", {}).get("ai_calls", 0), 0)
        self.assertEqual(result["by_engine"]["engines"]["scrapling"]["ai_calls"], 0)

    def test_a_held_back_mention_is_still_stored_when_ai_never_runs(self) -> None:
        """A page-level mention must not be lost just because AI is disabled."""
        search = FakeSearchConnector([self.base + "/irregular.html"])
        result = self._run({"web": search})
        from_depth = [
            r for r in self.loaded
            if (r.extra.get("extraction") or {}).get("engine") == "scrapling"
        ]
        self.assertGreaterEqual(len(from_depth), 1)
        self.assertGreater(result["total"], 0)


class AiEscalationTests(CollectIntegrationBase):
    """The AI stage, with the model call stubbed."""

    def _stub_ai(self, records):
        from reviewbot.connectors import scrapegraph_source as sg

        class StubExtractor:
            available = True

            def __init__(self) -> None:
                self.spend = __import__(
                    "reviewbot.scraping.budget", fromlist=["SpendBudget"]
                ).SpendBudget()
                self.calls = 0

            def extract(self, html, url=""):
                self.calls += 1
                outcome = sg.ExtractionOutcome(backend="stub", model="stub")
                outcome.records = records
                return outcome

        return StubExtractor()

    def test_ai_extraction_supersedes_a_page_level_mention(self) -> None:
        from reviewbot.connectors import scrapegraph_source as sg

        quote = (
            "I have bought from Harbour Tools for six years and the only time "
            "anything went wrong they replaced it without argument."
        )
        stub = self._stub_ai([sg.AiRecord(text=quote, kind="review")])

        search = FakeSearchConnector([self.base + "/irregular.html"])
        with patch.object(collect, "_scrapegraph_ready", lambda: True), \
             patch.object(sg, "AiExtractor", lambda **kw: stub):
            result = self._run({"web": search})

        self.assertEqual(stub.calls, 1)
        kinds = [
            (r.extra.get("extraction") or {}).get("kind") for r in self.loaded
            if (r.extra.get("extraction") or {}).get("engine") == "scrapegraphai"
        ]
        self.assertEqual(kinds, ["review"])
        self.assertGreater(result["total"], 0)

    def test_the_mention_survives_when_ai_finds_nothing(self) -> None:
        from reviewbot.connectors import scrapegraph_source as sg

        stub = self._stub_ai([])
        search = FakeSearchConnector([self.base + "/irregular.html"])
        with patch.object(collect, "_scrapegraph_ready", lambda: True), \
             patch.object(sg, "AiExtractor", lambda **kw: stub):
            result = self._run({"web": search})

        self.assertEqual(stub.calls, 1)
        from_depth = [
            r for r in self.loaded
            if (r.extra.get("extraction") or {}).get("engine") == "scrapling"
        ]
        self.assertGreaterEqual(len(from_depth), 1)
        self.assertGreater(result["total"], 0)

    def test_ai_is_not_called_for_a_page_that_parsed_cleanly(self) -> None:
        from reviewbot.connectors import scrapegraph_source as sg

        stub = self._stub_ai([sg.AiRecord(text="x" * 80)])
        search = FakeSearchConnector([self.base + "/structural_reviews.html"])
        with patch.object(collect, "_scrapegraph_ready", lambda: True), \
             patch.object(sg, "AiExtractor", lambda **kw: stub):
            self._run({"web": search})
        self.assertEqual(stub.calls, 0)

    def test_an_ai_failure_does_not_lose_the_held_back_mention(self) -> None:
        from reviewbot.connectors import scrapegraph_source as sg

        class Exploding:
            available = True
            spend = None

            def extract(self, html, url=""):
                raise RuntimeError("model down")

        search = FakeSearchConnector([self.base + "/irregular.html"])
        with patch.object(collect, "_scrapegraph_ready", lambda: True), \
             patch.object(sg, "AiExtractor", lambda **kw: Exploding()):
            result = self._run({"web": search})
        self.assertGreater(result["total"], 0)


if __name__ == "__main__":
    unittest.main()
