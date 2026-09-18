"""Incrementality and backfills (§4): per-source minimal stale sets plus a
journaled `backfill` command — no author syntax (the engine decides, per the
propuesta); row-level incrementality stays pending on §2 partition_by.

Touching one source rebuilds only its downstream (plus code-changed models,
transitive via fingerprints); untouched branches keep their snapshots. A
backfill reuses a past run's context with corrected sources under an explicit
reason, recorded in history next to the corrected run id.
"""
import os
import tempfile
import unittest
from pathlib import Path

import duckdb

from strata import analysis
from strata.analysis import Checker, Project
from strata.cli import main as cli_main
from strata.parser import parse_strata
from strata import exec as ex

SRC = '''source s1(ns: "n", dataset: "s1") { columns: { x: int64 } }
source s2(ns: "n", dataset: "s2") { columns: { y: int64 } }
model m1 { from s1 }
model m2 { from s2 }
model m3 { from m1 select { z = x + 1 } }
model m4 { from m2 select { z = y + 1 } }
'''


def write_module(d, text=SRC):
    path = os.path.join(d, "m.strata")
    Path(path).write_text(text)
    return path


def build(path):
    proj = Project(parse_strata(Path(path).read_text(), path))
    Checker(proj).check_all()
    return proj


def seed(con):
    con.execute('CREATE TABLE s1(x BIGINT)')
    con.execute('CREATE TABLE s2(y BIGINT)')
    con.execute("INSERT INTO s1 VALUES (1)")
    con.execute("INSERT INTO s2 VALUES (2)")


class TestPerSourceStaleness(unittest.TestCase):
    def test_only_downstream_rebuilds(self):
        d = tempfile.mkdtemp()
        path = write_module(d)
        con = duckdb.connect()
        self.addCleanup(con.close)
        seed(con)
        proj = build(path)
        self.assertEqual(ex.run(con, proj, proj.typed, path)[0],
                         ['m1', 'm2', 'm3', 'm4'])
        self.assertEqual(ex.run(con, proj, proj.typed, path, only_stale=True)[0], [])
        con.execute("INSERT INTO s2 VALUES (3)")
        self.assertEqual(ex.run(con, proj, proj.typed, path, only_stale=True)[0],
                         ['m2', 'm4'])
        self.assertEqual(con.execute('SELECT * FROM v_m4 ORDER BY 1').fetchall(),
                         [(3,), (4,)])
        self.assertEqual(con.execute('SELECT * FROM v_m1').fetchall(), [(1,)])

    def test_code_change_rebuilds_its_branch(self):
        d = tempfile.mkdtemp()
        path = write_module(d)
        con = duckdb.connect()
        self.addCleanup(con.close)
        seed(con)
        proj = build(path)
        ex.run(con, proj, proj.typed, path)
        changed = SRC.replace('model m2 { from s2 }', 'model m2 { from s2 select { y = y } }')
        Path(path).write_text(changed)
        proj = build(path)
        self.assertEqual(ex.run(con, proj, proj.typed, path, only_stale=True)[0],
                         ['m2', 'm4'])


class TestBackfill(unittest.TestCase):
    def test_journaled_correction(self):
        d = tempfile.mkdtemp()
        path = write_module(d)
        con = duckdb.connect()
        self.addCleanup(con.close)
        seed(con)
        proj = build(path)
        ex.run(con, proj, proj.typed, path)
        rid = ex.load_history(path)[-1]["run_id"]
        con.execute('CREATE TABLE s1_fixed(x BIGINT)')
        con.execute("INSERT INTO s1_fixed VALUES (10)")
        applied, _, _ = ex.run(con, proj, proj.typed, path, names=['m1', 'm3'],
                               source_overrides={"s1": {"dataset": "s1_fixed"}},
                               reason="corrected extract", backfill_of=rid)
        self.assertEqual(applied, ['m1', 'm3'])
        entry = ex.load_history(path)[-1]
        self.assertEqual((entry.get("backfill_of"), entry.get("reason")),
                         (rid, "corrected extract"))
        self.assertEqual(con.execute('SELECT * FROM v_m3').fetchall(), [(11,)])

    def test_backfill_cli(self):
        d = tempfile.mkdtemp()
        path = write_module(d)
        wh = os.path.join(d, "w.duckdb")
        con = duckdb.connect(wh)
        seed(con)
        con.close()
        self.assertEqual(cli_main(["run", path, "-o", wh]), 0)
        con = duckdb.connect(wh)
        con.execute('CREATE TABLE s2_fixed(y BIGINT)')
        con.execute("INSERT INTO s2_fixed VALUES (20)")
        con.close()
        rid = ex.load_history(path)[-1]["run_id"]
        rc = cli_main(["backfill", path, rid, "--source", "s2=s2_fixed",
                       "--reason", "late-arriving fix", "-o", wh])
        self.assertEqual(rc, 0)
        con = duckdb.connect(wh)
        self.addCleanup(con.close)
        self.assertEqual(con.execute('SELECT * FROM v_m4 ORDER BY 1').fetchall(),
                         [(21,)])
        self.assertEqual(con.execute('SELECT * FROM v_m3').fetchall(), [(2,)])
        entry = ex.load_history(path)[-1]
        self.assertEqual(entry.get("backfill_of"), rid)
        self.assertIn("late-arriving", entry.get("reason", ""))
        self.assertEqual(cli_main(["backfill", path, "deadbeefcafe",
                                   "--reason", "x", "-o", wh]), 1)
        self.assertEqual(cli_main(["backfill", path, rid, "--source", "nope",
                                   "--reason", "x", "-o", wh]), 1)


if __name__ == '__main__':
    unittest.main()