"""`test <model> { expect ... }` (Fase 5): declarative data tests.

Regression coverage for two real bugs found while writing user
documentation (never caught before because nothing exercised
`strata test` through the actual CLI entry point, and `row_count` never
had a case with an operator other than `==`):

1. `cmd_test` (strata/cli.py) called `duckdb.connect(...)` without ever
   importing `duckdb` — every invocation of `strata test` crashed with
   `NameError`, unconditionally.
2. `run_tests`'s `row_count` check (strata/exec.py) hardcoded `got !=
   expected` regardless of the comparison operator actually written —
   `expect row_count >= 1` (or `<=`, `!=`, `<`, `>`) was silently treated
   as `expect row_count == 1`.
"""
import os
import tempfile
import unittest
from pathlib import Path

from strata.analysis import Checker, Project
from strata.cli import main
from strata.parser import parse_strata

try:
    import duckdb
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

from strata import exec as ex


SRC = ('source s(ns: "n", dataset: "s") { columns: { id: int64 nonnull } }\n'
       'model m { from s }\n')


def build(text, path):
    proj = Project(parse_strata(text, path))
    tms = Checker(proj).check_all()
    return proj, tms


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestRowCountOperators(unittest.TestCase):
    """run_tests must honor the actual comparison operator, not just '=='."""

    def _con(self):
        con = duckdb.connect()
        con.execute("CREATE TABLE s (id BIGINT)")
        con.execute("INSERT INTO s VALUES (1), (2), (3)")
        return con

    def _run(self, expect_clause, path):
        text = SRC + f"test m {{ {expect_clause} }}\n"
        Path(path).write_text(text)
        proj, tms = build(text, path)
        con = self._con()
        ex.materialize(con, proj, tms, names=["m"])
        return ex.run_tests(con, proj, tms, ["m"], branch="main")

    def test_ge_passes_when_count_exceeds_threshold(self):
        d = tempfile.mkdtemp()
        results = self._run("expect row_count >= 1", os.path.join(d, "m.strata"))
        self.assertIn("row_count >= 1", results[0])

    def test_ge_fails_when_count_is_below_threshold(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ex.StrataTestError) as cm:
            self._run("expect row_count >= 10", os.path.join(d, "m.strata"))
        self.assertIn(">=", str(cm.exception))

    def test_le_passes(self):
        d = tempfile.mkdtemp()
        results = self._run("expect row_count <= 3", os.path.join(d, "m.strata"))
        self.assertIn("row_count <= 3", results[0])

    def test_le_fails(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ex.StrataTestError):
            self._run("expect row_count <= 2", os.path.join(d, "m.strata"))

    def test_ne_passes_when_counts_differ(self):
        d = tempfile.mkdtemp()
        results = self._run("expect row_count != 5", os.path.join(d, "m.strata"))
        self.assertIn("row_count != 5", results[0])

    def test_ne_fails_when_counts_match(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ex.StrataTestError):
            self._run("expect row_count != 3", os.path.join(d, "m.strata"))

    def test_eq_still_works(self):
        d = tempfile.mkdtemp()
        results = self._run("expect row_count == 3", os.path.join(d, "m.strata"))
        self.assertIn("row_count == 3", results[0])


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestCliStrataTest(unittest.TestCase):
    """`strata test` through the real CLI entry point (main()), not just the
    library function — this is exactly the path that was broken."""

    def test_cli_test_passes_without_crashing(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.strata")
        Path(path).write_text(SRC + "test m { expect row_count >= 1 }\n")
        db = os.path.join(d, "w.duckdb")
        con = duckdb.connect(db)
        con.execute("CREATE TABLE s (id BIGINT)")
        con.execute("INSERT INTO s VALUES (1), (2)")
        con.close()
        self.assertEqual(main(["test", path, "-o", db]), 0)

    def test_cli_test_reports_failure_without_crashing(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.strata")
        Path(path).write_text(SRC + "test m { expect row_count >= 10 }\n")
        db = os.path.join(d, "w.duckdb")
        con = duckdb.connect(db)
        con.execute("CREATE TABLE s (id BIGINT)")
        con.execute("INSERT INTO s VALUES (1), (2)")
        con.close()
        self.assertEqual(main(["test", path, "-o", db]), 1)


if __name__ == "__main__":
    unittest.main()
