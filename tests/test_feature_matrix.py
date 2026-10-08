"""M36: dialect feature matrix.

One runnable (non-docker) file that locks the per-warehouse SQL spellings for
the primitives the audit named (`SAFE_OFFSET`, `GET`, `JSON_QUERY`/`JSON_VALUE`,
`SPLIT`) plus the intentional fail-loud dialect gaps, and sweeps the catalog so
every declared plain function pins to a dialect spelling or an explicit
``*_UNAVAILABLE`` gap — a regression net for "the checker knows a function the
codegen cannot express on this warehouse" without a live warehouse.
"""
import unittest

from strata import analysis, functions, sqlgen
from strata.analysis import Checker, StrataError
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE
from strata.parser import parse_strata

SRC = '''source s(ns: "n", dataset: "s") {
  columns: { a: string, doc: json, xs: array(int64), key: string, n: int64 }
}
'''

# Documented dialect gaps: a `*_UNAVAILABLE` catalog spelling that makes the
# call fail loud instead of emitting SQL the warehouse will reject.
GAPS = {"bigquery": {"lpad", "rpad"}}


def model_sql(body, dialect=DUCKDB):
    module = parse_strata(SRC + f'model m {{ from s select {{ {body} }} }}')
    proj = analysis.Project(module)
    Checker(proj).check_all()
    return sqlgen.model_sql(proj.typed["m"], dialect=dialect)


class TestSplitPartMatrix(unittest.TestCase):
    """split_part: SPLIT_PART everywhere, SPLIT(...)[SAFE_OFFSET(n-1)] on BQ."""

    def _sql(self, dialect):
        return model_sql('sp = split_part(a, "-", 2)', dialect)

    def test_duckdb_postgres_snowflake_keep_split_part(self):
        for d in (DUCKDB, POSTGRES, SNOWFLAKE):
            with self.subTest(dialect=d.name):
                self.assertIn("SPLIT_PART(a, '-', 2)", self._sql(d))

    def test_bigquery_emulates_with_safe_offset(self):
        sql = self._sql(BIGQUERY)
        self.assertIn("SPLIT(a, '-')[SAFE_OFFSET(2 - 1)]", sql)
        self.assertNotIn("SPLIT_PART(", sql)


class TestArrayGetSafeOffsetGetMatrix(unittest.TestCase):
    """array_get: 0-based element access — SAFE_OFFSET (BQ), GET (SF)."""

    def _sql(self, dialect):
        return model_sql('v = array_get(xs, n)', dialect)

    def test_duckdb_list_extract(self):
        self.assertIn("LIST_EXTRACT(xs, (n) + 1)", self._sql(DUCKDB))

    def test_postgres_array_subscript(self):
        self.assertIn("(xs)[CAST((n) + ARRAY_LOWER(xs, 1) AS INTEGER)]",
                      self._sql(POSTGRES))

    def test_bigquery_safe_offset(self):
        sql = self._sql(BIGQUERY)
        self.assertIn("(xs)[SAFE_OFFSET(n)]", sql)
        self.assertNotIn("GET(", sql)

    def test_snowflake_get(self):
        sql = self._sql(SNOWFLAKE)
        self.assertIn("CAST(GET(xs, n) AS BIGINT)", sql)
        self.assertNotIn("SAFE_OFFSET", sql)


class TestStructGetMatrix(unittest.TestCase):
    """struct_get: struct_extract / -> / field access / GET by dialect."""

    def _sql(self, dialect):
        return model_sql('s = struct("id", n), v = struct_get(s, "id")', dialect)

    def test_literal_field_forms(self):
        self.assertIn("struct_extract(s, 'id')", self._sql(DUCKDB))
        self.assertIn("(s ->> 'id')", self._sql(POSTGRES))
        self.assertIn("s.id", self._sql(BIGQUERY))
        self.assertIn("GET(s, 'id')", self._sql(SNOWFLAKE))

    def test_dynamic_field_rejected_at_compile(self):
        # A dynamic struct_get field is a compile-time error (E063) on every
        # dialect; the codegen RuntimeError branches are defense-in-depth.
        for d in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
            with self.subTest(dialect=d.name):
                with self.assertRaises(StrataError) as cm:
                    model_sql('s = struct("id", n), v = struct_get(s, key)', d)
        self.assertEqual(cm.exception.code, "E063")


