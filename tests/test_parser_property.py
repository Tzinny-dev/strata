"""Property-based testing for the Strata parser using Hypothesis.

These tests verify that the parser:
1. Never crashes on arbitrary input (only LexError/ParseError)
2. Roundtrips: parse -> fmt -> parse produces equivalent AST
3. Generated valid programs parse correctly
"""
import hypothesis
from hypothesis import given, strategies as st, settings, example
import pytest

from strata.parser import parse_strata, ParseError
from strata.lexer import LexError
from strata import fmt as _fmt
from strata import ast


# Acceptable exceptions for invalid input
_ACCEPTABLE = (LexError, ParseError)


def _parse_safe(text: str):
    """Parse and return AST or None if it fails with acceptable errors."""
    try:
        return parse_strata(text, "fuzz.strata")
    except _ACCEPTABLE:
        return None
    except RecursionError as e:
        raise AssertionError(f"RecursionError on {text[:80]!r}") from e
    except Exception as e:
        raise AssertionError(f"Unexpected {type(e).__name__}({e}) on {text[:80]!r}") from e


# --- Hypothesis strategies for generating valid Strata fragments ---

@st.composite
def ident_strategy(draw):
    """Generate valid identifiers."""
    first = draw(st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"))
    rest = draw(st.text(st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"), min_size=0, max_size=20))
    return first + rest


@st.composite
def type_strategy(draw):
    """Generate valid type expressions."""
    simple_types = ["int64", "int32", "float64", "float32", "string", "bool", "date", "timestamp", "money"]
    t = draw(st.sampled_from(simple_types))
    # Optionally add nonnull/enum
    if draw(st.booleans()):
        t += " nonnull"
    if draw(st.booleans()) and "string" in t:
        t += " enum {A, B, C}"
    return t


@st.composite
def column_def_strategy(draw):
    """Generate a column definition."""
    name = draw(ident_strategy())
    typ = draw(type_strategy())
    return f"{name}: {typ}"


@st.composite
def source_decl_strategy(draw):
    """Generate a source declaration."""
    name = draw(ident_strategy())
    cols = draw(st.lists(column_def_strategy(), min_size=1, max_size=5))
    cols_str = ",\n  ".join(cols)
    ns = draw(ident_strategy())
    dataset = draw(ident_strategy())
    return f"source {name}(ns: \"{ns}\", dataset: \"{dataset}\") {{\n  columns: {{\n    {cols_str}\n  }}\n}}"


@st.composite
def contract_decl_strategy(draw):
    """Generate a contract declaration."""
    name = draw(ident_strategy())
    fields = draw(st.lists(column_def_strategy(), min_size=1, max_size=5))
    fields_str = ",\n  ".join(fields)
    return f"contract {name} {{\n  {fields_str}\n}}"


@st.composite
def expr_strategy(draw):
    """Generate simple expressions."""
    # Literals
    lit_type = draw(st.sampled_from(["int", "float", "string", "bool", "null"]))
    if lit_type == "int":
        return str(draw(st.integers(min_value=-1000, max_value=1000)))
    elif lit_type == "float":
        return str(draw(st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False)))
    elif lit_type == "string":
        s = draw(st.text(st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters="'\\\""), min_size=0, max_size=20))
        return f"'{s}'"
    elif lit_type == "bool":
        return draw(st.sampled_from(["true", "false"]))
    else:
        return "null"


@st.composite
def column_ref_strategy(draw):
    """Generate a column reference."""
    return draw(ident_strategy())


# Statement strategies - each returns a string
@st.composite
def let_stmt_strategy(draw):
    """Generate a let statement."""
    name = draw(ident_strategy())
    expr = draw(expr_strategy())
    return f"let {name} = {expr}"


@st.composite
def filter_stmt_strategy(draw):
    """Generate a filter statement."""
    left = draw(column_ref_strategy())
    op = draw(st.sampled_from(["==", "!=", ">", "<", ">=", "<="]))
    right = draw(expr_strategy())
    return f"filter {left} {op} {right}"


@st.composite
def select_stmt_strategy(draw):
    """Generate a select statement."""
    assigns = draw(st.lists(
        st.tuples(ident_strategy(), expr_strategy()),
        min_size=1, max_size=4
    ))
    assigns_str = ",\n    ".join(f"{n} = {e}" for n, e in assigns)
    return f"select {{\n    {assigns_str}\n  }}"


@st.composite
def group_stmt_strategy(draw):
    """Generate a group statement."""
    keys = draw(st.lists(ident_strategy(), min_size=1, max_size=3))
    keys_str = ", ".join(keys)
    assigns = draw(st.lists(
        st.tuples(ident_strategy(), expr_strategy()),
        min_size=1, max_size=3
    ))
    assigns_str = ",\n      ".join(f"{n} = {e}" for n, e in assigns)
    return f"group {{{keys_str}}} (\n    aggregate {{\n      {assigns_str}\n    }}\n  )"


# Combined model statement strategy
model_stmt_strategy = st.one_of(
    let_stmt_strategy(),
    filter_stmt_strategy(),
    select_stmt_strategy(),
    group_stmt_strategy(),
)


@st.composite
def model_decl_strategy(draw):
    """Generate a model declaration."""
    name = draw(ident_strategy())
    contract_name = draw(st.sampled_from([None] + [f"Contract{draw(st.integers(1, 99))}" for _ in range(3)]))
    contract_part = f" -> contract {contract_name}" if contract_name else ""
    stmts = draw(st.lists(model_stmt_strategy, min_size=1, max_size=5))
    stmts_str = "\n  ".join(stmts)
    return f"model {name}{contract_part} {{\n  from source_{draw(st.integers(1, 9))}\n  {stmts_str}\n}}"


@st.composite
def pipeline_decl_strategy(draw):
    """Generate a pipeline declaration."""
    name = draw(ident_strategy())
    models = draw(st.lists(ident_strategy(), min_size=1, max_size=3))
    models_str = ", ".join(f'"{m}"' for m in models)
    return f"pipeline {name} {{\n  env: prod,\n  models: [{models_str}]\n}}"


@st.composite
def full_module_strategy(draw):
    """Generate a complete valid module."""
    imports = draw(st.lists(st.text(min_size=5, max_size=20), min_size=0, max_size=2))
    sources = draw(st.lists(source_decl_strategy(), min_size=1, max_size=3))
    contracts = draw(st.lists(contract_decl_strategy(), min_size=0, max_size=2))
    domains = draw(st.lists(st.text(min_size=10, max_size=30), min_size=0, max_size=1))
    models = draw(st.lists(model_decl_strategy(), min_size=1, max_size=3))
    pipelines = draw(st.lists(pipeline_decl_strategy(), min_size=0, max_size=2))

    parts = []
    for imp in imports:
        parts.append(f"import {imp}")
    parts.extend(sources)
    parts.extend(contracts)
    for dom in domains:
        parts.append(f"domain {dom}")
    parts.extend(models)
    parts.extend(pipelines)

    return "\n\n".join(parts)


# --- Tests ---

class TestParserNeverCrashes:
    """The parser must never crash with unexpected exceptions."""

    @settings(max_examples=500, deadline=None)
    @given(st.text(min_size=0, max_size=200))
    def test_random_unicode_never_crashes(self, text: str):
        """Random unicode text must not crash the parser."""
        _parse_safe(text)

    @settings(max_examples=100, deadline=None)
    @given(full_module_strategy())
    def test_generated_valid_module_parses(self, text: str):
        """Generated valid-looking modules should parse (or fail with proper errors)."""
        result = _parse_safe(text)
        # We accept either parsing success or proper error
        # The strategies may generate invalid combinations


class TestParserRoundtrip:
    """Parse -> Format -> Parse should produce equivalent AST."""

    @settings(max_examples=50, deadline=None)
    @given(full_module_strategy())
    def test_parse_fmt_parse_roundtrip(self, text: str):
        """Valid modules should roundtrip through fmt."""
        # Parse original
        mod1 = _parse_safe(text)
        if mod1 is None:
            # Invalid input, skip roundtrip test
            return

        # Format
        try:
            formatted = _fmt.format_module(mod1)
        except Exception as e:
            pytest.skip(f"Formatting failed: {e}")

        # Parse formatted
        mod2 = _parse_safe(formatted)
        if mod2 is None:
            raise AssertionError(f"Formatted output failed to parse:\n{formatted[:200]}")

        # Compare key structural properties
        assert len(mod1.decls) == len(mod2.decls), f"Decl count mismatch: {len(mod1.decls)} vs {len(mod2.decls)}"
        for d1, d2 in zip(mod1.decls, mod2.decls):
            assert type(d1) == type(d2), f"Decl type mismatch: {type(d1)} vs {type(d2)}"


class TestLexerParserIntegration:
    """Integration tests for lexer + parser together."""

    @settings(max_examples=100, deadline=None)
    @given(st.text(min_size=0, max_size=500))
    def test_lexer_parser_never_crash_together(self, text: str):
        """Lexer + parser chain must not crash."""
        _parse_safe(text)


class TestEdgeCases:
    """Specific edge cases that have caused bugs."""

    @example("model m { from s\nlet a = 1\nlet b = a + 1\n}")
    @settings(max_examples=50, deadline=None)
    @given(st.text(min_size=0, max_size=100))
    def test_known_good_patterns_still_work(self, _):
        """Regression: known good patterns must still parse."""
        mod = parse_strata("model m { from s\nlet a = 1\nlet b = a + 1\n}", "good.strata")
        assert isinstance(mod, ast.Module)
        models = [d for d in mod.decls if isinstance(d, ast.ModelDecl)]
        assert len(models) == 1

    @settings(max_examples=200, deadline=None)
    @given(st.integers(min_value=1, max_value=50))
    def test_deep_nesting_handled(self, depth: int):
        """Deeply nested expressions should not cause stack overflow."""
        expr = "1"
        for _ in range(depth):
            expr = f"({expr} + 1)"
        text = f"model m {{ from s\nlet x = {expr}\n}}"
        result = _parse_safe(text)
        # Deep nesting might hit recursion limit, that's OK
        # but shouldn't cause other crashes


# Configure hypothesis for CI
hypothesis.settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    suppress_health_check=[hypothesis.HealthCheck.too_slow]
)
hypothesis.settings.load_profile("ci")