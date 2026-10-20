"""One behavioural contract for account storage, run against two backends.

* `MemoryAccountsTests` always runs, against `tests/memory_store.py`.
* `PostgresAccountsTests` runs against a REAL database when
  REVIEWBAY_TEST_DATABASE_URL is set, and is skipped otherwise.

The point of running the same tests twice is that the default suite has to work
without a database, so the API tests use the in-memory store — and a fake that
behaves differently from production would make those tests pass while the real
thing fails. Holding both to one contract is what makes the fake trustworthy.

The Postgres run TRUNCATEs the `app` tables between tests, so it refuses any
database whose name does not look like a throwaway one.

    # e.g. against a local pgserver instance:
    REVIEWBAY_TEST_DATABASE_URL=postgresql://postgres:@127.0.0.1:54451/reviewbay_test \
        pytest tests/test_accounts_contract.py
"""

from __future__ import annotations

import os
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from memory_store import MemoryStore

from reviewbot import accounts

TEST_DB_URL = os.environ.get("REVIEWBAY_TEST_DATABASE_URL", "").strip()


class AccountsContract:
    """Mixed into a TestCase that provides make_store() and seed_reviews()."""

    def make_store(self) -> accounts.Store:  # pragma: no cover - abstract
        raise NotImplementedError

    def seed_reviews(self, brand: str, n: int) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.store = self.make_store()
        self._previous = accounts.set_store(self.store)
        self.addCleanup(accounts.set_store, self._previous)
        env = unittest.mock.patch.dict(
            os.environ,
            {"SIGNUP_ENABLED": "1", "SIGNUP_INVITE_CODE": "", "ADMIN_EMAILS": ""},
        )
        env.start()
        self.addCleanup(env.stop)

    # --- registration and login ------------------------------------------------

    def test_register_then_authenticate(self):
        user = accounts.register("Ann@Example.com", "correct horse battery")
        self.assertEqual(user.email, "Ann@Example.com")
        again = accounts.authenticate("ann@example.com", "correct horse battery")
        self.assertEqual(again.id, user.id)

    def test_email_is_unique_regardless_of_case(self):
        accounts.register("ann@example.com", "correct horse battery")
        with self.assertRaises(accounts.EmailTaken):
            accounts.register("  ANN@example.COM ", "another long password")

    def test_wrong_password_and_unknown_email_fail_identically(self):
        accounts.register("ann@example.com", "correct horse battery")
        with self.assertRaises(accounts.InvalidCredentials) as wrong:
            accounts.authenticate("ann@example.com", "wrong password here")
        with self.assertRaises(accounts.InvalidCredentials) as unknown:
            accounts.authenticate("nobody@example.com", "correct horse battery")
        self.assertEqual(str(wrong.exception), str(unknown.exception))

    def test_password_is_not_stored_in_plain_text(self):
        accounts.register("ann@example.com", "correct horse battery")
        _user, stored = self.store.user_by_email("ann@example.com")
        self.assertNotIn("correct horse battery", stored)
        self.assertTrue(stored.startswith("scrypt$"))

    def test_an_outdated_hash_is_upgraded_on_the_next_successful_login(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        with unittest.mock.patch.object(accounts, "_SCRYPT_N", 2**12):
            weak = accounts.hash_password("correct horse battery")
        self.store.set_password_hash(user.id, weak)
        self.assertTrue(accounts.needs_rehash(weak))
        accounts.authenticate("ann@example.com", "correct horse battery")
        _u, upgraded = self.store.user_by_email("ann@example.com")
        self.assertFalse(accounts.needs_rehash(upgraded))
        self.assertTrue(accounts.verify_password("correct horse battery", upgraded))

    # --- sessions ----------------------------------------------------------------

    def test_a_session_resolves_to_its_user_until_it_is_ended(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        token = accounts.start_session(user)
        self.assertEqual(accounts.session_user(token), user)
        accounts.end_session(token)
        self.assertIsNone(accounts.session_user(token))

    def test_only_the_token_hash_is_stored(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        token = accounts.start_session(user)
        self.assertIsNone(self.store.session(token, datetime.now(timezone.utc)))
        self.assertIsNotNone(
            self.store.session(accounts.token_hash(token), datetime.now(timezone.utc))
        )

    def test_an_expired_session_is_rejected_and_cleaned_up(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        old = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.create_session(accounts.token_hash("stale-token"), user.id, old)
        self.assertIsNone(accounts.session_user("stale-token"))
        self.assertGreaterEqual(self.store.delete_expired_sessions(datetime.now(timezone.utc)), 1)

    def test_an_unknown_or_absurd_token_is_rejected(self):
        self.assertIsNone(accounts.session_user(None))
        self.assertIsNone(accounts.session_user("no-such-token"))
        self.assertIsNone(accounts.session_user("x" * 5000))

    def test_a_used_session_slides_its_expiry_forward(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        hashed = accounts.token_hash("sliding-token")
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        self.store.create_session(hashed, user.id, soon)
        # Pretend the session was last seen two days ago.
        self.store.extend_session(hashed, soon, datetime.now(timezone.utc) - timedelta(days=2))
        self.assertEqual(accounts.session_user("sliding-token"), user)
        _u, expires, _seen = self.store.session(hashed, datetime.now(timezone.utc))
        self.assertGreater(expires, soon + timedelta(days=1))

    # --- brands ----------------------------------------------------------------

    def _cfg(self, name, website=None):
        return {"name": name, "keywords": [name], "sources": ["web"], "limit": 50,
                "website": website}

    def test_adding_a_brand_registers_it_and_makes_the_account_a_member(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        canonical, added = accounts.add_brand(user, self._cfg("Blue Bottle"))
        self.assertTrue(added)
        self.assertEqual(canonical["name"], "Blue Bottle")
        self.assertEqual(accounts.brand_keys(user), ["blue bottle"])
        self.assertTrue(accounts.get_brand("BLUE BOTTLE")["active"])

    def test_the_first_spelling_of_a_brand_is_canonical_for_everyone(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        bob = accounts.register("bob@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Blue Bottle"))
        # Review ids hash the brand name case-sensitively, so a second spelling
        # collected separately would duplicate every review.
        canonical, _added = accounts.add_brand(bob, self._cfg("blue BOTTLE"))
        self.assertEqual(canonical["name"], "Blue Bottle")

    def test_a_missing_website_is_filled_in_but_an_existing_one_is_kept(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        bob = accounts.register("bob@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme"))
        accounts.add_brand(bob, self._cfg("Acme", "https://acme.example"))
        self.assertEqual(accounts.get_brand("acme")["website"], "https://acme.example")
        accounts.add_brand(ann, self._cfg("Acme", "https://other.example"))
        self.assertEqual(accounts.get_brand("acme")["website"], "https://acme.example")

    def test_adding_the_same_brand_twice_is_not_a_second_membership(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        accounts.add_brand(user, self._cfg("Acme"))
        _c, added = accounts.add_brand(user, self._cfg("ACME"))
        self.assertFalse(added)
        self.assertEqual(accounts.brand_keys(user), ["acme"])

    def test_memberships_carry_the_canonical_name(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        bob = accounts.register("bob@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Blue Bottle"))
        accounts.add_brand(bob, self._cfg("BLUE bottle"))
        self.assertEqual(accounts.memberships(bob), [("blue bottle", "Blue Bottle")])

    def test_brands_are_listed_newest_first(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        for name in ("First", "Second", "Third"):
            accounts.add_brand(user, self._cfg(name))
        self.assertEqual(accounts.brand_keys(user), ["third", "second", "first"])

    def test_the_brand_limit_applies_to_new_brands_only(self):
        user = accounts.register("ann@example.com", "correct horse battery")
        with unittest.mock.patch.object(accounts, "MAX_BRANDS_PER_ACCOUNT", 2):
            accounts.add_brand(user, self._cfg("One"))
            accounts.add_brand(user, self._cfg("Two"))
            with self.assertRaises(accounts.BrandLimitReached):
                accounts.add_brand(user, self._cfg("Three"))
            # Re-adding one you already have is not a new brand.
            accounts.add_brand(user, self._cfg("one"))

    def test_removing_a_brand_others_track_does_not_deactivate_it(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        bob = accounts.register("bob@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme"))
        accounts.add_brand(bob, self._cfg("Acme"))
        result = accounts.remove_brand(ann, "ACME")
        self.assertEqual((result["removed"], result["orphaned"]), (True, False))
        self.assertTrue(accounts.get_brand("acme")["active"])
        self.assertEqual(accounts.brand_keys(ann), [])
        self.assertEqual(accounts.brand_keys(bob), ["acme"])

    def test_removing_the_last_tracker_deactivates_the_brand(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme"))
        result = accounts.remove_brand(ann, "Acme")
        self.assertTrue(result["orphaned"])
        self.assertFalse(accounts.get_brand("acme")["active"])
        self.assertNotIn("acme", [b["brand_key"] for b in accounts.registry_brands()])
        self.assertIn(
            "acme", [b["brand_key"] for b in accounts.registry_brands(include_inactive=True)]
        )

    def test_re_adding_a_deactivated_brand_reactivates_it(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme"))
        accounts.remove_brand(ann, "Acme")
        accounts.add_brand(ann, self._cfg("acme"))
        self.assertTrue(accounts.get_brand("acme")["active"])

    def test_removing_a_brand_you_do_not_track_changes_nothing(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        bob = accounts.register("bob@example.com", "correct horse battery")
        accounts.add_brand(bob, self._cfg("Acme"))
        result = accounts.remove_brand(ann, "Acme")
        self.assertEqual((result["removed"], result["orphaned"]), (False, False))
        self.assertTrue(accounts.get_brand("acme")["active"])

    def test_a_membership_needs_an_existing_brand(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        with self.assertRaises(Exception):  # FK violation / KeyError
            self.store.add_membership(ann.id, "never-registered")

    def test_collection_timestamps_are_recorded(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme"))
        self.assertIsNone(accounts.get_brand("acme")["last_collected_at"])
        accounts.mark_collected("Acme")
        accounts.mark_rerun("Acme")
        brand = accounts.get_brand("acme")
        self.assertIsNotNone(brand["last_collected_at"])
        self.assertIsNotNone(brand["last_rerun_at"])
        with self.assertRaises(ValueError):
            self.store.stamp_brand("acme", "name; DROP TABLE app.users")

    def test_account_brands_reports_review_counts(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme"))
        accounts.add_brand(ann, self._cfg("Globex"))
        self.seed_reviews("Acme", 3)
        rows = {b["name"]: b for b in accounts.account_brands(ann)}
        self.assertEqual(rows["Acme"]["reviews"], 3)
        self.assertEqual(rows["Globex"]["reviews"], 0)

    def test_registry_rows_convert_to_worker_brand_config(self):
        ann = accounts.register("ann@example.com", "correct horse battery")
        accounts.add_brand(ann, self._cfg("Acme", "https://acme.example"))
        cfg = accounts.as_brand_config(accounts.registry_brands()[0])
        self.assertEqual(cfg["name"], "Acme")
        self.assertEqual(cfg["sources"], ["web"])
        self.assertEqual(cfg["website"], "https://acme.example")
        self.assertEqual(cfg["limit"], 50)


class MemoryAccountsTests(AccountsContract, unittest.TestCase):
    def make_store(self):
        return MemoryStore()

    def seed_reviews(self, brand, n):
        self.store.review_counts[accounts.brand_key(brand)] = n


@unittest.skipUnless(TEST_DB_URL, "REVIEWBAY_TEST_DATABASE_URL not set")
class PostgresAccountsTests(AccountsContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]
        if not any(word in name for word in ("test", "verify", "scratch")):
            raise unittest.SkipTest(
                f"refusing to TRUNCATE tables in {name!r}: use a throwaway database "
                "whose name contains 'test'"
            )
        cls._env = unittest.mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DB_URL})
        cls._env.start()
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "reviewbay_migrate_for_tests",
            Path(__file__).resolve().parent.parent / "scripts" / "migrate.py",
        )
        migrate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrate)
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            code = migrate.main([])
        if code != 0:
            raise RuntimeError(f"migrations failed against the test database (exit {code})")

    @classmethod
    def tearDownClass(cls):
        from reviewbot import db

        db.close_idle()
        cls._env.stop()

    def make_store(self):
        from reviewbot import db

        with db.cursor(commit=True) as cur:
            cur.execute(
                "TRUNCATE app.user_brands, app.sessions, app.brands, app.users "
                "RESTART IDENTITY CASCADE"
            )
            cur.execute("DELETE FROM marts.reviews WHERE id LIKE 'contract-test-%%'")
        return accounts.PostgresStore()

    def seed_reviews(self, brand, n):
        from reviewbot import db

        with db.cursor(commit=True) as cur:
            for i in range(n):
                cur.execute(
                    "INSERT INTO marts.reviews (id, brand, source, text, relevant) "
                    "VALUES (%s, %s, 'web', %s, TRUE)",
                    (f"contract-test-{brand}-{i}", brand, f"review {i} of {brand}"),
                )


if __name__ == "__main__":
    unittest.main()
