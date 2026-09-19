"""§2 warehouse semantics: partition_by and freshness integration.

Tests that verify:
- partition_by column appears in materialized view SQL
- freshness + partition_by validation in runtime_pins
- Error cases: partition_by column not in schema, etc.
"""
import os
import tempfile
import unittest
from pathlib import Path

import duckdb

from strata.analysis import Checker, Project
from strata.exec import materialize, PinError
from strata.parser import parse_strata
from strata import sqlgen


SRC = '''source s(ns: "n", dataset: "s") { columns: { x: int64, ds: string } }
'''

SRC_COLS = ("x INT, ds VARCHAR")

def write_module(d, text):
    path = os.path.join(d, "m.strata")
    Path(path).write_text(text)
    return path

def build_and_run(d, text, source_data=None):
    """Build, parse, and execute a Strata module against DuckDB.

    source_data: dict of {table_name: [(val1, val2, ...), ...]}
    """
    path = write_module(d, text)
    proj = Project(parse_strata(Path(path).read_text(), path))
    Checker(proj).check_all()
    con = duckdb.connect()
    if source_data:
        for table, rows in source_data.items():
            con.execute(f"CREATE TABLE {table} ({SRC_COLS})")
            for row in rows:
                con.execute(f"INSERT INTO {table} VALUES ({", ".join(repr(v) for v in row)})")
    applied, pins = materialize(con, proj, proj.typed, list(proj.typed.keys()))
    return con, proj, applied, pins


class TestPartitionBySQL(unittest.TestCase):
    def test_partition_by_adds_column_to_select(self):
        """partition_by [ds] adds ds AS __partition_col to base subquery."""
        text = SRC + 'model m { from s partition_by [ds] }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        sql = sqlgen.model_sql(proj.typed["m"])
        self.assertIn("ds AS __partition_col", sql)
        self.assertIn("SELECT", sql)

    def test_partition_by_with_freshness(self):
        """partition_by + freshness: both attributes stored in plan."""
        text = SRC + 'model m { from s partition_by [ds] freshness incremental }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(len(m.plan.partition_by), 1)
        self.assertEqual(m.plan.freshness, "incremental")

    def test_freshness_only(self):
        """freshness without partition_by: no partition validation."""
        text = SRC + 'model m { from s freshness incremental }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(m.plan.partition_by, [])
        self.assertEqual(m.plan.freshness, "incremental")


class TestPartitionByExecution(unittest.TestCase):
    def test_partition_by_column_materialized(self):
        """partition_by column is in base subquery, view has model columns."""
        d = tempfile.mkdtemp()
        text = SRC + 'model m { from s partition_by [ds] }\n'
        con, proj, applied, pins = build_and_run(d, text, source_data={
            "s": [(1, '2024-01-01'), (2, '2024-01-02')]
        })
        self.addCleanup(con.close)
        self.assertEqual(applied, ["m"])
        cols = [r[0] for r in con.execute("DESCRIBE stg_main__m").fetchall()]
        self.assertIn("x", cols)
        result = con.execute("SELECT * FROM stg_main__m ORDER BY x").fetchall()
        self.assertEqual(len(result), 2)
        # Verify SQL has partition col in base subquery
        sql = sqlgen.model_sql(proj.typed["m"])
        self.assertIn("ds AS __partition_col", sql)

    def test_partition_by_with_contract(self):
        """partition_by + contract: freshness validation passes."""
        d = tempfile.mkdtemp()
        text = SRC + (
            'contract c { x: int64, ds: string }\n'
            'model m -> contract c { from s partition_by [ds] freshness incremental }\n'
        )
        con, proj, applied, pins = build_and_run(d, text, source_data={
            "s": [(1, '2024-01-01'), (2, '2024-01-02')]
        })
        self.addCleanup(con.close)
        self.assertEqual(applied, ["m"])
        self.assertTrue(any("freshness partition_by present" in p for p in pins))

    def test_partition_by_chain(self):
        """partition_by in chained models."""
        d = tempfile.mkdtemp()
        text = SRC + (
            'model m1 { from s }\n'
            'model m2 { from m1 partition_by [ds] }\n'
        )
        con, proj, applied, pins = build_and_run(d, text, source_data={
            "s": [(1, '2024-01-01')]
        })
        self.addCleanup(con.close)
        self.assertEqual(applied, ["m1", "m2"])
        m2 = proj.typed["m2"]
        self.assertEqual(len(m2.plan.partition_by), 1)

    def test_freshness_without_contract_no_pin_error(self):
        """freshness without contract: no pin validation, no error."""
        d = tempfile.mkdtemp()
        text = SRC + 'model m { from s partition_by [ds] freshness incremental }\n'
        con, proj, applied, pins = build_and_run(d, text, source_data={
            "s": [(1, '2024-01-01')]
        })
        self.addCleanup(con.close)
        self.assertEqual(applied, ["m"])
        self.assertEqual(pins, [])


