import random
import unittest
from pathlib import Path

from strata.parser import parse_strata, ParseError
from strata.lexer import LexError
from strata import ast

EX = Path(__file__).parent.parent / "examples"

# "Fuzzing del parser" (plan-hito-2, calidad de código): the lexer/parser must
# never crash with an *unexpected* exception on malformed input. Feeding random
# token soup (and von-Neumann mutations of a valid module) is the cheap way to
# prove that: garbage must fail with LexError/ParseError (a proper diagnostic),
# or parse cleanly -- never IndexError/TypeError and friends.
#
# Deterministic: the PRNG is seeded, so a red run reproduces exactly.
_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz_ \t\n{}()[]{},;=:.<>+-*/%&|!?\"'#$@\\0123456789"
    "\u00e9\u00a0\u20ac\ufffd"
)
_ACCEPTABLE = (LexError, ParseError)


def _expect_no_crash(text, seed):
    try:
        parse_strata(text, "fuzz.strata")
    except _ACCEPTABLE:
        return
    except RecursionError:
        raise AssertionError(
            f"seed {seed}: RecursionError on {text[:80]!r} "
            f"(no depth guard or runaway recursion)")
    except Exception as exc:  # pragma: no cover - only fires on bugs
        raise AssertionError(
            f"seed {seed}: unexpected {type(exc).__name__}({exc}) "
            f"on {text[:80]!r}")


class TestParserFuzzRandom(unittest.TestCase):
    def test_random_token_soup_never_crashes(self):
        rng = random.Random(20260920)
        for i in range(3000):
            ln = rng.randrange(0, 40)
            text = "".join(rng.choice(_ALPHABET) for _ in range(ln))
            _expect_no_crash(text, seed=i)

    def test_fragments_of_valid_language_never_crash(self):
        rng = random.Random(7)
        frags = [
            "model m { from s }",
            "source s(x: int64) { connector: 'x' }",
            "contract c { a: string nonnull }",
            "domain k = int64",
            "let x = 1 + 2",
            "group by a { out_count = count(*) }",
            "filter a > 0 and b == 's'",
            "sort a desc",
            "take 10",
            "fn f(a) { a * 2 }",
            "join products on order_id == product_id expect many_to_one",
            "import foo.bar",
            "pipeline p { models: [m] }",
            "test t { model: m, checks: { row_count > 0 } }",
            "test t { model: m, checks: { total > 0 } }",
            "assign { col = x + y }",
            "incremental { cdc_column: updated_at, merge_strategy: append }",
        ]
        for i in range(1500):
            n = rng.randrange(1, 5)
            joined = "\n".join(rng.choice(frags) for _ in range(n))
            mutations = rng.randrange(0, 3)
            text = joined
            for _ in range(mutations):
                op = rng.randrange(3)
                pos = rng.randrange(len(text) + 1)
                if op == 0 and text:
                    text = text[:pos] + text[pos + 1:]
                elif op == 1 and text:
                    text = text[:pos] + rng.choice(_ALPHABET) + text[pos:]
                else:
                    text = text[:pos] + rng.choice(_ALPHABET) + text[pos:]
            _expect_no_crash(text, seed=i)


class TestParserFuzzMutation(unittest.TestCase):
    def setUp(self):
        self.valid = (EX / "daily_orders.strata").read_text()

    def test_every_character_deletion(self):
        for i in range(len(self.valid)):
            text = self.valid[:i] + self.valid[i + 1:]
            _expect_no_crash(text, seed=("delete", i))

    def test_every_truncation_point(self):
        for i in range(len(self.valid)):
            _expect_no_crash(self.valid[:i], seed=("truncate", i))

    def test_truncations_with_reopening_braces(self):
        # Truncate then re-balance: keeps the parser inside body scopes while
        # starving them of their expected tokens (the classic EOF bug class).
        rng = random.Random(2026)
        for i in range(1000):
            cut = rng.randrange(len(self.valid))
            tail = self.valid[:cut]
            for ch in list(tail):
                if ch == "{":
                    tail += "}"
                elif ch == "(":
                    tail += ")"
                elif ch == "[":
                    tail += "]"
            _expect_no_crash(tail, seed=("rebalance", i))

    def test_random_byte_mutation(self):
        rng = random.Random(42)
        base = bytearray(self.valid.encode("utf-8"))
        for i in range(3000):
            buf = bytearray(base)
            for _ in range(rng.randrange(1, 8)):
                buf[rng.randrange(len(buf))] = rng.randrange(1, 256)
            _expect_no_crash(buf.decode("utf-8", "replace"), seed=("mutate", i))

    def test_fuzz_still_parses_generated_good_shape(self):
        # Regression guard so the fuzzer doesn't silently rot into "always
        # errors": a well-formed simple module must still parse, and mutated
        # long chains of `let` must too.
        mod = parse_strata("model m { from s\nlet a = 1\nlet b = a + 1\n}",
                           "good.strata")
        self.assertIsInstance(mod, ast.Module)
        self.assertEqual(len([d for d in mod.decls if isinstance(d, ast.ModelDecl)]), 1)


if __name__ == "__main__":
    unittest.main()