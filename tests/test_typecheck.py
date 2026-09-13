import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import StrataError, Checker, build_down_edges, blast_radius
from strata.parser import parse_strata

EX = Path(__file__).parent.parent / "examples"


def check_text(text, path="<t>"):
    proj = analysis.Project(parse_strata(text, path))
    Checker(proj).check_all()
    return proj


class TestDailyOrdersTypecheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = check_text((EX / "daily_orders.strata").read_text(), "daily_orders.strata")

    def test_schema_matches_contract(self):
        tm = self.proj.typed["daily_orders"]
        self.assertEqual(
            set(tm.schema),
            {"country", "order_day", "order_id", "customer_id",
             "gross_amount", "net_amount"})

    def test_types_and_nullability(self):
        tm = self.proj.typed["daily_orders"]
        self.assertFalse(tm.schema["gross_amount"].nullable)
        self.assertEqual(str(tm.schema["gross_amount"].t), "money(USD)")
        self.assertFalse(tm.schema["net_amount"].nullable)
        self.assertFalse(tm.schema["order_id"].nullable)
        self.assertEqual(str(tm.schema["order_id"].t), "int64")

    def test_lineage(self):
        tm = self.proj.typed["daily_orders"]
        dest = tm.lineage["net_amount"]
        self.assertEqual(
            {(o.node, o.col, o.kind) for o in dest},
            {("orders", "gross_amount_usd", "aggregated"),
             ("refunds", "discount_usd", "aggregated")})
        self.assertEqual(tm.lineage["country"][0].kind, "grouped")
        self.assertEqual(tm.lineage["country"][0].col, "country")

    def test_reads(self):
        tm = self.proj.typed["daily_orders"]
        self.assertIn(("orders", "order_id"), tm.reads)
        self.assertIn(("refunds", "discount_usd"), tm.reads)

    def test_fingerprint_deterministic(self):
        proj2 = check_text((EX / "daily_orders.strata").read_text(), "daily_orders.strata")
        self.assertEqual(proj2.typed["daily_orders"].fingerprint,
                         self.proj.typed["daily_orders"].fingerprint)


class TestNegative(unittest.TestCase):
    BASE = """
source orders(ns: "crm", dataset: "prod_orders") {
  columns: { order_id: int64 nonnull, country: string nonnull, amount: money }
}
"""

    def test_missing_contract_column(self):
        text = self.BASE + """
contract C { other: int64 nonnull }
model m -> contract C { from orders }
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E010")

    def test_type_mismatch(self):
        text = self.BASE + """
contract C { country: date nonnull }
model m -> contract C { from orders }
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E011")

    def test_nullable_vs_nonnull(self):
        text = self.BASE + """
contract C { amount: money nonnull }
model m -> contract C { from orders }
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E012")

    def test_aggregate_outside_group(self):
        text = self.BASE + """
model m {
  from orders
  derive { n = count(orders.order_id) }
}
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E056")

    def test_unknown_column(self):
        text = self.BASE + """
model m { from orders let x = orders.nope }
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E040")

    def test_currency_mismatch(self):
        text = self.BASE + """
contract C { a: money(EUR) nonnull }
model m -> contract C { from orders let a = orders.amount }
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E011")


class TestLineageDiff(unittest.TestCase):
    def test_blast_radius(self):
        text = """
source orders(ns: "crm", dataset: "prod_orders") {
  columns: { order_id: int64 nonnull, country: string nonnull }
}
model base_m { from orders let c = upper(orders.country) derive { country = c } }
model consumers {
  from base_m
  let upper_country = upper(base_m.country)
  derive { c2 = upper_country }
}
"""
        proj = check_text(text)
        tms = proj.typed
        down = build_down_edges(tms)
        self.assertIn(("orders", "country"), down)
        self.assertIn(("base_m", "country"), down[("orders", "country")])
        radius = blast_radius(tms, [("orders", "country")])
        self.assertIn(("consumers", "c2"), radius)
        self.assertIn(("base_m", "country"), radius)


class TestForeignKeyRules(unittest.TestCase):
    def test_currency_mismatch_comparison(self):
        text = """
source orders(ns: "crm", dataset: "prod_orders") {
  columns: { order_id: int64 nonnull, price_usd: money(USD) nonnull, price_eur: money(EUR) nonnull }
}
model m { from orders filter orders.price_usd == orders.price_eur }
"""
        with self.assertRaises(StrataError) as cm:
            check_text(text)
        self.assertEqual(cm.exception.code, "E051")


if __name__ == "__main__":
    unittest.main()