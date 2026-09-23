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


class TestBigQueryWarehouseMock(unittest.TestCase):
    def test_get_adapter_bigquery_with_mock(self):
        from unittest.mock import MagicMock
        import sys

        mock_bq = MagicMock()
        mock_client = MagicMock()
        mock_bq.Client.return_value = mock_client
        mock_cloud = MagicMock()
        mock_cloud.bigquery = mock_bq
        sys.modules['google'] = MagicMock()
        sys.modules['google.cloud'] = mock_cloud
        sys.modules['google.cloud.bigquery'] = mock_bq
        # reload to pick up mock
        import importlib
        import strata.adapters
        importlib.reload(strata.adapters)
        wh = strata.adapters.get_adapter('bigquery', project='proj', dataset='ds')
        self.assertEqual(wh.dataset, 'ds')
        # cleanup
        del sys.modules['google.cloud.bigquery']
        del sys.modules['google.cloud']
        del sys.modules['google']
        importlib.reload(strata.adapters)

    def test_bigquery_conn_execute(self):
        from unittest.mock import MagicMock
        import sys

        mock_bq = MagicMock()
        mock_client = MagicMock()
        mock_job = MagicMock()
        mock_job.result.return_value = []
        mock_job.schema = []
        mock_client.query.return_value = mock_job
        mock_bq.Client.return_value = mock_client
        mock_cloud = MagicMock()
        mock_cloud.bigquery = mock_bq
        sys.modules['google'] = MagicMock()
        sys.modules['google.cloud'] = mock_cloud
        sys.modules['google.cloud.bigquery'] = mock_bq
        import importlib
        import strata.adapters
        import strata.dbcompat
        importlib.reload(strata.dbcompat)
        importlib.reload(strata.adapters)
        con = strata.adapters.get_adapter('bigquery', project='p', dataset='d')
        # Warehouse also acts as conn for exec.py
        con.execute('SELECT 1')
        self.assertTrue(mock_client.query.called)
        del sys.modules['google.cloud.bigquery']
        del sys.modules['google.cloud']
        del sys.modules['google']
        importlib.reload(strata.dbcompat)
        importlib.reload(strata.adapters)


class TestSnowflakeWarehouseMock(unittest.TestCase):
    def test_get_adapter_snowflake_with_mock(self):
        from unittest.mock import MagicMock
        import sys

        mock_sf = MagicMock()
        mock_raw = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_raw.cursor.return_value = mock_cursor
        mock_sf.connect.return_value = mock_raw
        mock_parent = MagicMock()
        mock_parent.connector = mock_sf
        sys.modules['snowflake'] = mock_parent
        sys.modules['snowflake.connector'] = mock_sf
        import importlib
        import strata.adapters
        importlib.reload(strata.adapters)
        wh = strata.adapters.get_adapter('snowflake', account='a', user='u', password='p')
        self.assertEqual(wh.__class__.__name__, 'SnowflakeWarehouse')
        del sys.modules['snowflake.connector']
        del sys.modules['snowflake']
        importlib.reload(strata.adapters)

    def test_snowflake_open_warehouse_url(self):
        from unittest.mock import MagicMock
        import sys

        mock_sf = MagicMock()
        mock_raw = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_raw.cursor.return_value = mock_cursor
        mock_sf.connect.return_value = mock_raw
        mock_parent = MagicMock()
        mock_parent.connector = mock_sf
        sys.modules['snowflake'] = mock_parent
        sys.modules['snowflake.connector'] = mock_sf
        import importlib
        import strata.cli
        import strata.dbcompat
        importlib.reload(strata.dbcompat)
        # open_warehouse should parse snowflake:// URL
        con = strata.cli.open_warehouse('snowflake://user:pass@acct/db/schema?warehouse=WH&role=ROLE')
        self.assertEqual(con.__class__.__name__, 'SnowflakeConn')
        del sys.modules['snowflake.connector']
        del sys.modules['snowflake']
        importlib.reload(strata.dbcompat)

    def test_bigquery_open_warehouse_url(self):
        from unittest.mock import MagicMock
        import sys

        mock_bq = MagicMock()
        mock_client = MagicMock()
        mock_job = MagicMock()
        mock_job.result.return_value = []
        mock_job.schema = []
        mock_client.query.return_value = mock_job
        mock_bq.Client.return_value = mock_client
        mock_cloud = MagicMock()
        mock_cloud.bigquery = mock_bq
        sys.modules['google'] = MagicMock()
        sys.modules['google.cloud'] = mock_cloud
        sys.modules['google.cloud.bigquery'] = mock_bq
        import importlib
        import strata.cli
        import strata.dbcompat
        importlib.reload(strata.dbcompat)
        con = strata.cli.open_warehouse('bigquery://proj/ds?location=US')
        self.assertEqual(con.__class__.__name__, 'BigQueryConn')
        self.assertEqual(con.dataset, 'ds')
        del sys.modules['google.cloud.bigquery']
        del sys.modules['google.cloud']
        del sys.modules['google']
        importlib.reload(strata.dbcompat)


if __name__ == '__main__':
    unittest.main()
