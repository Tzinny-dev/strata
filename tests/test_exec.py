import os
import tempfile
import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import Checker, Project
from strata.parser import parse_strata
from strata.exec import PinError, load_manifest, save_manifest

EX = Path(__file__).parent.parent / "examples"

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

from strata.seed import seed_sql


def build_text(text, path):
    proj = analysis.Project(parse_strata(text, path))
    Checker(proj).check_all()
    return proj


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestExec(unittest.TestCase):
    def test_run_daily_orders(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "daily_orders.strata")
        Path(path).write_text((EX / "daily_orders.strata").read_text())

        import duckdb
        con = duckdb.connect()
        for stmt in seed_sql()[0].split(";"):
            if stmt.strip():
                con.execute(stmt)

        proj = build_text(Path(path).read_text(), path)
        tms = proj.typed
        from strata import exec as ex
        applied, pins, note = ex.run(con, proj, tms, path, only_stale=False)
        self.assertEqual(applied, ["daily_orders"])
        self.assertTrue(any("net_amount" in p for p in pins))

        n = con.execute("SELECT count(*) FROM v_daily_orders").fetchone()[0]
        self.assertEqual(n, 3)  # ES, MX, BR -- CO test order excluded

        gross = con.execute("SELECT gross_amount FROM v_daily_orders "
                            "WHERE country='ES'").fetchone()[0]
        net = con.execute("SELECT net_amount FROM v_daily_orders "
                          "WHERE country='ES'").fetchone()[0]
        self.assertEqual(float(gross), 210.00)
        self.assertEqual(float(net), 200.00)

        # second run: nothing stale
        applied2, pins2, note = ex.run(con, proj, tms, path, only_stale=True)
        self.assertEqual(applied2, [])

    def test_check_autonomous_subcommand(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "daily_orders.strata")
        Path(path).write_text((EX / "daily_orders.strata").read_text())
        from strata.cli import cmd_check
        from types import SimpleNamespace
        rc = cmd_check(SimpleNamespace(file=path, dialect="duckdb"))
        self.assertEqual(rc, 0)
        rc_bad = cmd_check(SimpleNamespace(file=path, dialect="oracle"))
        self.assertEqual(rc_bad, 4)

    def test_seed_autonomous_subcommand(self):
        import duckdb
        d = tempfile.mkdtemp()
        path = os.path.join(d, "daily_orders.strata")
        Path(path).write_text((EX / "daily_orders.strata").read_text())
        w = os.path.join(d, "seed.duckdb")
        from strata.cli import cmd_seed
        from types import SimpleNamespace
        rc = cmd_seed(SimpleNamespace(file=path, output=w))
        self.assertEqual(rc, 0)
        fresh = duckdb.connect(w)
        n_orders = fresh.execute("SELECT count(*) FROM orders").fetchone()[0]
        n_refunds = fresh.execute("SELECT count(*) FROM refunds").fetchone()[0]
        fresh.close()
        self.assertEqual(n_orders, 5)
        self.assertEqual(n_refunds, 2)

    def test_run_sin_o_no_crash(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "daily_orders.strata")
        Path(path).write_text((EX / "daily_orders.strata").read_text())
        from strata.cli import cmd_run
        from types import SimpleNamespace
        rc = cmd_run(SimpleNamespace(file=path, seed=True, only_stale=False,
                                    dialect="duckdb", output=None))
        self.assertEqual(rc, 0)

    def test_run_output_persists_fresh_warehouse(self):
        """§13: `strata run -o warehouse.duckdb` (cli hook REAL L265/L149/L171)
        materializes byte-deterministic views into a REAL on-disk duckdb file
        — a FRESH duckdb.connect(path) after cmd_run sees v_daily_orders with
        3 rows and the exact byte-certain ES gross/net (§10 gate)."""
        with __import__("tempfile").TemporaryDirectory() as d:
            src = Path(d) / "daily_orders.strata"
            src.write_text((EX / "daily_orders.strata").read_text())
            warehouse = Path(d) / "warehouse.duckdb"
            from strata.cli import main
            import io as _io
            err = _io.StringIO()
            with __import__("contextlib").redirect_stderr(err):
                code = main(["run", str(src), "--seed", "-o", str(warehouse)])
            self.assertEqual(code, 0, err.getvalue())
            import duckdb
            con2 = duckdb.connect(str(warehouse))
            n = con2.execute("SELECT count(*) FROM v_daily_orders").fetchone()[0]
            self.assertEqual(n, 3)
            gross = con2.execute("SELECT gross_amount FROM v_daily_orders "
                                 "WHERE country='ES'").fetchone()[0]
            net = con2.execute("SELECT net_amount FROM v_daily_orders "
                               "WHERE country='ES'").fetchone()[0]
            self.assertEqual(float(gross), 210.00)
            self.assertEqual(float(net), 200.00)
            con2.close()


    def test_unique_pin_fails(self):
        import duckdb
        con = duckdb.connect()
        con.execute("CREATE TABLE dupes (country VARCHAR)")
        con.execute("INSERT INTO dupes VALUES ('ES'), ('ES')")
        text = """
source dupes(ns: "crm", dataset: "dupes") {
  columns: { country: string nonnull }
}
contract C { country: string nonnull unique }
model m -> contract C { from dupes }
"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "bad.strata")
        Path(path).write_text(text)
        proj = build_text(text, path)
        from strata import exec as ex
        with self.assertRaises(PinError):
            ex.run(con, proj, proj.typed, path)

    def test_import_multi_file(self):
        """`import lib.base` merges sources/contracts/models (spec/grammar.md).

        Fail-loud: unknown import -> E022, circular import -> F045."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "lib").mkdir()
            (Path(d) / "lib" / "base.strata").write_text(
                'source orders(ns: "crm", dataset: "orders") {\n'
                '  columns: { order_id: int64 nonnull }\n'
                '}\n'
                'contract C { order_id : int64 nonnull }\n'
                'model base -> contract C { from orders }\n')
            main_p = Path(d) / "main.strata"
            main_p.write_text(
                'import lib.base\n'
                'model top -> contract C { from base }\n'
                'pipeline p { env: dev, models: [top] }\n')
            proj = Project(parse_strata(main_p.read_text(), str(main_p)),
                           search_dirs=[d])
            Checker(proj).check_all()
            self.assertIn("base", proj.models)
            self.assertIn("top", proj.typed)
            self.assertEqual(proj.imports, ["lib.base"])
            # unknown import fails loud
            bad = Path(d) / "bad.strata"
            bad.write_text('import lib.missing\nmodel m { from orders }\n')
            with self.assertRaises(analysis.StrataError):
                Checker(Project(parse_strata(bad.read_text(), str(bad)),
                                search_dirs=[d])).check_all()

    def test_pipeline_sources_override(self):
        """`pipeline { sources: { orders: from(dataset: ...) } }` rewrites
        the compiled table per env; a dangling override fails loud."""
        import duckdb
        with tempfile.TemporaryDirectory() as d:
            text = (
                'source orders(ns: "crm", dataset: "orders") {\n'
                '  columns: { order_id: int64 nonnull, v: int64 nonnull }\n'
                '}\n'
                'contract C { order_id : int64 nonnull, v : int64 nonnull }\n'
                'model m -> contract C { from orders }\n'
                'pipeline prod { env: prod, models: [m],\n'
                '  sources: { orders: from(ns: "crm", dataset: "orders_eu") } }\n')
            path = os.path.join(d, "p.strata")
            Path(path).write_text(text)
            proj = Project(parse_strata(text, path))
            Checker(proj).check_all()
            self.assertEqual(proj.pipeline_sources("prod"),
                             {"orders": {"ns": "crm", "dataset": "orders_eu"}})
            con = duckdb.connect()
            con.execute("CREATE TABLE orders_eu (order_id BIGINT, v BIGINT)")
            con.execute("INSERT INTO orders_eu VALUES (7, 7)")
            from strata import exec as ex
            applied, pins, _ = ex.run(
                con, proj, proj.typed, path, names=["m"],
                source_overrides=proj.pipeline_sources("prod"))
            self.assertEqual(applied, ["m"])
            n = con.execute("SELECT order_id FROM v_m").fetchone()[0]
            self.assertEqual(n, 7)
            # dangling override (no compiled table matches) fails loud
            with self.assertRaises(PinError):
                ex.run(con, proj, proj.typed, path, names=["m"],
                       source_overrides={"ghost": {"dataset": "nowhere"}})


