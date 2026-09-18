"""Nested array types and domain aliases (§4): recursive Array(T) plus
transparent `domain` aliases — static contracts, portable emission, live DuckDB.

Arrays nest to any depth over scalars, decimal/money and other arrays;
construction (homogeneous), element access, length and concatenation of
identical types work over every valid element type. Element-wise operations
with per-engine equality/ordering semantics (contains, sort, append and
friends) still require simple scalar elements, and so do array_agg (which
would build ragged multidim arrays on postgres) and expand (scalar
elements only): all rejected loudly. Domains are structural aliases
resolved at project load (cycles and unknown names are E078).
"""
import unittest

from strata import analysis, fmt, sqlgen
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE
from strata.parser import parse_strata, ParseError

try:
    import duckdb
except ImportError:
    duckdb = None

SRC = '''domain user_id = int64
domain matrix = array(array(int64))
domain amount = decimal(10, 2)
domain wallet = money(USD)
source s(ns: "n", dataset: "s") {
  columns: { id: user_id, m: matrix, p: array(amount), w: wallet,
             xs: array(int64), d: array(decimal(10, 2)) }
}
'''


def check(text, source=SRC):
    return analysis.Checker(analysis.Project(parse_strata(source + text))).check_all()


class TestNestedDeclarations(unittest.TestCase):
    def test_nested_and_parameterized_schema_types(self):
        tm = check('model m { from s }')['m']
        self.assertEqual(str(tm.schema['m'].t), 'array<array<int64>>')
        self.assertEqual(str(tm.schema['p'].t), 'array<decimal(10,2)>')
        self.assertEqual(str(tm.schema['d'].t), 'array<decimal(10,2)>')
        self.assertEqual(str(tm.schema['w'].t), 'money(USD)')
        self.assertEqual(str(tm.schema['id'].t), 'int64')

    def test_deep_nesting(self):
        tm = check('model m { from s select { z = array_construct(array_construct(array_construct(1))) } }')['m']
        self.assertEqual(str(tm.schema['z'].t), 'array<array<array<int64>>>')

    def test_domain_chains_and_contracts(self):
        text = ('domain a = user_id\n'
                'contract c { id: user_id, m: matrix }\n'
                'model m -> contract c { from s select { id = id, m = m } }\n'
                'model n { from s select { a = cast(id, "user_id") } }')
        tms = check(text)
        self.assertEqual(str(tms['m'].schema['id'].t), 'int64')
        self.assertEqual(str(tms['n'].schema['a'].t), 'int64')

    def test_unknown_domain_cycle_and_garbage(self):
        for text in [
            'source b(ns: "n", dataset: "b") { columns: { x: nosuch } }\nmodel m { from b }',
            'domain a = b\ndomain b = a',
            'domain a = a',
            'domain a = nosuch',
        ]:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, 'E078')
        with self.assertRaises(ParseError):
            parse_strata('source b(ns: "n", dataset: "b") { columns: { x: array(array) } }')
        with self.assertRaises(ParseError):
            parse_strata('source b(ns: "n", dataset: "b") { columns: { x: array(decimal) } }')

    def test_roundtrip_and_fingerprint(self):
        text = ('domain user_id = int64\n'
                'model m { from s select { z = array_construct(m) } }')
        formatted = fmt.format_module(parse_strata(SRC + text))
        self.assertIn('domain user_id = int64', formatted)
        self.assertIn('array(array(int64))', formatted)
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)
        original = check(text)['m']
        again = check(formatted)['m']
        self.assertEqual(again.fingerprint, original.fingerprint)
        self.assertEqual(again.schema, original.schema)