class TestJsonGetValueMatrix(unittest.TestCase):
    """json_get/json_value: the JSON_QUERY/JSON_VALUE split (BQ), GET (SF),
    JSON path operators (PG), JSON_EXTRACT (DuckDB)."""

    def _sql(self, body, dialect):
        return model_sql(body, dialect)

    def test_literal_key_forms(self):
        cases = {
            DUCKDB: ("JSON_EXTRACT(doc, '$.key')", "JSON_EXTRACT_STRING"),
            POSTGRES: ("(doc -> 'key')", "JSONB_TYPEOF"),
            BIGQUERY: ("JSON_QUERY(doc, '$.key')", "JSON_VALUE(doc, '$.key')"),
            SNOWFLAKE: ("GET(doc, 'key')", "TYPEOF"),
        }
        for d, (get_frag, value_frag) in cases.items():
            with self.subTest(dialect=d.name):
                self.assertIn(get_frag, self._sql('j = json_get(doc, "key")', d))
                self.assertIn(value_frag, self._sql('v = json_value(doc, "key")', d))

    def test_exclusive_spellings(self):
        bq = self._sql('j = json_get(doc, "key")', BIGQUERY)
        sf = self._sql('v = json_value(doc, "key")', SNOWFLAKE)
        for d, sql in ((BIGQUERY, bq), (SNOWFLAKE, sf)):
            with self.subTest(dialect=d.name):
                self.assertNotIn("JSON_EXTRACT", sql)
                self.assertNotIn("->", sql)
                self.assertNotIn("->>", sql)
        self.assertNotIn("JSON_QUERY", sf)
        self.assertNotIn("SAFE_OFFSET", sf)

    def test_dynamic_key_fails_loud_only_on_bigquery(self):
        # BigQuery needs a literal JSONPath, so a runtime key raises; the
        # exact-key lookup (JSON_EXTRACT/->/GET) works elsewhere, always with
        # NULLIF(key, '') folding the degenerate empty key to NULL.
        with self.assertRaises(RuntimeError):
            model_sql('j = json_get(doc, key)', BIGQUERY)
        self.assertIn("JSON_EXTRACT(doc, NULLIF(key, ''))",
                      model_sql('j = json_get(doc, key)', DUCKDB))
        self.assertIn("(doc -> NULLIF(key, ''))",
                      model_sql('j = json_get(doc, key)', POSTGRES))
        self.assertIn("GET(doc, NULLIF(key, ''))",
                      model_sql('j = json_get(doc, key)', SNOWFLAKE))


class TestCatalogDialectParity(unittest.TestCase):
    """Every declared plain (non-collection, non-date) function pins to a SQL
    spelling on every warehouse, or to an explicit *_UNAVAILABLE gap. Guards
    the catalog↔codegen feature matrix without a live warehouse."""

    def test_plain_functions_emit_on_every_dialect(self):
        plain = [f for f in functions.FUNCTIONS
                 if not f.collection and f.unit_names is None]
        self.assertGreater(len(plain), 25)
        for dialect in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
            for f in plain:
                with self.subTest(dialect=dialect.name, fn=f.name):
                    try:
                        sql = functions.emit_sql(f.name, "x, y, z", dialect)
                    except RuntimeError as err:
                        self.assertIn(
                            f.name, GAPS.get(dialect.name, ()),
                            f"{dialect.name} cannot express {f.name}(): {err}")
                        continue
                    self.assertNotIn("None(", sql)
                    self.assertIn("(", sql)
                    self.assertNotIn("UNAVAILABLE", sql)


if __name__ == "__main__":
    unittest.main()