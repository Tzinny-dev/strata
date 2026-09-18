"""Fase 4: grammar-as-code <-> GBNF emission (constrained decoding).

Lockstep guarantees under test:
1. strata/grammar.py KEYWORDS/TYPE_KEYWORDS/SYMBOLS match exactly what
   strata/lexer.py produces: no token the lexer can emit is outside the grammar.
2. grammar.validate() -> [] (all rule refs resolve, all keywords used).
3. emit_gbnf() closure: every referenced rule is defined and LHS/RHS names
   match exactly (GBNF requires identical names).
4. The grammar covers the real corpus: examples + bench cases parse.
5. All statement heads the parser dispatches on appear as terminals in the GBNF.
6. CLI: `strata grammar [--doc]` exits 0 and emits the rule set.
"""
import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from strata import grammar
from strata import lexer as lexer_mod
from strata.cli import main as cli_main
from strata.parser import parse_strata

ROOT = Path(__file__).resolve().parents[1]


class TestLexicalLockstep(unittest.TestCase):
    def test_keywords_match_lexer(self):
        self.assertEqual(sorted(grammar.KEYWORDS), sorted(lexer_mod.KEYWORDS))

    def test_type_keywords_match_lexer(self):
        self.assertEqual(sorted(grammar.TYPE_KEYWORDS), sorted(lexer_mod.TYPE_KEYWORDS))

    def test_symbols_match_lexer(self):
        # every multi-char symbol the lexer scans must exist in the grammar table
        for s in lexer_mod.SYMBOLS:
            self.assertIn(s, grammar.SYMBOLS)


class TestGrammarConsistency(unittest.TestCase):
    def test_validate_clean(self):
        self.assertEqual(grammar.validate(), [])

    def test_gbnf_rules_are_closed(self):
        text = grammar.emit_gbnf()
        names = set(re.findall(r"^([a-z_][a-z0-9_]*) ::=", text, re.M))
        self.assertTrue(names)
        # strip char-class bodies AND quoted terminals (escape-aware, in one pass
        # so a quoted "[" or a class containing '"' cannot confuse the scan)
        strip = re.compile(r'\[[^\]]*\]|"(?:[^"\\]|\\.)*"')
        undefined = set()
        for line in text.splitlines():
            if "::=" not in line:
                continue
            _lhs, rhs = line.split("::=", 1)
            rhs = strip.sub(" ", rhs)
            rhs = re.sub(r"[()\[\]|*+?]", " ", rhs)
            for tok in rhs.split():
                if tok not in names:
                    undefined.add(tok)
        self.assertEqual(undefined, set())

    def test_lhs_names_use_underscores_only(self):
        text = grammar.emit_gbnf()
        for m in re.finditer(r"^([A-Za-z0-9_\-]+) ::=", text, re.M):
            self.assertNotIn("-", m.group(1))

    def test_gbnf_covers_all_statement_heads(self):
        text = grammar.emit_gbnf()
        for kw in ["from", "join_left", "join_inner", "join_anti", "join_semi",
                   "filter", "where", "let", "select", "derive", "aggregate",
                   "group", "sort", "take"]:
            self.assertIn(f'"{kw}"', text, kw)


class TestGrammarCoversCorpus(unittest.TestCase):
    def test_examples_parse(self):
        files = sorted((ROOT / "examples").glob("*.strata"))
        self.assertTrue(files)
        for f in files:
            parse_strata(f.read_text(), str(f))

    def test_bench_cases_parse(self):
        files = sorted((ROOT / "bench" / "cases").rglob("*.strata"))
        self.assertTrue(files)
        for f in files:
            parse_strata(f.read_text(), str(f))


class TestGrammarCli(unittest.TestCase):
    def test_cli_grammar_plain(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["grammar"])
        self.assertEqual(rc, 0)
        self.assertIn("root ::= top_decl*", buf.getvalue())
        self.assertIn('model_decl ::= "model" (ident | string)', buf.getvalue())

    def test_cli_grammar_doc(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["grammar", "--doc"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("# top_decl:", out)
        self.assertIn("root ::= top_decl*", out)

    def test_cli_grammar_check_accepts_corpus(self):
        f = ROOT / "examples" / "daily_orders.strata"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["grammar", "--check", str(f)])
        self.assertEqual(rc, 0)
        self.assertIn("accepted", buf.getvalue())

    def test_cli_grammar_check_rejects_garbage(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".strata", delete=False) as tf:
            tf.write("model { from broken start}")
            path = tf.name
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = cli_main(["grammar", "--check", path])
        self.assertEqual(rc, 2)
        self.assertIn("REJECTED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