class TestManifest(unittest.TestCase):
    def test_manifest_roundtrip(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.strata")
        Path(p).write_text("")
        save_manifest(p, {"a": "ff", "b": "aa"})
        self.assertEqual(load_manifest(p), {"a": "ff", "b": "aa"})


class TestStale(unittest.TestCase):
    def test_stale_detection(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "daily_orders.strata")
        src = (EX / "daily_orders.strata").read_text()
        Path(path).write_text(src)
        proj1 = build_text(src, path)
        f1 = proj1.typed["daily_orders"].fingerprint
        save_manifest(path, {"daily_orders": f1})
        from strata import exec as ex
        self.assertEqual(ex.stale_models(proj1.typed, path), [])
        # changed source clears staleness
        changed = src.replace("upper(orders.country)", "lower(orders.country)")
        Path(path).write_text(changed)
        proj2 = build_text(changed, path)
        self.assertIn("daily_orders", ex.stale_models(proj2.typed, path))


if __name__ == "__main__":
    unittest.main()

class TestFmtLintReplayRollback(unittest.TestCase):
    def test_fmt_idempotent_and_fp_stable(self):
        from strata.parser import parse_strata
        from strata.fmt import format_module
        src = (EX / "daily_orders.strata").read_text()
        once = format_module(parse_strata(src, "daily_orders.strata"))
        twice = format_module(parse_strata(once, "fmt.strata"))
        self.assertEqual(once, twice)
        p1 = Project(parse_strata(src, "a"))
        Checker(p1).check_all()
        p2 = Project(parse_strata(once, "b"))
        Checker(p2).check_all()
        self.assertEqual(p1.typed["daily_orders"].fingerprint,
                         p2.typed["daily_orders"].fingerprint)

    def test_lint_warns(self):
        from strata.cli import cmd_lint
        from types import SimpleNamespace
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_lint(SimpleNamespace(file=str(EX / "daily_orders.strata"),
                                          search_dir=None, strict=False))
        self.assertEqual(rc, 0)
        # daily_orders has owner; craft a model without contract/owner
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.strata"
            p.write_text('source s(ns: "n", dataset: "s") {\n  columns: { a: int64 }\n}\nmodel m { from s }\n')
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = cmd_lint(SimpleNamespace(file=str(p), search_dir=None, strict=True))
            self.assertEqual(rc2, 2)
            self.assertIn("W001", buf2.getvalue())
            self.assertIn("W002", buf2.getvalue())

    def test_run_records_history_replay_rollback(self):
        import duckdb
        from strata import exec as ex
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "h.strata"
            src.write_text((EX / "daily_orders.strata").read_text())
            proj = Project(parse_strata(src.read_text(), str(src)))
            Checker(proj).check_all()
            con = duckdb.connect()
            for stmt in __import__("strata.seed", fromlist=["seed_sql"]).seed_sql()[0].split(";"):
                if stmt.strip():
                    con.execute(stmt)
            applied, pins, _ = ex.run(con, proj, proj.typed, str(src))
            self.assertEqual(applied, ["daily_orders"])
            hist = ex.load_history(str(src))
            self.assertEqual(len(hist), 1)
            self.assertEqual(len(hist[0]["run_id"]), 12)
            # replay finds it by prefix
            self.assertIsNotNone(ex.find_run(str(src), hist[0]["run_id"][:7]))
            # rollback repoints manifest to that run
            ex.save_manifest(str(src), {"daily_orders": "deadbeefdeadbeef"})
            from strata.cli import cmd_rollback
            from types import SimpleNamespace
            rc = cmd_rollback(SimpleNamespace(file=str(src), run_id=hist[0]["run_id"]))
            self.assertEqual(rc, 0)
            self.assertEqual(ex.load_manifest(str(src))["daily_orders"],
                             hist[0]["fingerprints"]["daily_orders"])
