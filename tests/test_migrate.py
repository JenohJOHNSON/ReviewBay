"""Tests for the migration runner (`scripts/migrate.py`).

What these tests can and cannot establish, stated plainly because it matters
for how much the runner should be trusted:

* They DO verify the ordering, the checksumming, the destructive-SQL guard, the
  fact that the guard runs before a connection is opened, and that no
  connection string reaches stdout.
* They do NOT verify that `ddl.sql` and `002_scraping_engines.sql` execute
  against a real Postgres, because no reachable database exists in this
  environment. The SQL itself is unverified; the runner around it is not.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_migrate():
    """Import scripts/migrate.py, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location(
        "reviewbay_migrate", ROOT / "scripts" / "migrate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migrate = _load_migrate()


class DiscoveryTests(unittest.TestCase):
    def test_base_schema_is_ordered_before_numbered_migrations(self):
        names = [m.name for m in migrate.discover()]
        self.assertIn("ddl.sql", names)
        self.assertIn("002_scraping_engines.sql", names)
        # Sorting by filename alone would put 002 first and then create tables
        # that ddl.sql's schemas do not exist for yet.
        self.assertLess(names.index("ddl.sql"), names.index("002_scraping_engines.sql"))

    def test_numbered_files_are_ordered_numerically_not_lexically(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("ddl.sql", "002_b.sql", "010_c.sql", "009_d.sql"):
            (tmp / name).write_text("SELECT 1;", encoding="utf-8")
        with unittest.mock.patch.object(migrate, "MIGRATIONS_DIR", tmp):
            names = [m.name for m in migrate.discover()]
        # Lexical ordering would give 002, 010, 009.
        self.assertEqual(names, ["ddl.sql", "002_b.sql", "009_d.sql", "010_c.sql"])

    def test_unrecognised_sql_files_are_reported_and_skipped(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "ddl.sql").write_text("SELECT 1;", encoding="utf-8")
        (tmp / "scratch.sql").write_text("SELECT 1;", encoding="utf-8")
        with unittest.mock.patch.object(migrate, "MIGRATIONS_DIR", tmp):
            names = [m.name for m in migrate.discover()]
        self.assertEqual(names, ["ddl.sql"])

    def test_checksum_changes_with_content(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = tmp / "ddl.sql"
        path.write_text("SELECT 1;", encoding="utf-8")
        first = migrate.Migration(path).checksum
        path.write_text("SELECT 2;", encoding="utf-8")
        self.assertNotEqual(first, migrate.Migration(path).checksum)


class DestructiveGuardTests(unittest.TestCase):
    def _findings(self, sql: str) -> list[str]:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = tmp / "ddl.sql"
        path.write_text(sql, encoding="utf-8")
        return migrate.destructive_findings(migrate.Migration(path))

    def test_the_repositorys_own_migrations_are_not_destructive(self):
        # The whole point of 002 being additive. If this ever fails, the
        # migration has started dropping or retyping something.
        for m in migrate.discover():
            with self.subTest(migration=m.name):
                self.assertEqual(migrate.destructive_findings(m), [])

    def test_drop_table_is_caught(self):
        self.assertTrue(self._findings("DROP TABLE marts.reviews;"))

    def test_truncate_is_caught(self):
        self.assertTrue(self._findings("TRUNCATE marts.reviews;"))

    def test_delete_from_is_caught(self):
        self.assertTrue(self._findings("DELETE FROM marts.reviews WHERE 1=1;"))

    def test_column_type_change_is_caught(self):
        self.assertTrue(
            self._findings("ALTER TABLE marts.reviews ALTER COLUMN rating TYPE integer;")
        )

    def test_rename_is_caught(self):
        self.assertTrue(self._findings("ALTER TABLE marts.reviews RENAME TO old_reviews;"))

    def test_drop_inside_a_line_comment_is_not_caught(self):
        # 002's own commentary discusses what it does NOT do, so a guard that
        # read comments would refuse the migration it is meant to allow.
        self.assertEqual(self._findings("-- we never DROP TABLE anything\nSELECT 1;"), [])

    def test_drop_inside_a_block_comment_is_not_caught(self):
        self.assertEqual(self._findings("/* DROP TABLE x; */ SELECT 1;"), [])

    def test_drop_inside_a_string_literal_is_not_caught(self):
        self.assertEqual(
            self._findings("INSERT INTO notes (body) VALUES ('DROP TABLE x');"), []
        )

    def test_create_index_is_not_mistaken_for_a_drop(self):
        self.assertEqual(
            self._findings("CREATE INDEX IF NOT EXISTS a_idx ON marts.reviews (brand);"), []
        )

    def test_add_column_is_not_destructive(self):
        self.assertEqual(
            self._findings("ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS x text;"),
            [],
        )


class SafeTargetTests(unittest.TestCase):
    def test_password_is_not_returned(self):
        url = "postgresql://user:sup3rs3cret@ep-x.neon.tech/reviewbay?sslmode=require"
        target = migrate.safe_target(url)
        self.assertNotIn("sup3rs3cret", target)
        self.assertNotIn("user", target)
        self.assertEqual(target, "ep-x.neon.tech/reviewbay")

    def test_url_without_credentials_still_yields_a_host(self):
        self.assertEqual(
            migrate.safe_target("postgresql://localhost:5432/reviewbay"),
            "localhost:5432/reviewbay",
        )

    def test_password_containing_an_at_sign_is_still_stripped(self):
        url = "postgresql://user:p@ssw@rd@host.example/db"
        target = migrate.safe_target(url)
        self.assertEqual(target, "host.example/db")
        self.assertNotIn("ssw", target)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(str(sql).split()), params))
        if self.conn.fail_on and self.conn.fail_on in str(sql):
            raise RuntimeError("syntax error at or near something")
        self._last_select = "SELECT filename, checksum" in str(sql)

    def fetchall(self):
        return list(self.conn.ledger.items()) if getattr(self, "_last_select", False) else []


class FakeConnection:
    """Just enough psycopg surface for the runner's transaction handling."""

    def __init__(self, ledger=None, fail_on=None):
        self.ledger = dict(ledger or {})
        self.fail_on = fail_on
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        # Mirror the real INSERT ... ON CONFLICT so a second run sees it applied.
        for sql, params in self.executed:
            if "INSERT INTO public.reviewbay_migrations" in sql and params:
                self.ledger[params[0]] = params[1]

    def rollback(self):
        self.rollbacks += 1

    def applied_sql(self) -> list[str]:
        return [sql for sql, _ in self.executed]


class LedgerTests(unittest.TestCase):
    """Exercises the plan/apply loop against a fake connection."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.tmp / "ddl.sql").write_text("CREATE SCHEMA IF NOT EXISTS raw;", encoding="utf-8")
        (self.tmp / "002_x.sql").write_text(
            "CREATE INDEX IF NOT EXISTS a_idx ON raw.t (b);", encoding="utf-8"
        )

    def _run(self, argv, conn):
        import contextlib
        import io
        import os

        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                unittest.mock.patch.dict(
                    os.environ, {"DATABASE_URL": "postgresql://u:p@h/db"}, clear=False
                )
            )
            stack.enter_context(unittest.mock.patch.object(migrate, "MIGRATIONS_DIR", self.tmp))
            stack.enter_context(unittest.mock.patch("reviewbot.db.connect", lambda **_k: conn))
            with contextlib.redirect_stdout(buf):
                code = migrate.main(argv)
        return code, buf.getvalue()

    def test_pending_migrations_are_applied_and_recorded(self):
        conn = FakeConnection()
        code, out = self._run([], conn)
        self.assertEqual(code, 0)
        self.assertIn("Applied 2 file(s)", out)
        self.assertEqual(sorted(conn.ledger), ["002_x.sql", "ddl.sql"])
        joined = " ".join(conn.applied_sql())
        self.assertIn("CREATE SCHEMA IF NOT EXISTS raw;", joined)
        self.assertIn("CREATE INDEX IF NOT EXISTS a_idx", joined)

    def test_dry_run_changes_nothing(self):
        conn = FakeConnection()
        code, out = self._run(["--dry-run"], conn)
        self.assertEqual(code, 0)
        self.assertIn("would apply 2 file(s)", out)
        self.assertEqual(conn.ledger, {})
        self.assertNotIn("CREATE SCHEMA IF NOT EXISTS raw;", " ".join(conn.applied_sql()))

    def test_already_applied_migrations_are_skipped(self):
        ledger = {m.name: m.checksum for m in self._discover()}
        conn = FakeConnection(ledger=ledger)
        code, out = self._run([], conn)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to do", out)

    def test_force_reapplies_an_applied_migration(self):
        ledger = {m.name: m.checksum for m in self._discover()}
        conn = FakeConnection(ledger=ledger)
        code, out = self._run(["--force"], conn)
        self.assertEqual(code, 0)
        self.assertIn("Applied 2 file(s)", out)

    def test_a_changed_file_is_flagged_and_not_rerun_without_force(self):
        ledger = {"ddl.sql": "0000000000000000", "002_x.sql": "1111111111111111"}
        conn = FakeConnection(ledger=ledger)
        code, out = self._run([], conn)
        self.assertEqual(code, 0)
        self.assertIn("file changed since it was applied", out)
        self.assertIn("pass --force", out)
        self.assertIn("Nothing to do", out)

    def test_a_failure_rolls_back_and_stops_before_the_next_file(self):
        conn = FakeConnection(fail_on="CREATE SCHEMA")
        code, out = self._run([], conn)
        self.assertEqual(code, 1)
        self.assertEqual(conn.rollbacks, 1)
        self.assertIn("FAILED", out)
        self.assertIn("rolled back", out)
        # The second migration must not be attempted after the first failed.
        self.assertNotIn("CREATE INDEX IF NOT EXISTS a_idx", " ".join(conn.applied_sql()))
        self.assertEqual(conn.ledger, {})

    def test_only_applies_a_single_named_file(self):
        conn = FakeConnection()
        code, out = self._run(["--only", "002_x.sql"], conn)
        self.assertEqual(code, 0)
        self.assertIn("Applied 1 file(s)", out)
        self.assertEqual(list(conn.ledger), ["002_x.sql"])

    def test_the_connection_string_never_reaches_stdout(self):
        conn = FakeConnection()
        _code, out = self._run([], conn)
        self.assertNotIn("u:p@h", out)
        self.assertIn("target", out)

    def _discover(self):
        with unittest.mock.patch.object(migrate, "MIGRATIONS_DIR", self.tmp):
            return migrate.discover()


class EntryPointTests(unittest.TestCase):
    def _run(self, argv, migrations_dir=None, env=None):
        import contextlib
        import io
        import os

        buf = io.StringIO()
        patches = [unittest.mock.patch.dict(os.environ, env or {}, clear=False)]
        if migrations_dir is not None:
            patches.append(unittest.mock.patch.object(migrate, "MIGRATIONS_DIR", migrations_dir))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            # Clear every variable database_url() consults, so the test does not
            # depend on the developer's shell.
            for key in ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL"):
                if key not in (env or {}):
                    os.environ.pop(key, None)
            with contextlib.redirect_stdout(buf):
                code = migrate.main(argv)
        return code, buf.getvalue()

    def test_missing_database_url_exits_2_with_instructions(self):
        code, out = self._run(["--status"])
        self.assertEqual(code, 2)
        self.assertIn("DATABASE_URL is not set", out)
        self.assertIn("sslmode=require", out)

    def test_destructive_file_is_refused_before_any_connection_attempt(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "ddl.sql").write_text("DROP TABLE marts.reviews;", encoding="utf-8")

        def explode(*_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("connected despite destructive SQL")

        with unittest.mock.patch("reviewbot.db.connect", explode):
            # A valid-looking URL, so the only thing that can stop the run is
            # the guard rather than a missing configuration.
            code, out = self._run(
                [],
                migrations_dir=tmp,
                env={"DATABASE_URL": "postgresql://u:p@h/db"},
            )
        self.assertEqual(code, 3)
        self.assertIn("REFUSED", out)
        self.assertIn("Nothing was executed", out)

    def test_unknown_only_target_exits_2(self):
        code, out = self._run(["--only", "no_such_file.sql"])
        self.assertEqual(code, 2)
        self.assertIn("no migration named", out)

    def test_connection_failure_is_reported_not_raised(self):
        def refuse(*_a, **_k):
            raise RuntimeError("connection refused")

        with unittest.mock.patch("reviewbot.db.connect", refuse):
            code, out = self._run([], env={"DATABASE_URL": "postgresql://u:p@h/db"})
        self.assertEqual(code, 2)
        self.assertIn("could not connect", out)
        self.assertNotIn(":p@", out)  # the URL must not be echoed


if __name__ == "__main__":
    unittest.main()
