"""Real execution of Strata SQL against BigQuery (SQL emission + live run).

Same seed fixture as Postgres E2E: v_daily_orders must yield the exact
3-row result DuckDB produces.

Skips when no BigQuery emulator or real credentials are available.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata.analysis import Checker, Project
from strata.dialects import BIGQUERY
from strata.parser import parse_strata
from strata import sqlgen

from tests.bq_harness import ephemeral_bigquery


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
  derive { net_amount = gross_amount_usd - if(refunds.discount_usd is not null, refunds.discount_usd, 0) }
  select { country, order_day, order_id, gross_amount = gross_amount_usd, net_amount }
  aggregate { country, order_day }
}
'''


def build(text, path):
    proj = Project(parse_strata(text, path))
    tms = Checker(proj).check_all()
    return proj, tms


class TestBigQueryLiveE2E(unittest.TestCase):
    """Same seed fixture, same emitted model SQL, REAL BigQuery:
    v_daily_orders must yield the exact 3-row result DuckDB produces.
    Skips when no BigQuery emulator or credentials are found.
    """

    @classmethod
    def setUpClass(cls):
        cls.project = os.environ.get("BIGQUERY_PROJECT", "strata-test")
        cls.dataset = os.environ.get("BIGQUERY_DATASET", "strata_test")
        cls.location = os.environ.get("BIGQUERY_LOCATION", "US")

    def _run_with_bq(self, test_fn):
        """Run test function with BigQuery connection, skipping if unavailable."""
        with ephemeral_bigquery(
            project=self.project,
            dataset=self.dataset,
            location=self.location,
        ) as con:
            if con is None:
                self.skipTest("No BigQuery emulator or credentials available")
            test_fn(con)

    def test_bigquery_roundtrip_matches_duckdb_rows(self):
        """Emit BigQuery SQL, run it, verify results match DuckDB."""
        self._run_with_bq(self._test_impl)

    def _test_impl(self, con):

        # Create source tables and insert test data
        project_dataset = f"`{self.project}.{self.dataset}`"

        # Create orders table
        con.execute(f"""
            CREATE OR REPLACE TABLE {project_dataset}.orders (
                order_id INT64,
                customer_id INT64,
                country STRING,
                gross_amount_usd NUMERIC(38,2),
                is_test BOOL,
                order_day DATE
            )
        """)

        # Create refunds table
        con.execute(f"""
            CREATE OR REPLACE TABLE {project_dataset}.refunds (
                order_id INT64,
                discount_usd NUMERIC(38,2),
                refunded_at TIMESTAMP
            )
        """)

        # Insert test data matching DuckDB test
        con.execute(f"""
            INSERT INTO {project_dataset}.orders VALUES
            (1, 1001, 'ES', 120.00, FALSE, DATE '2026-09-01'),
            (2, 1001, 'ES', 90.00, FALSE, DATE '2026-09-01'),
            (3, 1002, 'MX', 200.00, FALSE, DATE '2026-09-02'),
            (4, 1003, 'BR', 75.50, FALSE, DATE '2026-09-02'),
            (5, 1004, 'CO', 40.00, TRUE, DATE '2026-09-03')
        """)

        con.execute(f"""
            INSERT INTO {project_dataset}.refunds VALUES
            (2, 10.00, TIMESTAMP '2026-09-02 10:00:00'),
            (4, 5.50, TIMESTAMP '2026-09-03 09:30:00')
        """)

        # Build project and emit BigQuery SQL
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "daily_orders.strata"
            path.write_text(MODEL_TEXT)
            proj, tms = build(MODEL_TEXT, str(path))
            sql = sqlgen.full_sql(proj.typed, list(proj.typed), dialect=BIGQUERY)

            # Execute the full SQL
            con.execute(sql)

            # Query the result view
            rows = con.fetch(
                f"SELECT country, order_day, order_id, gross_amount, net_amount "
                f"FROM `{project_dataset}.v_daily_orders` "
                f"ORDER BY order_day, country"
            )

            # Expected results (same as DuckDB/Postgres test)
            expected = [
                ("ES", "2026-09-01", 2, 210.00, 200.00),
                ("BR", "2026-09-02", 1, 75.50, 70.00),
                ("MX", "2026-09-02", 1, 200.00, 200.00),
            ]

            # Compare - BigQuery returns DATE/TIMESTAMP as strings or objects
            actual = []
            for r in rows:
                country, order_day, order_id, gross, net = r
                # Convert to comparable format
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