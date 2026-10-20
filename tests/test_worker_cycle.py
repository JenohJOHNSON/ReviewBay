"""The worker cycle, and the QC pass that was missing from it.

`qc()` sets `relevant`, and every dashboard, trend, compare, export and RAG
query filters on `relevant IS NOT FALSE`. It was wired only into
`ingestion.run.main()`, which no deployment target runs: `docker-compose.yml`
and `railway.worker.json` both start `reviewbot.worker`, whose cycle did the
Airbyte import, the local sources and enrichment, and then stopped. So on a real
deployment nothing ever ran the global pass.
"""

from __future__ import annotations

import contextlib
import os
import unittest
import unittest.mock

from reviewbot import db, worker


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(" ".join(str(sql).split()))

    def executemany(self, sql, params=None):
        self.conn.executed.append(" ".join(str(sql).split()))

    def fetchall(self):
        return list(self.conn.rows)

    def fetchone(self):
        return None


class FakeConnection:
    def __init__(self, rows=()):
        self.rows = rows
        self.executed: list[str] = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@contextlib.contextmanager
def fake_connection(conn):
    yield conn


class RunCycleTests(unittest.TestCase):
    def _patch_stages(self, stack, qc_result=None, qc_error=None):
        """Stub out everything the cycle does except the part under test."""
        stack.enter_context(unittest.mock.patch.object(worker, "normalize_from_config", lambda: 3))
        stack.enter_context(unittest.mock.patch.object(worker, "_load_config", lambda: {}))
        stack.enter_context(
            unittest.mock.patch.object(worker, "run_source", lambda s, config=None: 2)
        )
        stack.enter_context(unittest.mock.patch.object(worker, "enrich", lambda: 5))
        stack.enter_context(
            unittest.mock.patch.object(worker, "_local_sources", lambda: ["app_store"])
        )

        calls: list[bool] = []

        def fake_qc(*_a, **_k):
            calls.append(True)
            if qc_error is not None:
                raise qc_error
            return qc_result or {"checked": 7, "irrelevant": 2}

        from reviewbot import qc as qc_module

        stack.enter_context(unittest.mock.patch.object(qc_module, "qc", fake_qc))
        return calls

    def test_the_cycle_runs_the_qc_pass(self):
        with contextlib.ExitStack() as stack:
            calls = self._patch_stages(stack)
            stack.enter_context(unittest.mock.patch.dict(os.environ, {}, clear=False))
            os.environ.pop("WORKER_QC", None)
            result = worker.run_cycle()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["qc_checked"], 7)
        self.assertEqual(result["qc_irrelevant"], 2)

    def test_the_earlier_stages_still_report_their_counts(self):
        with contextlib.ExitStack() as stack:
            self._patch_stages(stack)
            os.environ.pop("WORKER_QC", None)
            result = worker.run_cycle()
        self.assertEqual(result["airbyte"], 3)
        self.assertEqual(result["local"], 2)
        self.assertEqual(result["enriched"], 5)

    def test_worker_qc_0_turns_it_off(self):
        with contextlib.ExitStack() as stack:
            calls = self._patch_stages(stack)
            stack.enter_context(unittest.mock.patch.dict(os.environ, {"WORKER_QC": "0"}))
            result = worker.run_cycle()
        self.assertEqual(calls, [])
        self.assertNotIn("qc_checked", result)

    def test_a_qc_failure_does_not_fail_the_cycle(self):
        with contextlib.ExitStack() as stack:
            self._patch_stages(stack, qc_error=RuntimeError("model unavailable"))
            os.environ.pop("WORKER_QC", None)
            with self.assertLogs("reviewbot.worker", "ERROR"):
                result = worker.run_cycle()
        # Collection and enrichment already succeeded; QC is best-effort and must
        # not discard their work.
        self.assertEqual(result["enriched"], 5)
        self.assertNotIn("qc_checked", result)

    def test_a_database_config_error_still_propagates(self):
        # main() has a dedicated handler that logs "idle until DATABASE_URL is
        # configured", which the generic except would otherwise swallow.
        with contextlib.ExitStack() as stack:
            self._patch_stages(stack, qc_error=db.DatabaseConfigError("no url"))
            os.environ.pop("WORKER_QC", None)
            with self.assertRaises(db.DatabaseConfigError):
                worker.run_cycle()


class QcWithoutAKeyTests(unittest.TestCase):
    def test_qc_skips_cleanly_with_no_api_key_instead_of_logging_a_traceback_per_batch(self):
        from reviewbot import qc as qc_module

        conn = FakeConnection(rows=[("id1", "some text", "Acme")] * 30)

        def judge(*_a, **_k):  # pragma: no cover - must never be reached
            raise AssertionError("called the model without an API key")

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                unittest.mock.patch.object(qc_module.db, "connection", lambda: fake_connection(conn))
            )
            stack.enter_context(
                unittest.mock.patch.object(qc_module.db, "like_param", lambda v: v)
            )
            stack.enter_context(unittest.mock.patch.object(qc_module, "_judge_batch", judge))
            stack.enter_context(unittest.mock.patch.dict(os.environ, {}, clear=False))
            os.environ.pop("OPENAI_API_KEY", None)
            result = qc_module.qc()

        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["reason"], "no_api_key")
        self.assertEqual(result["skipped"], 30)

    def test_the_free_trusted_source_pass_still_runs_without_a_key(self):
        from reviewbot import qc as qc_module

        conn = FakeConnection(rows=[])
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                unittest.mock.patch.object(qc_module.db, "connection", lambda: fake_connection(conn))
            )
            stack.enter_context(
                unittest.mock.patch.object(qc_module.db, "like_param", lambda v: v)
            )
            os.environ.pop("OPENAI_API_KEY", None)
            qc_module.qc()
        # Reviews from the brand's own page or app store listing are on-topic by
        # construction and need no model call, so that work is not key-gated.
        self.assertTrue(
            any("SET relevant = TRUE" in sql for sql in conn.executed),
            conn.executed,
        )


if __name__ == "__main__":
    unittest.main()
