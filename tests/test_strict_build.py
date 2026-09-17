"""Strict contract mode (§1): `strata build --strict` fails E014 unless every
built model declares -> contract (verify_contract only checks models that
declare one; without the flag a bare model builds silently)."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from strata import cli

CONTRACT = '''
contract c {
  a : string nonnull
  n : int64
}
'''

SRC_CONTRACTED = 'source s(ns: "ns", dataset: "ds") {\n' \
    '  columns: { a: string nonnull, n: int64 }\n' \
    '}\n' + CONTRACT + \
    'model m -> contract c {\n  from s\n  select { a = a, n = n }\n}\n'

SRC_BARE = 'source s(ns: "ns", dataset: "ds") {\n' \
    '  columns: { a: string nonnull, n: int64 }\n' \
    '}\n' + CONTRACT + \
    'model m {\n  from s\n  select { a = a, n = n }\n}\n' \
    'model m2 -> contract c {\n  from s\n  select { a = a, n = n }\n}\n'


class TestStrictBuild(unittest.TestCase):
    def build(self, src, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.strata"
            path.write_text(src)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["build", str(path), *extra])
            return rc, out.getvalue(), err.getvalue()

    def test_contracted_model_builds_under_strict(self):
        rc, out, err = self.build(SRC_CONTRACTED, "--strict")
        self.assertEqual(rc, 0, err)
        self.assertIn("model m -> contract c", out)
        self.assertEqual((rc, out, err), self.build(SRC_CONTRACTED))

    def test_bare_model_fails_strict_with_e014(self):
        self.assert_missing_contracts(self.build(SRC_BARE, "--strict"), "m")

    def test_bare_model_builds_without_strict(self):
        rc, out, err = self.build(SRC_BARE)
        self.assertEqual(rc, 0, err)
        self.assertIn("model m\n", out)
        self.assertEqual(err, "")

    def test_strict_gates_only_built_models(self):
        # Unrelated bare models are outside the selected dependency graph.
        rc, out, err = self.build(SRC_BARE, "m2", "--strict")
        self.assertEqual(rc, 0, err)
        self.assertIn("model m2 -> contract c", out)
        self.assert_missing_contracts(
            self.build(SRC_BARE, "m2", "m", "--strict"), "m")

    def assert_missing_contracts(self, result, *names):
        rc, out, err = result
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "error: E014: strict mode requires every built model "
                         "to declare -> contract: " + ", ".join(names) + "\n")

    def test_transitive_bare_dependencies_fail(self):
        src = SRC_BARE + '''
model middle -> contract c { from m }
model target -> contract c { from middle }
'''
        self.assert_missing_contracts(
            self.build(src, "target", "--strict"), "m")

    def test_contracted_dependencies_pass(self):
        src = SRC_CONTRACTED + 'model target -> contract c { from m }\n'
        rc, out, err = self.build(src, "target", "--strict")
        self.assertEqual(rc, 0, err)
        self.assertIn("model target -> contract c", out)
        self.assertNotIn("model m -> contract c", out)

    def test_all_missing_contracts_are_sorted(self):
        src = SRC_BARE + 'model z { from s }\nmodel a { from s }\n'
        self.assert_missing_contracts(self.build(src, "--strict"), "a", "m", "z")

    def test_source_only_does_not_require_contract(self):
        src = 'source s(ns: "ns", dataset: "ds") { columns: { n: int64 } }'
        rc, out, err = self.build(src, "--strict")
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")

    def test_existing_contract_errors_still_fail(self):
        cases = [
            (CONTRACT.replace("a : string nonnull", "missing : string"), "E010"),
            (CONTRACT.replace("a : string nonnull", "a : date"), "E011"),
            (CONTRACT.replace("n : int64", "n : int64 nonnull"), "E012"),
            ("", "E061"),
        ]
        for contract, code in cases:
            with self.subTest(code=code):
                src = SRC_CONTRACTED.replace(CONTRACT, contract)
                rc, out, err = self.build(src, "--strict")
                self.assertNotEqual(rc, 0)
                self.assertIn(code, err)
                self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
