"""Entrega B: phase-C physical-schema pins.

The compiled plan trusts the declared source schema; the warehouse is not
trusted. Before publishing, each contract field must exist in the staged
view and its DuckDB storage type must match the declared Strata type
(decimal precision/scale exact; integral widening into int64 allowed;
narrowing or drift fails the pin and nothing is published).
"""
import unittest

from strata.analysis import Checker, Project
from strata.exec import PinError, materialize
from strata.parser import parse_strata

try:
    import duckdb
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

MODULE = '''source t(ns: "n", dataset: "d") {
  columns: {
    id:     int64 nonnull,
    amount: money nonnull,
    ts:     timestamp nonnull,
    ratio:  float64,
    who:    string nonnull,
  }
}
contract C {
  id:     int64 nonnull
  amount: money nonnull
  ts:     timestamp nonnull
  ratio:  float64
  who:    string nonnull
}
model m -> contract C { from t }
'''


def build():
    proj = Project(parse_strata(MODULE))
    tms = Checker(proj).check_all()
    return proj, tms


def con_with(amount="DECIMAL(38,2)", ts="TIMESTAMP", id_type="BIGINT", drop_who=False):
    con = duckdb.connect()
    cols = [f"id {id_type}", f"amount {amount}", f"ts {ts}", "ratio DOUBLE"]
    values = ["1", "9.50", "TIMESTAMP '2026-09-01 10:00:00'", "0.5"]
    if not drop_who:
        cols.append("who VARCHAR")
        values.append("'a'")
    con.execute("CREATE TABLE t (" + ", ".join(cols) + ")")
    con.execute("INSERT INTO t VALUES (" + ", ".join(values) + ")")
    return con


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestPhysicalSchemaPins(unittest.TestCase):
    def attempt(self, **kw):
        """Materialize m; return (PinError or None, live-tables set)."""
        proj, tms = build()
        con = con_with(**kw)
        try:
            materialize(con, proj, tms, names=["m"])
            error = None
        except PinError as pe:
            error = pe
        except Exception as ex:  # warehouse-side failure must abort the run too
            error = ex
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        con.close()
        return error, tables

    def test_matching_schema_publishes_and_reports_schema_pins(self):
        error, tables = self.attempt()
        self.assertIsNone(error)
        self.assertIn("v_m", tables)

    def test_schema_pin_reported_per_field(self):
        proj, tms = build()
        con = con_with()
        try:
            applied, pins = materialize(con, proj, tms, names=["m"])
            for field in ("id", "amount", "ts", "ratio", "who"):
                self.assertTrue(
                    any(f"m.{field}:" in line and "(schema)" in line for line in pins),
                    f"no schema pin reported for {field}: {pins}")
        finally:
            con.close()

    def test_wrong_physical_type_fails_without_publishing(self):
        error, tables = self.attempt(amount="DOUBLE")
        self.assertIsNotNone(error)
        self.assertIn("amount", str(error))
        self.assertIn("DOUBLE", str(error))
        self.assertNotIn("v_m", tables)

    def test_decimal_precision_must_match_contract(self):
        error, tables = self.attempt(amount="DECIMAL(10,2)")
        self.assertIsNotNone(error)
        self.assertIn("DECIMAL(10,2)", str(error))
        self.assertNotIn("v_m", tables)

    def test_timestamp_with_time_zone_is_rejected(self):
        error, tables = self.attempt(ts="TIMESTAMP WITH TIME ZONE")
        self.assertIsNotNone(error)
        self.assertIn("TIMESTAMP WITH TIME ZONE", str(error))
        self.assertNotIn("v_m", tables)

    def test_upstream_drift_aborts_execution_without_publishing(self):
        # The physical source table lacks `who` (declared in the source
        # schema): compilation succeeds, the warehouse refuses to build the
        # staged view, the run aborts and nothing is published.
        error, tables = self.attempt(drop_who=True)
        self.assertIsNotNone(error)
        self.assertIn("who", str(error))
        self.assertNotIn("v_m", tables)

    def test_missing_column_in_staged_view_fails_the_phase_c_pin(self):
        from strata.exec import runtime_pins
        proj, tms = build()
        con = con_with()
        try:
            # Staged view missing the contract column `who`.
            con.execute("CREATE VIEW stg_main__m AS "
                        "SELECT id, amount, ts, ratio FROM t")
            with self.assertRaises(PinError) as ctx:
                runtime_pins(con, proj, tms["m"], "stg_main__m", [])
            self.assertIn("who", str(ctx.exception))
            self.assertIn("missing from materialized schema", str(ctx.exception))
        finally:
            con.close()

    def test_integral_widening_into_int64_is_allowed(self):
        error, tables = self.attempt(id_type="INTEGER")
        self.assertIsNone(error)
        self.assertIn("v_m", tables)


if __name__ == "__main__":
    unittest.main()
