"""Brands belong to accounts: add, list, delete, re-run, import, and isolation.

End to end through the real routes, the real middleware and real sign-in, with
the in-memory account store. Collection itself is replaced by a recorder:
these tests are about who may start what, not about scraping.
"""

from __future__ import annotations

import os
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from auth_helpers import isolate_environment, reset_auth_state
from fastapi.testclient import TestClient
from memory_store import MemoryStore

from reviewbot import accounts
from reviewbot.api import main, onboarding

PASSWORD = "correct horse battery"


class BrandApiTestCase(unittest.TestCase):
    def setUp(self):
        isolate_environment(self)
        reset_auth_state()
        self.addCleanup(reset_auth_state)
        os.environ.update({"SIGNUP_ENABLED": "1", "SIGNUP_INVITE_CODE": "", "ADMIN_EMAILS": ""})
        self.store = MemoryStore()
        previous = accounts.set_store(self.store)
        self.addCleanup(accounts.set_store, previous)

        self.started: list[dict] = []

        def fake_start(cfg):
            # Mirror the real single-flight claim so "already running" is real.
            if not onboarding._claim(cfg["name"]):
                return False
            self.started.append(cfg)
            return True

        for target, kwargs in (
            ((main.db, "database_url"), {"return_value": "postgresql://test@localhost/reviewbay_test"}),
            ((onboarding, "start_async"), {"side_effect": fake_start}),
            ((onboarding, "_persist_brand"), {}),
            ((onboarding, "forget_brand"), {"return_value": True}),
            ((onboarding, "RERUN_COOLDOWN_SECONDS"), {"new": 300}),
        ):
            p = unittest.mock.patch.object(*target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        onboarding._JOBS.clear()
        self.addCleanup(onboarding._JOBS.clear)

        self.ann = self.account("ann@example.com")
        self.bob = self.account("bob@example.com")

    def account(self, email: str) -> TestClient:
        client = TestClient(main.app, follow_redirects=False)
        resp = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD})
        self.assertEqual(resp.status_code, 200, resp.text)
        return client

    def add(self, client, name, **extra):
        return client.post("/api/brands", json={"name": name, **extra})

    def names(self, client):
        return client.get("/api/brands").json()["brands"]

    def finish_jobs(self):
        """Mark every claimed job finished, as a completed collection would."""
        for job in onboarding._JOBS.values():
            job.update(status="done", phase="done")


class AddAndListTests(BrandApiTestCase):
    def test_a_searched_brand_stays_in_the_account(self):
        resp = self.add(self.ann, "Blue Bottle", website="bluebottlecoffee.com")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["started"])
        self.assertEqual(self.names(self.ann), ["Blue Bottle"])
        # ...from any browser: a fresh sign-in sees the same list.
        again = TestClient(main.app)
        again.post("/api/auth/login", json={"email": "ann@example.com", "password": PASSWORD})
        self.assertEqual(self.names(again), ["Blue Bottle"])
        detail = again.get("/api/me/brands").json()["brands"][0]
        self.assertEqual(detail["website"], "https://bluebottlecoffee.com/")

    def test_accounts_do_not_see_each_others_brands(self):
        self.add(self.ann, "Acme")
        self.assertEqual(self.names(self.bob), [])
        resp = self.bob.get("/api/stats?brand=Acme")
        # Not even "exists but not yours": that would reveal Ann tracks Acme.
        self.assertEqual((resp.status_code, resp.json()["error"]), (404, "not_in_account"))

    def test_data_endpoints_need_one_of_your_brands(self):
        self.add(self.ann, "Acme")
        with unittest.mock.patch.object(main.stats, "get_stats", return_value={"total": 3}) as got:
            self.assertEqual(self.ann.get("/api/stats?brand=acme").json(), {"total": 3})
        got.assert_called_once_with("Acme")  # the canonical spelling
        self.assertEqual(self.ann.get("/api/stats").status_code, 400)
        for path in ("/api/insights", "/api/report", "/api/trend", "/api/reviews", "/api/runs",
                     "/api/sentiment-alert", "/api/collection-health", "/api/export/reviews.csv"):
            with self.subTest(path=path):
                self.assertEqual(self.ann.get(path + "?brand=Globex").status_code, 404)

    def test_a_second_spelling_joins_the_first(self):
        self.add(self.ann, "Blue Bottle")
        self.finish_jobs()
        resp = self.add(self.bob, "BLUE bottle")
        self.assertEqual(resp.json()["brand"], "Blue Bottle")
        self.assertEqual(self.names(self.bob), ["Blue Bottle"])

    def test_adding_a_brand_that_is_being_collected_does_not_start_another(self):
        self.add(self.ann, "Acme")
        resp = self.add(self.bob, "acme")
        self.assertFalse(resp.json()["started"])
        self.assertEqual(len(self.started), 1)

    def test_adding_a_brand_collected_moments_ago_reuses_that_data(self):
        self.add(self.ann, "Acme")
        self.finish_jobs()
        accounts.mark_collected("Acme")
        onboarding._JOBS.clear()
        resp = self.add(self.bob, "Acme").json()
        self.assertTrue(resp["reused"])
        self.assertFalse(resp["started"])
        self.store.review_counts["acme"] = 42
        status = self.bob.get("/api/brands/Acme/status").json()
        self.assertEqual((status["status"], status["collected"]), ("done", 42))

    def test_the_brand_limit_is_enforced(self):
        with unittest.mock.patch.object(accounts, "MAX_BRANDS_PER_ACCOUNT", 2):
            self.add(self.ann, "One")
            self.add(self.ann, "Two")
            resp = self.add(self.ann, "Three")
        self.assertEqual((resp.status_code, resp.json()["error"]), (409, "brand_limit"))

    def test_an_invalid_website_is_still_rejected_before_anything_is_stored(self):
        resp = self.add(self.ann, "Probe", website="http://169.254.169.254/")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.names(self.ann), [])
        self.assertEqual(self.started, [])


