"""scripts/copy_database.py against REAL Postgres: two databases, old and new.

Skipped unless REVIEWBAY_TEST_DATABASE_URL points at a server where the user may
CREATE DATABASE (a local pgserver instance qualifies). The test creates and drops
its own pair of databases next to that one; it never touches the named database.

The "old" database gets only ddl.sql, like a Neon database that never ran
migration 002, so the copy has to cope with the target having more columns and
tables than the source.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import random
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
TEST_DB_URL = os.environ.get("REVIEWBAY_TEST_DATABASE_URL", "").strip()


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"reviewbay_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_db(url: str, db: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + db))


@unittest.skipUnless(TEST_DB_URL, "REVIEWBAY_TEST_DATABASE_URL not set")
class CopyDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        base = urlsplit(TEST_DB_URL).path.lstrip("/") or "reviewbay_test"
        if "test" not in base:
            raise unittest.SkipTest("refusing to create databases next to a non-test database")
        cls.src_name, cls.dst_name = f"{base}_copy_src", f"{base}_copy_dst"
        cls.admin = _with_db(TEST_DB_URL, "postgres")
        try:
            with psycopg.connect(cls.admin, autocommit=True) as conn:
                for name in (cls.src_name, cls.dst_name):
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                    conn.execute(f'CREATE DATABASE "{name}"')
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"cannot create test databases here: {type(exc).__name__}")
        cls.src, cls.dst = _with_db(TEST_DB_URL, cls.src_name), _with_db(TEST_DB_URL, cls.dst_name)
        cls.migrate, cls.copy = _load("migrate"), _load("copy_database")
        cls.run_migrate(cls.src, ["--only", "ddl.sql"])   # the old, pre-002 schema
        cls.run_migrate(cls.dst, [])                      # everything

    @classmethod
    def tearDownClass(cls):
        import psycopg

        from reviewbot import db

        db.close_idle()
        with psycopg.connect(cls.admin, autocommit=True) as conn:
            for name in (cls.src_name, cls.dst_name):
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    @classmethod
    def run_migrate(cls, url, argv):
        from reviewbot import db

        db.close_idle()
        with unittest.mock.patch.dict(os.environ, {"DATABASE_URL": url}), \
                contextlib.redirect_stdout(io.StringIO()):
            assert cls.migrate.main(argv) == 0

    def setUp(self):
        import psycopg

        for url in (self.src, self.dst):
            with psycopg.connect(url) as conn:
                conn.execute("TRUNCATE raw.reviews_raw, marts.reviews, marts.saved_reports, "
                             "marts.scrape_runs, airbyte.app_brand_map RESTART IDENTITY")
        self.vector = [round(random.uniform(-1, 1), 6) for _ in range(768)]
        with psycopg.connect(self.src) as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO raw.reviews_raw (id, brand, source, text, captured_at) "
                    "VALUES (%s, 'Acme', 'web', %s, '2026-10-01T00:00:00')", (f"r{i}", f"review {i}"))
                conn.execute(
                    "INSERT INTO marts.reviews (id, brand, source, text, sentiment, embedding, relevant) "
                    "VALUES (%s, 'Acme', 'web', %s, 'positive', %s::vector, TRUE)",
                    (f"r{i}", f"review {i}", str(self.vector)))
            # Ids with gaps, as a real table has after deletions.
            conn.execute("INSERT INTO marts.saved_reports (id, brand, payload, token) "
                         "VALUES (5, 'Acme', '{}'::jsonb, 'tok-a'), (9, 'Acme', '{}'::jsonb, 'tok-b')")
            conn.execute("INSERT INTO marts.scrape_runs (brand, sources, reviews_found, status, "
                         "started_at, finished_at) VALUES ('Acme', 'web', 3, 'ok', now(), now())")
            conn.execute("INSERT INTO airbyte.app_brand_map (app_id, brand) VALUES ('123', 'Acme')")

    def run_copy(self, *argv, source=None, target=None):
        out = io.StringIO()
        env = {"SOURCE_DATABASE_URL": source or self.src, "DATABASE_URL": target or self.dst}
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = self.copy.main(list(argv))
        return code, out.getvalue()

    def scalar(self, url, query):
        import psycopg

        with psycopg.connect(url) as conn:
            return conn.execute(query).fetchone()[0]

    def test_everything_is_copied_from_an_older_schema(self):
        code, out = self.run_copy()
        self.assertEqual(code, 0, out)
        for table, expected in (("raw.reviews_raw", 3), ("marts.reviews", 3),
                                ("marts.saved_reports", 2), ("marts.scrape_runs", 1),
                                ("airbyte.app_brand_map", 1)):
            with self.subTest(table=table):
                self.assertEqual(self.scalar(self.dst, f"SELECT COUNT(*) FROM {table}"), expected)
        # The source never had 002's columns; the target fills them with defaults.
        self.assertIn("defaults for", out)
        self.assertNotIn("FAILED", out)

    def test_embeddings_survive_the_copy_exactly(self):
        self.run_copy()
        same = self.scalar(self.dst,
                           f"SELECT embedding = '{self.vector}'::vector FROM marts.reviews WHERE id = 'r0'")
        self.assertTrue(same)
        # ...and are searchable on the new server.
        nearest = self.scalar(self.dst,
                              f"SELECT id FROM marts.reviews ORDER BY embedding <=> '{self.vector}'::vector LIMIT 1")
        self.assertIn(nearest, ("r0", "r1", "r2"))

    def test_sequences_move_past_the_copied_ids(self):
        import psycopg

        self.run_copy()
        with psycopg.connect(self.dst) as conn:
            new_id = conn.execute("INSERT INTO marts.saved_reports (brand, payload) "
                                  "VALUES ('Acme', '{}'::jsonb) RETURNING id").fetchone()[0]
        self.assertEqual(new_id, 10)  # after the copied 5 and 9, not a collision at 1

    def test_a_second_run_copies_only_what_is_missing(self):
        import psycopg

        self.run_copy()
        with psycopg.connect(self.src) as conn:  # a straggler written during the switch
            conn.execute("INSERT INTO marts.reviews (id, brand, source, text) "
                         "VALUES ('late', 'Acme', 'web', 'arrived during the move')")
        code, out = self.run_copy()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.scalar(self.dst, "SELECT COUNT(*) FROM marts.reviews"), 4)
        # A table with no unique key is not copied twice.
        self.assertEqual(self.scalar(self.dst, "SELECT COUNT(*) FROM airbyte.app_brand_map"), 1)
        self.assertIn("no unique key", out)

    def test_nothing_on_the_target_is_overwritten(self):
        import psycopg

        with psycopg.connect(self.dst) as conn:
            conn.execute("INSERT INTO marts.reviews (id, brand, source, text) "
                         "VALUES ('r0', 'Acme', 'web', 'the target''s own version')")
        self.run_copy()
        self.assertEqual(self.scalar(self.dst, "SELECT text FROM marts.reviews WHERE id = 'r0'"),
                         "the target's own version")

    def test_the_source_is_never_written(self):
        before = self.scalar(self.src, "SELECT COUNT(*) FROM marts.reviews")
        self.run_copy()
        self.assertEqual(self.scalar(self.src, "SELECT COUNT(*) FROM marts.reviews"), before)
        self.assertIsNone(self.scalar(self.src, "SELECT to_regclass('app.users')"))

    def test_a_dry_run_changes_nothing(self):
        code, out = self.run_copy("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.scalar(self.dst, "SELECT COUNT(*) FROM marts.reviews"), 0)

    def test_the_same_database_is_refused(self):
        code, out = self.run_copy(target=self.src)
        self.assertEqual(code, 2)
        self.assertIn("same database", out)

    def test_no_password_is_printed(self):
        _code, out = self.run_copy()
        for url in (self.src, self.dst):
            password = urlsplit(url).password
            if password:
                self.assertNotIn(password, out)
        self.assertNotIn("postgresql://", out)


if __name__ == "__main__":
    unittest.main()
