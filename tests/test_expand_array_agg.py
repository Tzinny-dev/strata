"""Delivery 2 of JSON/arrays: the array_agg aggregate and the expand-to-rows
statement — static contracts, portable emission and live DuckDB.

array_agg collects the group's non-NULL values into one typed array (DuckDB and
Postgres FILTER, BigQuery IGNORE NULLS, Snowflake drops NULL elements already);
an empty or all-NULL group yields NULL so nullability agrees across warehouses.
expand runs in the base subquery as a lateral unnest per dialect and turns a
typed array column's elements into rows; `expand xs` shadows the array with its
element column, `expand xs as e` keeps both.
"""
import unittest

from strata import analysis, fmt, sqlgen
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE
from strata.parser import parse_strata

try:
    import duckdb
except ImportError:
    duckdb = None

SRC = '''source s(ns: "n", dataset: "s") {
  columns: { k: string, xs: array(int64), ys: array(string) nonnull,
             js: array(json), n: int64 }
}
'''


def check(text):
    return analysis.Checker(analysis.Project(parse_strata(SRC + text))).check_all()['m']


class TestArrayAgg(unittest.TestCase):
    def test_type_nullability_and_placement(self):
        for expr, typ in [('array_agg(n)', 'array<int64>'),
                          ('array_agg(k)', 'array<string>'),
                          ('array_agg(concat(k, k))', 'array<string>')]:
            with self.subTest(expr=expr):
                tm = check(f'model m {{ from s group {{ k }} ( aggregate {{ a = {expr} }} ) }}')
                self.assertEqual(str(tm.schema['a'].t), typ)
                self.assertTrue(tm.schema['a'].nullable)
        self.assertIn('array_agg', analysis.functions.AGGREGATES)

    def test_invalid_uses_fail_before_sql(self):
        cases = [
            ('model m { from s select { a = array_agg(xs) } }', 'E056'),
            ('model m { from s group { k } ( aggregate { a = array_agg(xs) } ) }', 'E063'),
            ('model m { from s group { k } ( aggregate { a = array_agg(null) } ) }', 'E063'),
            ('model m { from s group { k } ( aggregate { a = array_agg() } ) }', 'E062'),
            ('model m { from s group { k } ( aggregate { a = array_agg(n, k) } ) }', 'E062'),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, code)

    def test_dialect_emission(self):
        tm = check('model m { from s group { k } ( aggregate { a = array_agg(n) } ) }')
        markers = {DUCKDB: 'ARRAY_AGG(n) FILTER (WHERE n IS NOT NULL)',
                   POSTGRES: 'ARRAY_AGG(n) FILTER (WHERE n IS NOT NULL)',
                   BIGQUERY: 'ARRAY_AGG(n IGNORE NULLS)',
                   SNOWFLAKE: 'ARRAY_AGG(n)'}
        for dialect, marker in markers.items():
            with self.subTest(dialect=dialect.name):
                self.assertIn(marker, sqlgen.model_sql(tm, dialect))


