# -- dialects -------------------------------------------------
import unittest
from pathlib import Path

from strata.dialects import get_dialect, DUCKDB, BIGQUERY, SNOWFLAKE


class TestDialectRegistry(unittest.TestCase):
    def test_registry_roundtrip(self):
        self.assertIs(get_dialect("duckdb"), DUCKDB)
        self.assertIs(get_dialect("bigquery"), BIGQUERY)
        self.assertIs(get_dialect("snowflake"), SNOWFLAKE)

    def test_unknown_dialect_fails_loudly(self):
        with self.assertRaises(ValueError):
            get_dialect("oracle")

    def test_quote_ident(self):
        self.assertEqual(DUCKDB.ident("country"), "country")
        self.assertEqual(BIGQUERY.ident("country"), "`country`")
        self.assertEqual(SNOWFLAKE.ident("country"), '"country"')

    def test_sql_type_map(self):
        self.assertEqual(DUCKDB.sql_type("int64"), "BIGINT")
        self.assertEqual(BIGQUERY.sql_type("int64"), "INT64")
        self.assertEqual(SNOWFLAKE.sql_type("int64"), "BIGINT")
        self.assertEqual(BIGQUERY.sql_type("string"), "STRING")
        self.assertEqual(SNOWFLAKE.sql_type("string"), "VARCHAR")
        self.assertEqual(SNOWFLAKE.sql_type("uuid"), "VARCHAR")
        self.assertEqual(BIGQUERY.sql_type("uuid"), "STRING")

    def test_money_decimal_array(self):
        self.assertEqual(DUCKDB.decimal_sql(18, 4), "DECIMAL(18,4)")
        self.assertEqual(BIGQUERY.decimal_sql(18, 4), "NUMERIC(18,4)")
        self.assertEqual(SNOWFLAKE.decimal_sql(18, 4), "NUMBER(18,4)")
        self.assertEqual(DUCKDB.money, "DECIMAL(38,2)")
        self.assertEqual(BIGQUERY.money, "NUMERIC(38,2)")
        self.assertEqual(SNOWFLAKE.money, "NUMBER(38,2)")

    def test_anti_semi_capability(self):
        self.assertTrue(DUCKDB.supports_anti_semi)
        self.assertFalse(BIGQUERY.supports_anti_semi)
        self.assertFalse(SNOWFLAKE.supports_anti_semi)


class TestCastTargets(unittest.TestCase):
    """cast() target resolution must be dialect-correct, not warehouse-*luck*."""

    def test_string_cast(self):
        self.assertEqual(DUCKDB.cast_target("string"), "VARCHAR")
        self.assertEqual(BIGQUERY.cast_target("string"), "STRING")
        self.assertEqual(SNOWFLAKE.cast_target("string"), "VARCHAR")

    def test_decimal_cast(self):
        self.assertEqual(DUCKDB.cast_target("decimal(18,4)"), "DECIMAL(18,4)")
        self.assertEqual(BIGQUERY.cast_target("decimal(18,4)"), "NUMERIC(18,4)")
        self.assertEqual(SNOWFLAKE.cast_target("decimal(18,4)"), "NUMBER(18,4)")

    def test_money_cast(self):
        self.assertEqual(DUCKDB.cast_target("money"), "DECIMAL(38,2)")
        self.assertEqual(BIGQUERY.cast_target("money"), "NUMERIC(38,2)")
        self.assertEqual(SNOWFLAKE.cast_target("money"), "NUMBER(38,2)")

    def test_unknown_cast_fails_loudly(self):
        with self.assertRaises(ValueError):
            DUCKDB.cast_target("blob")


class TestIdentifierSafety(unittest.TestCase):
    """Protected/derived columns that happen to be reserved words must not
    silently produce broken SQL on whichever warehouse you emit for."""

    def test_reserved_word_quoted(self):
        self.assertEqual(BIGQUERY.ident("order"), "`order`")
        self.assertEqual(SNOWFLAKE.ident("order"), '"order"')

    def test_qualified_reserved(self):
        self.assertEqual(BIGQUERY.qualified("t0", "order"), "t0.`order`")
        self.assertEqual(SNOWFLAKE.qualified("t0", "order"), 't0."order"')


if __name__ == "__main__":
    unittest.main()
