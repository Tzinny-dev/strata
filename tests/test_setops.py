"""Set operations and deduplication (§4): union/intersect/except over same-shaped
models, chained set-ops, joins after the chain, deterministic keyed dedup,
DISTINCT aggregation and qualified references to the combined rows.

A set model combines its current rows (filters and lets before the first
set-op shape the left branch; joins after the chain attach to the combined
rows) with same-shaped upstream models. Set-ops are consecutive (`from a
union b union c`); everything after the last set-op sees the combined rows.
Both branches spell the same column aliases in the same order, so DuckDB's
by-name matching and every other warehouse's by-position matching agree.
`union` is DISTINCT, `union all` keeps duplicates; intersect/except are
always DISTINCT (the ALL variants are not portable). `dedup` is SELECT
DISTINCT over the final row set, full rows only; `dedup by k1, k2` keeps one
row per key deterministically (ROW_NUMBER over the key partition, ordered by
the remaining outputs). `count(distinct x)` is the only DISTINCT aggregate.
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
source cd(ns: "n", dataset: "cd") {
  columns: { x: int64, y: string }
}
model a { from s }
model b { from t }
model c { from t }
model r { from w }
model cdm { from cd }
model af { from s select { x = f, y = y, f = f, j = j, xs = xs } }
'''


def check(text):
    return analysis.Checker(analysis.Project(parse_strata(SRC + text))).check_all()


class TestSetOpSchemas(unittest.TestCase):
    def test_union_schema_unifies_and_relaxes_nullability(self):
        tm = check('model m { from a union b }')['m']
        self.assertEqual([str(c.t) for c in tm.schema.values()],
                         ['int64', 'string', 'float64', 'json', 'array<int64>'])
        self.assertEqual(tm.plan.set_ops, [('union', False, 'b')])
        kinds = {(o.node, o.col, o.kind) for o in tm.lineage['x']}
        self.assertIn(('b', 'x', 'set'), kinds)
        self.assertIn(('a', 'x'), tm.reads)
        self.assertIn(('b', 'x'), tm.reads)

    def test_chained_union_schema_and_branch_types(self):
        tm = check('model m { from a union b union c }')['m']
        self.assertEqual(tm.plan.set_ops, [('union', False, 'b'), ('union', False, 'c')])
        name, types, unified = tm.plan.setop_cols[0]
        self.assertEqual(name, 'x')
        self.assertEqual([str(t) for t in types], ['int64', 'int64', 'int64'])
        self.assertEqual(str(unified), 'int64')
        sql = sqlgen.model_sql(tm)
        self.assertEqual(sql.count('UNION'), 2)

    def test_chained_widening_casts_every_branch(self):
        tm = check('model m { from a union af union b }')['m']
        self.assertEqual(str(tm.schema['x'].t), 'float64')
        sql = sqlgen.model_sql(tm)
        # af already carries the unified type (float64); the int64 branches
        # (b_left and v_b) cast to DOUBLE
        self.assertIn('CAST(b_left.x AS DOUBLE)', sql)
        self.assertNotIn('CAST(v_af.x', sql)
        self.assertIn('CAST(v_b.x AS DOUBLE)', sql)
        self.assertEqual(sql.count('UNION'), 2)

    def test_shape_mismatches_fail_before_sql(self):
        cases = [
            ('model m { from a union r }', 'E077'),
            ('model m { from a union t }', 'E076'),
            ('model m { from a select { x = x } union b }', 'E076'),
            ('model m { from a sort { x } union b }', 'E076'),
            ('model m { from a take 1 union b }', 'E076'),
            ('model m { from a union b filter x > 1 union b }', 'E076'),
            ('model m { from a union b let z = x union c }', 'E076'),
            ('model m { from a join_left b on a.x == b.x union c }', 'E076'),
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

    def test_qualified_ref_to_right_model(self):
        tm = check('model m { from a union b select { z = b.x * 10, y = y } }')['m']
        # the qualifier collapses to the combined column (bare name, OUTER)
        self.assertIn('SELECT (x * 10) AS z, y AS y', sqlgen.model_sql(tm))

    def test_roundtrip_and_fingerprint(self):
        for text in ['model m { from a union b }',
                     'model m { from a union all b filter x > 1 }',
                     'model m { from a intersect b }',
                     'model m { from a except b dedup }',
                     'model m { from a union b union c }',
                     'model m { from a union b dedup by x, y }',
                     'model m { from a group { y } ( aggregate { n = count(distinct x) } ) }',
                     'model m { from a union b join_left c on a.x == c.x }']:
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
        chained = sqlgen.model_sql(check('model m { from a union b union c }')['m'])
        self.assertEqual(chained.count('UNION'), 2)

    def test_post_setop_join_emission(self):
        for dialect in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
            tm = check('model m { from a union b join_left c on a.x == c.x }')['m']
            sql = sqlgen.model_sql(tm, dialect)
            self.assertIn('JOIN', sql)
            self.assertIn(' t0', sql)


class TestDedup(unittest.TestCase):
    def test_dedup_flag_and_emission(self):
        tm = check('model m { from a dedup }')['m']
        self.assertTrue(tm.plan.distinct)
        self.assertIn('SELECT DISTINCT', sqlgen.model_sql(tm))
        plain = sqlgen.model_sql(check('model m { from a }')['m'])
        self.assertNotIn('DISTINCT', plain)

    def test_dedup_by_key_flag_emission_and_order(self):
        tm = check('model m { from a union all b dedup by y }')['m']
        self.assertEqual([k.name for k in tm.plan.dedup_keys], ['y'])
        # deterministic tiebreak orders by the remaining outputs
        sql = sqlgen.model_sql(tm)
        self.assertIn('ROW_NUMBER() OVER (PARTITION BY y ORDER BY x, f, j, xs) AS __rn', sql)
        self.assertIn('WHERE __rn = 1', sql)

    def test_dedup_roundtrip(self):
        for text in ['model m { from a dedup }',
                     'model m { from a dedup by x }',
                     'model m { from a union b dedup by x, y }']:
            with self.subTest(text=text):
                formatted = fmt.format_module(parse_strata(SRC + text))
                self.assertIn('dedup', formatted)
                self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)

    def test_dedup_by_errors(self):
        cases = [
            # keyed + full-row dedup conflict
            ('model m { from a dedup dedup by x }', 'E076'),
            # keys must be plain output columns
            ('model m { from a dedup by x + 1 }', 'E076'),
            ('model m { from a union b dedup by b.x }', 'E076'),
            # key must name an output column
            ('model m { from a select { z = x } dedup by y }', 'E076'),
            # sort after keyed dedup must reference outputs too
            ('model m { from a select { z = x, y = y } dedup by y sort { x } }', 'E076'),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, code)


class TestCountDistinct(unittest.TestCase):
    def test_count_distinct_emission(self):
        tm = check('model m { from a group { y } ( aggregate { n = count(distinct x) } ) }')['m']
        self.assertIn('COUNT(DISTINCT x)', sqlgen.model_sql(tm))

    def test_count_distinct_roundtrip(self):
        text = 'model m { from a group { y } ( aggregate { n = count(distinct x) } ) }'
        formatted = fmt.format_module(parse_strata(SRC + text))
        self.assertIn('count(distinct x)', formatted)
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)

    def test_distinct_restricted_to_count(self):
        cases = [
            ('model m { from a group { y } ( aggregate { n = sum(distinct x) } ) }', 'E096'),
            ('model m { from a group { y } ( aggregate { n = count(distinct *) } ) }', 'E096'),
            ('model m { from a select { n = count(distinct x) over () } }', 'E096'),
        ]
        for text, code in cases:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, code)


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
        self.con.execute('CREATE TABLE w(y VARCHAR, x BIGINT, f DOUBLE, j JSON, xs BIGINT[])')
        self.con.execute("INSERT INTO w VALUES ('r',3,3.5,'[1]',[]),('s',4,4.5,'null',NULL)")
        self.con.execute('CREATE TABLE cd(x BIGINT, y VARCHAR)')
        self.con.execute("INSERT INTO cd VALUES (1,'a'),(1,'a'),(1,'b'),(2,'a'),(2,'b'),(3,'a')")
        tms = check('')
        for v in ('a', 'b', 'c', 'cdm'):
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

    def test_chained_set_ops(self):
        # (a union b) union c — c is a second copy of t, so the result matches
        # the single union (UNION de-duplicates across all three branches).
        self.assertEqual(sorted(self.rows('model m { from a union b union c }'), key=self._key),
                         sorted(self.rows('model m { from a union b }'), key=self._key))
        # chain with an intersect tail narrows: every row must be in each
        # branch's right side, so c (a second copy of t) keeps only {2q, 3r}
        self.assertEqual(sorted(self.rows('model m { from a union b intersect c }'), key=self._key),
                         sorted(self.rows('model m { from c }'), key=self._key))

    def test_join_after_set_op(self):
        rows = self.rows('model m { from a union b join_left c on a.x == c.x }')
        # left join keeps the unmatched combined row (x=1) with NULL c columns
        self.assertEqual(sorted(r[0] for r in rows), [1, 2, 2, 3])
        matched = [r for r in rows if r[0] != 1]
        self.assertTrue(all(r[5] == r[0] for r in matched))
        orphan = [r for r in rows if r[0] == 1][0]
        self.assertTrue(all(v is None for v in orphan[5:]))

    def test_join_after_set_op_with_group(self):
        # post-set-op join feeds a group over the joined rows; the left join
        # keeps the unmatched combined row (p) with its NULL c columns
        rows = self.rows('model m { from a union b join_left c on a.x == c.x '
                         'group { y } ( aggregate { n = count() } ) }')
        self.assertEqual(sorted(rows), [('p', 1), ('q', 2), ('r', 1)])

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

    def test_qualified_ref_right_model_executes(self):
        rows = self.rows('model m { from a union b select { z = b.x * 10, y = y } }')
        self.assertEqual(sorted(rows), [(10, 'p'), (20, 'q'), (20, 'q'), (30, 'r')])

    def test_dedup_by_key_executes(self):
        # y=q group (two rows: a's f=NULL and b's f=2.5) keeps b's row: the
        # deterministic tiebreak orders by the remaining columns, NULLs last.
        rows = self.rows('model m { from a union all b dedup by y }')
        self.assertEqual(sorted(rows, key=self._key),
                         [(1, 'p', 1.5, '{"k":1}', [1, 2]),
                          (2, 'q', 2.5, 'null', None),
                          (3, 'r', 3.5, '[1]', [])])
        # dedup by key over a reduced projection
        rows = self.rows('model m { from a union all b select { x = x, y = y } dedup by x }')
        self.assertEqual(sorted(rows), [(1, 'p'), (2, 'q'), (3, 'r')])

    def test_dedup_by_key_with_sort(self):
        rows = self.rows('model m { from a union all b dedup by y sort { x desc } }')
        self.assertEqual([r[0] for r in rows], [3, 2, 1])

    def test_count_distinct_executes(self):
        rows = self.rows('model m { from cd group { y } ( aggregate { n = count(distinct x) } ) }')
        self.assertEqual(sorted(rows), [('a', 3), ('b', 2)])

    def test_dedup_over_complex_types(self):
        rows = self.rows('model m { from a dedup }')
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r[0] for r in rows), [1, 2])


if __name__ == '__main__':
    unittest.main()