"""Real execution of Strata SQL against Snowflake (SQL emission + live run).

Same seed fixture as Postgres E2E: v_daily_orders must yield the exact
3-row result DuckDB produces.

Skips when no Snowflake emulator (LocalStack) or real credentials are available.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata.analysis import Checker, Project
from strata.dialects import SNOWFLAKE
from strata.parser import parse_strata
from strata import sqlgen

from tests.sf_harness import ephemeral_snowflake


MODEL_TEXT = '''source orders(ns: "n", dataset: "orders") {
  columns: { order_id: int64 nonnull, customer_id: int64 nonnull, country: string nonnull, gross_amount_usd: money nonnull, is_test: bool nonnull, order_day: date nonnull }
}

source refunds(ns: "n", dataset: "refunds") {
  columns: { order_id: int64 nonnull, discount_usd: money nonnull, refunded_at: timestamp nonnull }
}

contract DailyOrdersContract {
  country: string nonnull
  order_day: date nonnull
  order_id: int64 nonnull
  gross_amount: money nonnull
  net_amount: money nonnull
}

model daily_orders -> contract DailyOrdersContract {
  from orders
  join_left refunds on orders.order_id == refunds.order_id
  filter orders.is_test == false
  group { country, order_day } (
    aggregate {
      order_id = count(orders.order_id),
      gross_amount = sum(orders.gross_amount_usd),
      net_amount = sum(orders.gross_amount_usd - coalesce(refunds.discount_usd, 0)),
    }
  )
}
'''


def build(text, path):
    proj = Project(parse_strata(text, path))
    tms = Checker(proj).check_all()
    return proj, tms


class TestSnowflakeLiveE2E(unittest.TestCase):
    """Same seed fixture, same emitted model SQL, REAL Snowflake:
    v_daily_orders must yield the exact 3-row result DuckDB produces.
    Skips when no Snowflake emulator or credentials are found.
    """

    @classmethod
    def setUpClass(cls):
        cls.account = os.environ.get("SNOWFLAKE_ACCOUNT", "test")
        cls.user = os.environ.get("SNOWFLAKE_USER", "test")
        cls.password = os.environ.get("SNOWFLAKE_PASSWORD", "test")
        cls.warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "TEST_WH")
        cls.database = os.environ.get("SNOWFLAKE_DATABASE", "STRATA_TEST")
        cls.schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")

    def _run_with_sf(self, test_fn):
        """Run test function with Snowflake connection, skipping if unavailable."""
        with ephemeral_snowflake(
            account=self.account,
            user=self.user,
            password=self.password,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema,
        ) as con:
            if con is None:
                self.skipTest("No Snowflake emulator or credentials available")
            test_fn(con)

    def test_snowflake_roundtrip_matches_duckdb_rows(self):
        """Emit Snowflake SQL, run it, verify results match DuckDB."""
        self._run_with_sf(self._test_impl)

    def _test_impl(self, con):
        # Create source tables and insert test data
        # Snowflake uses schema-qualified names
        schema_qualified = f"{self.database}.{self.schema}"

        # Create orders table
        con.execute(f"""
            CREATE OR REPLACE TABLE {schema_qualified}.orders (
                order_id BIGINT,
                customer_id BIGINT,
                country VARCHAR,
                gross_amount_usd NUMERIC(38,2),
                is_test BOOLEAN,
                order_day DATE
            )
        """)

        # Create refunds table
        con.execute(f"""
            CREATE OR REPLACE TABLE {schema_qualified}.refunds (
                order_id BIGINT,
                discount_usd NUMERIC(38,2),
                refunded_at TIMESTAMP
            )
        """)

        # Insert test data matching DuckDB test
        con.execute(f"""
            INSERT INTO {schema_qualified}.orders VALUES
            (1, 1001, 'ES', 120.00, FALSE, DATE '2026-09-01'),
            (2, 1001, 'ES', 90.00, FALSE, DATE '2026-09-01'),
            (3, 1002, 'MX', 200.00, FALSE, DATE '2026-09-02'),
            (4, 1003, 'BR', 75.50, FALSE, DATE '2026-09-02'),
            (5, 1004, 'CO', 40.00, TRUE, DATE '2026-09-03')
        """)

        con.execute(f"""
            INSERT INTO {schema_qualified}.refunds VALUES
            (2, 10.00, TIMESTAMP '2026-09-02 10:00:00'),
            (4, 5.50, TIMESTAMP '2026-09-03 09:30:00')
        """)

        # Build project and emit Snowflake SQL
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "daily_orders.strata"
            path.write_text(MODEL_TEXT)
            proj, tms = build(MODEL_TEXT, str(path))
            sql = sqlgen.full_sql(proj.typed, list(proj.typed), dialect=SNOWFLAKE)

            # Execute the full SQL
            con.execute(sql)

            # Query the result view
            rows = con.fetch(
                f"SELECT country, order_day, order_id, gross_amount, net_amount "
                f"FROM {schema_qualified}.v_daily_orders "
                f"ORDER BY order_day, country"
            )

            # Expected results (same as DuckDB/Postgres test)
            expected = [
                ("ES", "2026-09-01", 2, 210.00, 200.00),
                ("BR", "2026-09-02", 1, 75.50, 70.00),
                ("MX", "2026-09-02", 1, 200.00, 200.00),
            ]

            # Compare - Snowflake returns native Python types
            actual = []
            for r in rows:
                country, order_day, order_id, gross, net = r
                actual.append((
                    str(country),
                    str(order_day),
                    int(order_id),
                    float(gross),
                    float(net),
                ))

            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()