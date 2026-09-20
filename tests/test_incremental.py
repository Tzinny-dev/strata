"""Tests for incremental model support."""
import unittest
from strata.parser import parse_strata
from strata.analysis import Project, Checker, StrataError


SRC = """
source s(ns: "n", dataset: "s") {
  columns: { id: int64, name: string, value: float64, updated_at: timestamp }
}
"""


class TestIncrementalParsing(unittest.TestCase):
    def test_incremental_basic(self):
        """Basic incremental model is parsed correctly."""
        text = SRC + 'model m { from s incremental }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)

    def test_incremental_with_merge_strategy(self):
        """merge_strategy: upsert type-checks once cdc_column/merge_keys back it
        (bare merge_strategy alone is now E088, see test_upsert_requires_*)."""
        text = SRC + ('model m { from s incremental merge_strategy: upsert '
                       'merge_keys: [id] cdc_column: updated_at }\n')
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)
        self.assertEqual(m.plan.merge_strategy, "upsert")

    def test_replace_strategy_has_no_extra_requirements(self):
        """merge_strategy: replace (or no strategy) is still a plain full
        rebuild every run, so it needs neither cdc_column nor merge_keys."""
        text = SRC + 'model m { from s incremental merge_strategy: replace }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        self.assertEqual(proj.typed["m"].plan.merge_strategy, "replace")

    def test_upsert_requires_cdc_column(self):
        text = SRC + 'model m { from s incremental merge_strategy: upsert merge_keys: [id] }\n'
        proj = Project(parse_strata(text, "<test>"))
        with self.assertRaises(StrataError) as cm:
            Checker(proj).check_all()
        self.assertEqual(cm.exception.code, "E088")

    def test_upsert_requires_merge_keys(self):
        text = SRC + ('model m { from s incremental merge_strategy: upsert '
                       'cdc_column: updated_at }\n')
        proj = Project(parse_strata(text, "<test>"))
        with self.assertRaises(StrataError) as cm:
            Checker(proj).check_all()
        self.assertEqual(cm.exception.code, "E089")

    def test_append_requires_cdc_column(self):
        text = SRC + 'model m { from s incremental merge_strategy: append }\n'
        proj = Project(parse_strata(text, "<test>"))
        with self.assertRaises(StrataError) as cm:
            Checker(proj).check_all()
        self.assertEqual(cm.exception.code, "E088")

    def test_unknown_merge_strategy_rejected(self):
        text = SRC + 'model m { from s incremental merge_strategy: bogus }\n'
        proj = Project(parse_strata(text, "<test>"))
        with self.assertRaises(StrataError) as cm:
            Checker(proj).check_all()
        self.assertEqual(cm.exception.code, "E086")

    def test_incremental_upsert_rejected_on_grouped_model(self):
        text = SRC + """model m {
  from s
  group { id } (
    aggregate { total = sum(value) }
  )
  incremental
  merge_strategy: upsert
  merge_keys: [id]
  cdc_column: updated_at
}
"""
        proj = Project(parse_strata(text, "<test>"))
        with self.assertRaises(StrataError) as cm:
            Checker(proj).check_all()
        self.assertEqual(cm.exception.code, "E087")

    def test_incremental_with_merge_keys(self):
        """Incremental with merge_keys is parsed correctly."""
        text = SRC + 'model m { from s incremental merge_keys: [id] }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)
        self.assertEqual(len(m.plan.merge_keys), 1)

    def test_incremental_with_cdc_column(self):
        """Incremental with cdc_column is parsed correctly."""
        text = SRC + 'model m { from s incremental cdc_column: updated_at }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)
        self.assertEqual(m.plan.cdc_column, "updated_at")

    def test_incremental_full_config(self):
        """Incremental with full configuration is parsed correctly."""
        text = SRC + """model m {
  from s
  incremental
  merge_strategy: upsert
  merge_keys: [id]
  cdc_column: updated_at
}
"""
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)
        self.assertEqual(m.plan.merge_strategy, "upsert")
        self.assertEqual(len(m.plan.merge_keys), 1)
        self.assertEqual(m.plan.cdc_column, "updated_at")

    def test_incremental_with_freshness(self):
        """Incremental with freshness is parsed correctly."""
        text = SRC + 'model m { from s incremental freshness 1h }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)
        self.assertEqual(m.plan.freshness, ["1h"])


if __name__ == "__main__":
    unittest.main()
