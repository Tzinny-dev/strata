import os
import tempfile
import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import Checker
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