class DeleteTests(BrandApiTestCase):
    def test_deleting_removes_the_brand_from_the_account(self):
        self.add(self.ann, "Acme")
        resp = self.ann.delete("/api/brands/Acme")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.names(self.ann), [])
        self.assertTrue(resp.json()["refresh_stopped"])
        onboarding.forget_brand.assert_called_once_with("Acme")

    def test_deleting_a_brand_someone_else_tracks_keeps_it_refreshing(self):
        self.add(self.ann, "Acme")
        self.add(self.bob, "Acme")
        resp = self.ann.delete("/api/brands/acme").json()
        self.assertFalse(resp["refresh_stopped"])
        self.assertEqual(self.names(self.bob), ["Acme"])
        onboarding.forget_brand.assert_not_called()

    def test_you_cannot_delete_a_brand_you_do_not_track(self):
        self.add(self.ann, "Acme")
        self.assertEqual(self.bob.delete("/api/brands/Acme").status_code, 404)
        self.assertEqual(self.names(self.ann), ["Acme"])

    def test_the_reviews_survive_and_re_adding_is_instant(self):
        self.add(self.ann, "Acme")
        self.finish_jobs()
        self.store.review_counts["acme"] = 12
        self.ann.delete("/api/brands/Acme")
        self.assertFalse(accounts.get_brand("Acme")["active"])
        self.add(self.ann, "Acme")
        detail = self.ann.get("/api/me/brands").json()["brands"][0]
        self.assertEqual(detail["reviews"], 12)
        self.assertTrue(accounts.get_brand("Acme")["active"])


