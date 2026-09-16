"""The function catalog: one declaration read by the typechecker and codegen.

Regression for the bug this module closes: `upper(a, b)` used to typecheck and
emit `UPPER(a, b)` — invalid SQL that only failed at the warehouse instead of at
compile time, because the names were listed in three places and no consumer
checked arity. Every assertion here is about the single source of truth holding.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata import analysis, functions, sqlgen
from strata.analysis import StrataError, Checker
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE, Dialect
from strata.parser import parse_strata

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False


SRC = ('source s(ns: "n", dataset: "s") {\n'
       '  columns: { a: string, b: string, n: int64, f: float64, m: money }\n'
       '}\n')


def project(body, path="<t>"):
    proj = analysis.Project(parse_strata(SRC + body, path))
    return proj, Checker(proj).check_all()


def model(body):
    proj, tms = project("model m {\n  from s\n" + body + "\n}\n")
    return tms["m"]


def error(module_body):
    """Compile a module body expected to fail; return the StrataError."""
    proj = analysis.Project(parse_strata(SRC + module_body, "<t>"))
    with unittest.TestCase().assertRaises(StrataError) as cm:
        Checker(proj).check_all()
    return cm.exception


def sql_of(body, dialect=DUCKDB):
    proj, tms = project("model m {\n  from s\n" + body + "\n}\n")
    return sqlgen.model_sql(tms["m"], dialect=dialect)


class TestCatalogShape(unittest.TestCase):
    def test_names_unique_and_lookup_agrees(self):
        names = [f.name for f in functions.FUNCTIONS]
        self.assertEqual(len(names), len(set(names)))
        for f in functions.FUNCTIONS:
            self.assertIs(functions.get(f.name), f)

    def test_aggregate_set_is_derived_not_hardcoded(self):
        self.assertEqual(
            functions.AGGREGATES,
            frozenset(f.name for f in functions.FUNCTIONS if f.aggregate))
        self.assertEqual(analysis.AGGREGATES, functions.AGGREGATES)
        self.assertIn("count", functions.AGGREGATES)
        self.assertNotIn("upper", functions.AGGREGATES)

    def test_only_declared_functions_speak_sql(self):
        self.assertIsNone(functions.get("date_add"))
        self.assertEqual(functions.emit_sql("upper", "x", DUCKDB), "UPPER(x)")


class TestArity(unittest.TestCase):
    """Wrong arity must fail at compile time, never emit invalid SQL."""

    def test_upper_wrong_arity(self):
        self.assertEqual(error("model m { from s\n  select { x = upper(a, b) }\n}\n").code, "E062")
        self.assertEqual(error("model m { from s\n  select { x = upper() }\n}\n").code, "E062")

    def test_single_arg_functions_reject_extra_arguments(self):
        for call in ("sum(n, n)", "avg(n, n)", "max(a, b)", "lower(a, b)"):
            with self.subTest(call=call):
                self.assertEqual(
                    error("model m {\n  from s\n  group { a } (\n"
                          f"    aggregate {{ x = {call} }}\n  )\n}}\n").code,
                    "E062")

    def test_count_accepts_zero_one_and_star_but_not_two(self):
        ok = model("  group { a } (\n    aggregate { n = count() }\n  )")
        self.assertEqual(str(ok.schema["n"].t), "int64")
        self.assertEqual(error("model m {\n  from s\n  group { a } (\n"
                               "    aggregate { x = count(a, a) }\n  )\n}\n").code, "E062")

    def test_coalesce_requires_at_least_one_argument(self):
        self.assertEqual(error("model m { from s\n  select { x = coalesce() }\n}\n").code, "E062")

    def test_cast_is_a_construct_with_fixed_arity(self):
        self.assertEqual(error("model m { from s\n  select { x = cast(a) }\n}\n").code, "E062")
        tm = model("  select { x = cast(a, \"string\") }")
        self.assertEqual(str(tm.schema["x"].t), "string")


class TestArgumentTypes(unittest.TestCase):
    def test_string_functions_reject_non_strings(self):
        for body in ("select { x = upper(n) }",
                     "select { x = lower(n) }",
                     "select { x = upper(m) }"):
            with self.subTest(body=body):
                self.assertEqual(
                    error("model m { from s\n  " + body + "\n}\n").code, "E063")

    def test_numeric_aggregates_reject_strings(self):
        for fn in ("sum", "avg"):
            with self.subTest(fn=fn):
                self.assertEqual(
                    error("model m {\n  from s\n  group { a } (\n"
                          f"    aggregate {{ x = {fn}(a) }}\n  )\n}}\n").code,
                    "E063")

    def test_money_is_numeric_for_sum(self):
        tm = model("  group { a } (\n    aggregate { total = sum(m) }\n  )")
        self.assertEqual(str(tm.schema["total"].t), "money(USD)")

    def test_unknown_and_null_literals_adapt(self):
        # A NULL literal carries no type, so it must not trip the string check.
        tm = model("  select { x = coalesce(a, null) }")
        self.assertEqual(str(tm.schema["x"].t), "string")

    def test_coalesce_mismatch_is_rejected(self):
        self.assertEqual(
            error("model m { from s\n  select { x = coalesce(a, n) }\n}\n").code,
            "E058")


class TestResultTypes(unittest.TestCase):
    def test_count_is_nonnull_int64(self):
        tm = model("  group { a } (\n    aggregate { k = count(*), j = count(n) }\n  )")
        self.assertEqual(str(tm.schema["k"].t), "int64")
        self.assertFalse(tm.schema["k"].nullable)
        self.assertFalse(tm.schema["j"].nullable)

    def test_avg_is_float64_and_sum_keeps_its_type(self):
        tm = model("  group { a } (\n    aggregate { x = avg(n), y = sum(n) }\n  )")
        self.assertEqual(str(tm.schema["x"].t), "float64")
        self.assertEqual(str(tm.schema["y"].t), "int64")

    def test_min_max_preserve_the_argument_type(self):
        tm = model("  group { a } (\n    aggregate { lo = min(n), hi = max(n) }\n  )")
        self.assertEqual(str(tm.schema["lo"].t), "int64")
        self.assertEqual(str(tm.schema["hi"].t), "int64")

    def test_nullability_propagates_from_the_argument(self):
        self.assertTrue(model("  select { x = upper(a) }").schema["x"].nullable)


class TestStarArgument(unittest.TestCase):
    def test_count_star_and_count_empty_emit_count_star(self):
        self.assertIn("COUNT(*)", sql_of("  group { a } (\n    aggregate { k = count(*) }\n  )"))
        self.assertIn("COUNT(*)", sql_of("  group { a } (\n    aggregate { k = count() }\n  )"))

    def test_star_outside_count_is_rejected(self):
        self.assertEqual(
            error("model m {\n  from s\n  group { a } (\n"
                  "    aggregate { k = sum(*) }\n  )\n}\n").code, "E064")
        self.assertEqual(
            error("model m { from s\n  select { x = * }\n}\n").code, "E064")
        self.assertEqual(
            error("model m { from s\n  select { x = upper(*) }\n}\n").code, "E064")


class TestSqlSpelling(unittest.TestCase):
    def test_dialect_map_keeps_the_last_word_on_spelling(self):
        renamed = Dialect("renamed", DUCKDB._quote, {}, lambda p, s: "N", "N",
                          lambda e: "N", True, function_map={"upper": "UPPERCASE_FN"})
        sql = sql_of("  select { x = upper(a) }", dialect=renamed)
        self.assertIn("UPPERCASE_FN(", sql)
        self.assertNotIn("UPPER(", sql.replace("UPPERCASE_FN(", ""))

    def test_default_spelling_comes_from_the_catalog(self):
        for name, spelling in (("upper", "UPPER"), ("count", "COUNT"),
                               ("sum", "SUM"), ("coalesce", "COALESCE")):
            with self.subTest(name=name):
                self.assertEqual(functions.emit_sql(name, "x", None),
                                 f"{spelling}(x)")

    def test_concat_is_a_spec_named_variadic(self):
        self.assertEqual(error("model m { from s\n  select { x = concat() }\n}\n").code, "E062")
        tm = model("  select { x = concat(a, b) }")
        self.assertEqual(str(tm.schema["x"].t), "string")
        self.assertIn("CONCAT(", sql_of("  select { x = concat(a, b) }"))

    def test_codegen_refuses_functions_the_catalog_does_not_declare(self):
        with self.assertRaises(RuntimeError):
            functions.emit_sql("date_add", "x", None)


class TestStringFunctions(unittest.TestCase):
    """Step 3 of the approved order: string functions over the one catalog."""

    def test_string_catalog_completeness(self):
        names = {f.name for f in functions.FUNCTIONS}
        self.assertLessEqual(
            {"length", "substring", "trim", "ltrim", "rtrim", "replace",
             "lpad", "rpad", "startswith", "split_part", "regexp_replace",
             "left", "right"}, names)

    def test_string_arity_is_checked(self):
        for call in ("length()", "length(a, b)", "trim(a, b)", "ltrim(a, b)",
                     "rtrim(a, b)", "replace(a, b)", "lpad(a, 2)",
                     "rpad(a, 2)", "startswith(a)", "startswith()",
                     "split_part(a, b)", "split_part(a, \"-\", 2, 4)",
                     "regexp_replace(a, b)", "left(a)", "left()",
                     "right(a)", "right()", "substring(a)",
                     "substring(a, 1, 2, 3)"):
            with self.subTest(call=call):
                self.assertEqual(
                    error("model m { from s\n  select { x = "
                          + call + " }\n}\n").code, "E062")

    def test_string_argument_types_are_checked_positionally(self):
        for call in ("length(n)", "substring(n, 2)", "substring(a, b)",
                     "trim(n)", "ltrim(f)", "rtrim(m)", "replace(a, n, b)",
                     "lpad(a, b, \"x\")", "lpad(a, 2, n)", "rpad(a, b, \"x\")",
                     "startswith(n, \"x\")", "startswith(a, n)",
                     "split_part(a, n, 2)", "split_part(a, \"-\", b)",
                     "regexp_replace(a, n, \"x\")", "left(n, 2)",
                     "left(a, b)", "right(n, 2)", "right(a, b)"):
            with self.subTest(call=call):
                self.assertEqual(
                    error("model m { from s\n  select { x = "
                          + call + " }\n}\n").code, "E063")

    def test_positional_error_message_names_the_position(self):
        msg = error("model m { from s\n  select { x = split_part(a, n, 2) }\n}\n")
        self.assertIn("argument 2", str(msg))
        self.assertIn("string", str(msg))
        msg2 = error("model m { from s\n  select { x = left(a, b) }\n}\n")
        self.assertIn("int", str(msg2))

    def test_result_types_and_nullability(self):
        tm = model("  select {\n"
                   "    l = length(a),\n"
                   "    s2 = substring(a, 2),\n"
                   "    sw = startswith(a, \"or\"),\n"
                   "    sp = split_part(a, \"-\", 2),\n"
                   "    lf = left(a, 3),\n"
                   "    rg = right(a, 3),\n"
                   "    tr = trim(a),\n"
                   "  }")
        for name, want in (("l", "int64"), ("s2", "string"), ("sw", "bool"),
                           ("sp", "string"), ("lf", "string"),
                           ("rg", "string"), ("tr", "string")):
            with self.subTest(col=name):
                self.assertEqual(str(tm.schema[name].t), want)
                self.assertTrue(tm.schema[name].nullable)

    def test_sql_spelling_by_dialect(self):
        duck = sql_of("  select {\n"
                      "    s3 = substring(a, 2, 3),\n"
                      "    s2 = substring(a, 2),\n"
                      "    sw = startswith(a, \"or\"),\n"
                      "    sp = split_part(a, \"-\", 2),\n"
                      "  }", dialect=DUCKDB)
        self.assertIn("SUBSTRING(a FROM 2 FOR 3)", duck)
        self.assertIn("SUBSTRING(a FROM 2)", duck)
        self.assertIn("STARTS_WITH(a, 'or')", duck)
        self.assertIn("SPLIT_PART(a, '-', 2)", duck)
        bq = sql_of("  select {\n"
                    "    s3 = substring(a, 2, 3),\n"
                    "    sw = startswith(a, \"or\"),\n"
                    "    sp = split_part(a, \"-\", 2),\n"
                    "  }", dialect=BIGQUERY)
        self.assertIn("SUBSTRING(a FROM 2 FOR 3)", bq)
        self.assertIn("LIKE_PREFIX(a, 'or')", bq)
        self.assertIn("SPLIT(a, '-')[SAFE_OFFSET(2 - 1)]", bq)
        sf = sql_of("  select { sw = startswith(a, \"or\") }", dialect=SNOWFLAKE)
        self.assertIn("STARTSWITH(a, 'or')", sf)
        for d in (DUCKDB, BIGQUERY, SNOWFLAKE, POSTGRES):
            with self.subTest(dialect=d.name):
                self.assertIn("TRIM(a)",
                              sql_of("  select { t = trim(a) }", dialect=d))

    def test_lpad_rpad_rejected_on_bigquery(self):
        # GoogleSQL has no LPAD/RPAD; the dialect adapter names the gap and
        # codegen fails loud instead of emitting a silently-broken call.
        with self.assertRaises(RuntimeError):
            sql_of("  select { p = lpad(a, 2, \"x\") }", dialect=BIGQUERY)
        with self.assertRaises(RuntimeError):
            sql_of("  select { p = rpad(a, 2, \"x\") }", dialect=BIGQUERY)
        self.assertIn("LPAD(a, 2, 'x')",
                      sql_of("  select { p = lpad(a, 2, \"x\") }", dialect=DUCKDB))

    def test_direct_emit_sql_paths(self):
        # No-dialect path: SPLIT_PART is the catalog default.
        self.assertEqual(functions.emit_sql("split_part", "x, d, 2", None),
                         "SPLIT_PART(x, d, 2)")
        # BigQuery emulates split_part with SPLIT; other dialects keep it.
        self.assertEqual(functions.emit_sql("split_part", "x, d, 2", BIGQUERY),
                         "SPLIT(x, d, 2)")
        self.assertEqual(functions.emit_sql("split_part", "x, d, 2", SNOWFLAKE),
                         "SPLIT_PART(x, d, 2)")
        self.assertEqual(functions.emit_sql("startswith", "x, 'p'", DUCKDB),
                         "STARTS_WITH(x, 'p')")


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestStringExecutionDuckDB(unittest.TestCase):
    """End-to-end: string SQL runs on DuckDB with the expected values."""

    def test_string_pipeline_over_seed_data(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex

        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { order_id: int64, country: string, is_test: bool }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  filter is_test == false\n'
            '  select {\n'
            '    tag     = concat(left(country, 1), "-"),\n'
            '    code    = lpad(cast(order_id, "string"), 6, "0"),\n'
            '    head    = substring(country, 1, 2),\n'
            '    tail    = right(country, 1),\n'
            '    trimmed = trim("  " || country || "  "),\n'
            '    es      = startswith(country, "ES"),\n'
            '    missing = split_part(country, "|", 3),\n'
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
            "SELECT tag, code, head, tail, trimmed, es, missing "
            "FROM v_w ORDER BY code").fetchall()
        self.assertEqual(len(rows), 4)  # CO test row filtered
        first = rows[0]                 # order 1: ES, 120.00
        self.assertEqual(first[0], "E-")
        self.assertEqual(first[1], "000001")
        self.assertEqual(first[2], "ES")
        self.assertEqual(first[3], "S")
        self.assertEqual(first[4], "ES")
        self.assertTrue(first[5])
        # part 3 of a 1-part split: empty string (SPLIT_PART semantics)
        self.assertEqual(first[6], "")
        self.assertFalse(rows[3][5])    # BR row


if __name__ == "__main__":
    unittest.main()


