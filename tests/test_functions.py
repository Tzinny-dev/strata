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
        self.assertIsNone(functions.get("nullif_zero"))
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
            functions.emit_sql("nullif_zero", "x", None)


class TestStringFunctions(unittest.TestCase):
    """Step 3 of the approved order: string functions over the one catalog."""

    def test_string_catalog_completeness(self):
        names = {f.name for f in functions.FUNCTIONS}
        self.assertLessEqual(
            {"length", "substring", "trim", "ltrim", "rtrim", "replace",
             "lpad", "rpad", "startswith", "split_part", "regexp_replace",
             "left", "right", "like", "rlike"}, names)

    def test_string_arity_is_checked(self):
        for call in ("length()", "length(a, b)", "trim(a, b)", "ltrim(a, b)",
                     "rtrim(a, b)", "replace(a, b)", "lpad(a, 2)",
                     "rpad(a, 2)", "startswith(a)", "startswith()",
                     "split_part(a, b)", "split_part(a, \"-\", 2, 4)",
                     "regexp_replace(a, b)", "left(a)", "left()",
                     "right(a)", "right()", "substring(a)",
                     "substring(a, 1, 2, 3)", "like(a)", "like()",
                     "rlike(a)", "rlike()"):
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
                     "left(a, b)", "right(n, 2)", "right(a, b)",
                     "like(n, \"x\")", "like(a, n)", "rlike(n, \"x\")",
                     "rlike(a, n)"):
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
                   "    lk = like(a, \"or%\"),\n"
                   "    rl = rlike(a, \"^o\"),\n"
                   "  }")
        for name, want in (("l", "int64"), ("s2", "string"), ("sw", "bool"),
                           ("sp", "string"), ("lf", "string"),
                           ("rg", "string"), ("tr", "string"),
                           ("lk", "bool"), ("rl", "bool")):
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

    def test_like_rlike_spelling_by_dialect(self):
        for d in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
            with self.subTest(dialect=d.name):
                text = "  select {\n    a1 = like(a, \"or%\"),\n    r1 = rlike(a, \"^o\"),\n  }"
                sql = sql_of(text, dialect=d)
                self.assertIn("(a LIKE 'or%')", sql)
        self.assertIn("REGEXP_MATCHES(a, '^o')",
                      sql_of("  select { r1 = rlike(a, \"^o\") }", DUCKDB))
        self.assertIn("(a ~ '^o')", sql_of("  select { r1 = rlike(a, \"^o\") }", POSTGRES))
        self.assertIn("REGEXP_CONTAINS(a, '^o')",
                      sql_of("  select { r1 = rlike(a, \"^o\") }", BIGQUERY))
        self.assertIn("REGEXP_LIKE(a, '^o')",
                      sql_of("  select { r1 = rlike(a, \"^o\") }", SNOWFLAKE))

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


