"""Date functions: parser, typing, dialect emission and live calendar semantics."""
import unittest
from datetime import date, datetime

from strata import analysis, ast, fmt, functions, sqlgen
from strata.dialects import BIGQUERY, DUCKDB, POSTGRES, SNOWFLAKE
from strata.parser import parse_strata, ParseError

try:
    import duckdb
except ImportError:
    duckdb = None

SRC = '''source s(ns: "n", dataset: "s") {
  columns: { d: date nonnull, e: date, t: timestamp nonnull,
             n: int64, day: int64 nonnull }
}
'''


def module(expr):
    return parse_strata(SRC + 'model m {\n from s\n select { x = ' + expr + ' }\n}\n')


def model(expr):
    return analysis.Checker(analysis.Project(module(expr))).check_all()['m']


class TestDateFunctions(unittest.TestCase):
    def test_kwarg_roundtrip(self):
        for expr in ('date_add(d, days: 1)', 'date_sub(d, months: -2)',
                     'date_add(d, days: n + day)', 'date_trunc(d, month)',
                     'date_diff(d, e, day)'):
            with self.subTest(expr=expr):
                text = fmt.format_module(module(expr))
                self.assertEqual(fmt.format_module(parse_strata(text)), text)
        call = module('date_add(d, days: 1)').decls[1].stmts[1].assigns[0].expr
        self.assertIsInstance(call.args[1], ast.Kwarg)
        self.assertIsNotNone(call.args[1].span)
        # The unit vocabulary does not reserve names in normal expressions.
        self.assertEqual(str(model('day').schema['x'].t), 'int64')

    def test_arity(self):
        for expr in ('date_add()', 'date_add(d)', 'date_sub(d)',
                     'date_add(d, days: 1, months: 2)', 'date_trunc(d)',
                     'date_diff(d, e)', 'date_diff(d, e, day, day)'):
            with self.subTest(expr=expr), self.assertRaises(analysis.StrataError) as cm:
                model(expr)
            self.assertEqual(cm.exception.code, 'E062')

    def test_invalid_arguments(self):
        cases = {'date_add(d, days: true)': 'E063',
                 'date_add(d, days: 1.5)': 'E063',
                 'date_add(d, 1)': 'E072',
                 'date_add(d, hours: 1)': 'E071',
                 'date_trunc(d, nonsense)': 'E071',
                 'date_diff(d, t, day)': 'E073',
                 'upper(days: 1)': 'E072'}
        for expr, code in cases.items():
            with self.subTest(expr=expr), self.assertRaises(analysis.StrataError) as cm:
                model(expr)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(ParseError):
            module('date_add(d, days: 1 months: 2)')

    def test_types_and_nullability(self):
        cases = [('date_add(d, days: 1)', 'date', False),
                 ('date_add(d, days: n)', 'date', True),
                 ('date_sub(d, months: null)', 'date', True),
                 ('date_sub(e, months: -1)', 'date', True),
                 ('date_add(t, years: 1)', 'timestamp', False),
                 ('date_trunc(t, month)', 'timestamp', False),
                 ('date_trunc(d, month)', 'date', False),
                 ('date_diff(d, e, day)', 'int64', True)]
        for expr, typ, nullable in cases:
            with self.subTest(expr=expr):
                col = model(expr).schema['x']
                self.assertEqual((str(col.t), col.nullable), (typ, nullable))

    def test_dynamic_lineage_and_window_rejection(self):
        tm = model('date_add(d, days: n)')
        self.assertIn(('s', 'n'), tm.reads)
        self.assertIn(('s', 'n'), [o.key() for o in tm.lineage['x']])
        text = SRC + 'model m { from s\n filter date_add(d, days: row_number() over ()) == d\n}'
        with self.assertRaises(analysis.StrataError) as cm:
            analysis.Checker(analysis.Project(parse_strata(text))).check_all()
        self.assertEqual(cm.exception.code, 'E065')

    @unittest.skipIf(duckdb is None, 'DuckDB not installed')
    def test_live_calendar(self):
        cases = [('date_add(d, months: 1)', date(2024, 3, 29)),
                 ('date_sub(d, months: -1)', date(2024, 3, 29)),
                 ('date_add(d, years: 1)', date(2025, 2, 28)),
                 ('date_sub(d, days: 1)', date(2024, 2, 28)),
                 ('date_add(d, days: n + 1)', date(2024, 3, 3)),
                 ('date_add(d, days: null)', None),
                 ('date_trunc(d, month)', date(2024, 2, 1)),
                 ('date_diff(d, e, month)', 13),
                 ('date_diff(e, d, month)', -13),
                 ('date_diff(d, e, quarter)', 4),
                 ('date_add(t, years: 1)', datetime(2025, 2, 28, 12, 30)),
                 ('date_trunc(t, month)', datetime(2024, 2, 1))]
        con = duckdb.connect()
        try:
            con.execute('CREATE TABLE s(d DATE, e DATE, t TIMESTAMP, n BIGINT, day BIGINT)')
            con.execute("INSERT INTO s VALUES ('2024-02-29', '2025-03-01', '2024-02-29 12:30:00', 2, 1)")
            for expr, want in cases:
                with self.subTest(expr=expr):
                    tm = model(expr)
                    cursor = con.execute(sqlgen.model_sql(tm))
                    self.assertEqual(cursor.fetchone(), (want,))
                    self.assertEqual(str(cursor.description[0][1]),
                                     {'date': 'DATE', 'timestamp': 'TIMESTAMP', 'int64': 'BIGINT'}[str(tm.schema['x'].t)])
        finally:
            con.close()


    def test_dialect_emission(self):
        expected = {
            BIGQUERY: 'DATE_ADD(d, INTERVAL (1) DAY)',
            POSTGRES: "CAST((d + (1) * INTERVAL '1 DAY') AS DATE)",
            DUCKDB: 'CAST((d + (1) * INTERVAL 1 DAY) AS DATE)',
            SNOWFLAKE: 'DATEADD(DAY, (1), d)',
        }
        tm = model('date_add(d, days: 1)')
        for dialect, fragment in expected.items():
            with self.subTest(dialect=dialect.name):
                self.assertIn(fragment, sqlgen.model_sql(tm, dialect))
                for fn in functions.FUNCTIONS:
                    if fn.unit_names is None:
                        continue
                    for unit in fn.unit_names:
                        if fn.name in ('date_add', 'date_sub'):
                            expr = f'{fn.name}(d, {unit}: n)'
                        elif fn.name == 'date_trunc':
                            expr = f'date_trunc(d, {unit})'
                        else:
                            expr = f'date_diff(d, e, {unit})'
                        self.assertIn('SELECT', sqlgen.model_sql(model(expr), dialect))
        self.assertIn('QUARTER', sqlgen.model_sql(model('date_add(d, quarters: 1)'), BIGQUERY))
        self.assertIn("INTERVAL '3 MONTH'", sqlgen.model_sql(model('date_add(d, quarters: 1)'), POSTGRES))
        self.assertIn('DATEDIFF(month, d, e)', sqlgen.model_sql(model('date_diff(d, e, month)'), SNOWFLAKE))
        self.assertIn('CAST(e AS DATE), CAST(d AS DATE), MONTH',
                      sqlgen.model_sql(model('date_diff(d, e, month)'), BIGQUERY))
        self.assertIn('WEEK(MONDAY)', sqlgen.model_sql(model('date_trunc(d, week)'), BIGQUERY))
        self.assertIn('DAYOFWEEKISO', sqlgen.model_sql(model('date_trunc(d, week)'), SNOWFLAKE))
        self.assertIn("DATETIME(t, 'UTC')", sqlgen.model_sql(model('date_add(t, years: 1)'), BIGQUERY))
        self.assertIn("TIMESTAMP_TRUNC(t, MONTH, 'UTC')", sqlgen.model_sql(model('date_trunc(t, month)'), BIGQUERY))

    def test_catalog_dialect_coverage(self):
        # Sparse overrides use catalog spelling; dates require typed emission.
        names = {fn.name for fn in functions.FUNCTIONS}
        for dialect in (BIGQUERY, DUCKDB, POSTGRES, SNOWFLAKE):
            self.assertLessEqual(set(dialect.function_map), names)
            for fn in functions.FUNCTIONS:
                with self.subTest(dialect=dialect.name, fn=fn.name):
                    unavailable = dialect.function_map.get(fn.name, '').endswith('_UNAVAILABLE')
                    if fn.unit_names is not None or fn.collection or unavailable:
                        with self.assertRaises(RuntimeError):
                            functions.emit_sql(fn.name, 'x', dialect)
                    else:
                        self.assertTrue(functions.emit_sql(fn.name, 'x', dialect))

    @unittest.skipIf(duckdb is None, 'DuckDB not installed')
    def test_week_boundaries_and_month_end(self):
        con = duckdb.connect()
        try:
            con.execute('CREATE TABLE s(d DATE, e DATE, t TIMESTAMP, n BIGINT, day BIGINT)')
            con.execute("INSERT INTO s VALUES ('2024-03-31', '2024-04-01', '2024-03-31 23:59:59', NULL, 1)")
            cases = [('date_add(d, months: 1)', date(2024, 4, 30)),
                     ('date_add(d, quarters: 1)', date(2024, 6, 30)),
                     ('date_trunc(d, week)', date(2024, 3, 25)),
                     ('date_diff(d, e, week)', 1),
                     ('date_diff(e, d, week)', -1),
                     ('date_add(d, days: n)', None),
                     ('date_diff(t, cast("2024-04-01 00:00:00", "timestamp"), day)', 1)]
            for expr, want in cases:
                with self.subTest(expr=expr):
                    self.assertEqual(con.execute(sqlgen.model_sql(model(expr))).fetchone(), (want,))
            text = SRC + 'model m { from s\n filter date_add(d, days: 1) == e\n}'
            tm = analysis.Checker(analysis.Project(parse_strata(text))).check_all()['m']
            self.assertEqual(len(con.execute(sqlgen.model_sql(tm)).fetchall()), 1)
            self.assertIn('WHERE (DATE_ADD(', sqlgen.model_sql(tm, BIGQUERY))
        finally:
            con.close()
