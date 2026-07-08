from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from reviewbot.api import main
from reviewbot.api.rag import Answer, Source


class ApiSmokeTests(unittest.TestCase):
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
            self.assertEqual(client.get("/api/stats").json(), {"total": 0})
            self.assertEqual(client.get("/api/insights").json()["summary"], "No reviews")
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
            stats = client.get("/api/stats")
            reviews = client.get("/api/reviews")
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


if __name__ == "__main__":
    unittest.main()