class TestLikeRlikeInfixOperators(unittest.TestCase):
    """`like`/`rlike` are also infix operators (contextual: in primary
    position they remain identifiers/calls). Same semantics as the calls."""

    def test_spelling_by_dialect_matches_call_form(self):
        text = "  select {\n    o = a like \"or%\",\n    p = a rlike \"^o\",\n  }"
        for d in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
            with self.subTest(dialect=d.name):
                sql = sql_of(text, dialect=d)
                self.assertIn("(a LIKE 'or%')", sql)
                if d.name == "duckdb":
                    self.assertIn("REGEXP_MATCHES(a, '^o')", sql)
                elif d.name == "postgres":
                    self.assertIn("(a ~ '^o')", sql)
                elif d.name == "bigquery":
                    self.assertIn("REGEXP_CONTAINS(a, '^o')", sql)
                else:
                    self.assertIn("REGEXP_LIKE(a, '^o')", sql)

    def test_infix_binds_with_and_or(self):
        sql = sql_of('select { o = a like "or%" and b like "x" or a == b }')
        self.assertIn("((a LIKE 'or%') AND (b LIKE 'x')) OR (a = b)", sql)

    def test_nullable_null_adapts(self):
        inf = model('select { o = a like null }').schema["o"]
        self.assertTrue(inf.nullable)

    def test_type_errors_like_rlike(self):
        err = error('model m { from s select { o = n like "or%" } }')
        self.assertEqual(err.code, "E051")
        self.assertIn("string", str(err))
        err = error('model m { from s select { o = a rlike 5 } }')
        self.assertEqual(err.code, "E051")
        err = error('model m { from s select { o = n rlike n } }')
        self.assertEqual(err.code, "E051")

    @staticmethod
    def _project_like_cols(body):
        root = 'source s2(ns: "n", dataset: "s") { columns: { like: string, rlike: string } }\n'
        proj = analysis.Project(parse_strata(root + body, "<t>"))
        return proj, Checker(proj).check_all()

    def test_like_rlike_stay_identifiers_in_primary_position(self):
        # a column named like, and like()/rlike() as calls: contextual, not reserved.
        proj, tms = self._project_like_cols(
            'model m { from s2 filter like == "b"\n'
            '  select { o = like(like, "x"), p = rlike(like, "^x") }\n}\n')
        self.assertEqual(tms["m"].schema["o"].t.name, "bool")
        self.assertEqual(tms["m"].schema["p"].t.name, "bool")

    def test_column_named_like_roundtrips_fmt(self):
        proj, tms = self._project_like_cols(
            'model m { from s2 select { o = like like "or%" } }\n')
        sql = sqlgen.model_sql(tms["m"], dialect=DUCKDB)
        self.assertIn("(like LIKE 'or%')", sql)

    def test_filter_with_infix_operator_parses(self):
        # the motivating form from the backlog: a filter/where condition.
        m = model('filter a like "or%"\nselect { o = a }')
        self.assertEqual(m.schema["o"].t.name, "string")
        proj, tms = project('model m { from s filter a rlike "^o" select { o = a } }')
        self.assertEqual(tms["m"].schema["o"].t.name, "string")


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
            '    lk      = like(country, "E%"),\n'
            '    rl      = rlike(country, "^E"),\n'
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
            "SELECT tag, code, head, tail, trimmed, es, missing, lk, rl "
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
        self.assertTrue(first[7])       # ES matches LIKE 'E%'
        self.assertTrue(first[8])       # ES matches regexp ^E
        self.assertFalse(rows[3][5])    # BR row
        self.assertFalse(rows[3][7])
        self.assertFalse(rows[3][8])

    def test_infix_like_rlike_in_filter_runs_on_duckdb(self):
        import duckdb
        from strata import exec as ex
        from strata.seed import seed_sql

        d = tempfile.mkdtemp()
        path = os.path.join(d, "infix.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { country: string }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  filter country like "E%" and country rlike "^E"\n'
            '  select {\n'
            '    country = country,\n'
            '    e_like  = country like "E%",\n'
            '    e_rx    = country rlike "^E",\n'
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
            "SELECT country, e_like, e_rx FROM v_w").fetchall()
        self.assertGreater(len(rows), 0)
        for country, e_like, e_rx in rows:
            self.assertTrue(e_like)
            self.assertTrue(e_rx)
        self.assertEqual(rows[0][0], "ES")  # only ES survives the filter


class TestMapDictConstructors(unittest.TestCase):
    """Typed map<string, V> constructors and accessor."""

    def test_map_inferred_type(self):
        tm = model('select { m = map("a", n, "b", 9) }')
        self.assertEqual(str(tm.schema["m"].t), "map<string,int64>")

    def test_dict_alias_same_semantics(self):
        tm = model('select { d = dict("k", n) }')
        self.assertEqual(str(tm.schema["d"].t), "map<string,int64>")

    def test_map_get_returns_value_type(self):
        tm = model('select { m = map("a", n), v = map_get(m, "a") }')
        self.assertEqual(str(tm.schema["v"].t), "int64")

    def test_map_get_nullable(self):
        tm = model('select { m = map("a", n), v = map_get(m, "x") }')
        self.assertTrue(tm.schema["v"].nullable)

    def test_map_odd_args_fails(self):
        self.assertEqual(
            error('model m { from s select { m = map("a") } }').code, "E062")

    def test_map_non_string_key_fails(self):
        self.assertEqual(
            error('model m { from s select { m = map(n, 1) } }').code, "E063")

    def test_map_mixed_value_types_fails(self):
        self.assertEqual(
            error('model m { from s select { m = map("a", n, "b", f) } }').code, "E063")

    def test_map_value_date_fails(self):
        self.assertEqual(
            error('model m { from s select { m = map("a", n, "b", cast(n, "date")) } }').code,
            "E063")

    def test_map_get_non_map_fails(self):
        self.assertEqual(
            error('model m { from s select { v = map_get(n, "a") } }').code, "E063")

    def test_map_get_non_string_key_fails(self):
        self.assertEqual(
            error('model m { from s select { m = map("a", n), v = map_get(m, n) } }').code,
            "E063")


class TestMapContractTypes(unittest.TestCase):
    """map() type specs in contracts and domains."""

    def test_contract_column_map_type(self):
        body = 'contract t { c: map(string, int64) }'
        proj, tms = project(body)
        # contract is not a model; verify parsing succeeds and type resolves
        self.assertTrue(True)

    def test_domain_map_type(self):
        body = 'domain m = map(string, json)'
        proj, tms = project(body)
        # domains are checked but not in tms; just ensure no error
        self.assertTrue(True)


