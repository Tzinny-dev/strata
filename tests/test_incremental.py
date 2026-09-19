"""Tests for incremental model support."""
import unittest
from strata.parser import parse_strata
from strata.analysis import Project, Checker


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
        """Incremental with merge_strategy is parsed correctly."""
        text = SRC + 'model m { from s incremental merge_strategy: upsert }\n'
        proj = Project(parse_strata(text, "<test>"))
        Checker(proj).check_all()
        m = proj.typed["m"]
        self.assertTrue(m.plan.incremental)
        self.assertEqual(m.plan.merge_strategy, "upsert")

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
