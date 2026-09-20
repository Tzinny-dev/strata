"""`if(cond, then, else)` and `case(cond, val, [cond, val, ...], [else])`.

Both compile through the one function catalog (strata/functions.py) the
typechecker and codegen already share for every other function, reusing
the same branch-type unification `coalesce` uses (`functions._unify_all`,
backed by `types.unify`). Emitted as `CASE WHEN...END`, identical across
DuckDB/Postgres/BigQuery/Snowflake — no dialect-specific codegen needed.
Ordinary scalar expressions: no placement restriction (unlike window
functions), usable anywhere an expression is, and nest freely.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import StrataError, Checker
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE
from strata import sqlgen
from strata.fmt import format_module
from strata.parser import parse_strata

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False


SRC = ('source s(ns: "n", dataset: "s") {\n'
       '  columns: { x: int64 nonnull, n: int64, name: string }\n'
       '}\n')


def check_model(body):
    proj = analysis.Project(parse_strata(SRC + body, "<t>"))
    tms = Checker(proj).check_all()
    return tms["m"]


def check_error(body):
    proj = analysis.Project(parse_strata(SRC + body, "<t>"))
    with unittest.TestCase().assertRaises(StrataError) as cm:
        Checker(proj).check_all()
    return cm.exception


class TestConditionalSyntax(unittest.TestCase):
    def test_if_with_nonnull_branches_is_nonnull(self):
        tm = check_model(
            'model m { from s\n  select { a = if(x > 1, "big", "small") }\n}\n')
        self.assertEqual(str(tm.schema["a"].t), "string")
        self.assertFalse(tm.schema["a"].nullable)

    def test_if_with_a_nullable_branch_is_nullable(self):
        tm = check_model(
            'model m { from s\n  select { a = if(x > 1, name, "small") }\n}\n')
        self.assertTrue(tm.schema["a"].nullable)

    def test_case_without_else_is_always_nullable(self):
        tm = check_model(
            'model m { from s\n  select { a = case(x == 1, "one") }\n}\n')
        self.assertTrue(tm.schema["a"].nullable)

    def test_case_with_else_and_nonnull_branches_is_nonnull(self):
        tm = check_model(
            'model m { from s\n'
            '  select { a = case(x == 1, "one", x == 2, "two", "other") }\n}\n')
        self.assertFalse(tm.schema["a"].nullable)
        self.assertEqual(str(tm.schema["a"].t), "string")

    def test_nested_if_in_case_and_case_in_if(self):
        tm = check_model(
            'model m { from s\n'
            '  select {\n'
            '    a = case(x == 1, if(x > 0, "pos", "neg"), "other"),\n'
            '    b = if(x == 1, case(x == 1, "one"), "not one")\n'
            '  }\n}\n')
        self.assertEqual(str(tm.schema["a"].t), "string")
        self.assertEqual(str(tm.schema["b"].t), "string")

    def test_null_literal_branch_adapts(self):
        tm = check_model(
            'model m { from s\n  select { a = if(x > 1, "big", null) }\n}\n')
        self.assertEqual(str(tm.schema["a"].t), "string")
        self.assertTrue(tm.schema["a"].nullable)


class TestConditionalTypeErrors(unittest.TestCase):
    def test_if_condition_must_be_bool(self):
        self.assertEqual(
            check_error('model m { from s\n  select { a = if(x, "big", "small") }\n}\n').code,
            "E090")

    def test_case_condition_must_be_bool(self):
        self.assertEqual(
            check_error('model m { from s\n  select { a = case(x, "one") }\n}\n').code,
            "E090")

    def test_if_branch_type_mismatch(self):
        self.assertEqual(
            check_error('model m { from s\n  select { a = if(x > 1, "big", x) }\n}\n').code,
            "E058")

    def test_case_branch_type_mismatch(self):
        self.assertEqual(
            check_error(
                'model m { from s\n'
                '  select { a = case(x == 1, "one", x == 2, 2) }\n}\n').code,
            "E058")

    def test_if_arity(self):
        self.assertEqual(
            check_error('model m { from s\n  select { a = if(x > 1, "big") }\n}\n').code,
            "E062")

    def test_case_needs_at_least_one_pair(self):
        self.assertEqual(
            check_error('model m { from s\n  select { a = case(x) }\n}\n').code,
            "E062")


class TestConditionalSQL(unittest.TestCase):
    def sql(self, body, dialect=DUCKDB):
        proj = analysis.Project(parse_strata(SRC + "model m { from s\n  " + body + "\n}\n", "<t>"))
        return sqlgen.model_sql(analysis.Checker(proj).check_all()["m"], dialect=dialect)

    def test_if_emission(self):
        sql = self.sql('select { a = if(x > 1, "big", "small") }')
        self.assertIn("CASE WHEN (x > 1) THEN 'big' ELSE 'small' END AS a", sql)

    def test_case_with_else_emission(self):
        sql = self.sql('select { a = case(x == 1, "one", x == 2, "two", "other") }')
        self.assertIn(
            "CASE WHEN (x = 1) THEN 'one' WHEN (x = 2) THEN 'two' ELSE 'other' END AS a", sql)

    def test_case_without_else_omits_else(self):
        sql = self.sql('select { a = case(x == 1, "one") }')
        self.assertIn("CASE WHEN (x = 1) THEN 'one' END AS a", sql)
        self.assertNotIn("ELSE", sql)

    def test_same_sql_across_all_four_dialects(self):
        body = 'select { a = if(x > 1, "big", "small") }'
        texts = {d.name: self.sql(body, dialect=d)
                 for d in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE)}
        self.assertEqual(len(set(texts.values())), 1, texts)

    def test_round_trip_through_the_formatter(self):
        body = 'select { a = if(x > 1, "big", "small"), b = case(x == 1, "one", "other") }'
        once = format_module(parse_strata(SRC + "model m { from s\n  " + body + "\n}\n", "<a>"))
        twice = format_module(parse_strata(once, "<b>"))
        self.assertEqual(once, twice)
        self.assertIn('if((x > 1), "big", "small")', once)
        self.assertIn('case((x == 1), "one", "other")', once)


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestConditionalExecutionDuckDB(unittest.TestCase):
    def test_if_and_case_over_seed_data(self):
        import duckdb
        from strata import exec as ex

        d = tempfile.mkdtemp()
        path = os.path.join(d, "c.strata")
        text = (
            'source s(ns: "n", dataset: "s") {\n'
            '  columns: { x: int64 nonnull }\n'
            '}\n'
            'model m {\n'
            '  from s\n'
            '  select {\n'
            '    x = x,\n'
            '    a = if(x > 1, "big", "small"),\n'
            '    b = case(x == 1, "one", x == 2, "two", "other"),\n'
            '    c = case(x == 1, "one")\n'
            '  }\n'
            '}\n')
        Path(path).write_text(text)
        con = duckdb.connect()
        con.execute("CREATE TABLE s (x BIGINT)")
        con.execute("INSERT INTO s VALUES (1), (2), (3)")

        proj = analysis.Project(parse_strata(text, path))
        tms = Checker(proj).check_all()
        ex.materialize(con, proj, tms, names=["m"])
        rows = {r[0]: r[1:] for r in con.execute(
            "SELECT x, a, b, c FROM v_m ORDER BY x").fetchall()}
        self.assertEqual(rows[1], ("small", "one", "one"))
        self.assertEqual(rows[2], ("big", "two", None))
        self.assertEqual(rows[3], ("big", "other", None))


if __name__ == "__main__":
    unittest.main()