class TestMapCodegenByDialect(unittest.TestCase):
    """Cross-dialect SQL emission for map()/dict() and map_get()."""

    def assert_sql_contains(self, body, dialect, expected_frag):
        tm = model(body)
        sql = sql_of(body, dialect=dialect)
        self.assertIn(expected_frag, sql, f"dialect {dialect.name}: missing {expected_frag!r}")

    def test_map_constructor_duckdb(self):
        self.assert_sql_contains('select { m = map("a", n, "b", 9) }', DUCKDB,
                                 "map(CAST(['a', 'b'] AS VARCHAR[]), CAST([n, 9] AS BIGINT[]))")

    def test_map_constructor_postgres(self):
        self.assert_sql_contains('select { m = map("a", n, "b", 9) }', POSTGRES,
                                 "jsonb_build_object('a', n, 'b', 9)")

    def test_map_constructor_bigquery(self):
        self.assert_sql_contains('select { m = map("a", n, "b", 9) }', BIGQUERY,
                                 "JSON_OBJECT('a', n, 'b', 9)")

    def test_map_constructor_snowflake(self):
        self.assert_sql_contains('select { m = map("a", n, "b", 9) }', SNOWFLAKE,
                                 "OBJECT_CONSTRUCT_KEEP_NULL('a', n, 'b', 9)")

    def test_map_get_duckdb(self):
        self.assert_sql_contains('select { m = map("a", n), v = map_get(m, "a") }', DUCKDB,
                                 "CAST(map_extract(m, NULLIF('a', ''))[1] AS BIGINT)")

    def test_map_get_postgres_scalar(self):
        self.assert_sql_contains('select { m = map("a", n), v = map_get(m, "a") }', POSTGRES,
                                 "CAST((m ->> NULLIF('a', '')) AS BIGINT)")

    def test_map_get_postgres_json_value(self):
        self.assert_sql_contains('select { m = map("a", json_build("v", n)), v = map_get(m, "a") }', POSTGRES,
                                 "(m -> NULLIF('a', ''))")

    def test_map_get_bigquery_literal_key(self):
        self.assert_sql_contains('select { m = map("a", n), v = map_get(m, "a") }', BIGQUERY,
                                 "CAST(JSON_VALUE(m, '$.a') AS INT64)")

    def test_map_get_bigquery_dynamic_key_fails(self):
        # Analysis accepts (a is string column), BQ codegen fails at emit time
        # Test the codegen error directly
        tm = model('select { m = map("a", n), v = map_get(m, a) }')
        with self.assertRaises(RuntimeError) as cm:
            sqlgen.model_sql(tm, dialect=BIGQUERY)
        self.assertIn("dynamic key", str(cm.exception))

    def test_map_get_snowflake(self):
        self.assert_sql_contains('select { m = map("a", n), v = map_get(m, "a") }', SNOWFLAKE,
                                 "CAST(GET(m, NULLIF('a', '')) AS BIGINT)")


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available")
class TestMapExecutionDuckDB(unittest.TestCase):
    """End-to-end: map/map_get runs on DuckDB with expected values."""

    def test_map_roundtrip(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex
        import strata.analysis as analysis
        from strata.parser import parse_strata
        from strata.analysis import Checker

        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { order_id: int64, customer_id: int64, country: string }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  select {\n'
            '    m   = map("id", order_id, "cust", customer_id),\n'
            '    id  = map_get(m, "id"),\n'
            '    cust = map_get(m, "cust"),\n'
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
        rows = con.execute("SELECT id, cust FROM v_w ORDER BY id").fetchall()
        self.assertGreater(len(rows), 0)
        for id_val, cust_val in rows:
            self.assertIsInstance(id_val, int)
            self.assertIsInstance(cust_val, int)

    def test_map_filter_on_value(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex
        import strata.analysis as analysis
        from strata.parser import parse_strata
        from strata.analysis import Checker

        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { order_id: int64, customer_id: int64, country: string }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  filter customer_id > 1001\n'
            '  select {\n'
            '    m = map("id", order_id, "cust", customer_id),\n'
            '    id = map_get(m, "id"),\n'
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
        rows = con.execute("SELECT id FROM v_w ORDER BY id").fetchall()
        self.assertGreater(len(rows), 0)
        for (id_val,) in rows:
            self.assertIsInstance(id_val, int)


class TestStructConstructors(unittest.TestCase):
    """Typed struct constructors and accessor."""

    def test_struct_inferred_type(self):
        tm = model('select { s = struct("id", n, "name", a) }')
        self.assertEqual(str(tm.schema["s"].t), "struct<id:int64,name:string>")

    def test_struct_get_returns_field_type(self):
        tm = model('select { s = struct("id", n, "name", a), v = struct_get(s, "id") }')
        self.assertEqual(str(tm.schema["v"].t), "int64")

    def test_struct_get_nullable(self):
        tm = model('select { s = struct("id", n), v = struct_get(s, "id") }')
        self.assertTrue(tm.schema["v"].nullable)

    def test_struct_odd_args_fails(self):
        self.assertEqual(
            error('model m { from s select { s = struct("a") } }').code, "E062")

    def test_struct_non_string_name_fails(self):
        self.assertEqual(
            error('model m { from s select { s = struct(n, 1) } }').code, "E063")

    def test_struct_get_non_struct_fails(self):
        self.assertEqual(
            error('model m { from s select { v = struct_get(n, "id") } }').code, "E063")

    def test_struct_get_non_string_field_fails(self):
        self.assertEqual(
            error('model m { from s select { s = struct("id", n), v = struct_get(s, n) } }').code,
            "E063")


class TestStructContractTypes(unittest.TestCase):
    """struct field specs in contracts and domains."""

    def test_contract_column_struct_type(self):
        body = 'contract t { s: struct<id: int64, name: string> }'
        proj, tms = project(body)
        self.assertTrue(True)

    def test_domain_struct_type(self):
        body = 'domain my = struct<id: int64, name: string>'
        proj, tms = project(body)
        self.assertTrue(True)


class TestStructCodegenByDialect(unittest.TestCase):
    """Cross-dialect SQL emission for struct() and struct_get()."""

    def assert_sql_contains(self, body, dialect, expected_frag):
        sql = sql_of(body, dialect=dialect)
        self.assertIn(expected_frag, sql, f"dialect {dialect.name}: missing {expected_frag!r}")

    def test_struct_constructor_duckdb(self):
        self.assert_sql_contains('select { s = struct("id", n, "name", a) }', DUCKDB,
                                 "{'id': n, 'name': a}")

    def test_struct_constructor_postgres(self):
        self.assert_sql_contains('select { s = struct("id", n, "name", a) }', POSTGRES,
                                 "jsonb_build_object('id', n, 'name', a)")

    def test_struct_constructor_bigquery(self):
        self.assert_sql_contains('select { s = struct("id", n, "name", a) }', BIGQUERY,
                                 "STRUCT(n AS id, a AS name)")

    def test_struct_constructor_snowflake(self):
        self.assert_sql_contains('select { s = struct("id", n, "name", a) }', SNOWFLAKE,
                                 "OBJECT_CONSTRUCT_KEEP_NULL('id', n, 'name', a)")

    def test_struct_get_duckdb(self):
        self.assert_sql_contains('select { s = struct("id", n), v = struct_get(s, "id") }', DUCKDB,
                                 "struct_extract(s, 'id')")

    def test_struct_get_postgres(self):
        self.assert_sql_contains('select { s = struct("id", n), v = struct_get(s, "id") }', POSTGRES,
                                 "(s ->> 'id')")

    def test_struct_get_bigquery(self):
        self.assert_sql_contains('select { s = struct("id", n), v = struct_get(s, "id") }', BIGQUERY,
                                 "s.id")

    def test_struct_get_snowflake(self):
        self.assert_sql_contains('select { s = struct("id", n), v = struct_get(s, "id") }', SNOWFLAKE,
                                 "GET(s, 'id')")


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available")
class TestStructExecutionDuckDB(unittest.TestCase):
    """End-to-end: struct/struct_get runs on DuckDB."""

    def test_struct_roundtrip(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex
        import strata.analysis as analysis
        from strata.parser import parse_strata
        from strata.analysis import Checker

        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { order_id: int64, customer_id: int64, country: string }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  select {\n'
            '    st   = struct("id", order_id, "cust", customer_id),\n'
            '    id   = struct_get(st, "id"),\n'
            '    cust = struct_get(st, "cust"),\n'
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
        rows = con.execute("SELECT id, cust FROM v_w ORDER BY id").fetchall()
        self.assertGreater(len(rows), 0)
        for id_val, cust_val in rows:
            self.assertIsInstance(id_val, int)
            self.assertIsInstance(cust_val, int)

    def test_struct_in_filter(self):
        import duckdb
        from strata.seed import seed_sql
        from strata import exec as ex
        import strata.analysis as analysis
        from strata.parser import parse_strata
        from strata.analysis import Checker

        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.strata")
        text = (
            'source orders(ns: "n", dataset: "orders") {\n'
            '  columns: { order_id: int64, customer_id: int64, country: string }\n'
            '}\n'
            'model w {\n'
            '  from orders\n'
            '  filter customer_id > 1001\n'
            '  select {\n'
            '    st = struct("id", order_id, "cust", customer_id),\n'
            '    id = struct_get(st, "id"),\n'
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
        rows = con.execute("SELECT id FROM v_w ORDER BY id").fetchall()
        self.assertGreater(len(rows), 0)
        for (id_val,) in rows:
            self.assertIsInstance(id_val, int)


if __name__ == "__main__":
    unittest.main()