class TestExpandStatement(unittest.TestCase):
    def test_shadow_and_as_forms(self):
        tm = check('model m { from s expand xs }')
        self.assertEqual(str(tm.schema['xs'].t), 'int64')
        self.assertTrue(tm.schema['xs'].nullable)
        self.assertEqual({(o.node, o.col, o.kind) for o in tm.lineage['xs']},
                         {('s', 'xs', 'expanded')})
        tm = check('model m { from s expand xs as e }')
        self.assertEqual(str(tm.schema['e'].t), 'int64')
        self.assertEqual(str(tm.schema['xs'].t), 'array<int64>')
        self.assertEqual(tm.plan.expand, ('xs', 'e', 'int64'))

    def test_expand_json_element(self):
        tm = check('model m { from s expand js as j }')
        self.assertEqual(str(tm.schema['j'].t), 'json')
        self.assertTrue(tm.schema['j'].nullable)

    def test_invalid_uses_fail_before_sql(self):
        cases = [
            ('model m { from s expand k }', 'E075'),
            ('model m { from s expand xs expand ys }', 'E075'),
            ('model m { from s expand nope }', 'E075'),
            ('model m { from s expand xs as k }', 'E075'),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, code)

    def test_roundtrip_and_fingerprint(self):
        for text in ['model m { from s expand xs }',
                     'model m { from s expand xs as e }']:
            with self.subTest(text=text):
                formatted = fmt.format_module(parse_strata(SRC + text))
                self.assertIn('expand xs', formatted)
                original = check(text)
                again = check(fmt.format_module(parse_strata(SRC + text)))
                self.assertEqual(again.fingerprint, original.fingerprint)
                self.assertEqual(again.schema, original.schema)

    def test_dialect_emission(self):
        tm = check('model m { from s expand xs as e }')
        markers = {DUCKDB: 'CROSS JOIN LATERAL UNNEST(t0.xs) AS u0(e)',
                   POSTGRES: 'CROSS JOIN LATERAL UNNEST(t0.xs) AS u0(e)',
                   BIGQUERY: 'CROSS JOIN UNNEST(t0.xs) AS e',
                   SNOWFLAKE: 'CROSS JOIN LATERAL FLATTEN(input => t0.xs) AS u0'}
        for dialect, marker in markers.items():
            with self.subTest(dialect=dialect.name):
                self.assertIn(marker, sqlgen.model_sql(tm, dialect))
        sf = sqlgen.model_sql(tm, SNOWFLAKE)
        self.assertIn('CAST(u0.VALUE AS BIGINT)', sf)
        sf_json = sqlgen.model_sql(check('model m { from s expand js as j }'), SNOWFLAKE)
        self.assertIn('u0.VALUE AS j', sf_json)
        self.assertNotIn('CAST(u0.VALUE', sf_json)
        shadow = sqlgen.model_sql(check('model m { from s expand xs }'), DUCKDB)
        self.assertIn('AS u0(e)', shadow)
        self.assertNotIn('t0.xs AS xs', shadow)


@unittest.skipIf(duckdb is None, 'DuckDB not installed')
class TestExpandAggExecution(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE s(k VARCHAR, xs BIGINT[], ys VARCHAR[], '
                         'js JSON[], n BIGINT)')

    def insert(self, k, xs, n):
        self.con.execute(
            "INSERT INTO s VALUES (?, ?, ?, ['{\"x\":1}'::JSON, 'null'::JSON], ?)",
            [k, xs, ['a', 'b'], n])

    def sql(self, tm, dialect=DUCKDB):
        return sqlgen.model_sql(tm, dialect)

    def test_array_agg_collects_non_null_values(self):
        self.insert('a', None, 5)
        self.insert('a', None, None)
        self.insert('b', None, 7)
        self.insert('c', None, None)
        tm = check('model m { from s group { k } ( aggregate { arr = array_agg(n) } ) }')
        rows = self.con.execute(self.sql(tm)).fetchall()
        self.assertEqual(dict(rows), {'a': [5], 'b': [7], 'c': None})

    def test_expand_rows_null_empty_and_shadow(self):
        self.insert('a', [1, 2, None], 0)
        self.insert('b', None, 0)
        self.insert('c', [], 0)
        tm = check('model m { from s expand xs select { e = xs } }')
        self.assertEqual(self.con.execute(self.sql(tm)).fetchall(),
                         [(1,), (2,), (None,)])
        tm = check('model m { from s expand xs as e }')
        schema = self.con.execute(self.sql(tm)).description
        self.assertEqual([d[0] for d in schema],
                         ['k', 'xs', 'ys', 'js', 'n', 'e'])
        self.assertEqual(self.con.execute(self.sql(tm)).fetchall(),
                         [(k, xs, ['a', 'b'], ['{"x":1}', 'null'], n, e)
                          for k, xs, n, e in [('a', [1, 2, None], 0, 1),
                                              ('a', [1, 2, None], 0, 2),
                                              ('a', [1, 2, None], 0, None)]])

    def test_expand_then_group(self):
        self.insert('a', [1, 2, 2], 0)
        self.insert('b', [5], 0)
        tm = check('model m { from s expand xs group { k } '
                   '( aggregate { c = count(), s = sum(xs) } ) }')
        rows = self.con.execute(self.sql(tm)).fetchall()
        self.assertEqual({r[0]: (r[1], r[2]) for r in rows},
                         {'a': (3, 5), 'b': (1, 5)})

    def test_expand_after_group_still_sums(self):
        self.insert('a', [10, 20], 0)
        tm = check('model m { from s expand xs select { e = xs * 2 } }')
        self.assertEqual(self.con.execute(self.sql(tm)).fetchall(), [(20,), (40,)])


if __name__ == '__main__':
    unittest.main()