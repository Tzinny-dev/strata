"""Real execution of strata.exec against a real Postgres server (not just
SQL emission — see tests/test_sqlgen.py::TestPostgresLiveE2E for that).

Scoped slice (plan-hito-2.md backlog #2): bootstrap `run()`, `--only-stale`
no-op, physical-schema pins (pass and fail), and `rollback_to_run` after a
source mutation — mirroring the representative DuckDB tests for each
(test_exec.py::test_run_daily_orders, test_schema_pins.py,
test_snapshots.py::test_rollback_restores_prior_results_after_source_
mutation). Explicitly NOT covered here (documented as backlog, not
silently assumed to work): replay/execute_run, strata gc, the incremental
merge_strategy execution path, and check_join_cardinality on Postgres.

Skips entirely if no local Postgres server installation is found (same
criterion as TestPostgresLiveE2E) — never touches the system's own
Postgres service, uses a throwaway cluster via tests/pg_harness.py.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata.analysis import Checker, Project
from strata.dialects import POSTGRES
from strata.exec import PinError
from strata.parser import parse_strata
from strata import exec as ex

from tests.pg_harness import ephemeral_postgres, find_pgbin


MODEL_TEXT = '''source orders(ns: "n", dataset: "orders") {
  columns: { id: int64 nonnull, amount: money nonnull, country: string nonnull }
}

contract OrderContract {
  id: int64 nonnull
  amount: money nonnull
  country: string nonnull
}

model m -> contract OrderContract {
  from orders
}
'''


def build(text, path):
    proj = Project(parse_strata(text, path))
    tms = Checker(proj).check_all()
    return proj, tms


def write_module(d, text):
    path = os.path.join(d, "m.strata")
    Path(path).write_text(text)
    return path


@unittest.skipUnless(find_pgbin(), "no postgres server installation found")
class TestExecPostgres(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._pg = ephemeral_postgres()
        self.con = self._pg.__enter__()
        self.addCleanup(self._pg.__exit__, None, None, None)

    def _run(self, path, proj, tms, only_stale=False):
        return ex.run(self.con, proj, tms, path, only_stale=only_stale,
                       dialect=POSTGRES)

    def test_bootstrap_run_and_second_run_is_noop(self):
        path = write_module(self.d, MODEL_TEXT)
        proj, tms = build(MODEL_TEXT, path)
        self.con.execute(
            "CREATE TABLE orders (id BIGINT, amount NUMERIC(38,2), country TEXT)")
        self.con.execute(
            "INSERT INTO orders VALUES (1, 10.00, 'ES'), (2, 20.00, 'MX')")

        applied, pins, note = self._run(path, proj, tms)
        self.assertEqual(applied, ["m"])
        self.assertTrue(any("amount" in p for p in pins))
        rows = self.con.execute("SELECT id, amount, country FROM v_m ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0][1]), 10.00)

        applied2, pins2, note2 = self._run(path, proj, tms, only_stale=True)
        self.assertEqual(applied2, [])

    def test_physical_schema_pin_fails_without_publishing(self):
        path = write_module(self.d, MODEL_TEXT)
        proj, tms = build(MODEL_TEXT, path)
        # amount declared money -> NUMERIC(38,2); this table has the wrong
        # precision, which must fail the pin instead of silently publishing.
        self.con.execute(
            "CREATE TABLE orders (id BIGINT, amount NUMERIC(10,2), country TEXT)")
        self.con.execute("INSERT INTO orders VALUES (1, 10.00, 'ES')")
        with self.assertRaises(PinError) as cm:
            self._run(path, proj, tms)
        self.assertIn("incompatible with contract", str(cm.exception))
        # Nothing published: no v_m view should exist.
        views = self.con.execute(
            "SELECT viewname FROM pg_views WHERE schemaname='public' "
            "AND viewname='v_m'").fetchall()
        self.assertEqual(views, [])

    def test_rollback_restores_prior_results_after_source_mutation(self):
        path = write_module(self.d, MODEL_TEXT)
        proj, tms = build(MODEL_TEXT, path)
        self.con.execute(
            "CREATE TABLE orders (id BIGINT, amount NUMERIC(38,2), country TEXT)")
        self.con.execute("INSERT INTO orders VALUES (1, 10.00, 'ES')")

        self._run(path, proj, tms)
        rid1 = ex.load_history(path)[-1]["run_id"]
        first = self.con.execute("SELECT * FROM v_m ORDER BY id").fetchall()

        self.con.execute("INSERT INTO orders VALUES (2, 20.00, 'MX')")
        applied2, _, _ = self._run(path, proj, tms, only_stale=True)
        self.assertEqual(applied2, ["m"])
        second = self.con.execute("SELECT * FROM v_m ORDER BY id").fetchall()
        self.assertNotEqual(first, second)

        ex.rollback_to_run(self.con, ex.find_run(path, rid1))
        self.assertEqual(
            self.con.execute("SELECT * FROM v_m ORDER BY id").fetchall(), first)

        # A later source mutation must not alter the rolled-back run's data.
        self.con.execute("INSERT INTO orders VALUES (3, 30.00, 'BR')")
        self.assertEqual(
            self.con.execute("SELECT * FROM v_m ORDER BY id").fetchall(), first)


if __name__ == "__main__":
    unittest.main()
