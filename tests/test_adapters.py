"""Warehouse adapters: abstract interface and DuckDB implementation."""
import unittest

from strata.adapters import (get_adapter, DuckDBWarehouse,
                              AdapterNotAvailable, Warehouse)


class TestAdapterNotAvailable(unittest.TestCase):
    def test_help_contains_package(self):
        e = AdapterNotAvailable("postgres", "psycopg2-binary", "msg")
        self.assertIn("psycopg2-binary", e.help)
        self.assertIn("postgres", e.dialect)


class TestDuckDBWarehouse(unittest.TestCase):
    def setUp(self):
        self.wh = get_adapter("duckdb")

    def test_execute_and_fetch(self):
        self.wh.execute("CREATE TABLE t(x INT)")
        self.wh.execute("INSERT INTO t VALUES (1),(2)")
        self.assertEqual(self.wh.fetch("SELECT * FROM t ORDER BY 1"),
                         [(1,), (2,)])

    def test_materialize(self):
        self.wh.materialize("v_m", "SELECT 1 AS x")
        self.assertEqual(self.wh.fetch("SELECT * FROM v_m"), [(1,)])

    def test_drop(self):
        self.wh.execute("CREATE TABLE tmp(x INT)")
        self.wh.drop("tmp")
        with self.assertRaises(Exception):
            self.wh.fetch("SELECT * FROM tmp")

    def test_list_views(self):
        self.wh.execute("CREATE OR REPLACE VIEW v_m AS SELECT 1 AS x")
        views = self.wh.list_views()
        self.assertIn("v_m", views)


class TestAdapterUnavailable(unittest.TestCase):
    def test_postgres_raises(self):
        with self.assertRaises(AdapterNotAvailable) as cm:
            get_adapter("postgres")
        self.assertIn("psycopg2-binary", cm.exception.help)

    def test_bigquery_raises(self):
        with self.assertRaises(AdapterNotAvailable) as cm:
            get_adapter("bigquery")
        self.assertIn("google-cloud-bigquery", cm.exception.help)

    def test_snowflake_raises(self):
        with self.assertRaises(AdapterNotAvailable) as cm:
            get_adapter("snowflake")
        self.assertIn("snowflake-connector-python", cm.exception.help)


if __name__ == '__main__':
    unittest.main()
