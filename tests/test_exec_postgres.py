"""Real execution of strata.exec against a real Postgres server (not just
SQL emission — see tests/test_sqlgen.py::TestPostgresLiveE2E for that).

plan-hito-2.md backlog #2 scoped this to: bootstrap `run()`, `--only-stale`
no-op, physical-schema pins (pass and fail), and `rollback_to_run` after a
source mutation. Backlog #7 closes the rest of the engine — replay/
execute_run, strata gc, the incremental merge_strategy execution path
(with cdc_column pushdown), and check_join_cardinality — all verified to
already work against real Postgres via strata/dbcompat.py with zero
additional production code, plus the CLI wiring itself
(strata.cli.open_warehouse: `-o postgres://...` now opens a real
connection instead of always defaulting to DuckDB).

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
from strata.cli import main

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


@unittest.skipUnless(find_pgbin(), "no postgres server installation found")
class TestJoinCardinalityPostgres(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._pg = ephemeral_postgres(port=55435)
        self.con = self._pg.__enter__()
        self.addCleanup(self._pg.__exit__, None, None, None)

    def test_many_to_one_passes_and_fanout_aborts(self):
        text = '''source o(ns: "n", dataset: "o") {
  columns: { id: int64 nonnull, cust: int64 nonnull }
}
source c(ns: "n", dataset: "c") {
  columns: { id: int64 nonnull, name: string nonnull }
}
model m {
  from o
  join_inner c on o.cust == c.id expect many_to_one
  select { id = o.id, name = c.name }
}
'''
        path = write_module(self.d, text)
        proj, tms = build(text, path)
        self.con.execute("CREATE TABLE o (id BIGINT, cust BIGINT)")
        self.con.execute("CREATE TABLE c (id BIGINT, name TEXT)")
        self.con.execute("INSERT INTO c VALUES (10, 'x')")
        self.con.execute("INSERT INTO o VALUES (1, 10), (2, 10)")
        applied, pins, note = ex.run(self.con, proj, tms, path, dialect=POSTGRES)
        self.assertEqual(applied, ["m"])
        self.assertTrue(any("many_to_one" in p for p in pins))

        # A second row on the "one" side turns it into a fanout: must abort.
        self.con.execute("INSERT INTO c VALUES (10, 'y')")
        with self.assertRaises(PinError):
            ex.run(self.con, proj, tms, path, only_stale=True, dialect=POSTGRES)


@unittest.skipUnless(find_pgbin(), "no postgres server installation found")
class TestIncrementalMergePostgres(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._pg = ephemeral_postgres(port=55436)
        self.con = self._pg.__enter__()
        self.addCleanup(self._pg.__exit__, None, None, None)

    def test_stale_mutation_ignored_after_pushdown_merge(self):
        text = '''source s(ns: "n", dataset: "s") {
  columns: { id: int64, v: int64, ts: timestamp }
}
model m {
  from s
  incremental
  merge_strategy: upsert
  merge_keys: [id]
  cdc_column: ts
}
'''
        path = write_module(self.d, text)
        proj, tms = build(text, path)
        self.con.execute("CREATE TABLE s (id BIGINT, v BIGINT, ts TIMESTAMP)")
        self.con.execute("INSERT INTO s VALUES (1, 10, '2026-01-01 00:00:00')")
        ex.run(self.con, proj, tms, path, dialect=POSTGRES)

        # Mutate the already-merged row WITHOUT bumping ts (must be
        # ignored) and add a genuinely new one.
        self.con.execute("UPDATE s SET v = 999 WHERE id = 1")
        self.con.execute("INSERT INTO s VALUES (2, 20, '2026-01-02 00:00:00')")
        proj2, tms2 = build(text, path)
        ex.run(self.con, proj2, tms2, path, only_stale=True, dialect=POSTGRES)
        rows = dict(self.con.execute("SELECT id, v FROM v_m").fetchall())
        self.assertEqual(rows, {1: 10, 2: 20},
                         "id=1 must keep its pre-merge value on Postgres too")


@unittest.skipUnless(find_pgbin(), "no postgres server installation found")
class TestGcPostgres(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._pg = ephemeral_postgres(port=55437)
        self.con = self._pg.__enter__()
        self.addCleanup(self._pg.__exit__, None, None, None)

    def test_apply_drops_only_the_retired_run(self):
        text = 'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\nmodel m { from s }\n'
        path = write_module(self.d, text)
        self.con.execute("CREATE TABLE s (id BIGINT)")
        self.con.execute("INSERT INTO s VALUES (0)")
        rids = []
        for value in (1, 2, 3):
            self.con.execute(f"UPDATE s SET id={value}")
            proj, tms = build(text, path)
            ex.run(self.con, proj, tms, path, dialect=POSTGRES)
            rids.append(ex.load_history(path)[-1]["run_id"])
        r1, r2, r3 = rids
        plan = ex.gc_snapshots(self.con, path, keep=2, apply=True)
        self.assertTrue(plan["applied"])
        self.assertEqual(set(ex.run_tables(self.con).values()), {r2, r3})
        self.assertEqual(
            self.con.execute("SELECT * FROM v_m").fetchall(), [(3,)])


@unittest.skipUnless(find_pgbin(), "no postgres server installation found")
class TestReplayPostgres(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._pg = ephemeral_postgres(port=55438)
        self.con = self._pg.__enter__()
        self.addCleanup(self._pg.__exit__, None, None, None)

    def test_execute_run_reproduces_original_from_frozen_inputs(self):
        text = 'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\nmodel m { from s }\n'
        path = write_module(self.d, text)
        self.con.execute("CREATE TABLE s (id BIGINT)")
        self.con.execute("INSERT INTO s VALUES (1)")
        proj, tms = build(text, path)
        ex.run(self.con, proj, tms, path, dialect=POSTGRES)
        rid = ex.load_history(path)[-1]["run_id"]
        original = self.con.execute("SELECT * FROM v_m ORDER BY id").fetchall()

        self.con.execute("DELETE FROM s")
        self.con.execute("INSERT INTO s VALUES (99)")
        applied, pins, orig = ex.execute_run(self.con, proj, tms, path, rid,
                                             dialect=POSTGRES)
        self.assertEqual(applied, ["m"])
        self.assertEqual(
            self.con.execute("SELECT * FROM v_m ORDER BY id").fetchall(), original)


@unittest.skipUnless(find_pgbin(), "no postgres server installation found")
class TestCliPostgres(unittest.TestCase):
    """The one piece with no prior test path at all: the actual CLI entry
    point (main()), not a direct call into strata.exec — proves
    `strata run --dialect postgres -o postgres://...` really opens a
    Postgres connection end to end, via strata.cli.open_warehouse."""

    def test_cli_run_against_a_postgres_dsn(self):
        with ephemeral_postgres(port=55439) as con:
            if con is None:
                self.skipTest("no postgres server installation found")
            con.execute("CREATE TABLE s (id BIGINT)")
            con.execute("INSERT INTO s VALUES (1), (2)")
            # main() opens ITS OWN connection via open_warehouse(); this
            # setup must be committed on `con` first or it stays invisible
            # in that other session (PGConn disables autocommit).
            con.raw.commit()
            d = tempfile.mkdtemp()
            path = write_module(
                d, 'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\n'
                   'model m { from s }\n')
            rc = main(["run", path, "--dialect", "postgres", "-o", con.dsn])
            self.assertEqual(rc, 0)
            rows = con.execute("SELECT * FROM v_m ORDER BY id").fetchall()
            self.assertEqual(rows, [(1,), (2,)])


if __name__ == "__main__":
    unittest.main()