class TestNestedFunctions(unittest.TestCase):
    def test_construct_get_length_concat(self):
        cases = [
            ('array_construct(m)', 'array<array<array<int64>>>', False),
            ('array_construct(array_construct(1, 2), array_construct(3))',
             'array<array<int64>>', False),
            ('array_get(m, 0)', 'array<int64>', True),
            ('array_length(m)', 'int64', True),
            ('array_concat(m, m)', 'array<array<int64>>', True),
            ('array_concat(d, d)', 'array<decimal(10,2)>', True),
        ]
        for expr, typ, nullable in cases:
            with self.subTest(expr=expr):
                col = check(f'model m {{ from s select {{ x = {expr} }} }}')['m'].schema['x']
                self.assertEqual((str(col.t), col.nullable), (typ, nullable))

    def test_scalar_only_operations_reject_nested(self):
        cases = [
            'array_contains(m, array_construct(1))',
            'array_sort(m)',
            'array_append(m, array_construct(1))',
            'array_prepend(array_construct(1), m)',
            'array_remove(m, array_construct(1))',
            'array_index_of(m, array_construct(1))',
            'array_contains(d, 1.5)',
        ]
        for expr in cases:
            with self.subTest(expr=expr):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(f'model m {{ from s select {{ x = {expr} }} }}')
                self.assertEqual(cm.exception.code, 'E063')

    def test_dialect_emission(self):
        tm = check('model m { from s select { z = array_construct(m) } }')['m']
        markers = {DUCKDB: 'CAST(m AS BIGINT[][])',
                   POSTGRES: 'CAST(m AS BIGINT[][])',
                   BIGQUERY: 'CAST(m AS ARRAY<ARRAY<INT64>>)',
                   SNOWFLAKE: 'ARRAY_CONSTRUCT'}
        for dialect, marker in markers.items():
            with self.subTest(dialect=dialect.name):
                self.assertIn(marker, sqlgen.model_sql(tm, dialect))


@unittest.skipIf(duckdb is None, 'DuckDB not installed')
class TestNestedExecution(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE s(id BIGINT, m BIGINT[][], p DECIMAL(10,2)[][] '
                         ', w DECIMAL(38,2), xs BIGINT[], d DECIMAL(10,2)[])')
        self.con.execute("INSERT INTO s VALUES (7, [[1,2],[3]], [[1.5,NULL]], 9.99, [1], [2.5]),"
                         "(8, NULL, [], NULL, [], NULL),"
                         "(9, [], [[]], 0.5, NULL, [])")

    def rows(self, text):
        tm = check(text)['m']
        return self.con.execute(sqlgen.model_sql(tm)).fetchall()

    def test_nested_passthrough_construct_get_length(self):
        self.assertEqual(self.rows('model m { from s select { z = m } }'),
                         [([[1, 2], [3]],), (None,), ([],)])
        self.assertEqual(self.rows('model m { from s select { z = array_construct(m) } }'),
                         [([[[1, 2], [3]]],), ([None],), ([[]],)])
        self.assertEqual(self.rows('model m { from s select { z = array_get(m, 0) } }'),
                         [([1, 2],), (None,), (None,)])
        self.assertEqual(self.rows('model m { from s select { z = array_length(m) } }'),
                         [(2,), (None,), (0,)])

    def test_parameterized_and_domain_columns(self):
        self.assertEqual(self.rows('model m { from s select { z = p } }'),
                         [([[1.5, None]],), ([],), ([[]],)])
        self.assertEqual(self.rows('model m { from s select { z = array_length(p) } }'),
                         [(1,), (0,), (1,)])
        self.assertEqual(self.rows('model m { from s select { z = id + 1 } }'),
                         [(8,), (9,), (10,)])

    def test_nested_union_and_dedup(self):
        text = ('model a { from s select { z = m } }\n'
                'model b { from s select { z = m } filter id > 7 }\n'
                'model m { from a union b }')
        tms = check(text)
        for v in ('a', 'b'):
            self.con.execute(f'CREATE VIEW v_{v} AS ' + sqlgen.model_sql(tms[v]))
        rows = self.con.execute(sqlgen.model_sql(tms['m'])).fetchall()
        self.assertEqual(sorted((str(r) for r in rows)),
                         sorted((str(r) for r in [([[1, 2], [3]],), (None,), ([],)])))
        self.assertEqual(len(self.rows('model m { from s select { z = m } dedup }')), 3)


if __name__ == '__main__':
    unittest.main()