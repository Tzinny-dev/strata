"""The function catalog: one declaration read by the typechecker and codegen.

Regression for the bug this module closes: `upper(a, b)` used to typecheck and
emit `UPPER(a, b)` — invalid SQL that only failed at the warehouse instead of at
compile time, because the names were listed in three places and no consumer
checked arity. Every assertion here is about the single source of truth holding.
"""
import unittest

from strata import analysis, functions, sqlgen
from strata.analysis import StrataError, Checker
from strata.dialects import DUCKDB, Dialect
from strata.parser import parse_strata


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
        self.assertIsNone(functions.get("length"))
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
            functions.emit_sql("length", "x", None)


if __name__ == "__main__":
    unittest.main()


