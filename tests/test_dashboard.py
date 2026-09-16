"""Fase 4: `strata dashboard` -- one-screen supervision surface. Deterministic
render + JSON agent artifact, staleness vs manifest, run history, protected-
consumer blast surface, and fail-loud rendering of modules that don't typecheck."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from strata import cli
from strata import exec as exec_mod

EX = Path(__file__).resolve().parent.parent / "examples"


def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


class TestDashboard(unittest.TestCase):
    def test_render_example_and_exit_zero(self):
        rc, out = run_cli(["dashboard", str(EX / "daily_orders.strata")])
        self.assertEqual(rc, 0)
        self.assertIn("DASHBOARD", out)
        for name in ("daily_orders", "OrderMetrics"):
            self.assertIn(name, out)
        self.assertIn("contract", out)
        self.assertIn("lineage (model edges):", out)
        self.assertIn("stale vs manifest:", out)

    def test_json_is_valid_deterministic_agent_artifact(self):
        argv = ["dashboard", str(EX / "daily_orders.strata"), "--json"]
        rc1, out1 = run_cli(argv)
        rc2, out2 = run_cli(argv)
        self.assertEqual(rc1, 0)
        self.assertEqual(out1, out2)  # deterministic
        d = json.loads(out1)
        self.assertEqual(d["health"]["n_models"], len(d["models"]))
        self.assertNotIn("compile_error", d)
        self.assertIn("daily_orders", [m["name"] for m in d["models"]])
        self.assertTrue(all(set(m) >= {"name", "contract", "fingerprint", "stale"}
                            for m in d["models"]))

    def test_staleness_and_run_history(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "mod.strata"
            src.write_text((EX / "daily_orders.strata").read_text())
            rc, out = run_cli(["dashboard", str(src), "--json"])
            d = json.loads(out)
            self.assertEqual(rc, 0)
            self.assertEqual(d["runs"], [])  # never materialized
            self.assertEqual(d["stale"], sorted(m["name"] for m in d["models"]))
            fps = {m["name"]: m["fingerprint"] for m in d["models"]}
            entry = exec_mod.record_run(str(src), {
                "fingerprints": fps, "applied": sorted(fps),
                "branch": "feat", "dialect": "duckdb"})
            exec_mod.save_manifest(str(src), fps)
            rc, out = run_cli(["dashboard", str(src), "--json"])
            d2 = json.loads(out)
            self.assertEqual(d2["stale"], [])
            self.assertEqual(d2["runs"][-1]["run_id"], entry["run_id"])
            self.assertEqual(d2["runs"][-1]["branch"], "feat")

    def test_protected_blast_surface(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "blast.strata"
            src.write_text(
                'source s(ns: "n", dataset: "d") {\n'
                '  columns: {\n'
                '    id: int64 nonnull,\n'
                '  }\n'
                '}\n'
                'contract c1 {\n'
                '  id: int64 nonnull protected\n'
                '}\n'
                'model m1 -> contract c1 {\n'
                '  from s\n'
                '}\n'
                'model m2 {\n'
                '  from m1\n'
                '}\n')
            rc, out = run_cli(["dashboard", str(src)])
            self.assertEqual(rc, 0)
            self.assertIn("m1.id <- m2.id", out)
            self.assertIn("m1 -> m2", out)
            rc, out = run_cli(["dashboard", str(src), "--json"])
            d = json.loads(out)
            self.assertEqual(d["health"]["n_edges"], 1)
            self.assertEqual(d["protected_consumed"],
                             [{"col": "m1.id", "consumers": ["m2.id"]}])

    def test_broken_module_fails_loud_but_renders(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "broken.strata"
            text = (EX / "daily_orders.strata").read_text()
            src.write_text(text.replace(
                "    country:          string nonnull,\n", "", 1))
            rc, out = run_cli(["dashboard", str(src)])
            self.assertEqual(rc, 1)
            self.assertIn("typecheck FAILED", out)
            self.assertIn("dashboard covers the models that compiled", out)
            rc, out = run_cli(["dashboard", str(src), "--json"])
            d = json.loads(out)
            self.assertEqual(rc, 1)
            self.assertIn("compile_error", d)


if __name__ == "__main__":
    unittest.main()