"""Fase 4: column-level semantic diff (spec/compiler-design.md §7 ref1..ref2)."""
import unittest

from strata.analysis import Checker, Project
from strata.diff import diff_projects, breaking_cols, impact_radius, render, to_json_dict
from strata.parser import parse_strata


def diff_texts(base_text, head_text):
    projs = []
    for text in (base_text, head_text):
        proj = Project(parse_strata(text, "<t>"))
        Checker(proj).check_all()
        projs.append(proj)
    return diff_projects(projs[0], projs[1]), projs[0].typed, projs[1].typed

BASE = """
source orders(ns: "crm", dataset: "prod_orders") {
  columns: { order_id: int64 nonnull, country: string nonnull,
             region: string nonnull, note: string }
}
model base_m { from orders let c = upper(orders.country) derive { country = c } }
model consumers {
  from base_m
  let upper_country = upper(base_m.country)
  derive { c2 = upper_country }
}
"""


def with_source_cols(cols):
    """Swap the whole source `columns: { ... }` block (may span lines), keeping
    models untouched (they only read order_id/country, so any source-level
    change still typechecks)."""
    lines, in_block = [], False
    for line in BASE.splitlines():
        if "columns: {" in line:
            lines.append(f"  columns: {{ {cols} }}")
            in_block = True
        elif in_block and "}" in line:
            in_block = False
        elif not in_block:
            lines.append(line)
    return "\n".join(lines)


class TestSemanticDiff(unittest.TestCase):
    def test_identical_modules(self):
        changes, _, _ = diff_texts(BASE, BASE)
        self.assertEqual(changes, [])

    def test_taxonomy_added_removed_retyped_widened_contract(self):
        # one scenario per non-obvious kind; scenarios pass the FULL source
        # columns block (they only touch region/note, which nobody reads)
        P = "order_id: int64 nonnull, country: string nonnull"
        cases = [
            (f"{P}, region: int64 nonnull, note: string", "retyped",
             "orders.region", "region: string -> int64"),
            (f"{P}, region: string nonnull, note: string, extra: bool", "added",
             "orders.extra", None),
            (f"{P}, region: string, note: string", "narrowed", "orders.region", None),
            (f"{P}, region: string nonnull, note: string nonnull", "widened",
             "orders.note", None),
            (f"{P}, region: string nonnull protected, note: string", "contract",
             "orders.region", None),
            (f"{P}, region: string nonnull", "removed", "orders.note", None),
        ]
        for cols, kind, key, detail in cases:
            changes, _, _ = diff_texts(BASE, with_source_cols(cols))
            flat = [(c.model, c.col, c.kind) for mc in changes for c in mc.columns]
            self.assertIn((key.split(".")[0], key.split(".")[1], kind), flat,
                          f"scenario {cols!r}: {flat}")
            if detail:
                got = [c.detail for mc in changes for c in mc.columns
                       if (c.model, c.col) == (key.split(".")[0], key.split(".")[1])]
                self.assertIn(detail, got[0])

    def test_breaking_kinds_and_radius_flow_to_consumers(self):
        # nonnull -> nullable propagates through base_m into consumers.c2
        head = with_source_cols("order_id: int64 nonnull, country: string,"
                                " region: string nonnull, note: string")
        changes, base_tms, _ = diff_texts(BASE, head)
        brk = breaking_cols(changes)
        self.assertIn(("orders", "country"), brk)
        radius = impact_radius(base_tms, changes)
        self.assertIn(("base_m", "country"), radius)
        self.assertIn(("consumers", "c2"), radius)

    def test_widening_is_not_breaking(self):
        changes, _, _ = diff_texts(
            BASE, with_source_cols("order_id: int64 nonnull, country: string nonnull,"
                                   " region: string nonnull, note: string nonnull"))
        brk = [c for c in breaking_cols(changes) if c[1] == "note"]
        self.assertEqual(brk, [])

    def test_model_added_removed(self):
        changes, _, _ = diff_texts(
            BASE, BASE + "\nmodel extra_m { from orders derive { r = orders.region } }")
        kinds = {mc.model: mc.kind for mc in changes}
        self.assertEqual(kinds.get("extra_m"), "added")
        changes, _, _ = diff_texts(
            BASE + "\nmodel extra_m { from orders derive { r = orders.region } }", BASE)
        kinds = {mc.model: mc.kind for mc in changes}
        self.assertEqual(kinds.get("extra_m"), "removed")

    def test_render_and_json_shapes(self):
        # narrow a CONSUMED column so the downstream impact section renders
        changes, base_tms, _ = diff_texts(
            BASE, with_source_cols("order_id: int64 nonnull, country: string,"
                                   " region: string nonnull, note: string"))
        radius = impact_radius(base_tms, changes)
        text = "\n".join(render(changes, radius))
        self.assertIn("BREAKING", text)
        self.assertIn("downstream impact", text)
        js = to_json_dict("b.strata", "h.strata", changes, radius)
        self.assertEqual(js["base"], "b.strata")
        self.assertTrue(any(c["kind"] == "narrowed" for c in js["breaking"]))
        self.assertIn(["base_m", "country"], js["radius"])


if __name__ == "__main__":
    unittest.main()
