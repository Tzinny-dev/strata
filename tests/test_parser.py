import os
import unittest
from pathlib import Path

from strata.parser import parse_strata, ParseError
from strata import ast

EX = Path(__file__).parent.parent / "examples"


class TestParserDailyOrders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = parse_strata((EX / "daily_orders.strata").read_text(), "daily_orders.strata")

    def test_top_decls(self):
        counts = {type(d) for d in self.mod.decls}
        self.assertIn(ast.SourceDecl, counts)
        self.assertIn(ast.ContractDecl, counts)
        self.assertIn(ast.ModelDecl, counts)
        self.assertIn(ast.PipelineDecl, counts)

    def test_model_stmts(self):
        m = next(d for d in self.mod.decls if isinstance(d, ast.ModelDecl))
        kinds = [type(s) for s in m.stmts]
        self.assertEqual(
            [ast.FromStmt, ast.JoinStmt, ast.LetStmt, ast.LetStmt, ast.LetStmt,
             ast.FilterStmt, ast.GroupStmt, ast.SortStmt],
            kinds,
        )
        grp = m.stmts[6]
        self.assertIsInstance(grp, ast.GroupStmt)
        self.assertEqual(len(grp.keys), 2)
        agg = grp.body[0]
        self.assertIsInstance(agg, ast.AggregateStmt)
        self.assertEqual([a.name for a in agg.assigns],
                         ["order_id", "customer_id", "gross_amount", "net_amount"])

    def test_pipeline(self):
        p = next(d for d in self.mod.decls if isinstance(d, ast.PipelineDecl))
        self.assertEqual(p.env, "prod")
        self.assertEqual(p.models[0].name, "daily_orders")


class TestParserMultinacional(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = parse_strata((EX / "multinacional.strata").read_text(), "multinacional.strata")

    def test_fns_and_generator(self):
        has_fn = any(isinstance(d, ast.FnDecl) for d in self.mod.decls)
        has_gen = any(isinstance(d, ast.GeneratorDecl) for d in self.mod.decls)
        self.assertTrue(has_fn)
        self.assertTrue(has_gen)

    def test_model_literal_in_fn(self):
        fn = next(d for d in self.mod.decls if isinstance(d, ast.FnDecl)
                  and d.name == "by_country")
        comp = fn.body
        self.assertIsInstance(comp, ast.ListComprehension)
        self.assertIsInstance(comp.body, ast.ModelValue)
        self.assertEqual(comp.var, "c")


class TestLexerBadInput(unittest.TestCase):
    def test_bad_char(self):
        from strata.lexer import LexError
        with self.assertRaises(LexError):
            parse_strata("model x { from orders \x00 }")

    def test_unbalanced(self):
        with self.assertRaises(ParseError):
            parse_strata("model x { from orders ")  # EOF inside model body


if __name__ == "__main__":
    unittest.main()