"""CLI command smoke coverage: build / compile / plan / fmt / lint / check / graph / lineage / profile / seed.

These invoke the command functions directly via `SimpleNamespace` args, the
same way test_exec.py drives run/replay — no CLI parser needed, no warehouse
dependencies (all duckdb/in-memory or static).
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from strata.cli import (cmd_build, cmd_check, cmd_compile, cmd_fmt, cmd_graph,
                        cmd_lineage, cmd_lint, cmd_plan, cmd_profile, cmd_seed)
from strata.exec import manifest_path
from strata.fmt import format_module
from strata.parser import parse_strata

EX = Path(__file__).parent.parent / "examples"


def run_cmd(fn, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(SimpleNamespace(**kwargs))
    return rc, out.getvalue(), err.getvalue()


class TestBuildCommands(unittest.TestCase):
    def test_cmd_build_daily_orders(self):
        rc, out, _ = run_cmd(cmd_build, file=str(EX / "daily_orders.strata"),
                             model=None, search_dir=None, strict=False)
        self.assertEqual(rc, 0)
        self.assertIn("daily_orders", out)

    def test_cmd_build_model_subset(self):
        rc, out, _ = run_cmd(cmd_build, file=str(EX / "multinacional.strata"),
                             model=["m_ES_orders"], search_dir=None, strict=False)
        self.assertEqual(rc, 0)
        self.assertIn("m_ES_orders", out)

    def test_cmd_build_strict_requires_contracts(self):
        path = Path(tempfile.mkdtemp()) / "no_contract.strata"
        path.write_text('source s(ns: "n", dataset: "s") { columns: { x: int64 } }\n'
                        'model m { from s }\n')
        rc, _, err = run_cmd(cmd_build, file=str(path), model=None,
                             search_dir=None, strict=True)
        self.assertEqual(rc, 2)
        self.assertIn("E014", err)

    def test_cmd_compile_duckdb(self):
        rc, out, _ = run_cmd(cmd_compile, file=str(EX / "daily_orders.strata"),
                             model=None, search_dir=None, dialect="duckdb")
        self.assertEqual(rc, 0)
        self.assertIn("CREATE", out)


class TestPlanCommands(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_cmd_plan_seeds_baseline_then_up_to_date(self):
        path = Path(self.d) / "p.strata"
        path.write_text((EX / "daily_orders.strata").read_text())
        rc, out, _ = run_cmd(cmd_plan, file=str(path), search_dir=None, seed=False)
        self.assertEqual(rc, 0)
        self.assertIn("STALE", out)
        rc, _, _ = run_cmd(cmd_plan, file=str(path), search_dir=None, seed=True)
        self.assertEqual(rc, 0)
        self.assertTrue(manifest_path(str(path)).exists())
        rc, out, _ = run_cmd(cmd_plan, file=str(path), search_dir=None, seed=False)
        self.assertEqual(rc, 0)
        self.assertIn("everything up to date", out)


class TestQualityCommands(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = 'source s(ns: "n", dataset: "s") { columns: { x: int64 } }\n' \
                   'model m { from s }\n'

    def _canonical(self):
        return format_module(parse_strata(self.src, "m.strata"))

    def test_cmd_fmt_print(self):
        path = Path(self.d) / "m.strata"
        path.write_text(self._canonical())
        rc, out, _ = run_cmd(cmd_fmt, file=str(path), search_dir=None,
                             check=False, write=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out, self._canonical())

    def test_cmd_fmt_check_clean(self):
        path = Path(self.d) / "m.strata"
        path.write_text(self._canonical())
        rc, out, _ = run_cmd(cmd_fmt, file=str(path), search_dir=None,
                             check=True, write=False)
        self.assertEqual(rc, 0)
        self.assertIn("formatted", out)

    def test_cmd_fmt_check_dirty(self):
        path = Path(self.d) / "m.strata"
        path.write_text(self.src)
        rc, _, _ = run_cmd(cmd_fmt, file=str(path), search_dir=None,
                           check=True, write=False)
        self.assertEqual(rc, 1)

    def test_cmd_fmt_write(self):
        path = Path(self.d) / "m.strata"
        path.write_text(self.src)
        rc, _, _ = run_cmd(cmd_fmt, file=str(path), search_dir=None,
                           check=False, write=True)
        self.assertEqual(rc, 0)
        self.assertEqual(path.read_text(), self._canonical())

    def test_cmd_lint_warns(self):
        path = Path(self.d) / "m.strata"
        path.write_text(self.src)
        rc, out, _ = run_cmd(cmd_lint, file=str(path), search_dir=None, strict=False)
        self.assertEqual(rc, 0)
        self.assertIn("W001", out)

    def test_cmd_lint_strict_fails(self):
        path = Path(self.d) / "m.strata"
        path.write_text(self.src)
        rc, _, _ = run_cmd(cmd_lint, file=str(path), search_dir=None, strict=True)
        self.assertEqual(rc, 2)

    def test_cmd_check_ok(self):
        rc, out, _ = run_cmd(cmd_check, file=str(EX / "daily_orders.strata"),
                             search_dir=None, dialect="duckdb")
        self.assertEqual(rc, 0)
        self.assertIn("check OK", out)
        self.assertIn("pins", out)

    def test_cmd_check_unknown_dialect(self):
        rc, _, err = run_cmd(cmd_check, file=str(EX / "daily_orders.strata"),
                             search_dir=None, dialect="oracle")
        self.assertEqual(rc, 4)
        self.assertIn("unknown dialect", err)


class TestInspectCommands(unittest.TestCase):
    def test_cmd_graph_dot(self):
        rc, out, _ = run_cmd(cmd_graph, file=str(EX / "daily_orders.strata"),
                             model=None, search_dir=None, format="dot")
        self.assertEqual(rc, 0)
        self.assertIn("digraph", out)

    def test_cmd_graph_mermaid(self):
        rc, out, _ = run_cmd(cmd_graph, file=str(EX / "daily_orders.strata"),
                             model=None, search_dir=None, format="mermaid")
        self.assertEqual(rc, 0)
        self.assertIn("flowchart", out)

    def test_cmd_graph_text(self):
        rc, out, _ = run_cmd(cmd_graph, file=str(EX / "daily_orders.strata"),
                             model=None, search_dir=None, format="text")
        self.assertEqual(rc, 0)
        self.assertIn("model graph", out)

    def test_cmd_lineage(self):
        rc, out, _ = run_cmd(cmd_lineage, file=str(EX / "daily_orders.strata"),
                             head2=None, search_dir=None, json=False, change=None)
        self.assertEqual(rc, 0)
        self.assertIn("lineage", out)

    def test_cmd_lineage_blast_radius(self):
        d = tempfile.mkdtemp()
        path = Path(d) / "chain.strata"
        path.write_text('source s(ns: "n", dataset: "s") { columns: { x: int64, y: int64 } }\n'
                        'model m1 { from s }\n'
                        'model m2 { from m1 }\n')
        rc, out, _ = run_cmd(cmd_lineage, file=str(path),
                             head2=None, search_dir=None, json=False,
                             change="m1:x")
        self.assertEqual(rc, 1)
        self.assertIn("E030", out)

    def test_cmd_profile(self):
        rc, out, _ = run_cmd(cmd_profile, file=str(EX / "daily_orders.strata"),
                             search_dir=None, dialect="duckdb", model=None,
                             run=False, output=None, seed=False)
        self.assertEqual(rc, 0)
        self.assertIn("profile:", out)


class TestSeedCommand(unittest.TestCase):
    def test_cmd_seed_in_memory(self):
        rc, out, _ = run_cmd(cmd_seed, file=str(EX / "daily_orders.strata"),
                             output=None)
        self.assertEqual(rc, 0)
        self.assertIn("seeded", out)


if __name__ == "__main__":
    unittest.main()