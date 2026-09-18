"""Set operations and deduplication (§4): union/intersect/except over same-shaped
models plus full-row dedup — static contracts, portable emission, live DuckDB.

A set model combines its current rows (filters and lets before the set-op
shape the left branch) with one upstream model; everything after the set-op
sees the combined rows. Both branches spell the same column aliases in the
same order, so DuckDB's by-name matching and every other warehouse's
by-position matching agree. `union` is DISTINCT, `union all` keeps duplicates;
intersect/except are always DISTINCT (the ALL variants are not portable).
`dedup` is SELECT DISTINCT over the final row set, full rows only.
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
  columns: { x: int64, y: string, f: float64, j: json, xs: array(int64) }
}
source t(ns: "n", dataset: "t") {
  columns: { x: int64, y: string, f: float64, j: json, xs: array(int64) }
}
source w(ns: "n", dataset: "w") {
  columns: { y: string, x: int64, f: float64, j: json, xs: array(int64) }
}
model a { from s }
model b { from t }
model r { from w }
model af { from s select { x = f, y = y, f = f, j = j, xs = xs } }
'''


def check(text):
    return analysis.Checker(analysis.Project(parse_strata(SRC + text))).check_all()


class TestSetOpSchemas(unittest.TestCase):
    def test_union_schema_unifies_and_relaxes_nullability(self):
        tm = check('model m { from a union b }')['m']
        self.assertEqual([str(c.t) for c in tm.schema.values()],
                         ['int64', 'string', 'float64', 'json', 'array<int64>'])
        self.assertEqual(tm.plan.set_op, ('union', False, 'b'))
        kinds = {(o.node, o.col, o.kind) for o in tm.lineage['x']}
        self.assertIn(('b', 'x', 'set'), kinds)
        self.assertIn(('a', 'x'), tm.reads)
        self.assertIn(('b', 'x'), tm.reads)

    def test_widening_unifies_with_casts(self):
        tm = check('model m { from a union af }')['m']
        self.assertEqual(str(tm.schema['x'].t), 'float64')
        sql = sqlgen.model_sql(tm)
        self.assertIn('CAST(b_left.x AS DOUBLE)', sql)
        self.assertIn('SELECT v_af.x, v_af.y', sql)

    def test_shape_mismatches_fail_before_sql(self):
        cases = [
            ('model m { from a union r }', 'E077'),
            ('model m { from a union t }', 'E076'),
            ('model m { from a union b union b }', 'E076'),
            ('model m { from a intersect a except a }', 'E076'),
            ('model m { from a select { x = x } union b }', 'E076'),
            ('model m { from a sort { x } union b }', 'E076'),
            ('model m { from a union m }', 'F001'),
            ('model m { from a union nosuch }', 'E020'),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, code)

    def test_incompatible_column_types_fail(self):
        text = ('model si { from s select { x = y, y = y, f = f, j = j, xs = xs } }\n'
                'model m { from a union si }')
        with self.assertRaises(analysis.StrataError) as cm:
            check(text)
        self.assertEqual(cm.exception.code, 'E077')

    def test_roundtrip_and_fingerprint(self):
        for text in ['model m { from a union b }',
                     'model m { from a union all b filter x > 1 }',
                     'model m { from a intersect b }',
                     'model m { from a except b dedup }']:
            with self.subTest(text=text):
                formatted = fmt.format_module(parse_strata(SRC + text))
                self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)
                original = check(text)['m']
                again = check(formatted)['m']
                self.assertEqual(again.fingerprint, original.fingerprint)
                self.assertEqual(again.schema, original.schema)

    def test_dialect_emission(self):
        ops = [('model m { from a union b }', 'UNION', 'UNION'),
               ('model m { from a union all b }', 'UNION ALL', 'UNION ALL'),
               ('model m { from a intersect b }', 'INTERSECT', 'INTERSECT'),
               ('model m { from a except b }', 'EXCEPT', 'EXCEPT')]
        for text, marker, _ in ops:
            for dialect in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
                with self.subTest(text=text, dialect=dialect.name):
                    sql = sqlgen.model_sql(check(text)['m'], dialect)
                    self.assertIn(marker, sql)
                    self.assertIn('b_left', sql)
        tm = check('model m { from a union b filter x > 1 }')['m']
        self.assertIn(') t0', sqlgen.model_sql(tm))
        bq = sqlgen.model_sql(check('model m { from a union all b }')['m'], BIGQUERY)
        self.assertIn('UNION ALL', bq)


class TestDedup(unittest.TestCase):
    def test_dedup_flag_and_emission(self):
        tm = check('model m { from a dedup }')['m']
        self.assertTrue(tm.plan.distinct)
        self.assertIn('SELECT DISTINCT', sqlgen.model_sql(tm))
        plain = sqlgen.model_sql(check('model m { from a }')['m'])
        self.assertNotIn('DISTINCT', plain)

    def test_dedup_roundtrip(self):
        text = 'model m { from a dedup }'
        formatted = fmt.format_module(parse_strata(SRC + text))
        self.assertIn('dedup', formatted)
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)


@unittest.skipIf(duckdb is None, 'DuckDB not installed')
class TestSetOpExecution(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        cols = '(x BIGINT, y VARCHAR, f DOUBLE, j JSON, xs BIGINT[])'
        self.con.execute(f'CREATE TABLE s{cols}')
        self.con.execute(f'CREATE TABLE t{cols}')
        self.con.execute("INSERT INTO s VALUES (1,'p',1.5,'{\"k\":1}',[1,2]),"
                         "(2,'q',NULL,'null',NULL),(1,'p',1.5,'{\"k\":1}',[1,2])")
        self.con.execute("INSERT INTO t VALUES (2,'q',2.5,'null',NULL),(3,'r',3.5,'[1]',[])")
        tms = check('')
        for v in ('a', 'b'):
            self.con.execute(f'CREATE VIEW v_{v} AS ' + sqlgen.model_sql(tms[v]))

    def rows(self, text):
        tm = check(text)['m']
        return self.con.execute(sqlgen.model_sql(tm)).fetchall()

    @staticmethod
    def _key(row):
        # None-safe ordering for rows mixing NULLs and values
        return tuple((v is None, v) for v in row)

    def test_union_distinct_all_intersect_except(self):
        self.assertEqual(sorted(self.rows('model m { from a union b }'), key=self._key),
                         [(1, 'p', 1.5, '{"k":1}', [1, 2]),
                          (2, 'q', 2.5, 'null', None),
                          (2, 'q', None, 'null', None),
                          (3, 'r', 3.5, '[1]', [])])
        self.assertEqual(len(self.rows('model m { from a union all b }')), 5)
        self.assertEqual(self.rows('model m { from a intersect b }'), [])
        self.assertEqual(sorted(self.rows('model m { from a except b }'), key=self._key),
                         [(1, 'p', 1.5, '{"k":1}', [1, 2]),
                          (2, 'q', None, 'null', None)])

    def test_pipeline_positions(self):
        # filter before the set-op shapes the left branch only
        self.assertEqual(sorted(self.rows('model m { from a filter x > 1 union b }'), key=self._key),
                         [(2, 'q', 2.5, 'null', None),
                          (2, 'q', None, 'null', None),
                          (3, 'r', 3.5, '[1]', [])])
        # filter after sees the combined rows
        self.assertEqual(sorted(self.rows('model m { from a union b filter f > 2 }'), key=self._key),
                         [(2, 'q', 2.5, 'null', None),
                          (3, 'r', 3.5, '[1]', [])])
        # union all + dedup == distinct union
        self.assertEqual(sorted(self.rows('model m { from a union all b dedup }'), key=self._key),
                         sorted(self.rows('model m { from a union b }'), key=self._key))
        # group after the union aggregates combined rows
        self.assertEqual(sorted(self.rows('model m { from a union b group { y } '
                                          '( aggregate { c = count() } ) }')),
                         [('p', 1), ('q', 2), ('r', 1)])
        # let after the set-op computes over combined rows
        self.assertEqual(sorted(self.rows('model m { from a union b '
                                          'select { z = x * 10, y = y } }')),
                         [(10, 'p'), (20, 'q'), (20, 'q'), (30, 'r')])

    def test_dedup_over_complex_types(self):
        rows = self.rows('model m { from a dedup }')
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r[0] for r in rows), [1, 2])


if __name__ == '__main__':
    unittest.main()
