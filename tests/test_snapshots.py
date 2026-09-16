"""Entrega C: run-addressed snapshot tables + snapshot rollback.

Each run materializes its results into TABLES named snap_<run_id>_<model>;
the run id is content-addressed over code fingerprints AND source table
content hashes, so two runs over different data never share snapshots.
`run` publishes atomically (whole run or nothing); `rollback_to_run`
repoints live views to a past run's frozen tables without re-executing,
immune to source changes since that run.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata import exec as ex
from strata.analysis import Checker, Project
from strata.parser import parse_strata
from strata.seed import seed_sql

try:
    import duckdb
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

EX = Path(__file__).resolve().parents[1] / "examples"


def build(path):
    proj = Project(parse_strata(Path(path).read_text(), str(path)))
    return proj, Checker(proj).check_all()


def con_seeded():
    con = duckdb.connect()
    for stmt in seed_sql()[0].split(";"):
        if stmt.strip():
            con.execute(stmt)
    return con


def tables(con):
    return {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main'").fetchall()}


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestSnapshotsAndRollback(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.module = os.path.join(self.dir.name, "m.strata")
        Path(self.module).write_text((EX / "daily_orders.strata").read_text())
        self.addCleanup(self.dir.cleanup)

    def two_runs_over_different_data(self, con):
        proj, tms = build(self.module)
        applied1, _, _ = ex.run(con, proj, tms, self.module)
        rid1 = ex.load_history(self.module)[-1]["run_id"]
        first = con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall()
        con.execute("INSERT INTO refunds VALUES (1, 5.00, TIMESTAMP '2026-09-01 12:00:00')")
        applied2, _, _ = ex.run(con, proj, tms, self.module, only_stale=True)
        rid2 = ex.load_history(self.module)[-1]["run_id"]
        second = con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall()
        self.assertEqual(applied2, ["daily_orders"])
        self.assertNotEqual(rid1, rid2)
        self.assertNotEqual(first, second)
        return proj, tms, rid1, rid2, first, second, applied1, applied2

    def test_run_freezes_results_into_snapshot_tables(self):
        con = con_seeded()
        self.addCleanup(con.close)
        proj, tms = build(self.module)
        ex.run(con, proj, tms, self.module)
        snaps = ex.load_history(self.module)[-1].get("snapshots")
        self.assertEqual(set(snaps), set(tms))
        for snap in snaps.values():
            self.assertIn(snap, tables(con))
        view_sql = con.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name='v_daily_orders'"
        ).fetchone()[0]
        self.assertIn(snaps["daily_orders"], view_sql)

    def test_two_runs_over_different_data_never_share_snapshots(self):
        con = con_seeded()
        self.addCleanup(con.close)
        _, _, rid1, rid2, first, second, applied1, applied2 = \
            self.two_runs_over_different_data(con)
        self.assertEqual(applied1, ["daily_orders"])
        self.assertEqual(applied2, ["daily_orders"])  # source hash changed -> stale
        self.assertNotEqual(rid1, rid2)
        self.assertNotEqual(first, second)

    def test_rollback_restores_prior_results_after_source_mutation(self):
        con = con_seeded()
        self.addCleanup(con.close)
        _, _, rid1, rid2, first, second, _, _ = self.two_runs_over_different_data(con)
        self.assertEqual(
            con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(),
            second)
        ex.rollback_to_run(con, ex.find_run(self.module, rid1))
        self.assertEqual(
            con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(),
            first)
        # Later source mutation cannot alter run 1's snapshots.
        con.execute("INSERT INTO orders VALUES "
                    "(99, 9999, 'ES', 999.00, FALSE, DATE '2026-09-05')")
        self.assertEqual(
            con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(),
            first)

    def test_rollback_fails_loud_when_snapshot_was_dropped(self):
        con = con_seeded()
        self.addCleanup(con.close)
        _, _, rid1, *_ = self.two_runs_over_different_data(con)
        e = ex.find_run(self.module, rid1)
        con.execute(f"DROP TABLE {e['snapshots']['daily_orders']}")
        with self.assertRaises(ex.PinError) as ctx:
            ex.rollback_to_run(con, e)
        self.assertIn("missing", str(ctx.exception))

    def test_snapshot_identity_conflict_never_overwrites_old_table(self):
        con = con_seeded()
        self.addCleanup(con.close)
        proj, tms = build(self.module)
        ex.run(con, proj, tms, self.module)
        entry = ex.load_history(self.module)[-1]
        snap = entry["snapshots"]["daily_orders"]
        before = con.execute(f"SELECT * FROM {snap} ORDER BY order_day").fetchall()
        con.execute("UPDATE orders SET gross_amount_usd = 888")
        ex.materialize(con, proj, tms, stage_only=True)
        with self.assertRaisesRegex(ex.PinError, "identity conflict"):
            ex.publish_snapshots(con, ["daily_orders"], entry["run_id"])
        self.assertEqual(con.execute(f"SELECT * FROM {snap} ORDER BY order_day").fetchall(), before)
        self.assertEqual(con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(), before)

    def test_repeated_run_is_idempotent(self):
        con = con_seeded()
        self.addCleanup(con.close)
        proj, tms = build(self.module)
        ex.run(con, proj, tms, self.module)
        first = ex.load_history(self.module)[-1]
        ex.run(con, proj, tms, self.module)
        second = ex.load_history(self.module)[-1]
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["snapshots"], second["snapshots"])
        self.assertEqual(ex.run(con, proj, tms, self.module, only_stale=True)[0], [])

    def test_failed_test_keeps_live_snapshot_and_metadata(self):
        with Path(self.module).open("a") as f:
            f.write("\ntest daily_orders { expect row_count == 3; }\n")
        con = con_seeded()
        self.addCleanup(con.close)
        proj, tms = build(self.module)
        ex.run(con, proj, tms, self.module)
        before = con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall()
        old_tables = tables(con)
        history = ex.history_path(self.module).read_bytes()
        manifest = ex.manifest_path(self.module).read_bytes()
        con.execute("INSERT INTO orders VALUES (99, 9999, 'ES', 999.00, FALSE, DATE '2026-09-05')")
        with self.assertRaises(ex.StrataTestError):
            ex.run(con, proj, tms, self.module)
        self.assertEqual(con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(), before)
        self.assertEqual(tables(con), old_tables)
        self.assertEqual(ex.history_path(self.module).read_bytes(), history)
        self.assertEqual(ex.manifest_path(self.module).read_bytes(), manifest)

    def test_failed_pin_keeps_last_snapshot(self):
        con = con_seeded()
        self.addCleanup(con.close)
        proj, tms = build(self.module)
        ex.run(con, proj, tms, self.module)
        before = con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall()
        history = ex.load_history(self.module)
        con.execute("UPDATE orders SET country = 'INVALID' WHERE order_id = 1")
        with self.assertRaises(ex.PinError):
            ex.run(con, proj, tms, self.module)
        self.assertEqual(con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(), before)
        self.assertEqual(ex.load_history(self.module), history)

    def test_replay_uses_frozen_inputs_after_source_mutation(self):
        Path(self.module).write_text(
            'source orders(ns: "test", dataset: "orders") {\n'
            '  columns: { id: int64 nonnull }\n'
            '}\n'
            'contract C { id: int64 nonnull }\n'
            'model m -> contract C { from orders }\n')
        con = duckdb.connect()
        self.addCleanup(con.close)
        con.execute("CREATE TABLE orders(id BIGINT)")
        con.execute("INSERT INTO orders VALUES (1), (2)")
        proj, tms = build(self.module)
        # run invokes materialize and records the frozen input inventory.
        ex.run(con, proj, tms, self.module)
        original_run = ex.load_history(self.module)[-1]
        original = con.execute("SELECT * FROM v_m ORDER BY id").fetchall()
        self.assertEqual(original, [(1,), (2,)])
        self.assertIn("orders", original_run["input_snapshots"])
        con.execute("DELETE FROM orders")
        con.execute("INSERT INTO orders VALUES (99)")
        self.assertEqual(con.execute("SELECT * FROM orders").fetchall(), [(99,)])
        applied, _, replayed = ex.execute_run(
            con, proj, tms, self.module, original_run["run_id"])
        self.assertEqual(applied, ["m"])
        self.assertEqual(replayed["run_id"], original_run["run_id"])
        self.assertEqual(con.execute("SELECT * FROM v_m ORDER BY id").fetchall(), original)
        self.assertEqual(ex.load_history(self.module)[-1]["replay_of"],
                         original_run["run_id"])

    def test_replay_rejects_missing_or_changed_frozen_input(self):
        con = con_seeded()
        self.addCleanup(con.close)
        proj, tms = build(self.module)
        ex.run(con, proj, tms, self.module)
        entry = ex.load_history(self.module)[-1]
        history = ex.load_history(self.module)
        before = con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall()
        snap = entry["input_snapshots"]["orders"]
        con.execute(f"UPDATE {snap} SET gross_amount_usd = 777")
        with self.assertRaisesRegex(ex.PinError, "content changed"):
            ex.execute_run(con, proj, tms, self.module, entry["run_id"])
        con.execute(f"DROP TABLE {snap}")
        with self.assertRaisesRegex(ex.PinError, "missing"):
            ex.execute_run(con, proj, tms, self.module, entry["run_id"])
        self.assertEqual(ex.load_history(self.module), history)
        self.assertEqual(con.execute("SELECT * FROM v_daily_orders ORDER BY order_day").fetchall(), before)

    def test_hash_tracks_duplicate_rows_and_effective_override(self):
        con = con_seeded()
        self.addCleanup(con.close)
        proj, _ = build(self.module)
        con.execute("CREATE TABLE alternate AS SELECT * FROM refunds")
        overrides = {"refunds": {"dataset": "alternate"}}
        before = ex.source_fingerprints(con, proj, overrides)
        con.execute("INSERT INTO alternate SELECT * FROM alternate")
        after = ex.source_fingerprints(con, proj, overrides)
        self.assertNotEqual(before["refunds"], after["refunds"])
        self.assertEqual(before["orders"], after["orders"])

