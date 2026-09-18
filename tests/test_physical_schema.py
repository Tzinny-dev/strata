"""Physical schema validation per dialect (§2): no execution."""
import unittest

from strata.exec import check_physical_schema
from strata.types import StrataType, Col, money
from strata.analysis import TypedModel
from collections import OrderedDict


def make_tm(name: str, schema: dict) -> TypedModel:
    tm = TypedModel(name=name, contract=None, attrs={})
    tm.schema = OrderedDict(schema)
    tm.plan = None
    tm.deps = []
    return tm


class TestPhysicalSchema(unittest.TestCase):
    def test_duckdb_green(self):
        tms = {
            "m": make_tm("m", {
                "x": Col(name="x", t=StrataType("int64", False)),
                "s": Col(name="s", t=StrataType("string", True)),
                "d": Col(name="d", t=StrataType("date", False)),
                "j": Col(name="j", t=StrataType("json", True)),
                "arr": Col(name="arr", t=StrataType("array", True, elem=StrataType("int64"))),
                "dec": Col(name="dec", t=StrataType("decimal", False, precision=10, scale=2)),
                "mon": Col(name="mon", t=money(True)),
            })
        }
        bad = check_physical_schema("duckdb", tms)
        self.assertEqual(bad, {})

    def test_postgres_green(self):
        tms = {
            "m": make_tm("m", {
                "x": Col(name="x", t=StrataType("int64", False)),
                "t": Col(name="t", t=StrataType("timestamp", False)),
                "u": Col(name="u", t=StrataType("uuid", True)),
                "arr": Col(name="arr", t=StrataType("array", True, elem=StrataType("string"))),
            })
        }
        bad = check_physical_schema("postgres", tms)
        self.assertEqual(bad, {})

    def test_bigquery_green(self):
        tms = {
            "m": make_tm("m", {
                "x": Col(name="x", t=StrataType("int64", False)),
                "f": Col(name="f", t=StrataType("float64", True)),
                "b": Col(name="b", t=StrataType("bool", False)),
            })
        }
        bad = check_physical_schema("bigquery", tms)
        self.assertEqual(bad, {})

    def test_snowflake_green(self):
        tms = {
            "m": make_tm("m", {
                "x": Col(name="x", t=StrataType("int64", False)),
                "ts": Col(name="ts", t=StrataType("timestamp", False)),
                "v": Col(name="v", t=StrataType("json", True)),
            })
        }
        bad = check_physical_schema("snowflake", tms)
        self.assertEqual(bad, {})

    def test_unknown_type_fails(self):
        tms = {
            "m": make_tm("m", {
                "bad": Col(name="bad", t=StrataType("unknown_type", False)),
            })
        }
        bad = check_physical_schema("duckdb", tms)
        self.assertIn("m", bad)
        self.assertTrue(any("unknown_type" in i for i in bad["m"]))

    def test_array_of_unknown_fails(self):
        tms = {
            "m": make_tm("m", {
                "arr": Col(name="arr", t=StrataType("array", True, elem=StrataType("unknown_type"))),
            })
        }
        bad = check_physical_schema("duckdb", tms)
        self.assertIn("m", bad)

    def test_decimal_and_money_expressible(self):
        """decimal and money are handled by decimal_sql/money, not type_map."""
        tms = {
            "m": make_tm("m", {
                "dec": Col(name="dec", t=StrataType("decimal", False, precision=5, scale=2)),
                "mon": Col(name="mon", t=money(cur="EUR")),
            })
        }
        bad = check_physical_schema("postgres", tms)
        self.assertEqual(bad, {})

    def test_every_declared_dialect_has_types(self):
        """All four dialects cover int64/float64/string/bool/date/timestamp/uuid/json."""
        for dialect in ("duckdb", "postgres", "bigquery", "snowflake"):
            tms = {
                "m": make_tm("m", {
                    c: Col(name=c, t=StrataType(t, False))
                    for c, t in [("x", "int64"), ("f", "float64"),
                                  ("s", "string"), ("b", "bool"),
                                  ("d", "date"), ("ts", "timestamp"),
                                  ("u", "uuid"), ("j", "json")]
                })
            }
            bad = check_physical_schema(dialect, tms)
            self.assertEqual(bad, {}, f"{dialect} missing a type: {bad}")


if __name__ == '__main__':
    unittest.main()