class TestPartitionByErrors(unittest.TestCase):
    def test_partition_by_column_not_in_schema(self):
        """static analysis catches missing partition_by column before execution."""
        d = tempfile.mkdtemp()
        text = SRC + (
            'contract c { x: int64, missing_col: string }\n'
            'model m -> contract c { from s partition_by [missing_col] freshness incremental }\n'
        )
        path = os.path.join(d, "m.strata")
        Path(path).write_text(text)
        proj = Project(parse_strata(Path(path).read_text(), path))
        with self.assertRaises(Exception) as ctx:
            Checker(proj).check_all()
        self.assertIn("missing_col", str(ctx.exception).lower())


class TestFreshnessStaleness(unittest.TestCase):
    def test_freshness_spec_parsing(self):
        """Parse various freshness specs."""
        from strata.exec import parse_freshness_threshold
        import datetime
        self.assertIsNone(parse_freshness_threshold("incremental"))
        self.assertEqual(parse_freshness_threshold("daily"), datetime.timedelta(hours=24))
        self.assertEqual(parse_freshness_threshold("weekly"), datetime.timedelta(days=7))
        self.assertEqual(parse_freshness_threshold("monthly"), datetime.timedelta(days=30))
        self.assertEqual(parse_freshness_threshold("1h"), datetime.timedelta(hours=1))
        self.assertEqual(parse_freshness_threshold("24h"), datetime.timedelta(hours=24))
        self.assertEqual(parse_freshness_threshold("7d"), datetime.timedelta(days=7))
        self.assertEqual(parse_freshness_threshold("2w"), datetime.timedelta(weeks=2))
        self.assertIsNone(parse_freshness_threshold("unknown"))

    def test_freshness_daily_parsed(self):
        """Freshness 'daily' is parsed correctly."""
        text = SRC + 'model m { from s freshness daily }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(m.plan.freshness, "daily")

    def test_freshness_1h_parsed(self):
        """Freshness '1h' is parsed correctly."""
        text = SRC + 'model m { from s freshness 1h }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(m.plan.freshness, "1h")

    def test_freshness_7d_parsed(self):
        """Freshness '7d' is parsed correctly."""
        text = SRC + 'model m { from s freshness 7d }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(m.plan.freshness, "7d")


class TestFreshnessColumn(unittest.TestCase):
    def test_freshness_column_parsed(self):
        """Freshness with freshness_column is parsed correctly."""
        text = SRC + 'model m { from s freshness 1h freshness_column: ts }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(m.plan.freshness, "1h")
        self.assertEqual(m.plan.freshness_column, "ts")

    def test_freshness_column_with_partition_by(self):
        """Freshness with partition_by and freshness_column is parsed correctly."""
        text = SRC + 'model m { from s partition_by [ds] freshness daily freshness_column: ts }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertEqual(m.plan.freshness, "daily")
        self.assertEqual(m.plan.freshness_column, "ts")
        self.assertEqual(len(m.plan.partition_by), 1)


if __name__ == "__main__":
    unittest.main()
