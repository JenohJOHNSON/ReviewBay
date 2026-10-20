from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from auth_helpers import SignedIn
from fastapi.testclient import TestClient
from memory_store import MemoryStore

from reviewbot.api import main
from reviewbot.api.rag import Answer, Source


class ApiSmokeTests(SignedIn, unittest.TestCase):
    def test_core_routes(self) -> None:
        client = TestClient(main.app)
        source = Source(
            text="Great service.",
            source="web",
            source_url="https://example.com",
            author=None,
            rating=None,
            brand="Blue Bottle",
            sentiment="positive",
            score=1.0,
        )

        with patch.object(main.stats, "get_stats", return_value={"total": 0}), patch.object(
            main.insights, "get_insights", return_value={"summary": "No reviews", "reviews_analyzed": 0}
        ), patch.object(main.rag, "answer", return_value=Answer("Grounded answer [1].", [source])):
            self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/stats?brand=Blue%20Bottle").json(), {"total": 0})
            self.assertEqual(
                client.get("/api/insights?brand=Blue%20Bottle").json()["summary"], "No reviews"
            )
            chat = client.post("/chat", json={"question": "What do people say?", "brand": "Blue Bottle"}).json()

        self.assertEqual(chat["answer"], "Grounded answer [1].")
        self.assertEqual(chat["sources"][0]["source"], "web")

    def test_readyz_reports_database_status(self) -> None:
        client = TestClient(main.app)

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql):
                self.sql = sql

            def fetchone(self):
                return (1,)

        class Conn:
            def cursor(self):
                return Cursor()

            def close(self):
                self.closed = True

        with patch.object(main.db, "connect", return_value=Conn()):
            ready = client.get("/readyz")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["checks"]["database"], "ok")

        with patch.object(main.db, "connect", side_effect=main.db.DatabaseConfigError("DATABASE_URL is not set")):
            not_ready = client.get("/readyz")

        self.assertEqual(not_ready.status_code, 503)
        self.assertEqual(not_ready.json()["status"], "error")

    def test_missing_database_config_returns_setup_payloads(self) -> None:
        client = TestClient(main.app)
        missing = main.db.DatabaseConfigError("DATABASE_URL is not set")

        with patch.object(main.db, "connect", side_effect=missing):
            stats = client.get("/api/stats?brand=Missing%20DB")
            reviews = client.get("/api/reviews?brand=Missing%20DB")
            insights = client.get("/api/insights?brand=Missing%20DB&refresh=true")
            chat = client.post("/chat", json={"question": "What changed?", "brand": "Missing DB"})

        self.assertEqual(stats.status_code, 200)
        self.assertEqual(stats.json()["setup_required"], "database")
        self.assertEqual(reviews.status_code, 200)
        self.assertEqual(reviews.json()["setup_required"], "database")
        self.assertEqual(insights.status_code, 200)
        self.assertEqual(insights.json()["setup_required"], "database")
        self.assertEqual(chat.status_code, 200)
        self.assertIn("DATABASE_URL", chat.json()["answer"])

    def test_add_brand_requires_database_config(self) -> None:
        client = TestClient(main.app)
        missing = main.db.DatabaseConfigError("DATABASE_URL is not set")

        with patch.object(main.db, "database_url", side_effect=missing), patch.object(
            main.onboarding, "start_async"
        ) as start_async:
            response = client.post("/api/brands", json={"name": "Amadeus"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["setup_required"], "database")
        self.assertIn("DATABASE_URL", response.json()["message"])
        start_async.assert_not_called()



class OnboardingValidationTests(SignedIn, unittest.TestCase):
    """The website field is a URL the server will later fetch.

    An unvalidated value here is a server-side request forgery with the user
    holding the trigger, which is why it is checked at the boundary and not
    somewhere further down the pipeline.
    """

    def test_an_internal_website_is_rejected_with_a_400(self) -> None:
        client = TestClient(main.app)
        for website in (
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8000/admin",
            "http://10.0.0.5/",
            "file:///etc/passwd",
            "http://api.railway.internal/",
        ):
            with self.subTest(website=website), patch.object(
                main.onboarding, "start_async"
            ) as start_async:
                response = client.post(
                    "/api/brands", json={"name": "Probe", "website": website}
                )
                self.assertEqual(response.status_code, 400, website)
                self.assertEqual(response.json()["error"], "invalid_website")
                start_async.assert_not_called()

    def test_a_public_website_is_accepted_and_canonicalized(self) -> None:
        client = TestClient(main.app)
        previous = main.accounts.set_store(MemoryStore())
        self.addCleanup(main.accounts.set_store, previous)
        with patch.object(main.onboarding, "_persist_brand"), patch.object(
            main.onboarding, "start_async"
        ), patch.object(main.db, "database_url", return_value="postgres://x"):
            response = client.post(
                "/api/brands",
                json={"name": "Example Co", "website": "HTTPS://WWW.Example.com/?utm_source=x"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_a_bare_domain_is_upgraded_to_https(self) -> None:
        """People type "example.com", not "https://example.com/"."""
        normalized = main.onboarding.normalize_website("example.com")
        self.assertTrue(normalized.startswith("https://example.com"))

    def test_an_empty_website_is_allowed_because_the_field_is_optional(self) -> None:
        self.assertIsNone(main.onboarding.normalize_website(""))
        self.assertIsNone(main.onboarding.normalize_website(None))

    def test_an_empty_brand_name_is_rejected(self) -> None:
        client = TestClient(main.app)
        response = client.post("/api/brands", json={"name": "   "})
        self.assertIn(response.status_code, (400, 422))

    def test_an_overlong_brand_name_is_rejected(self) -> None:
        client = TestClient(main.app)
        response = client.post("/api/brands", json={"name": "x" * 500})
        self.assertEqual(response.status_code, 422)


class RequestBoundsTests(SignedIn, unittest.TestCase):
    """Without bounds, one request could fan out to hundreds of model calls."""

    def test_an_overlong_question_is_rejected_before_any_work(self) -> None:
        client = TestClient(main.app)
        with patch.object(main.rag, "answer", side_effect=AssertionError("must not run")):
            response = client.post("/chat", json={"question": "x" * 5000})
        self.assertEqual(response.status_code, 422)

    def test_an_empty_question_is_rejected(self) -> None:
        client = TestClient(main.app)
        response = client.post("/chat", json={"question": ""})
        self.assertEqual(response.status_code, 422)

    def test_too_many_brands_are_rejected(self) -> None:
        """`brands` fans out to one embedding call and one query PER BRAND."""
        client = TestClient(main.app)
        with patch.object(main.rag, "answer", side_effect=AssertionError("must not run")):
            response = client.post(
                "/chat",
                json={"question": "What do people say?", "brands": [f"b{i}" for i in range(500)]},
            )
        self.assertEqual(response.status_code, 422)

    def test_a_reasonable_request_still_works(self) -> None:
        client = TestClient(main.app)
        source = Source(
            text="Great service.", source="web", source_url="https://example.com",
            author=None, rating=None, brand="B", sentiment="positive", score=1.0,
        )
        with patch.object(main.rag, "answer", return_value=Answer("Answer [1].", [source])):
            response = client.post(
                "/chat", json={"question": "What do people say?", "brands": ["A", "B"]}
            )
        self.assertEqual(response.status_code, 200)


class CollectionHealthTests(SignedIn, unittest.TestCase):
    def test_the_endpoint_returns_the_panel_payload(self) -> None:
        client = TestClient(main.app)
        payload = {
            "brand": "B",
            "totals": {"reviews": 10, "relevant": 9, "off_topic": 1, "sources_with_data": 2},
            "sources": [],
            "engines": {"available": False, "engines": [], "runs": []},
            "last_successful": None,
            "connectors": [],
        }
        with patch.object(main.health, "collection_health", return_value=payload):
            response = client.get("/api/collection-health?brand=B")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totals"]["reviews"], 10)

    def test_it_degrades_when_the_database_is_unconfigured(self) -> None:
        client = TestClient(main.app)
        with patch.object(main.db, "connection",
                          side_effect=main.db.DatabaseConfigError("DATABASE_URL is not set")):
            response = client.get("/api/collection-health?brand=B")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["setup_required"], "database")


class EngineNoteTests(unittest.TestCase):
    """"No runs recorded yet" must not be reported as "migration missing".

    Found by running the dashboard against a real, fully migrated database:
    the panel told the operator to run 002 because no run had recorded engine
    metrics yet. A brand refreshed only by the worker never records them, so it
    would have said so forever.
    """

    def _payload(self, columns_present: bool) -> dict:
        from reviewbot.api import health

        with patch.object(health, "_per_source", return_value=[]), patch.object(
            health, "_last_successful", return_value=None
        ), patch.object(health.runs, "get_run_detail", return_value=[]), patch.object(
            health.runs, "_extended_available", return_value=columns_present
        ), patch("reviewbot.sources.connector_statuses", return_value=[]):
            return health.collection_health("Acme")

    def test_columns_present_but_no_runs_yet_is_not_a_missing_migration(self) -> None:
        payload = self._payload(columns_present=True)
        self.assertNotIn("migration_required", payload)
        self.assertIn("next collection", payload["engines_note"])

    def test_missing_columns_still_asks_for_the_migration(self) -> None:
        payload = self._payload(columns_present=False)
        self.assertIn("migrate.py", payload["migration_required"])
        self.assertNotIn("engines_note", payload)


class FrontendEscapingTests(unittest.TestCase):
    """Stored review text is attacker-controlled and is rendered with innerHTML.

    `esc()` in index.html, compare.html and onboarding.html escaped only & < >
    while being used inside double-quoted href attributes, so a scraped
    source_url containing a double quote could close the attribute and inject
    its own (for example an onmouseover handler). This asserts every page's
    escaper covers the attribute context too.
    """

    STATIC = Path(main.__file__).parent / "static"

    def test_every_page_escapes_quotes_as_well_as_angle_brackets(self) -> None:
        pages = [p for p in self.STATIC.glob("*.html")]
        self.assertTrue(pages)
        checked = 0
        for page in pages:
            body = page.read_text(encoding="utf-8")
            if "function esc(" not in body:
                continue
            checked += 1
            with self.subTest(page=page.name):
                definition = body.split("function esc(", 1)[1].split("\n", 1)[0]
                self.assertIn("&quot;", definition, f"{page.name} does not escape double quotes")
                self.assertIn("&#39;", definition, f"{page.name} does not escape single quotes")
                self.assertIn("&lt;", definition)
                self.assertIn("&amp;", definition)
        self.assertGreaterEqual(checked, 5)


if __name__ == "__main__":
    unittest.main()
