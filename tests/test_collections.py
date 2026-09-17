"""Collection functions: static contracts, portable emission and live DuckDB."""
import json
import unittest

from strata import analysis, fmt, functions, sqlgen
from strata.dialects import DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE
from strata.parser import parse_strata

try:
    import duckdb
except ImportError:
    duckdb = None

SRC = '''source s(ns: "n", dataset: "s") {
  columns: { doc: json, xs: array(int64), ys: array(string) nonnull,
             js: array(json), n: int64, key: string }
}
'''


def model(expr, source=SRC):
    module = parse_strata(source + 'model m { from s select { x = ' + expr + ' } }')
    return analysis.Checker(analysis.Project(module)).check_all()['m']


class TestCollections(unittest.TestCase):
    def test_types_and_nullability(self):
        cases = [('xs', 'array<int64>', True),
                 ('json_get(doc, "key")', 'json', True),
                 ('json_value(doc, "key")', 'string', True),
                 ('array_length(xs)', 'int64', True),
                 ('array_length(ys)', 'int64', False),
                 ('array_get(xs, n)', 'int64', True),
                 ('array_get(ys, 0)', 'string', True),
                 ('array_get(js, 0)', 'json', True),
                 ('array_get(xs, null)', 'int64', True),
                 ('json_value(array_get(js, 0), "key")', 'string', True)]
        for expr, typ, nullable in cases:
            with self.subTest(expr=expr):
                col = model(expr).schema['x']
                self.assertEqual((str(col.t), col.nullable), (typ, nullable))

    def test_invalid_calls_fail_before_sql(self):
        cases = {
            'json_get()': 'E062', 'json_value(doc)': 'E062',
            'array_length()': 'E062', 'array_length(xs, n)': 'E062',
            'array_get(xs)': 'E062', 'array_get(xs, 0, 1)': 'E062',
            'array_get(xs, 1.5)': 'E063', 'array_get(xs, true)': 'E063',
            'array_length(doc)': 'E063', 'json_value(key, "x")': 'E063',
            'json_get(doc, 1)': 'E063', 'array_get(null, 0)': 'E063',
            'json_get(null, "x")': 'E063', 'array_length(null)': 'E063',
            'json_get(doc, key)': 'E074', 'json_value(doc, "$.x")': 'E074',
            'json_get(doc, "x.y")': 'E074', 'json_get(doc, "")': 'E074',
            'json_value(doc, null)': 'E074', 'array_get(*)': 'E064',
            'array_length(xs) over ()': 'E065',
        }
        for expr, code in cases.items():
            with self.subTest(expr=expr), self.assertRaises(analysis.StrataError) as cm:
                model(expr)
            self.assertEqual(cm.exception.code, code)

    def test_unsupported_array_element_is_diagnostic_not_crash(self):
        for elem in ('array', 'decimal', 'money'):
            with self.subTest(elem=elem), self.assertRaises(analysis.StrataError) as cm:
                model('xs', SRC.replace('array(int64)', f'array({elem})'))
            self.assertEqual(cm.exception.code, 'E063')

    def test_roundtrip_contracts_and_lineage(self):
        text = SRC + '''
contract c { size: int64 nonnull, item: string, value: json }
model m -> contract c { from s
  select { size = array_length(ys), item = array_get(ys, n),
           value = json_get(doc, "key") }
}
'''
        original = analysis.Checker(analysis.Project(parse_strata(text))).check_all()['m']
        formatted = fmt.format_module(parse_strata(text))
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)
        tm = analysis.Checker(analysis.Project(parse_strata(formatted))).check_all()['m']
        self.assertEqual(tm.fingerprint, original.fingerprint)
        self.assertEqual(tm.schema, original.schema)
        self.assertEqual(tm.lineage, original.lineage)
        self.assertIn(('s', 'n'), tm.reads)
        self.assertEqual({(o.node, o.col) for o in tm.lineage['item']}, {('s', 'ys'), ('s', 'n')})
        with self.assertRaises(analysis.StrataError) as cm:
            analysis.Checker(analysis.Project(parse_strata(text.replace('item: string', 'item: string nonnull')))).check_all()
        self.assertEqual(cm.exception.code, 'E012')

    def test_array_contract_roundtrip(self):
        text = SRC + 'contract c { x: array(int64) } model m -> contract c { from s select { x = xs } }'
        formatted = fmt.format_module(parse_strata(text))
        tm = analysis.Checker(analysis.Project(parse_strata(formatted))).check_all()['m']
        self.assertEqual(str(tm.schema['x'].t), 'array<int64>')
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)

    def test_dialect_emission(self):
        cases = [
            ('json_get(doc, "key")', ['JSON_EXTRACT', "-> 'key'", 'JSON_QUERY', 'GET(doc']),
            ('json_value(doc, "key")', ['JSON_EXTRACT_STRING', 'JSONB_TYPEOF', 'JSON_VALUE', 'TYPEOF']),
            ('array_length(xs)', ['ARRAY_LENGTH', 'CARDINALITY', 'ARRAY_LENGTH', 'ARRAY_SIZE']),
            ('array_get(xs, n)', ['LIST_EXTRACT', 'ARRAY_LOWER', 'SAFE_OFFSET', 'CAST(GET(xs, n) AS BIGINT)']),
        ]
        for expr, markers in cases:
            for dialect, marker in zip((DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE), markers):
                with self.subTest(expr=expr, dialect=dialect.name):
                    sql = sqlgen.model_sql(model(expr), dialect)
                    self.assertIn(marker, sql)
                    if expr.startswith('array_get'):
                        self.assertIn('CASE WHEN (n) >= 0 AND (n) <', sql)
        for name in ('json_get', 'json_value', 'array_length', 'array_get'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                functions.emit_sql(name, 'x', DUCKDB)
        tm = model('array_length(xs)')
        tm.plan.collection_arg_types.clear()
        with self.assertRaises(RuntimeError):
            sqlgen.model_sql(tm)



@unittest.skipIf(duckdb is None, 'DuckDB not installed')
class TestCollectionsExecution(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE s(doc JSON, xs BIGINT[], ys VARCHAR[], js JSON[], n BIGINT, key VARCHAR)')

    def insert(self, doc, xs=None, n=0):
        self.con.execute('INSERT INTO s VALUES (?, ?, ?, ?, ?, ?)',
                         [doc, xs, ['hello', None], ['{"key":"nested"}', 'null'], n, 'key'])

    def result(self, expr):
        cursor = self.con.execute(sqlgen.model_sql(model(expr)))
        return cursor.fetchall(), str(cursor.description[0][1])

    def test_json_scalar_container_missing_and_null(self):
        for value in ['hello "world"', '', True, False, 42, 1.5, None, [], {}, {'child': 3}]:
            with self.subTest(value=value):
                self.con.execute('DELETE FROM s')
                self.insert(json.dumps({'key': value}))
                rows, typ = self.result('json_get(doc, "key")')
                self.assertEqual(typ, 'JSON')
                self.assertEqual(json.loads(rows[0][0]), value)
                rows, typ = self.result('json_value(doc, "key")')
                expected = (None if value is None or isinstance(value, (list, dict)) else
                            value if isinstance(value, str) else json.dumps(value))
                self.assertEqual((rows, typ), ([(expected,)], 'VARCHAR'))
        for doc in [None, 'null', '{}', '[]', '42', '"text"']:
            with self.subTest(doc=doc):
                self.con.execute('DELETE FROM s')
                self.insert(doc)
                self.assertEqual(self.result('json_get(doc, "key")')[0], [(None,)])
                self.assertEqual(self.result('json_value(doc, "key")')[0], [(None,)])

    def test_arrays_empty_null_index_and_physical_types(self):
        for xs, n, expected in [([10, None, 30], 0, 10), ([10, None, 30], 1, None),
                                ([10, None, 30], 2, 30), ([10], -1, None),
                                ([10], 1, None), ([10], None, None),
                                ([10], 9223372036854775807, None),
                                ([10], -9223372036854775808, None),
                                ([], 0, None), (None, 0, None)]:
            with self.subTest(xs=xs, n=n):
                self.con.execute('DELETE FROM s')
                self.insert('{}', xs, n)
                self.assertEqual(self.result('array_get(xs, n)'), ([(expected,)], 'BIGINT'))
                self.assertEqual(self.result('array_length(xs)'),
                                 ([(None if xs is None else len(xs),)], 'BIGINT'))
        self.assertEqual(self.result('array_get(xs, null)')[0], [(None,)])
        self.assertEqual(self.result('array_get(ys, 0)'), ([('hello',)], 'VARCHAR'))
        self.assertEqual(self.result('array_get(ys, 1)')[0], [(None,)])
        self.assertEqual(self.result('array_get(js, 1)'), ([('null',)], 'JSON'))
        self.assertEqual(self.result('json_value(array_get(js, 0), "key")')[0], [('nested',)])

    def test_filter_and_nested_access(self):
        self.insert('{"outer":{"key":"yes"}}', [10], 0)
        self.assertEqual(self.result('json_value(json_get(doc, "outer"), "key")')[0], [('yes',)])
        text = SRC + '''model m { from s
          filter array_get(xs, n) == 10
          select { x = json_value(json_get(doc, "outer"), "key") }
        }'''
        tm = analysis.Checker(analysis.Project(parse_strata(text))).check_all()['m']
        self.assertEqual(self.con.execute(sqlgen.model_sql(tm)).fetchall(), [('yes',)])
        self.assertIn('SAFE_OFFSET', sqlgen.model_sql(tm, BIGQUERY))
