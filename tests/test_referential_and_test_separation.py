"""M35: `strata test` schema/test separation, fixtures, referential checks.

Scope decided with the user:
1. `strata test` stages into a dedicated test branch (`stg_test__*`) and runs
   the declarative tests against those staged views — the live `v_*` views are
   never created or replaced (schema/test separation).
2. `--fixtures FILE` loads raw SQL into the warehouse before staging, so tests
   run against isolated fixture data.
3. New referential assertion `expect <col> in <model>.<col>` (no orphans; NULLs
   allowed), checked at compile time (E101/E102) and evaluated by run_tests.
"""
import os
import tempfile
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

from strata.analysis import Checker, Project, StrataError
from strata.cli import main
from strata.fmt import format_module
from strata.parser import parse_strata

try:
    import duckdb
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

from strata import exec as ex


SRC = ('source s(ns: "n", dataset: "s") {\n'
       '  columns: { id: int64 nonnull, parent_id: int64 }\n'
       '}\n'
       'model parent { from s }\n'
       'model child { from s }\n')


def build(text, path):
    proj = Project(parse_strata(text, path))
    tms = Checker(proj).check_all()
    return proj, tms


def semantic_ast(value):
    """Ignore source locations, retaining all semantic AST fields."""
    if is_dataclass(value):
        return (type(value).__name__, {
            f.name: semantic_ast(getattr(value, f.name))
            for f in fields(value) if f.name not in ("span", "path")
        })
    if isinstance(value, (list, tuple)):
        return [semantic_ast(item) for item in value]
    if isinstance(value, dict):
        return {key: semantic_ast(item) for key, item in value.items()}
    return value


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestReferentialCompile(unittest.TestCase):
    """Referential checks validate at compile time (E101/E102)."""

    def test_valid_referential_compiles(self):
        text = SRC + 'test child { expect parent_id in parent.id }\n'
        proj, tms = build(text, os.path.join(tempfile.mkdtemp(), "m.strata"))
        self.assertIn("parent", tms)
        checks = proj.tests["child"][0].checks
        self.assertEqual(checks[0].kind, "referential")
        self.assertEqual(checks[0].ref_model, "parent")
        self.assertEqual(checks[0].ref_col, "id")

    def test_e101_referenced_model_missing(self):
        text = SRC + 'test child { expect parent_id in nope.id }\n'
        d = tempfile.mkdtemp()
        proj = Project(parse_strata(text, os.path.join(d, "m.strata")))
        ck = Checker(proj)
        ck.check_all()
        with self.assertRaises(StrataError) as cm:
            ck.check_tests()
        self.assertEqual(cm.exception.code, "E101")

    def test_e102_referenced_column_missing(self):
        text = SRC + 'test child { expect parent_id in parent.nosuch }\n'
        d = tempfile.mkdtemp()
        proj = Project(parse_strata(text, os.path.join(d, "m.strata")))
        ck = Checker(proj)
        ck.check_all()
        with self.assertRaises(StrataError) as cm:
            ck.check_tests()
        self.assertEqual(cm.exception.code, "E102")

    def test_fmt_round_trip_preserves_referential(self):
        source = SRC + 'test child { expect parent_id in parent.id; }\n'
        original = parse_strata(source)
        formatted = format_module(original)
        reparsed = parse_strata(formatted)
        self.assertEqual(semantic_ast(original), semantic_ast(reparsed))
        self.assertEqual(formatted, format_module(reparsed))
        self.assertIn("expect parent_id in parent.id;", formatted)


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestReferentialEvaluation(unittest.TestCase):
    """Orphan detection through materialize + run_tests (promoted views)."""

    def _seed(self, con, rows):
        con.execute("CREATE TABLE s (id BIGINT NOT NULL, parent_id BIGINT)")
        con.executemany("INSERT INTO s VALUES (?, ?)", rows)

    def test_no_orphans_passes(self):
        d = tempfile.mkdtemp()
        text = SRC + 'test child { expect parent_id in parent.id }\n'
        path = os.path.join(d, "m.strata")
        Path(path).write_text(text)
        proj, tms = build(text, path)
        con = duckdb.connect()
        self._seed(con, [(1, 1), (2, 2), (3, 1)])
        ex.materialize(con, proj, tms, names=["parent", "child"])
        results = ex.run_tests(con, proj, tms, ["child"])
        self.assertIn("ok  child: parent_id in parent.id", results[0])

    def test_nulls_are_allowed(self):
        d = tempfile.mkdtemp()
        text = SRC + 'test child { expect parent_id in parent.id }\n'
        path = os.path.join(d, "m.strata")
        Path(path).write_text(text)
        proj, tms = build(text, path)
        con = duckdb.connect()
        self._seed(con, [(1, None), (2, None)])
        ex.materialize(con, proj, tms, names=["parent", "child"])
        ex.run_tests(con, proj, tms, ["child"])

    def test_orphans_fail_loud(self):
        d = tempfile.mkdtemp()
        text = SRC + 'test child { expect parent_id in parent.id }\n'
        path = os.path.join(d, "m.strata")
        Path(path).write_text(text)
        proj, tms = build(text, path)
        con = duckdb.connect()
        self._seed(con, [(1, 1), (2, 9), (3, None)])
        # The swap path runs the declarative tests as its validation, so the
        # orphan check aborts materialize itself (fail-closed blue-green).
        with self.assertRaises(ex.StrataTestError) as cm:
            ex.materialize(con, proj, tms, names=["parent", "child"])
        self.assertIn("violated for 1 rows (orphans)", str(cm.exception))

    def test_referential_runs_against_staged_views(self):
        # staged=True resolves views stg_test__* — the separation path used by
        # `strata test` — and must not require promoted views to exist.
        d = tempfile.mkdtemp()
        text = SRC + 'test child { expect parent_id in parent.id }\n'
        path = os.path.join(d, "m.strata")
        Path(path).write_text(text)
        proj, tms = build(text, path)
        con = duckdb.connect()
        self._seed(con, [(1, 1), (2, 2)])
        ex.materialize(con, proj, tms, names=["parent", "child"],
                       stage_only=True, branch="test")
        results = ex.run_tests(con, proj, tms, ["child"], branch="test",
                               staged=True)
        self.assertIn("ok  child: parent_id in parent.id", results[0])


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestCliStrataTestSeparation(unittest.TestCase):
    """`strata test` through the CLI must never touch the live v_* views."""

    def test_separation_leaves_live_views_alone(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.strata")
        Path(path).write_text(SRC + 'test child { expect parent_id in parent.id }\n')
        db = os.path.join(d, "w.duckdb")
        con = duckdb.connect(db)
        con.execute("CREATE TABLE s (id BIGINT NOT NULL, parent_id BIGINT)")
        con.execute("INSERT INTO s VALUES (1, 1), (2, 2)")
        # A live v_parent view already exists (last-known-good): `strata test`
        # must leave it queryable and unchanged.
        con.execute("CREATE VIEW v_child AS SELECT 999 AS sentinel")
        con.close()
        self.assertEqual(main(["test", path, "-o", db]), 0)
        con = duckdb.connect(db)
        names = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        self.assertIn("stg_test__child", names)
        self.assertNotIn("v_parent", names)
        self.assertIn("v_child", names)
        self.assertEqual(con.execute("SELECT * FROM v_child").fetchall(), [(999,)])

    def test_cli_test_passes_without_crashing(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.strata")
        Path(path).write_text(SRC + "test child { expect parent_id in parent.id }\n")
        db = os.path.join(d, "w.duckdb")
        con = duckdb.connect(db)
        con.execute("CREATE TABLE s (id BIGINT NOT NULL, parent_id BIGINT)")
        con.execute("INSERT INTO s VALUES (1, 1), (2, 2)")
        con.close()
        self.assertEqual(main(["test", path, "-o", db]), 0)

    def test_cli_test_reports_failure_without_crashing(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.strata")
        Path(path).write_text(SRC + "test child { expect parent_id in parent.id }\n")
        db = os.path.join(d, "w.duckdb")
        con = duckdb.connect(db)
        con.execute("CREATE TABLE s (id BIGINT NOT NULL, parent_id BIGINT)")
        con.execute("INSERT INTO s VALUES (1, 1), (2, 9)")
        con.close()
        self.assertEqual(main(["test", path, "-o", db]), 1)

    def test_cli_fixtures_loads_isolated_data(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "m.strata")
        Path(path).write_text(SRC + "test parent { expect row_count >= 3 }\n")
        db = os.path.join(d, "w.duckdb")
        fixture = os.path.join(d, "fixtures.sql")
        Path(fixture).write_text(
            "CREATE TABLE s (id BIGINT NOT NULL, parent_id BIGINT);\n"
            "INSERT INTO s VALUES (1, 1), (2, 2), (3, 3);\n")
        self.assertEqual(main(["test", path, "-o", db, "--fixtures", fixture]), 0)


if __name__ == "__main__":
    unittest.main()