class RerunTests(BrandApiTestCase):
    def setUp(self):
        super().setUp()
        self.add(self.ann, "Acme")
        self.finish_jobs()
        onboarding._JOBS.clear()
        self.started.clear()

    def test_rerun_starts_a_collection(self):
        resp = self.ann.post("/api/brands/Acme/rerun")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["started"])
        self.assertEqual([c["name"] for c in self.started], ["Acme"])
        self.assertIsNotNone(accounts.get_brand("Acme")["last_rerun_at"])

    def test_a_rerun_while_running_attaches_instead_of_starting_another(self):
        self.ann.post("/api/brands/Acme/rerun")
        resp = self.ann.post("/api/brands/Acme/rerun").json()
        self.assertEqual((resp["started"], resp["already_running"]), (False, True))
        self.assertEqual(len(self.started), 1)

    def test_reruns_are_held_to_a_cooldown(self):
        self.ann.post("/api/brands/Acme/rerun")
        self.finish_jobs()
        resp = self.ann.post("/api/brands/Acme/rerun")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"], "cooldown")
        self.assertGreater(int(resp.headers["retry-after"]), 0)

    def test_the_cooldown_survives_a_restart(self):
        # No in-memory job at all: only the registry's durable timestamp.
        accounts.mark_collected("Acme")
        resp = self.ann.post("/api/brands/Acme/rerun")
        self.assertEqual(resp.status_code, 429)

    def test_an_old_collection_does_not_block_a_rerun(self):
        self.store.brands["acme"]["last_collected_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertEqual(self.ann.post("/api/brands/Acme/rerun").status_code, 200)

    def test_you_cannot_rerun_someone_elses_brand(self):
        self.assertEqual(self.bob.post("/api/brands/Acme/rerun").status_code, 404)
        self.assertEqual(self.started, [])

    def test_the_brand_list_reports_running_and_cooldown(self):
        self.ann.post("/api/brands/Acme/rerun")
        row = self.ann.get("/api/me/brands").json()["brands"][0]
        self.assertTrue(row["running"])
        self.finish_jobs()
        row = self.ann.get("/api/me/brands").json()["brands"][0]
        self.assertFalse(row["running"])
        self.assertGreater(row["cooldown_seconds"], 0)


class ImportTests(BrandApiTestCase):
    def test_browser_brands_are_imported_without_collecting(self):
        resp = self.ann.post("/api/me/brands/import",
                             json={"names": ["Acme", "Globex", "  ", "acme"]}).json()
        self.assertEqual(resp["imported"], ["Acme", "Globex", "Acme"])
        self.assertEqual(len(resp["skipped"]), 1)
        self.assertEqual(sorted(self.names(self.ann)), ["Acme", "Globex"])
        self.assertEqual(self.started, [])

    def test_import_respects_the_brand_limit(self):
        with unittest.mock.patch.object(accounts, "MAX_BRANDS_PER_ACCOUNT", 1):
            resp = self.ann.post("/api/me/brands/import", json={"names": ["A", "B"]}).json()
        self.assertEqual(resp["imported"], ["A"])
        self.assertEqual(resp["skipped"][0]["name"], "B")

    def test_import_is_bounded(self):
        resp = self.ann.post("/api/me/brands/import", json={"names": [f"b{i}" for i in range(100)]})
        self.assertEqual(resp.status_code, 422)


class ChatScopeTests(BrandApiTestCase):
    def test_chat_only_uses_your_brands(self):
        self.add(self.ann, "Acme")
        self.add(self.bob, "Globex")
        captured = {}

        def fake_answer(question, brand=None, brands=None):
            captured.update(brand=brand, brands=brands)
            return main.rag.Answer("ok", [])

        with unittest.mock.patch.object(main.rag, "answer", side_effect=fake_answer):
            self.ann.post("/chat", json={"question": "compare", "brands": ["Acme", "Globex"]})
        # Globex is Bob's: naming it does not pull it into Ann's answer.
        self.assertEqual(captured["brands"], ["Acme"])

    def test_all_my_brands_means_mine(self):
        self.add(self.ann, "Acme")
        self.add(self.ann, "Initech")
        with unittest.mock.patch.object(main.rag, "answer",
                                        return_value=main.rag.Answer("ok", [])) as answer:
            self.ann.post("/chat", json={"question": "what do people say?"})
        self.assertEqual(sorted(answer.call_args.kwargs["brands"]), ["Acme", "Initech"])

    def test_no_brands_means_no_search(self):
        with unittest.mock.patch.object(main.rag, "answer",
                                        side_effect=AssertionError("searched")):
            resp = self.ann.post("/chat", json={"question": "what do people say?"})
        self.assertIn("Add a brand first", resp.json()["answer"])

    def test_a_named_brand_must_be_yours(self):
        self.add(self.bob, "Globex")
        resp = self.ann.post("/chat", json={"question": "hi", "brand": "Globex"})
        self.assertEqual(resp.status_code, 404)


class SavedReportTests(BrandApiTestCase):
    def test_saved_reports_are_listed_for_their_owner_only(self):
        self.add(self.ann, "Acme")
        with unittest.mock.patch.object(main.report, "list_saved", return_value=[]) as listed:
            self.ann.get("/api/reports/saved?brand=Acme")
        owner = listed.call_args.kwargs["owner_id"]
        self.assertEqual(owner, self.store.user_by_email("ann@example.com")[0].id)
        self.assertFalse(listed.call_args.kwargs["is_admin"])

    def test_someone_elses_saved_report_is_a_plain_404(self):
        with unittest.mock.patch.object(main.report, "get_saved", return_value=None):
            resp = self.ann.get("/api/reports/saved/1")
        self.assertEqual((resp.status_code, resp.json()["error"]), (404, "not_found"))

    def test_saving_records_the_owner(self):
        self.add(self.ann, "Acme")
        with unittest.mock.patch.object(main.report, "save_report",
                                        return_value={"ok": True}) as saved:
            self.ann.post("/api/report/save?brand=Acme")
        self.assertEqual(saved.call_args.args, ("Acme",))
        self.assertIn("owner_id", saved.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
