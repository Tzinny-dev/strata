"""Fase 4: bench harness -- golden files as supervision/regression artifacts."""
import json
import shutil
import unittest
from pathlib import Path

from strata import bench as bench_mod

PROTO = Path(__file__).resolve().parent.parent


class TestBenchHarness(unittest.TestCase):
    def test_committed_goldens_all_green(self):
        self.assertEqual(bench_mod.run_cases(root=PROTO, update=False), 0)
        goldens = list((bench_mod.BENCH_DIR / "golden").glob("*.golden"))
        self.assertGreaterEqual(len(goldens), 4)

    def test_bless_drift_and_update_cycle(self):
        tmp = Path("/tmp/strata_bench_case")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        (tmp / "m.strata").write_text((PROTO / "bench/cases/store_simple/base.strata").read_text())
        bdir = tmp / "benchx"
        bdir.mkdir()
        (bdir / "manifest.json").write_text(json.dumps(
            {"cases": [{"name": "t/one", "type": "module", "module": "m.strata"}]}))
        orig = bench_mod.BENCH_DIR
        bench_mod.BENCH_DIR = bdir
        try:
            # 1. no golden yet -> exit 1 with a MISSING hint
            self.assertEqual(bench_mod.run_cases(root=tmp, update=False), 1)
            # 2. bless -> all green
            self.assertEqual(bench_mod.run_cases(root=tmp, update=True), 0)
            self.assertEqual(bench_mod.run_cases(root=tmp, update=False), 0)
            # 3. artifact drift -> exit 1
            g = bdir / "golden" / "t__one.golden"
            g.write_text(g.read_text().replace("model customer_revenue",
                                               "model DRIFTED"))
            self.assertEqual(bench_mod.run_cases(root=tmp, update=False), 1)
            # 4. intentional change -> re-bless -> green again
            self.assertEqual(bench_mod.run_cases(root=tmp, update=True), 0)
            self.assertEqual(bench_mod.run_cases(root=tmp, update=False), 0)
        finally:
            bench_mod.BENCH_DIR = orig
            shutil.rmtree(tmp)

    def test_unknown_case_type_is_harness_error(self):
        tmp = Path("/tmp/strata_bench_bad")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        bdir = tmp / "benchx"
        bdir.mkdir()
        (bdir / "manifest.json").write_text(json.dumps(
            {"cases": [{"name": "bad", "type": "wat"}]}))
        orig = bench_mod.BENCH_DIR
        bench_mod.BENCH_DIR = bdir
        try:
            self.assertEqual(bench_mod.run_cases(root=tmp, update=False), 2)
        finally:
            bench_mod.BENCH_DIR = orig
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
