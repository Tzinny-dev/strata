"""Real execution of `incremental merge_strategy: append|upsert` (exec.py).

These tests distinguish actually-incremental behavior from a coincidentally
correct full rebuild: a source row already folded into a prior snapshot
(below the cdc_column watermark) that mutates afterwards must NOT be picked
up by a later incremental run — the whole point of `cdc_column` is to skip
re-reading rows already processed. `merge_strategy: replace` is the contrast
case: it stays a full rebuild every run, so the same mutation IS picked up.

Compile-time rejection of ill-formed incremental configs (E086-E089) is
covered in test_incremental.py; this file only exercises the executor.
"""
import os
import tempfile
import unittest

from pathlib import Path

import duckdb

from strata.analysis import Checker, Project
from strata.parser import parse_strata
from strata import exec as ex


SRC = '''source s(ns: "n", dataset: "s") {
  columns: { id: int64, v: int64, ts: timestamp }
}
'''


def build(text, path):
    proj = Project(parse_strata(text, path))
    Checker(proj).check_all()
    return proj


def write_module(d, text):
    path = os.path.join(d, "m.strata")
    Path(path).write_text(text)
    return path


class TestIncrementalAppend(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.con = duckdb.connect()
        self.con.execute("CREATE TABLE s (id BIGINT, v BIGINT, ts TIMESTAMP)")

    def _run(self, text, only_stale):
        path = write_module(self.d, text)
        proj = build(text, path)
        return ex.run(self.con, proj, proj.typed, path, only_stale=only_stale)

    def test_append_only_merges_new_rows_and_ignores_stale_mutation(self):
        text = SRC + '''model m {
  from s
  incremental
  merge_strategy: append
  cdc_column: ts
}
'''
        self.con.execute(
            "INSERT INTO s VALUES (1, 10, '2026-01-01 00:00:00'), "
            "(2, 20, '2026-01-02 00:00:00')")
        applied, pins, note = self._run(text, only_stale=False)
        self.assertEqual(applied, ["m"])
        rows = dict(self.con.execute("SELECT id, v FROM v_m").fetchall())
        self.assertEqual(rows, {1: 10, 2: 20})

        # Mutate an already-merged row WITHOUT bumping ts, and add a genuinely
        # new one. A full rebuild would pick up the id=1 mutation; a real
        # incremental merge must not, because ts=1 is below the watermark.
        self.con.execute("UPDATE s SET v = 999 WHERE id = 1")
        self.con.execute("INSERT INTO s VALUES (3, 30, '2026-01-03 00:00:00')")
        applied2, pins2, note2 = self._run(text, only_stale=True)
        self.assertEqual(applied2, ["m"])
        rows2 = dict(self.con.execute("SELECT id, v FROM v_m").fetchall())
        self.assertEqual(
            rows2, {1: 10, 2: 20, 3: 30},
            "id=1 must keep its pre-merge value: the cdc_column watermark, "
            "not a full rebuild, must drive the merge")

    def test_append_duplicates_on_key_reuse(self):
        """append never dedups by key: unlike upsert, both rows survive."""
        text = SRC + '''model m {
  from s
  incremental
  merge_strategy: append
  cdc_column: ts
}
'''
        self.con.execute("INSERT INTO s VALUES (1, 10, '2026-01-01 00:00:00')")
        self._run(text, only_stale=False)
        self.con.execute("INSERT INTO s VALUES (1, 11, '2026-01-02 00:00:00')")
        self._run(text, only_stale=True)
        rows = self.con.execute(
            "SELECT v FROM v_m WHERE id = 1 ORDER BY v").fetchall()
        self.assertEqual([r[0] for r in rows], [10, 11])

    def test_first_run_has_no_prior_snapshot_to_merge_into(self):
        """Bootstrap: nothing to merge into yet, so the fresh full recompute
        is exactly what gets published."""
        text = SRC + '''model m {
  from s
  incremental
  merge_strategy: append
  cdc_column: ts
}
'''
        self.con.execute(
            "INSERT INTO s VALUES (1, 10, '2026-01-01 00:00:00'), "
            "(2, 20, '2026-01-02 00:00:00')")
        applied, pins, note = self._run(text, only_stale=False)
        self.assertEqual(applied, ["m"])
        n = self.con.execute("SELECT count(*) FROM v_m").fetchone()[0]
        self.assertEqual(n, 2)


class TestIncrementalUpsert(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.con = duckdb.connect()
        self.con.execute("CREATE TABLE s (id BIGINT, v BIGINT, ts TIMESTAMP)")

    def _run(self, text, only_stale):
        path = write_module(self.d, text)
        proj = build(text, path)
        return ex.run(self.con, proj, proj.typed, path, only_stale=only_stale)

    def test_upsert_replaces_by_key_and_ignores_stale_mutation(self):
        text = SRC + '''model m {
  from s
  incremental
  merge_strategy: upsert
  merge_keys: [id]
  cdc_column: ts
}
'''
        self.con.execute(
            "INSERT INTO s VALUES (1, 10, '2026-01-01 00:00:00'), "
            "(2, 20, '2026-01-02 00:00:00')")
        applied, pins, note = self._run(text, only_stale=False)
        self.assertEqual(applied, ["m"])
        self.assertEqual(
            dict(self.con.execute("SELECT id, v FROM v_m").fetchall()),
            {1: 10, 2: 20})

        # Mutate id=1 in place WITHOUT a fresh ts (must be ignored) and
        # legitimately update id=2 WITH a newer ts (must replace the old
        # row, not duplicate it), plus a brand new id=3.
        self.con.execute("UPDATE s SET v = 999 WHERE id = 1")
        self.con.execute(
            "INSERT INTO s VALUES (2, 21, '2026-01-03 00:00:00'), "
            "(3, 30, '2026-01-04 00:00:00')")
        applied2, pins2, note2 = self._run(text, only_stale=True)
        self.assertEqual(applied2, ["m"])
        rows = self.con.execute("SELECT id, v FROM v_m ORDER BY id").fetchall()
        self.assertEqual(rows, [(1, 10), (2, 21), (3, 30)])
        n = self.con.execute("SELECT count(*) FROM v_m").fetchone()[0]
        self.assertEqual(
            n, 3, "upsert must replace id=2's old row, not duplicate it")

    def test_upsert_composite_key(self):
        text = '''source s2(ns: "n", dataset: "s2") {
  columns: { a: int64, b: int64, v: int64, ts: timestamp }
}
model m {
  from s2
  incremental
  merge_strategy: upsert
  merge_keys: [a, b]
  cdc_column: ts
}
'''
        self.con.execute("CREATE TABLE s2 (a BIGINT, b BIGINT, v BIGINT, ts TIMESTAMP)")
        self.con.execute(
            "INSERT INTO s2 VALUES (1, 1, 10, '2026-01-01 00:00:00')")
        self._run(text, only_stale=False)
        self.con.execute(
            "INSERT INTO s2 VALUES (1, 1, 99, '2026-01-02 00:00:00')")
        self._run(text, only_stale=True)
        rows = self.con.execute("SELECT v FROM v_m").fetchall()
        self.assertEqual(rows, [(99,)])


class TestIncrementalReplaceIsFullRebuild(unittest.TestCase):
    def test_replace_picks_up_source_mutations(self):
        """Contrast case: merge_strategy: replace (or no strategy at all) has
        no special handling, so an old-row mutation IS visible next run."""
        d = tempfile.mkdtemp()
        con = duckdb.connect()
        con.execute("CREATE TABLE s (id BIGINT, v BIGINT, ts TIMESTAMP)")
        con.execute("INSERT INTO s VALUES (1, 10, '2026-01-01 00:00:00')")
        text = SRC + '''model m {
  from s
  incremental
  merge_strategy: replace
  cdc_column: ts
}
'''
        path = write_module(d, text)
        proj = build(text, path)
        ex.run(con, proj, proj.typed, path, only_stale=False)
        con.execute("UPDATE s SET v = 999 WHERE id = 1")
        ex.run(con, proj, proj.typed, path, only_stale=True)
        v = con.execute("SELECT v FROM v_m WHERE id = 1").fetchone()[0]
        self.assertEqual(v, 999)


if __name__ == "__main__":
    unittest.main()
