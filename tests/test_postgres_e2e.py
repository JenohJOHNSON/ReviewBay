"""End to end against a REAL Postgres: accounts, brands, reports, keyword search.

Skipped unless REVIEWBAY_TEST_DATABASE_URL names a throwaway database (its name
must contain "test"; tables are truncated). Everything is real — HTTP routes,
middleware, cookies, scrypt, the Postgres store, the stats, report and search
SQL — except the background scrape, which is replaced by a recorder so these
tests never touch the internet.

    REVIEWBAY_TEST_DATABASE_URL=postgresql://postgres:@127.0.0.1:54451/reviewbay_test \\
        pytest tests/test_postgres_e2e.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from auth_helpers import reset_auth_state

TEST_DB_URL = os.environ.get("REVIEWBAY_TEST_DATABASE_URL", "").strip()
PASSWORD = "correct horse battery"
ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(TEST_DB_URL, "REVIEWBAY_TEST_DATABASE_URL not set")
class PostgresEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in name:
            raise unittest.SkipTest(f"refusing to truncate tables in {name!r}")
        cls._env = unittest.mock.patch.dict(os.environ, {
            "DATABASE_URL": TEST_DB_URL, "OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "",
            "SIGNUP_ENABLED": "1", "SIGNUP_INVITE_CODE": "", "ADMIN_EMAILS": "",
        })
        cls._env.start()
        spec = importlib.util.spec_from_file_location("migrate_e2e", ROOT / "scripts" / "migrate.py")
        migrate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrate)
        with contextlib.redirect_stdout(io.StringIO()):
            if migrate.main([]) != 0:
                raise RuntimeError("migrations failed")

    @classmethod
    def tearDownClass(cls):
        from reviewbot import db

        db.close_idle()
        cls._env.stop()

    def setUp(self):
        from fastapi.testclient import TestClient

        from reviewbot import accounts, db, llm
        from reviewbot.api import insights, main, onboarding, report

        self.db, self.accounts, self.main, self.onboarding = db, accounts, main, onboarding
        with db.cursor(commit=True) as cur:
            cur.execute("TRUNCATE app.user_brands, app.sessions, app.brands, app.users "
                        "RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE marts.reviews, marts.saved_reports, raw.reviews_raw")
        previous = accounts.set_store(accounts.PostgresStore())
        self.addCleanup(accounts.set_store, previous)
        reset_auth_state()
        self.addCleanup(reset_auth_state)
        llm.reset_state()
        insights._CACHE.clear()
        report._CACHE.clear()
        onboarding._JOBS.clear()
        self.addCleanup(onboarding._JOBS.clear)

        self.started = []

        def record(cfg):
            if not onboarding._claim(cfg["name"]):
                return False
            self.started.append(cfg["name"])
            return True

        from reviewbot.ingestion import run as ingestion_run

        scratch = tempfile.mkdtemp(prefix="reviewbay-e2e-")
        for target, kwargs in (((onboarding, "start_async"), {"side_effect": record}),
                               ((onboarding, "_persist_brand"), {}),
                               ((onboarding, "forget_brand"), {"return_value": True}),
                               # The repo's own brands.yml, and no dynamic file:
                               # the registry is what is under test.
                               ((ingestion_run, "CONFIG_PATH"),
                                {"new": str(ROOT / "config" / "brands.yml")}),
                               ((ingestion_run, "DYNAMIC_CONFIG"),
                                {"new": os.path.join(scratch, "brands.dynamic.yml")})):
            p = unittest.mock.patch.object(*target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.client_cls = TestClient

    # --- helpers ---------------------------------------------------------------

    def account(self, email):
        client = self.client_cls(self.main.app, follow_redirects=False)
        resp = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD})
        self.assertEqual(resp.status_code, 200, resp.text)
        return client

    def sql(self, query, params=()):
        with self.db.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def sql_exec(self, query, params=()):
        with self.db.cursor(commit=True) as cur:
            cur.execute(query, params)

    def seed(self, brand, texts, sentiment="neutral"):
        with self.db.cursor(commit=True) as cur:
            for i, text in enumerate(texts):
                cur.execute(
                    "INSERT INTO marts.reviews (id, brand, source, source_url, text, sentiment, "
                    "relevant, captured_at, created_at) "
                    "VALUES (%s, %s, 'web', %s, %s, %s, TRUE, %s, %s)",
                    (f"e2e-{brand}-{i}", brand, f"https://example.com/{i}", text, sentiment,
                     f"2026-10-{10 + i:02d}T00:00:00", f"2026-10-{10 + i:02d}"),
                )

    def finish_jobs(self):
        for job in self.onboarding._JOBS.values():
            job.update(status="done", phase="done")

    # --- the lifecycle -------------------------------------------------------------

    def test_full_account_and_brand_lifecycle(self):
        ann = self.account("ann@example.com")
        bob = self.account("bob@example.com")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM app.sessions")[0][0], 2)

        resp = ann.post("/api/brands", json={"name": "Blue Bottle", "website": "bluebottlecoffee.com"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.started, ["Blue Bottle"])
        self.assertEqual(self.sql("SELECT name, website, active FROM app.brands"),
                         [("Blue Bottle", "https://bluebottlecoffee.com/", True)])

        # A second spelling joins the first instead of forking the data.
        self.finish_jobs()
        self.assertEqual(bob.post("/api/brands", json={"name": "blue BOTTLE"}).json()["brand"],
                         "Blue Bottle")

        self.seed("Blue Bottle", ["Great coffee", "Slow delivery", "Lovely staff"])
        mine = ann.get("/api/me/brands").json()["brands"]
        self.assertEqual((mine[0]["name"], mine[0]["reviews"]), ("Blue Bottle", 3))
        stats = ann.get("/api/stats?brand=blue%20bottle").json()
        self.assertEqual(stats["total"], 3)

        self.assertEqual(bob.get("/api/stats?brand=Globex").status_code, 404)

        # Re-run, then the cooldown — held by the durable registry timestamp.
        self.onboarding._JOBS.clear()
        self.accounts.mark_collected("Blue Bottle")
        self.assertEqual(ann.post("/api/brands/Blue%20Bottle/rerun").status_code, 429)
        self.sql_exec("UPDATE app.brands SET last_collected_at = now() - interval '2 hours'")
        self.assertEqual(ann.post("/api/brands/Blue%20Bottle/rerun").json()["started"], True)
        self.assertIsNotNone(self.sql("SELECT last_rerun_at FROM app.brands")[0][0])

        # Deleting: Bob still tracks it, so it keeps refreshing...
        self.assertFalse(ann.delete("/api/brands/Blue%20Bottle").json()["refresh_stopped"])
        self.assertEqual(ann.get("/api/brands").json()["brands"], [])
        # ...until Bob removes it too.
        self.assertTrue(bob.delete("/api/brands/Blue%20Bottle").json()["refresh_stopped"])
        self.assertEqual(self.sql("SELECT active FROM app.brands"), [(False,)])
        # The reviews survive.
        self.assertEqual(self.sql("SELECT COUNT(*) FROM marts.reviews")[0][0], 3)

        # The worker no longer refreshes it.
        from reviewbot.ingestion import run as ingestion_run

        names = [b["name"] for b in ingestion_run._load_config()["brands"]]
        self.assertNotIn("Blue Bottle", names)

        # Sign out deletes the session row, not just the cookie.
        ann.post("/api/auth/logout")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM app.sessions")[0][0], 1)
        self.assertEqual(ann.get("/api/me/brands").status_code, 401)

    def sql_exec(self, query, params=()):
        with self.db.cursor(commit=True) as cur:
            cur.execute(query, params)

    def test_the_worker_refreshes_brands_added_through_the_website(self):
        ann = self.account("ann@example.com")
        ann.post("/api/brands", json={"name": "Acme Rockets"})
        from reviewbot.ingestion import run as ingestion_run

        brands = {b["name"]: b for b in ingestion_run._load_config()["brands"]}
        self.assertIn("Acme Rockets", brands)
        self.assertTrue(brands["Acme Rockets"]["sources"])

    def test_a_migrated_database_is_never_told_to_run_the_migration(self):
        from reviewbot.api import health
        from reviewbot import runs

        runs._HAS_EXTENDED = None  # probe this database, not a cached answer
        ann = self.account("ann@example.com")
        ann.post("/api/brands", json={"name": "Acme"})
        self.seed("Acme", ["Great"])
        payload = health.collection_health("Acme")
        self.assertNotIn("migration_required", payload)
        self.assertIn("engines_note", payload)

    def test_the_real_missing_table_error_is_recognised(self):
        """The classification is tested against the error Postgres really raises."""
        try:
            with self.db.cursor() as cur:
                cur.execute("SELECT 1 FROM app.table_that_does_not_exist")
        except Exception as exc:  # noqa: BLE001
            self.assertTrue(self.accounts.is_schema_missing(exc))
        else:
            self.fail("expected an undefined-table error")
        self.assertTrue(self.accounts.schema_ready())

    # --- saved reports ----------------------------------------------------------------

    def test_saved_reports_belong_to_their_owner(self):
        ann = self.account("ann@example.com")
        bob = self.account("bob@example.com")
        ann.post("/api/brands", json={"name": "Acme"})
        self.seed("Acme", ["Fast shipping and a great price", "Broke after a week"])

        saved = ann.post("/api/report/save?brand=Acme").json()
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(len(ann.get("/api/reports/saved").json()["saved"]), 1)
        self.assertEqual(bob.get("/api/reports/saved").json()["saved"], [])
        self.assertEqual(bob.get(f"/api/reports/saved/{saved['id']}").status_code, 404)
        self.assertEqual(ann.get(f"/api/reports/saved/{saved['id']}").status_code, 200)

        # The public share link still works with no account at all.
        anonymous = self.client_cls(self.main.app)
        shared = anonymous.get(f"/api/shared/{saved['token']}")
        self.assertEqual((shared.status_code, shared.json().get("shared")), (200, True))

        # A report saved before accounts existed has no owner: admins only.
        self.sql_exec("INSERT INTO marts.saved_reports (brand, payload, token) "
                      "VALUES ('Acme', '{}'::jsonb, 'legacy-token')")
        legacy_id = self.sql("SELECT id FROM marts.saved_reports WHERE token = 'legacy-token'")[0][0]
        self.assertEqual(ann.get(f"/api/reports/saved/{legacy_id}").status_code, 404)
        with unittest.mock.patch.dict(os.environ, {"ADMIN_EMAILS": "ann@example.com"}):
            self.assertEqual(ann.get(f"/api/reports/saved/{legacy_id}").status_code, 200)

    # --- running out of tokens ------------------------------------------------------------

    def test_chat_still_answers_with_no_model_at_all(self):
        """No OpenAI or OpenRouter key: keyword search plus a quoted answer."""
        ann = self.account("ann@example.com")
        ann.post("/api/brands", json={"name": "Acme"})
        self.seed("Acme", ["The delivery was three days late", "Lovely packaging",
                           "Customer support never answered my email"])
        body = ann.post("/chat", json={"question": "What do people say about delivery?",
                                       "brand": "Acme"}).json()
        self.assertEqual(body["evidence"].get("retrieval"), "keyword")
        self.assertTrue(body["generated_by"]["extractive"])
        self.assertIn("delivery was three days late", body["sources"][0]["excerpt"])
        self.assertIn("[1]", body["answer"])

    def test_keyword_search_is_safe_and_falls_back_to_recent(self):
        from reviewbot.api import rag

        self.seed("Acme", ["Refund took a month", "Great refund policy", "Nice colours"])
        hits = rag.keyword_retrieve("refund", brand="Acme")
        self.assertEqual({h.retrieval for h in hits}, {"keyword"})
        self.assertEqual(len(hits), 2)
        # tsquery operators in a question reach Postgres as nothing at all.
        rag.keyword_retrieve("refund' & !(x):* | \\", brand="Acme")
        recent = rag.keyword_retrieve("what do they say about it?", brand="Acme")
        self.assertEqual({h.retrieval for h in recent}, {"recent"})
        self.assertEqual(len(recent), 3)


    # --- reviews saved without a search vector ---------------------------------------

    def raw(self, brand, texts, ratings=None):
        ratings = ratings or [None] * len(texts)
        with self.db.cursor(commit=True) as cur:
            for i, (text, rating) in enumerate(zip(texts, ratings)):
                cur.execute(
                    "INSERT INTO raw.reviews_raw (id, brand, source, source_url, rating, text, "
                    "captured_at) VALUES (%s, %s, 'web', %s, %s, %s, %s)",
                    (f"raw-{brand}-{i}", brand, f"https://example.com/r{i}", rating, text,
                     f"2026-10-{10 + i:02d}T00:00:00"),
                )

    def test_new_reviews_reach_the_dashboard_with_no_embeddings_provider(self):
        """The production failure: an empty OpenAI account left a new brand at 0 reviews."""
        from reviewbot.enrich import run as enrich_run

        ann = self.account("ann@example.com")
        ann.post("/api/brands", json={"name": "Acme"})
        self.raw("Acme", ["The delivery was three days late", "Lovely packaging",
                          "Support never answered"], ratings=[1, 5, 2])
        self.assertEqual(enrich_run.enrich(), 3)
        self.assertEqual(
            self.sql("SELECT count(*) FROM marts.reviews WHERE embedding IS NULL")[0][0], 3)
        stats = ann.get("/api/stats", params={"brand": "Acme"}).json()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["sentiment"]["negative"], 2)
        # Saved, so the next pass has nothing pending.
        self.assertEqual(enrich_run.enrich(), 0)
        body = ann.post("/chat", json={"question": "What about delivery?", "brand": "Acme"}).json()
        self.assertIn("delivery was three days late", body["sources"][0]["excerpt"])

    def test_missing_vectors_are_filled_in_once_a_provider_can_embed(self):
        from reviewbot import embeddings
        from reviewbot.api import rag
        from reviewbot.enrich import run as enrich_run

        self.raw("Acme", ["Great espresso", "Slow delivery"])
        enrich_run.enrich()
        with unittest.mock.patch.object(embeddings, "providers",
                                        lambda: [("openai", embeddings.EMBED_MODEL)]), \
                unittest.mock.patch.object(embeddings, "embed",
                                           side_effect=lambda texts: [[0.5] * 768 for _ in texts]):
            self.assertEqual(enrich_run.enrich(), 0)
        self.assertEqual(
            self.sql("SELECT count(*) FROM marts.reviews WHERE embedding IS NULL")[0][0], 0)
        with unittest.mock.patch.object(embeddings, "embed_one", return_value=[0.5] * 768):
            hits = rag.retrieve("espresso", brand="Acme")
        self.assertEqual([h.retrieval for h in hits], ["vector", "vector"])

    def test_a_vector_for_text_that_changed_meanwhile_is_not_written(self):
        from reviewbot.enrich import run as enrich_run

        self.seed("Acme", ["Original words"])
        read = enrich_run._read_unembedded("")
        self.sql_exec("UPDATE marts.reviews SET text = 'Edited words'")
        enrich_run._write_vectors(read, [[0.5] * 768])
        self.assertEqual(
            self.sql("SELECT count(*) FROM marts.reviews WHERE embedding IS NULL")[0][0], 1)

    def test_chat_also_finds_reviews_still_waiting_for_a_vector(self):
        import json

        from reviewbot import embeddings
        from reviewbot.api import rag

        self.seed("Acme", ["Great espresso shots"])
        self.sql_exec(
            "INSERT INTO marts.reviews (id, brand, source, source_url, text, sentiment, relevant, "
            "captured_at, embedding) VALUES ('vec-1', 'Acme', 'web', 'https://example.com/v', "
            "'Friendly baristas', 'positive', TRUE, '2026-10-01', %s::vector)",
            (json.dumps([0.5] * 768),))
        with unittest.mock.patch.object(embeddings, "embed_one", return_value=[0.5] * 768):
            hits = rag.retrieve("espresso", brand="Acme")
        self.assertEqual([h.retrieval for h in hits], ["vector", "keyword"])
        self.assertEqual(hits[1].text, "Great espresso shots")


if __name__ == "__main__":
    unittest.main()
