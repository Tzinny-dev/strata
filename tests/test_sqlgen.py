import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import Checker
from strata.parser import parse_strata
from strata import sqlgen

EX = Path(__file__).parent.parent / "examples"


def build(name):
    text = (EX / name).read_text()
    proj = analysis.Project(parse_strata(text, name))
    Checker(proj).check_all()
    return proj


class TestSQLGenDaily(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = build("daily_orders.strata")
        cls.sql = sqlgen.model_sql(cls.proj.typed["daily_orders"])

    def test_join_and_group(self):
        self.assertIn("LEFT JOIN refunds", self.sql)
        self.assertIn("GROUP BY", self.sql)

    def test_aggregates(self):
        self.assertIn("SUM(", self.sql)
        self.assertIn("COUNT(", self.sql)
        self.assertIn("COALESCE(", self.sql)

    def test_key_columns(self):
        self.assertIn("order_day", self.sql)
        self.assertIn("country", self.sql)

    def test_sorts(self):
        self.assertIn("ORDER BY", self.sql)


class TestSQLGenMultinacional(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = build("multinacional.strata")

    def test_generated_models_are_nodes(self):
        names = ["m_ES_orders", "m_MX_orders", "m_CO_orders", "m_BR_orders"]
        for n in names:
            self.assertIn(n, self.proj.typed)
            self.assertTrue(self.proj.models[n].generated)

    def test_generated_sql_filters(self):
        tm = self.proj.typed["m_ES_orders"]
        sql = sqlgen.model_sql(tm)
        self.assertIn("'ES'", sql)

    def test_contract_enforced_on_generated(self):
        tm = self.proj.typed["m_MX_orders"]
        self.assertEqual(tm.contract, "OrderMetrics")
        self.assertIn("country", tm.schema)


class TestFullSQLExecutableSyntax(unittest.TestCase):
    def test_all_models_sql(self):
        proj = build("daily_orders.strata")
        sql = sqlgen.full_sql(proj.typed, list(proj.typed))
        self.assertTrue(sql.strip().startswith("CREATE OR REPLACE VIEW"))

    def test_no_duplicate_column_alias(self):
        # let `country = upper(...)` shadows orders.country: the base projection
        # must not also emit t0.country, or DuckDB group-by disambiguates wrong.
        src = (EX / "daily_orders.strata").read_text().replace(
            "upper(orders.country)", "lower(orders.country)")
        proj = analysis.Project(parse_strata(src, "shadow.strata"))
        Checker(proj).check_all()
        sql = sqlgen.model_sql(proj.typed["daily_orders"])
        self.assertIn("LOWER(t0.country) AS country", sql)
        self.assertNotIn("t0.country AS country", sql)


if __name__ == "__main__":
    unittest.main()