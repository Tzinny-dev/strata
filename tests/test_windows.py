"""Window functions (v0.2): `fn(args) over (partition_by: [...], sort: [...])`.

Compiles through the one catalog both the typechecker and codegen read.
Placement is a compile-time rule: windows run after grouping in the outer
query, so they are legal only in `select`/`derive`/`aggregate` outputs, never
in `let`, `filter`, group keys, `sort`, or join conditions, and never nested.
Emitted as `FN(args) OVER (PARTITION BY ... ORDER BY ...)`, standard in all
four dialects.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import StrataError, Checker
from strata.dialects import DUCKDB
from strata import sqlgen
from strata.fmt import format_module
from strata.parser import parse_strata

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False


SRC = ('source s(ns: "n", dataset: "s") {\n'
       '  columns: { g: string, d: date, n: int64, f: float64, m: money }\n'
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


class TestWindowSyntax(unittest.TestCase):
    def test_row_number_with_partition_and_sort(self):
        tm = check_model(
            "model m { from s\n"
            "  select { rn = row_number() over (partition_by: [g], sort: [d desc]) }\n}\n")
        self.assertEqual(str(tm.schema["rn"].t), "int64")
        self.assertFalse(tm.schema["rn"].nullable)

    def test_sort_only_and_empty_head(self):
        tm = check_model(
            "model m { from s\n"
            "  select { p = lead(n) over (sort: [d]) }\n}\n")
        self.assertEqual(str(tm.schema["p"].t), "int64")
        self.assertTrue(tm.schema["p"].nullable)  # no next row may exist

    def test_typing_rules(self):
        tm = check_model(
            "model m { from s\n"
            "  select { r = rank() over (partition_by: [g]),\n"
            "           dr = dense_rank() over (partition_by: [g]),\n"
            "           a = lag(f) over (sort: [d]),\n"
            "           b = first_value(m) over (partition_by: [g], sort: [d]),\n"
            "           c = last_value(g) over (partition_by: [g], sort: [d]) }\n}\n")
        for col in ("r", "dr"):
            self.assertEqual(str(tm.schema[col].t), "int64")
            self.assertFalse(tm.schema[col].nullable)
        self.assertEqual(str(tm.schema["a"].t), "float64")
        self.assertEqual(str(tm.schema["b"].t), "money(USD)")
        self.assertEqual(str(tm.schema["c"].t), "string")

    def test_windowed_aggregates_share_catalog_rules(self):
        tm = check_model(
            "model m { from s\n"
            "  select { t = sum(n) over (partition_by: [g]),\n"
            "           c = count() over (partition_by: [g]),\n"
            "           a = avg(f) over (partition_by: [g]) }\n}\n")
        self.assertEqual(str(tm.schema["t"].t), "int64")
        self.assertEqual(str(tm.schema["c"].t), "int64")
        self.assertEqual(str(tm.schema["a"].t), "float64")

    def test_bad_clause_name_is_a_parse_error(self):
        with unittest.TestCase().assertRaises(Exception) as cm:
            parse_strata(SRC + "model m { from s\n  select { x = row_number() over (bogus: [g]) }\n}\n", "<t>")
        self.assertIn("partition_by", str(cm.exception))

    def test_over_binds_tighter_than_binary_operators(self):
        # `lag(n) over (...) + 1` is (lag-window) + 1, not lag of (n+1 over).
        tm = check_model(
            "model m { from s\n"
            "  select { x = lag(n) over (sort: [d]) + 1 }\n}\n")
        self.assertEqual(str(tm.schema["x"].t), "int64")


class TestWindowPlacement(unittest.TestCase):
    def test_window_in_let_filter_sort_having_group_keys(self):
        for body, where in (
            ("let x = rank() over (partition_by: [g])\n  select { y = n }", "let"),
            ("filter rank() over (partition_by: [g]) == 1\n  select { y = n }", "filter"),
            ("sort { rank() over (partition_by: [g]) }\n  select { y = n }", "sort"),
            ("group { g } (\n    where rank() over (partition_by: [g]) == 2\n    aggregate { c = count() }\n  )", "having"),
            ("group { rank() over (partition_by: [g]) } (\n    aggregate { c = count() }\n  )", "group keys"),
        ):
            with self.subTest(where=where):
                e = check_error("model m { from s\n  " + body + "\n}\n")
                self.assertEqual(e.code, "E065")
                self.assertIn(where, str(e))

    def test_window_in_group_body_is_stacked_reduction(self):
        e = check_error(
            "model m {\n  from s\n  group { g } (\n"
            "    aggregate { x = rank() over (partition_by: [g]) }\n  )\n}\n")
        self.assertEqual(e.code, "E065")

    def test_non_window_function_rejects_over(self):
        for call in ("upper(g) over (partition_by: [g])",
                     "coalesce(g, g) over (partition_by: [g])"):
            with self.subTest(call=call):
                e = check_error(
                    "model m { from s\n  select { x = " + call + " }\n}\n")
                self.assertEqual(e.code, "E065")

    def test_unknown_window_function(self):
        e = check_error(
            "model m { from s\n  select { x = ntile(4) over (partition_by: [g]) }\n}\n")
        self.assertEqual(e.code, "E059")

    def test_nested_window_is_rejected(self):
        e = check_error(
            "model m { from s\n"
            "  select { x = lag(lag(n) over (sort: [d])) over (sort: [d]) }\n}\n")
        self.assertEqual(e.code, "E065")

    def test_window_args_use_the_catalog_too(self):
        e = check_error(
            "model m { from s\n  select { x = first_value(upper(n, n)) over (sort: [d]) }\n}\n")
        self.assertEqual(e.code, "E062")
        e = check_error(
            "model m { from s\n  select { x = lag() over (sort: [d]) }\n}\n")
        self.assertEqual(e.code, "E062")


class TestWindowSQL(unittest.TestCase):
    def sql(self, body, dialect=DUCKDB):
        proj = analysis.Project(parse_strata(SRC + "model m { from s\n  " + body + "\n}\n", "<t>"))
        return sqlgen.model_sql(analysis.Checker(proj).check_all()["m"], dialect=dialect)

    def test_emission_shapes(self):
        sql = self.sql(
            "select { rn = row_number() over (partition_by: [g], sort: [d desc]),\n"
            "           p = lag(n) over (sort: [d]),\n"
            "           t = sum(n) over (partition_by: [g]) }")
        self.assertIn("ROW_NUMBER() OVER (PARTITION BY g ORDER BY d DESC)", sql)
        self.assertIn("LAG(n) OVER (ORDER BY d)", sql)
        self.assertIn("SUM(n) OVER (PARTITION BY g)", sql)

    def test_windows_land_in_the_outer_query(self):
        sql = self.sql(
            "select { rn = row_number() over (partition_by: [g], sort: [d]) }")
        # the base subquery must stay window-free; windows go in the outer SELECT
        base_sql = sql.split("WITH base AS (", 1)[1].rsplit(")\nSELECT", 1)[0]
        self.assertNotIn("OVER", base_sql)
        self.assertIn("ROW_NUMBER() OVER (PARTITION BY g ORDER BY d) AS rn", sql)

    def test_round_trip_through_the_formatter(self):
        body = "select { rn = row_number() over (partition_by: [g], sort: [d desc]) }"
        once = format_module(parse_strata(SRC + "model m { from s\n  " + body + "\n}\n", "<a>"))
        twice = format_module(parse_strata(once, "<b>"))
        self.assertEqual(once, twice)
        self.assertIn("over (partition_by: [g], sort: [d desc])", once)

    def test_lineage_records_windowed_origins(self):
        tm = check_model(
            "model m { from s\n"
            "  select { p = lag(n) over (partition_by: [g], sort: [d]) }\n}\n")
        self.assertTrue({(o.node, o.col, o.kind) for o in tm.lineage["p"]} >=
                        {("s", "n", "windowed"), ("s", "g", "windowed"),
                         ("s", "d", "windowed")})


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestWindowExecutionDuckDB(unittest.TestCase):
    """End-to-end: window SQL runs on DuckDB with the expected numbers."""

    def test_ranking_and_offset_over_seed_data(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex

        d = tempfile.mkdtemp()
        path = os.path.join(d, "w.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { country: string, order_day: date, gross_amount_usd: money, is_test: bool }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  filter is_test == false\n'
            '  select {\n'
            '    rn = row_number() over (partition_by: [country], sort: [order_day, gross_amount_usd desc]),\n'
            '    prev = lag(gross_amount_usd) over (partition_by: [country], sort: [order_day]),\n'
            '    total = sum(gross_amount_usd) over (partition_by: [country]),\n'
            '    grand = sum(gross_amount_usd) over ()\n'
            '  }\n'
            '}\n')
        Path(path).write_text(text)
        con = duckdb.connect()
        for stmt in seed_sql()[0].split(";"):
            if stmt.strip():
                con.execute(stmt)
        proj = analysis.Project(parse_strata(text, path))
        tms = Checker(proj).check_all()
        ex.materialize(con, proj, tms, names=["w"])
        rows = con.execute(
            "SELECT rn, prev, total, grand "
            "FROM v_w ORDER BY rn").fetchall()
        self.assertEqual(len(rows), 4)  # ES 2 + MX 1 + BR 1 (CO test row filtered)
        # rn is per-country: ES(2 rows) -> 1,2; MX/BR(1 row each) -> 1,1
        self.assertEqual(sorted(r[0] for r in rows), [1, 1, 1, 2])
        # prev: only ES's second row has a predecessor
        non_null_prev = [r for r in rows if r[1] is not None]
        self.assertEqual(len(non_null_prev), 1)
        self.assertAlmostEqual(float(non_null_prev[0][1]), 120.00, places=2)
        # total is the per-country sum; grand is the whole-set sum on every row
        self.assertAlmostEqual(float(rows[0][3]), 485.50, places=2)  # 120+90+200+75.5
        for r in rows:
            self.assertAlmostEqual(float(r[3]), 485.50, places=2)
        self.assertTrue(all(float(r[2]) <= 485.50 for r in rows))  # total | grand

    def test_rank_after_group(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex

        d = tempfile.mkdtemp()
        path = os.path.join(d, "w2.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { country: string, gross_amount_usd: money, is_test: bool }\n'
            '}\n'
            'model w2 {\n'
            '  from orders\n'
            '  filter is_test == false\n'
            '  group { country } (\n'
            '    aggregate { total = sum(gross_amount_usd) }\n'
            '  )\n'
            '  select { rk = rank() over (sort: [total desc]) }\n'
            '}\n')
        Path(path).write_text(text)
        con = duckdb.connect()
        for stmt in seed_sql()[0].split(";"):
            if stmt.strip():
                con.execute(stmt)
        proj = analysis.Project(parse_strata(text, path))
        tms = Checker(proj).check_all()
        ex.materialize(con, proj, tms, names=["w2"])
        rows = con.execute(
            "SELECT country, total, rk FROM v_w2 ORDER BY rk").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][2], 1)
        totals = [float(r[1]) for r in rows]
        self.assertEqual(totals, sorted(totals, reverse=True))


if __name__ == "__main__":
    unittest.main()