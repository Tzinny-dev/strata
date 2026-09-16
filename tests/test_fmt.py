"""Formatter round-trip regressions using the existing language examples."""
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

from strata.fmt import format_module
from strata.parser import parse_strata


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def semantic_ast(value):
    """Ignore source locations, retaining all semantic AST fields."""
    if is_dataclass(value):
        return (type(value).__name__, {
            f.name: semantic_ast(getattr(value, f.name))
            for f in fields(value) if f.name not in ("span", "path")
        })
    if isinstance(value, (list, tuple)):
        return [semantic_ast(item) for item in value]
    if isinstance(value, dict):
        return {key: semantic_ast(item) for key, item in value.items()}
    return value


class TestFormatterRoundTrip(unittest.TestCase):
    def test_multinacional_round_trip(self):
        path = EXAMPLES / "multinacional.strata"
        original = parse_strata(path.read_text(), str(path))
        formatted = format_module(original)
        reparsed = parse_strata(formatted, str(path))
        self.assertEqual(semantic_ast(original), semantic_ast(reparsed))
        self.assertEqual(formatted, format_module(reparsed))

    def assert_round_trip(self, source):
        original = parse_strata(source)
        formatted = format_module(original)
        reparsed = parse_strata(formatted)
        self.assertEqual(semantic_ast(original), semantic_ast(reparsed))
        self.assertEqual(formatted, format_module(reparsed))
        return formatted

    def test_null_arrays_and_sort(self):
        self.assert_round_trip('''
source s() { columns: { id: int64, tags: array(string) } }
model m {
  from s
  derive { missing = null }
  sort { id desc, -id asc }
  take 2
}
''')

    def test_declarative_test_literals(self):
        source = '''test m {
  expect row_count == 2;
  expect id >= 1;
  expect amount >= 1.5;
  expect code == "123";
  expect active == true;
  expect missing == null;
}'''
        self.assert_round_trip(source)
        checks = parse_strata(source).decls[0].checks
        self.assertIs(type(checks[1].value), int)
        self.assertIs(type(checks[2].value), float)
        self.assertIs(type(checks[3].value), str)

    def test_enum_values_with_spaces_and_punctuation(self):
        self.assert_round_trip('contract C { state: string enum {"in progress", "a,b", ES} }')

    def test_escaped_strings_and_interpolation(self):
        self.assert_round_trip(r'''fn literal() -> Str { "\${country}\\\"\n\t" }
fn template(country: Str) -> Str { "literal=\${country}; value=${country}" }
model "quoted name" { owner: "\${owner}" from s }
''')

    def test_small_and_large_float_literals(self):
        for literal in ("0.0000001", "100000000000000000000.0", "0.0"):
            with self.subTest(literal=literal):
                self.assert_round_trip(f"fn number() -> Expr {{ {literal} }}")
                self.assert_round_trip(f"test m {{ expect amount >= {literal}; }}")

    def test_quoted_model_names(self):
        for name in ('"two words"', '"model"', '"int64"', '""', '"a-b"'):
            with self.subTest(name=name):
                self.assert_round_trip(f"model {name} {{ from s }}")

    def test_empty_classification(self):
        self.assert_round_trip('contract C { id: string classification: "" }')

    def test_unsupported_nodes_fail_instead_of_disappearing(self):
        from strata import ast
        for module in (
            ast.Module(decls=[ast.Node()]),
            ast.Module(decls=[ast.ModelDecl(name="m", stmts=[ast.Stmt()])]),
            ast.Module(decls=[ast.FnDecl(name="f", return_type="Expr", body=ast.Node())]),
        ):
            with self.subTest(module=module):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    format_module(module)

    def test_non_finite_floats_are_rejected(self):
        from strata import ast
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                module = ast.Module(decls=[ast.FnDecl(
                    name="f", return_type="Expr", body=ast.Literal(value=value))])
                with self.assertRaisesRegex(ValueError, "finite"):
                    format_module(module)

    def test_existing_corpus(self):
        root = EXAMPLES.parent
        for path in sorted(EXAMPLES.glob("*.strata")) + sorted((root / "bench" / "cases").rglob("*.strata")):
            with self.subTest(path=str(path)):
                self.assert_round_trip(path.read_text())

    def test_formatted_pipeline_executes_with_same_order_and_tests(self):
        import duckdb
        from strata.analysis import Checker, Project
        from strata.sqlgen import model_sql
        from strata.exec import materialize

        source = '''source s() { columns: { id: int64 nonnull } }
model m { from s sort { id desc } take 2 }
test m { expect row_count == 2; expect id >= 2; }
'''
        formatted = self.assert_round_trip(source)
        results = []
        for text in (source, formatted):
            project = Project(parse_strata(text))
            checker = Checker(project)
            models = checker.check_all()
            checker.check_tests()
            con = duckdb.connect()
            try:
                con.execute("CREATE TABLE s(id BIGINT); INSERT INTO s VALUES (1), (3), (2)")
                materialize(con, project, models)
                results.append(con.execute(model_sql(models["m"])).fetchall())
            finally:
                con.close()
        self.assertEqual(results, [[(3,), (2,)], [(3,), (2,)]])


if __name__ == "__main__":
    unittest.main()
