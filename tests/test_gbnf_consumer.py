"""Independent GBNF consumer validates `strata grammar` against the real
corpus (Fase 4 lockstep, workstream: "real consumer of the GBNF").

The engine in strata/gbnf.py parses the *emitted GBNF text* (not the
grammar-as-code tables) and matches source programs at character level, the
same way llama.cpp would against the spelling of each token. These tests pin:

1. The emitted GBNF parses cleanly and all rule references resolve.
2. Every real program (examples + bench cases) is accepted — the grammar
   covers the actual language, not just a hand-written subset.
3. Round-trip: formatter output is accepted too (same token stream).
4. Garbage the Strata parser rejects is rejected by the GBNF consumer, so the
   grammar is not a runaway superset.
"""
import random
import re
import unittest
from pathlib import Path

from strata import gbnf, grammar, lexer
from strata.fmt import format_module
from strata.parser import parse_strata

ROOT = Path(__file__).resolve().parents[1]


def load_grammar():
    return gbnf.GbnfGrammar.from_text(grammar.emit_gbnf())


class TestGbnfParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load_grammar()

    def test_text_parses_and_is_closed(self):
        self.assertEqual(self.g.validate(), [])

    def test_root_is_top_decl_stream(self):
        self.assertIn("root", self.g.rules)
        self.assertTrue(self.g.accepts(""))

    def test_keyword_terminals_present(self):
        text = grammar.emit_gbnf()
        for kw in ["source", "contract", "model", "pipeline", "fn", "test",
                   "aggregate", "group", "derive", "filter", "select", "take"]:
            self.assertIn(f'"{kw}"', text, kw)


class TestGbnfCoversCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load_grammar()

    def _corpus(self):
        files = sorted((ROOT / "examples").glob("*.strata"))
        files += sorted((ROOT / "bench" / "cases").rglob("*.strata"))
        self.assertTrue(files)
        return files

    def test_examples_and_bench_accepted(self):
        for f in self._corpus():
            toks = lexer.Lexer(f.read_text()).tokenize()
            self.assertTrue(
                gbnf.accepts_program(toks, self.g),
                f"grammar rejected real program {f}",
            )

    def test_roundtrip_formatted_accepted(self):
        # formatter output must lex to the same stream and be accepted
        for f in self._corpus():
            src = f.read_text()
            m = parse_strata(src, str(f))
            formatted = format_module(m)
            toks = lexer.Lexer(formatted).tokenize()
            self.assertTrue(
                gbnf.accepts_program(toks, self.g),
                f"grammar rejected formatted {f}",
            )

    def test_trailing_commas_allowed(self):
        # corpus relies on trailing commas; pin the contract explicitly
        self.assertTrue(self._accepts(
            "pipeline p { env: dev, models: [a, b,], }"
        ))

    def _accepts(self, src):
        return gbnf.accepts_program(lexer.Lexer(src).tokenize(), self.g)


class TestGbnfRejectsGarbage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load_grammar()

    def test_parser_and_consumer_agree_on_garbage(self):
        cases = [
            "model { from broken start}",     # model needs a name
            "contract C { a: int, }",          # no trailing comma in contracts
            "pipeline p { models: [x }",       # unterminated list
            "fn f() -> Int { 1 + }",           # dangling operator
            "pipeline p { bogus: 1 }",         # unknown pipeline key
            "from nowhere",                    # stray statement at top level
            "source s { columns: { a int } }", # column needs a ':'
        ]
        for i, s in enumerate(cases):
            with self.subTest(case=i):
                try:
                    parse_strata(s, f"<neg{i}>")
                    self.fail(f"parser unexpectedly accepted: {s!r}")
                except Exception:
                    pass
                self.assertFalse(
                    gbnf.accepts_program(lexer.Lexer(s).tokenize(), self.g),
                    f"garbage accepted by grammar: {s!r}",
                )


class TestGbnfMatchesParserLanguage(unittest.TestCase):
    """Reverse-direction check: every program the emitted GBNF *accepts* must
    parse. We generate programs from the GBNF itself (deterministic, seeded)
    and demand each one round-trips through the real parser.

    Two classes of character-level acceptance are inherent to GBNF (llama.cpp
    matches token *spellings*, not token kinds) and are pinned here rather than
    treated as grammar bugs:

    1. keyword/ident collisions: ``ident`` also matches reserved-word
       spellings, which the token-aware parser rejects (e.g. ``on``).
    2. ``take N .. M`` range dots: the lexer/llama absorb ``.digits`` into a
       single FLOAT (``.10``), so the ``N..M`` spelling only works with spaces.
    """
    N_SEEDS = 300

    @classmethod
    def setUpClass(cls):
        cls.g = load_grammar()
        cls.PRECEDENCE = grammar.PRECEDENCE
        cls.RULES = grammar.RULES

    def _sample(self, seed):
        _BUDGET[0] = 150
        rng = random.Random(seed)
        ctx = {}
        chunks = []
        decls = ["source-decl", "contract-decl", "model-decl",
                 "pipeline-decl", "fn-decl", "test-decl"]
        for _ in range(rng.randrange(1, 3)):
            chunks.extend(sample_rule(rng.choice(decls), rng, ctx))
        return assemble(chunks)

    def test_sampled_language_parses(self):
        divergences = 0
        for seed in range(self.N_SEEDS):
            text = self._sample(seed)
            if budget_exhausted():
                continue  # GBNF-incomplete truncated sample
            try:
                parse_strata(text, f"<sample{seed}>")
                continue
            except Exception as e:
                toks = lexer.Lexer(text).tokenize()
                if not gbnf.accepts_program(toks, self.g):
                    continue  # sampler artifact, GBNF rejects it too
                if self._is_inherent(text, toks, e):
                    continue
                divergences += 1
                self.fail(f"seed {seed}: GBNF accepts but parser rejects: {e}\n  {text[:400]!r}")
        self.assertEqual(divergences, 0)

    def _is_inherent(self, text, toks, e):
        msg = str(e)
        # 1. keyword/ident collision: a reserved word is matched as an ident
        #    (char-level), but the token-aware parser sees a keyword where an
        #    operand was expected: "found KW 'x'" or "unexpected 'x' in expr".
        mkw = re.search(r"found KW '([^']+)'", msg) or re.search(r"unexpected '([^']+)' in expression", msg)
        if mkw and mkw.group(1) in set(grammar.KEYWORDS):
            return True
        # 2. take-range dots vs FLOAT absorption
        if ("FLOAT" in msg or "unexpected token" in msg) and re.search(r"\d[ \t]*\.[ \t]*\..\d", text):
            return True
        return False


