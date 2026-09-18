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
                 ('json_get(doc, key)', 'json', True),
                 ('json_value(doc, key)', 'string', True),
                 ('json_path(doc, "$.a.b")', 'json', True),
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
            'json_value(doc, "$.x")': 'E074',
            'json_path(doc, "a.b")': 'E074', 'json_path(doc, key)': 'E074',
            'json_path(doc, "$..b")': 'E074',
            'json_path(doc, "$[*] ? (@ > 1)")': 'E074',
            "json_path(doc, \"$['a.b']\")": 'E074',
            'json_path(doc, "$[*]")': 'E074',
            'json_get(doc, "x.y")': 'E074', 'json_get(doc, "")': 'E074',
            'json_value(doc, null)': 'E074', 'array_get(*)': 'E064',
            'array_length(xs) over ()': 'E065',
        }
        for expr, code in cases.items():
            with self.subTest(expr=expr), self.assertRaises(analysis.StrataError) as cm:
                model(expr)
            self.assertEqual(cm.exception.code, code)

    def test_unsupported_array_element_is_diagnostic_not_crash(self):
        from strata.parser import ParseError
        # Nested and parameterized elements are supported types now.
        for elem, typ in [('array(int64)', 'array<array<int64>>'),
                          ('decimal(10, 2)', 'array<decimal(10,2)>'),
                          ('money', 'array<money(USD)>'),
                          ('money(USD)', 'array<money(USD)>')]:
            with self.subTest(elem=elem):
                col = model('xs', SRC.replace('array(int64)', f'array({elem})')).schema['x']
                self.assertEqual(str(col.t), typ)
        # Bare parameterizable spellings are not types: loud at parse time.
        for elem in ('array', 'decimal'):
            with self.subTest(elem=elem):
                with self.assertRaises(ParseError):
                    model('xs', SRC.replace('array(int64)', f'array({elem})'))

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

    def test_dynamic_key_emission_and_fail_loud(self):
        # A runtime key is an exact-key lookup, so it is emitted only where the
        # dialect can express one without turning the key into a path: DuckDB (a
        # '$'-less path is an exact key), PostgreSQL (`jsonb -> text` takes a
        # text expression) and Snowflake (GET's field_name is a key, and for
        # VARIANT it accepts a VARCHAR expression). BigQuery requires a literal
        # or a query parameter, so it fails loud instead of emitting SQL the
        # engine rejects or that reads a different member.
        cases = [('json_get(doc, key)',
                  {DUCKDB: "JSON_EXTRACT(doc, NULLIF(key, ''))",
                   POSTGRES: "(doc -> NULLIF(key, ''))",
                   SNOWFLAKE: "GET(doc, NULLIF(key, ''))"}),
                 ('json_value(doc, key)',
                  {DUCKDB: "JSON_EXTRACT_STRING(doc, NULLIF(key, ''))",
                   POSTGRES: "JSONB_TYPEOF((doc -> NULLIF(key, '')))",
                   SNOWFLAKE: "CAST(GET(doc, NULLIF(key, '')) AS VARCHAR)"})]
        for expr, markers in cases:
            for dialect, marker in markers.items():
                with self.subTest(expr=expr, dialect=dialect.name):
                    self.assertIn(marker, sqlgen.model_sql(model(expr), dialect))
        for expr in ('json_get(doc, key)', 'json_value(doc, key)'):
            with self.subTest(expr=expr), self.assertRaises(RuntimeError):
                sqlgen.model_sql(model(expr), BIGQUERY)
        # A literal key keeps the portable fast path and is still emitted
        # everywhere, dynamic support or not.
        for dialect in (DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE):
            with self.subTest(dialect=dialect.name):
                self.assertIn('key', sqlgen.model_sql(model('json_get(doc, "key")'), dialect))

    def test_json_path_emission_quotes_the_path(self):
        # Every dialect takes the path as a quoted string/jsonpath literal;
        # emitting it raw is invalid SQL in all four (fixed after the first
        # json_path revision shipped `JSON_EXTRACT(doc, $.a.b)`). Snowflake's own
        # path notation has no '$' root, so it gets the path without it.
        markers = [DUCKDB, POSTGRES, BIGQUERY, SNOWFLAKE]
        expected = ["JSON_EXTRACT(doc, '$.a.b')",
                    "jsonb_path_query_first(doc::jsonb, '$.a.b', '{}'::jsonb, TRUE)",
                    "JSON_QUERY(doc, '$.a.b')",
                    "GET_PATH(doc, 'a.b')"]
        for dialect, marker in zip(markers, expected):
            with self.subTest(dialect=dialect.name):
                sql = sqlgen.model_sql(model('json_path(doc, "$.a.b")'), dialect)
                self.assertIn(marker, sql)

    def test_json_path_problem_is_shared_by_checker_and_codegen(self):
        for path in ('$.a.b', '$.xs[0]', '$', '$.a.b.c[3]'):
            with self.subTest(path=path):
                self.assertIsNone(functions.json_path_problem(path))
        # Rejected because they fail or diverge per warehouse: no '$' root,
        # filters, recursive descent, quoted keys and wildcards (verified
        # against real DuckDB and PostgreSQL: `$['a.b']` is a syntax error on
        # both, `$. "a.b"` is SQL/JSON only, `$[*]` is a list in DuckDB and the
        # first match in PostgreSQL).
        for path in ('a.b', '', '@.a', '$..b', '$[*] ? (@ > 1)', '$[?(@ > 1)]',
                     "$['a.b']", '$."a.b"', '$[*]', '$[1:2]', '$.a-b'):
            with self.subTest(path=path):
                self.assertIsNotNone(functions.json_path_problem(path))



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

    def test_dynamic_key_is_exact_member_lookup(self):
        # The runtime key is an exact object-member lookup: dots do not
        # traverse and brackets do not index (a '$'-less DuckDB path), and the
        # empty key is folded to NULL instead of DuckDB's whole-document
        # reading of the empty path.
        doc = {'plain': 'v', 'num': 3, 'obj': {'in': 1}, 'xs': [7, 8],
               'a.b': 11, 'a': {'b': 22}}
        cases = [('plain', 'v', '"v"'), ('num', '3', '3'), ('obj', None, '{"in":1}'),
                 ('xs', None, '[7,8]'), ('a.b', '11', '11'),
                 ('nope', None, None), ('', None, None), (None, None, None)]
        for key, scalar, as_json in cases:
            with self.subTest(key=key):
                self.con.execute('DELETE FROM s')
                self.con.execute('INSERT INTO s VALUES (?, ?, ?, ?, ?, ?)',
                                 [json.dumps(doc), None, ['hello', None],
                                  ['{"key":"nested"}', 'null'], 0, key])
                rows, typ = self.result('json_get(doc, key)')
                self.assertEqual(typ, 'JSON')
                self.assertEqual(None if rows[0][0] is None else json.loads(rows[0][0]),
                                 None if as_json is None else json.loads(as_json))
                self.assertEqual(self.result('json_value(doc, key)')[0], [(scalar,)])
        self.assertIn("NULLIF(key, '')", sqlgen.model_sql(model('json_get(doc, key)')))

    def test_json_path_member_index_missing_and_null(self):
        self.con.execute('DELETE FROM s')
        self.insert('{"a": {"b": 22}, "xs": [7, 8], "n": null}')
        self.assertEqual(json.loads(self.result('json_path(doc, "$.a.b")')[0][0][0]), 22)
        self.assertEqual(json.loads(self.result('json_path(doc, "$.xs[0]")')[0][0][0]), 7)
        self.assertEqual(self.result('json_path(doc, "$.missing")')[0], [(None,)])
        # A JSON null member comes back as JSON null, never as SQL NULL.
        self.assertEqual(self.result('json_path(doc, "$.n")')[0], [('null',)])
        for doc in (None, 'null', '42'):
            with self.subTest(doc=doc):
                self.con.execute('DELETE FROM s')
                self.insert(doc)
                self.assertEqual(self.result('json_path(doc, "$.a.b")')[0], [(None,)])
