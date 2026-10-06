"""Unit tests for strata/types.py: coercion, unification and column descriptors.

These cover the money/decimal/numeric coercion matrix (binary_type), the
coalesce/case least-upper-bound (unify), and the Col.describe rendering that
dbt/exporter tooling relies on.
"""
import unittest

from strata.types import (BOOL, DATE, FLOAT64, INT64, STRING, UNKNOWN,
                          Col, array, binary_type, decimal,
                          map_type, money, struct_type, unify)


class TestBinaryType(unittest.TestCase):
    def test_concat_string(self):
        self.assertEqual(binary_type("||", STRING, STRING), STRING)
        self.assertIs(binary_type("||", STRING, INT64), UNKNOWN)

    def test_money_money_same_currency(self):
        m = money("USD")
        self.assertEqual(binary_type("+", m, m), money("USD"))
        self.assertEqual(binary_type("-", m, m), money("USD"))
        self.assertEqual(binary_type("*", m, m), money("USD"))
        self.assertEqual(binary_type("/", m, m), FLOAT64)
        self.assertEqual(binary_type("%", m, m), money("USD"))

    def test_money_money_mixed_currency_is_error(self):
        with self.assertRaises(TypeError):
            binary_type("+", money("USD"), money("EUR"))

    def test_money_op_numeric(self):
        m = money("USD")
        self.assertEqual(binary_type("*", m, INT64), money("USD"))
        self.assertEqual(binary_type("*", INT64, m), money("USD"))
        self.assertIs(binary_type("*", m, STRING), UNKNOWN)
        self.assertIs(binary_type("+", m, INT64), UNKNOWN)
        self.assertIs(binary_type("/", m, STRING), UNKNOWN)

    def test_numeric_ops(self):
        self.assertEqual(binary_type("+", INT64, INT64), INT64)
        self.assertEqual(binary_type("*", INT64, FLOAT64), FLOAT64)
        d, d8 = decimal(18, 4), decimal(10, 2)
        self.assertEqual(binary_type("+", d, d8), decimal(10, 2))
        self.assertEqual(binary_type("-", d, INT64), decimal())
        self.assertIs(binary_type("%", STRING, INT64), UNKNOWN)
        self.assertEqual(binary_type("/", INT64, INT64), FLOAT64)
        self.assertIs(binary_type("/", DATE, DATE), UNKNOWN)
        self.assertIs(binary_type("==", INT64, INT64), UNKNOWN)


class TestUnify(unittest.TestCase):
    def test_equal_types_short_circuit(self):
        self.assertEqual(unify(INT64, INT64), INT64)

    def test_decimal_and_money_coerce_numeric(self):
        d = decimal(18, 4)
        self.assertEqual(unify(d, INT64), d)
        self.assertEqual(unify(INT64, d), d)
        m = money("USD")
        self.assertEqual(unify(m, INT64), m)
        self.assertEqual(unify(INT64, m), m)

    def test_money_money(self):
        self.assertEqual(unify(money("USD"), money("EUR")), money("USD"))

    def test_numeric_lub(self):
        self.assertEqual(unify(FLOAT64, INT64), FLOAT64)
        self.assertEqual(unify(decimal(), INT64), decimal())
        self.assertEqual(unify(INT64, INT64), INT64)

    def test_unrelated(self):
        self.assertIs(unify(STRING, DATE), UNKNOWN)


class TestTypeStrings(unittest.TestCase):
    def test_nested_type_rendering(self):
        self.assertTrue(str(money("MXN")).startswith("money"))
        self.assertIn("decimal", str(decimal(10, 2)))
        self.assertEqual(str(array(INT64)), "array<int64>")
        self.assertEqual(str(map_type(STRING, INT64)), "map<string,int64>")
        self.assertEqual(str(struct_type([])), "struct<>")
        self.assertEqual(str(struct_type([("a", INT64), ("b", STRING)])),
                         "struct<a:int64,b:string>")


class TestColDescribe(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(Col(name="c", t=INT64).describe(), "c: int64")

    def test_all_flags(self):
        c = Col(name="c", t=INT64, nullable=False, primary=True,
                enum=frozenset({"A", "B"}), protected=True,
                classification="pii")
        out = c.describe()
        self.assertIn("nonnull", out)
        self.assertIn("PK", out)
        self.assertIn("enum{A,B}", out)
        self.assertIn("protected", out)
        self.assertIn("class=pii", out)

    def test_unique_flag(self):
        self.assertIn("unique", Col(name="c", t=INT64, unique=True).describe())


class TestBool(unittest.TestCase):
    def test_bool_type_exists(self):
        self.assertEqual(BOOL.name, "bool")


if __name__ == "__main__":
    unittest.main()