def budget_exhausted():
    return _BUDGET[0] <= 0


def sample_rule(name, rng, ctx):
    global _BUDGET
    if ctx.get(name, 0) > 6:
        return []
    _BUDGET[0] -= 1
    if _BUDGET[0] <= 0:
        return []
    ctx[name] = ctx.get(name, 0) + 1
    if name == "expr":
        out = sample_expr(rng, ctx)
        ctx[name] -= 1
        return out
    _doc, alts = grammar.RULES[name]
    out = sample_node(parse_alt(rng.choice(alts)), rng, ctx)
    ctx[name] -= 1
    return out


def sample_expr(rng, ctx):
    def nxt(level):
        if level >= len(grammar.PRECEDENCE):
            return sample_rule("unary", rng, ctx)
        out = nxt(level + 1)
        while _BUDGET[0] > 6 and rng.random() < 0.4:
            rhs = nxt(level + 1)
            if not rhs:
                break
            out += [rng.choice(grammar.PRECEDENCE[level])] + rhs
        return out
    return nxt(0)


def parse_alt(alt):
    body = grammar._norm_hyphens(" ".join(p for p in (s.strip() for s in alt.split("::")) if p))
    toks = gbnf._gbnf_tokens(body)
    p = gbnf._Parser(toks)
    return p.parse_root()


def sample_node(node, rng, ctx):
    global _BUDGET
    _BUDGET[0] -= 1
    if _BUDGET[0] <= 0:
        return []
    if isinstance(node, gbnf.Lit):
        return [node.text]
    if isinstance(node, gbnf.CharClass):
        return [rng.choice(sorted(node.allow)) if node.allow else "x"]
    if isinstance(node, gbnf.AnyChar):
        return [rng.choice("abcXYZ019")]
    if isinstance(node, gbnf.Ref):
        if node.name in _TERMS:
            return [_sample_term(node.name, rng)]
        if node.name == "expr":
            return sample_expr(rng, ctx)
        if node.name not in grammar.RULES:
            key = node.name.replace("_", "-")
            if key not in grammar.RULES:
                raise AssertionError(f"unknown ref {node.name}")
            return sample_rule(key, rng, ctx)
        return sample_rule(node.name, rng, ctx)
    if isinstance(node, gbnf.Seq):
        out = []
        for item in node.items:
            out.extend(sample_node(item, rng, ctx))
        return out
    if isinstance(node, gbnf.Alt):
        return sample_node(rng.choice(node.branches), rng, ctx)
    if isinstance(node, gbnf.Rep):
        out = []
        for _ in range(node.lo):
            out.extend(sample_node(node.inner, rng, ctx))
        hi = node.hi
        n = rng.randrange(0, (hi if hi is not None else 2) + 1)
        for _ in range(n):
            out.extend(sample_node(node.inner, rng, ctx))
        return out
    raise AssertionError(f"node {node!r}")


def _sample_term(t, rng):
    if t == "IDENT":
        for _ in range(50):
            w = rng.choice("abcdefghijklmnopqrstuvwxyz") + "".join(
                rng.choice("abcdefghijklmnopqrstuvwxyz0123456789_")
                for _ in range(rng.randrange(0, 6)))
            kw = set(grammar.KEYWORDS) | set(grammar.TYPE_KEYWORDS)
            if w not in kw and w not in ("from", "env", "models", "sources", "over", "List"):
                return w
        return "z"
    if t == "INT":
        return str(rng.randrange(0, 10000))
    if t == "FLOAT":
        return f"{rng.randrange(0,100)}.{rng.randrange(0,100)}"
    if t == "STR":
        parts = []
        for _ in range(rng.randrange(0, 4)):
            if rng.random() < 0.3:
                ident = "".join(rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
                                for _ in range(rng.randrange(1, 5)))
                parts.append("${" + ident + "}")
            else:
                parts.append("".join(rng.choice("abc XYZ019_-") for _ in range(rng.randrange(0, 6))))
        return '"' + "".join(parts).replace("\\", "\\\\").replace('"', '\\"') + '"'
    if t == "TYPE_KW_SCALAR":
        return rng.choice(sorted(k for k in grammar.TYPE_KEYWORDS if k not in ("decimal", "array")))
    return rng.choice(sorted(grammar.TYPE_KEYWORDS))


def assemble(chunks):
    out = []
    word = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    for c in chunks:
        if out and c[0] in word and out[-1][-1] in word:
            out.append(" ")
        out.append(c)
    return "".join(out)


_BUDGET = [150]
_TERMS = {"STR", "INT", "FLOAT", "IDENT", "TYPE_KW", "TYPE_KW_SCALAR"}


if __name__ == "__main__":
    unittest.main()