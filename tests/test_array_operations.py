"""Array construction, membership and strict concatenation."""
import unittest

from strata import analysis, fmt, sqlgen
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE
from strata.parser import parse_strata
from tests import test_collections as fixtures

model = fixtures.model
SRC = fixtures.SRC


class TestArrayOperations(unittest.TestCase):
    def test_types(self):
        cases = [
            ('array_construct(null, 1, n)', 'array<int64>', False),
            ('array_construct(key, null)', 'array<string>', False),
            ('array_construct(doc)', 'array<json>', False),
            ('array_concat(xs, array_construct(1))', 'array<int64>', True),
            ('array_concat(ys, ys)', 'array<string>', False),
            ('array_contains(xs, n)', 'bool', True),
            ('array_contains(ys, "x")', 'bool', False),
            ('array_contains(ys, null)', 'bool', True),
            ('array_get(array_construct(1, 2), 0)', 'int64', True),
        ]
        for expr, typ, nullable in cases:
            with self.subTest(expr=expr):
                col = model(expr).schema['x']
                self.assertEqual((str(col.t), col.nullable), (typ, nullable))

    def test_rejections(self):
        cases = {
            'array_construct()': 'E062', 'array_construct(null)': 'E063',
            'array_construct(1, 1.0)': 'E063', 'array_construct(1, "x")': 'E063',
            'array_construct(*)': 'E064',
            'array_concat(xs)': 'E062', 'array_concat(xs, xs, xs)': 'E062',
            'array_concat(xs, ys)': 'E063', 'array_concat(xs, null)': 'E063',
            'array_concat(null, xs)': 'E063',
            'array_contains(xs)': 'E062', 'array_contains(xs, "1")': 'E063',
            'array_contains(js, doc)': 'E063', 'array_contains(null, 1)': 'E063',
            'array_contains(xs, 1.0)': 'E063',
            'array_construct(1) over ()': 'E065',
        }
        for expr, code in cases.items():
            with self.subTest(expr=expr), self.assertRaises(analysis.StrataError) as cm:
                model(expr)
            self.assertEqual(cm.exception.code, code)

    def test_roundtrip_and_lineage(self):
        text = SRC + '''contract c { x: array(int64) nonnull, found: bool }
model m -> contract c { from s
 select { x = array_construct(1, n, null), found = array_contains(xs, n) }
}'''
        a = analysis.Checker(analysis.Project(parse_strata(text))).check_all()['m']
        formatted = fmt.format_module(parse_strata(text))
        b = analysis.Checker(analysis.Project(parse_strata(formatted))).check_all()['m']
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.schema, b.schema)
        self.assertEqual(a.lineage, b.lineage)
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)
        self.assertEqual({(o.node, o.col) for o in b.lineage['found']}, {('s', 'xs'), ('s', 'n')})

    def test_dialects(self):
        for d, construct, contains, concat in [
                (DUCKDB, '[CAST(', 'LIST_CONTAINS', 'LIST_CONCAT'),
                (POSTGRES, 'ARRAY[CAST(', ' = ANY(', 'ARRAY_CAT'),
                (BIGQUERY, '[CAST(', ' IN UNNEST(', 'ARRAY_CONCAT'),
                (SNOWFLAKE, 'ARRAY_CONSTRUCT(', 'ARRAY_CONTAINS(TO_VARIANT(', 'ARRAY_CAT')]:
            with self.subTest(dialect=d.name):
                self.assertIn(construct, sqlgen.model_sql(model('array_construct(1, n, null)'), d))
                sql = sqlgen.model_sql(model('array_contains(xs, n)'), d)
                self.assertIn(contains, sql)
                self.assertIn('IS NULL OR (n) IS NULL THEN NULL', sql)
                sql = sqlgen.model_sql(model('array_concat(xs, xs)'), d)
                self.assertIn(concat, sql)
                self.assertIn('IS NULL OR (xs) IS NULL THEN NULL', sql)
