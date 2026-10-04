"""Real execution of strata.exec against BigQuery (engine-level tests).

Tests bootstrap run(), --only-stale no-op, physical-schema pins (pass and fail),
rollback_to_run after source mutation, and join cardinality checks.

Skips entirely if no BigQuery emulator or real credentials are available.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata.analysis import Checker, Project
from strata.dialects import BIGQUERY
from strata.exec import PinError
from strata.parser import parse_strata
from strata import exec as ex

from tests.bq_harness import ephemeral_bigquery


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


class TestExecBigQuery(unittest.TestCase):
    """Real execution tests against BigQuery."""

    @classmethod
    def setUpClass(cls):
        cls.project = os.environ.get("BIGQUERY_PROJECT", "strata-test")
        cls.dataset = os.environ.get("BIGQUERY_DATASET", "strata_test")
        cls.location = os.environ.get("BIGQUERY_LOCATION", "US")

    def _run_with_bq(self, test_fn):
        with ephemeral_bigquery(
            project=self.project,
            dataset=self.dataset,
            location=self.location,
        ) as con:
            if con is None:
                self.skipTest("No BigQuery emulator or credentials available")
            test_fn(con)

    def test_bootstrap_run_and_second_run_is_noop(self):
        self._run_with_bq(self._test_bootstrap)

    def _test_bootstrap(self, con):
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, MODEL_TEXT)
        proj, tms = build(MODEL_TEXT, path)

        project_dataset = f"`{self.project}.{self.dataset}`"
        con.execute(f"""
            CREATE OR REPLACE TABLE {project_dataset}.orders (
                id INT64, amount NUMERIC(38,2), country STRING
            )
        """)
        con.execute(f"""
            INSERT INTO {project_dataset}.orders VALUES
            (1, 10.00, 'ES'), (2, 20.00, 'MX')
        """)

        applied, pins, note = ex.run(con, proj, tms, path, only_stale=False, dialect=BIGQUERY)
        self.assertEqual(applied, ["m"])
        self.assertTrue(any("amount" in p for p in pins))
        rows = con.fetch(f"SELECT id, amount, country FROM {project_dataset}.v_m ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0][1]), 10.00)

        applied2, pins2, note2 = ex.run(con, proj, tms, path, only_stale=True, dialect=BIGQUERY)
        self.assertEqual(applied2, [])

    def test_physical_schema_pin_fails_without_publishing(self):
        self._run_with_bq(self._test_pin_fail)

    def _test_pin_fail(self, con):
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, MODEL_TEXT)
        proj, tms = build(MODEL_TEXT, path)

        project_dataset = f"`{self.project}.{self.dataset}`"
        # amount declared money -> NUMERIC(38,2); wrong precision must fail
        con.execute(f"""
            CREATE OR REPLACE TABLE {project_dataset}.orders (
                id INT64, amount NUMERIC(10,2), country STRING
            )
        """)
        con.execute(f"INSERT INTO {project_dataset}.orders VALUES (1, 10.00, 'ES')")

        with self.assertRaises(PinError) as cm:
            ex.run(con, proj, tms, path, dialect=BIGQUERY)
        self.assertIn("incompatible with contract", str(cm.exception))

        # Nothing published: no v_m view should exist
        rows = con.fetch(
            f"SELECT table_name FROM `{self.project}.{self.dataset}.INFORMATION_SCHEMA.VIEWS` "
            f"WHERE table_name = 'v_m'"
        )
        self.assertEqual(rows, [])

    def test_rollback_restores_prior_results_after_source_mutation(self):
        self._run_with_bq(self._test_rollback)

    def _test_rollback(self, con):
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, MODEL_TEXT)
        proj, tms = build(MODEL_TEXT, path)

        project_dataset = f"`{self.project}.{self.dataset}`"
        con.execute(f"""
            CREATE OR REPLACE TABLE {project_dataset}.orders (
                id INT64, amount NUMERIC(38,2), country STRING
            )
        """)
        con.execute(f"INSERT INTO {project_dataset}.orders VALUES (1, 10.00, 'ES')")

        ex.run(con, proj, tms, path, dialect=BIGQUERY)
        rid1 = ex.load_history(path)[-1]["run_id"]
        first = con.fetch(f"SELECT * FROM {project_dataset}.v_m ORDER BY id")

        con.execute(f"INSERT INTO {project_dataset}.orders VALUES (2, 20.00, 'MX')")
        applied2, _, _ = ex.run(con, proj, tms, path, only_stale=True, dialect=BIGQUERY)
        self.assertEqual(applied2, ["m"])
        second = con.fetch(f"SELECT * FROM {project_dataset}.v_m ORDER BY id")
        self.assertNotEqual(first, second)

        ex.rollback_to_run(con, ex.find_run(path, rid1))
        self.assertEqual(
            con.fetch(f"SELECT * FROM {project_dataset}.v_m ORDER BY id"), first
        )

        # Later mutation must not alter rolled-back run's data
        con.execute(f"INSERT INTO {project_dataset}.orders VALUES (3, 30.00, 'BR')")
        self.assertEqual(
            con.fetch(f"SELECT * FROM {project_dataset}.v_m ORDER BY id"), first
        )


class TestJoinCardinalityBigQuery(unittest.TestCase):
    """Join cardinality checks on BigQuery."""

    @classmethod
    def setUpClass(cls):
        cls.project = os.environ.get("BIGQUERY_PROJECT", "strata-test")
        cls.dataset = os.environ.get("BIGQUERY_DATASET", "strata_test")
        cls.location = os.environ.get("BIGQUERY_LOCATION", "US")

    def _run_with_bq(self, test_fn):
        with ephemeral_bigquery(
            project=self.project,
            dataset=self.dataset,
            location=self.location,
        ) as con:
            if con is None:
                self.skipTest("No BigQuery emulator or credentials available")
            test_fn(con)

    def test_many_to_one_passes_and_fanout_aborts(self):
        self._run_with_bq(self._test_join_card)

    def _test_join_card(self, con):
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
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, text)
        proj, tms = build(text, path)

        project_dataset = f"`{self.project}.{self.dataset}`"
        con.execute(f"CREATE OR REPLACE TABLE {project_dataset}.o (id INT64, cust INT64)")
        con.execute(f"CREATE OR REPLACE TABLE {project_dataset}.c (id INT64, name STRING)")
        con.execute(f"INSERT INTO {project_dataset}.c VALUES (10, 'x')")
        con.execute(f"INSERT INTO {project_dataset}.o VALUES (1, 10), (2, 10)")

        applied, pins, note = ex.run(con, proj, tms, path, dialect=BIGQUERY)
        self.assertEqual(applied, ["m"])
        self.assertTrue(any("many_to_one" in p for p in pins))

        # A second row on the "one" side turns it into a fanout: must abort
        con.execute(f"INSERT INTO {project_dataset}.c VALUES (10, 'y')")
        with self.assertRaises(PinError):
            ex.run(con, proj, tms, path, only_stale=True, dialect=BIGQUERY)


class TestIncrementalMergeBigQuery(unittest.TestCase):
    """Incremental merge_strategy upsert on BigQuery."""

    @classmethod
    def setUpClass(cls):
        cls.project = os.environ.get("BIGQUERY_PROJECT", "strata-test")
        cls.dataset = os.environ.get("BIGQUERY_DATASET", "strata_test")
        cls.location = os.environ.get("BIGQUERY_LOCATION", "US")

    def _run_with_bq(self, test_fn):
        with ephemeral_bigquery(
            project=self.project,
            dataset=self.dataset,
            location=self.location,
        ) as con:
            if con is None:
                self.skipTest("No BigQuery emulator or credentials available")
            test_fn(con)

    def test_stale_mutation_ignored_after_pushdown_merge(self):
        self._run_with_bq(self._test_merge)

    def _test_merge(self, con):
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
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, text)
        proj, tms = build(text, path)

        project_dataset = f"`{self.project}.{self.dataset}`"
        con.execute(f"CREATE OR REPLACE TABLE {project_dataset}.s (id INT64, v INT64, ts TIMESTAMP)")
        con.execute(f"INSERT INTO {project_dataset}.s VALUES (1, 10, TIMESTAMP '2026-01-01 00:00:00')")
        ex.run(con, proj, tms, path, dialect=BIGQUERY)

        # Mutate the already-merged row WITHOUT bumping ts (must be ignored)
        # and add a genuinely new one
        con.execute(f"UPDATE {project_dataset}.s SET v = 999 WHERE id = 1")
        con.execute(f"INSERT INTO {project_dataset}.s VALUES (2, 20, TIMESTAMP '2026-01-02 00:00:00')")
        proj2, tms2 = build(text, path)
        ex.run(con, proj2, tms2, path, only_stale=True, dialect=BIGQUERY)
        rows = dict(con.fetch(f"SELECT id, v FROM {project_dataset}.v_m"))
        self.assertEqual(rows, {1: 10, 2: 20}, "id=1 must keep its pre-merge value on BigQuery too")


class TestGcBigQuery(unittest.TestCase):
    """GC snapshots on BigQuery."""

    @classmethod
    def setUpClass(cls):
        cls.project = os.environ.get("BIGQUERY_PROJECT", "strata-test")
        cls.dataset = os.environ.get("BIGQUERY_DATASET", "strata_test")
        cls.location = os.environ.get("BIGQUERY_LOCATION", "US")

    def _run_with_bq(self, test_fn):
        with ephemeral_bigquery(
            project=self.project,
            dataset=self.dataset,
            location=self.location,
        ) as con:
            if con is None:
                self.skipTest("No BigQuery emulator or credentials available")
            test_fn(con)

    def test_apply_drops_only_the_retired_run(self):
        self._run_with_bq(self._test_gc)

    def _test_gc(self, con):
        text = 'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\nmodel m { from s }\n'
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, text)

        project_dataset = f"`{self.project}.{self.dataset}`"
        con.execute(f"CREATE OR REPLACE TABLE {project_dataset}.s (id INT64)")
        con.execute(f"INSERT INTO {project_dataset}.s VALUES (0)")

        rids = []
        for value in (1, 2, 3):
            con.execute(f"UPDATE {project_dataset}.s SET id={value}")
            proj, tms = build(text, path)
            ex.run(con, proj, tms, path, dialect=BIGQUERY)
            rids.append(ex.load_history(path)[-1]["run_id"])

        r1, r2, r3 = rids
        plan = ex.gc_snapshots(con, path, keep=2, apply=True)
        self.assertTrue(plan["applied"])
        self.assertEqual(set(ex.run_tables(con).values()), {r2, r3})
        self.assertEqual(con.fetch(f"SELECT * FROM {project_dataset}.v_m"), [(3,)])


class TestReplayBigQuery(unittest.TestCase):
    """Replay/execute_run on BigQuery."""

    @classmethod
    def setUpClass(cls):
        cls.project = os.environ.get("BIGQUERY_PROJECT", "strata-test")
        cls.dataset = os.environ.get("BIGQUERY_DATASET", "strata_test")
        cls.location = os.environ.get("BIGQUERY_LOCATION", "US")

    def _run_with_bq(self, test_fn):
        with ephemeral_bigquery(
            project=self.project,
            dataset=self.dataset,
            location=self.location,
        ) as con:
            if con is None:
                self.skipTest("No BigQuery emulator or credentials available")
            test_fn(con)

    def test_execute_run_reproduces_original_from_frozen_inputs(self):
        self._run_with_bq(self._test_replay)

    def _test_replay(self, con):
        text = 'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\nmodel m { from s }\n'
        self.d = tempfile.mkdtemp()
        path = write_module(self.d, text)

        project_dataset = f"`{self.project}.{self.dataset}`"
        con.execute(f"CREATE OR REPLACE TABLE {project_dataset}.s (id INT64)")
        con.execute(f"INSERT INTO {project_dataset}.s VALUES (1)")
        proj, tms = build(text, path)
        ex.run(con, proj, tms, path, dialect=BIGQUERY)
        rid = ex.load_history(path)[-1]["run_id"]
        original = con.fetch(f"SELECT * FROM {project_dataset}.v_m ORDER BY id")

        con.execute(f"DELETE FROM {project_dataset}.s")
        con.execute(f"INSERT INTO {project_dataset}.s VALUES (99)")
        applied, pins, orig = ex.execute_run(con, proj, tms, path, rid, dialect=BIGQUERY)
        self.assertEqual(applied, ["m"])
        self.assertEqual(
            con.fetch(f"SELECT * FROM {project_dataset}.v_m ORDER BY id"), original
        )


if __name__ == "__main__":
    unittest.main()