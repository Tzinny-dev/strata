"""Join cardinality expectations (§4): `expect many_to_one|one_to_one` on row
preserving joins — static shape validation plus a materialize-time duplicate
key probe that aborts the run like a pin failure.

many_to_one bounds every left row to at most one match by proving the right
keys unique; one_to_one additionally proves the left keys unique. Only
top-level AND of `==` between plain base columns (or a column and a literal)
is provable; anything exotic fails loudly at check time with E079.
"""
import unittest

from strata import analysis, exec as execmod, fmt, sqlgen
from strata.parser import parse_strata, ParseError

try:
    import duckdb
except ImportError:
    duckdb = None

SRC = '''source o(ns: "n", dataset: "o") {
  columns: { id: int64, cid: int64, v: float64, s: string }
}
source c(ns: "n", dataset: "c") {
  columns: { cid: int64, w: string }
}
'''


def check(text):
    return analysis.Checker(analysis.Project(parse_strata(SRC + text))).check_all()


class TestExpectStatic(unittest.TestCase):
    def test_key_extraction(self):
        tm = check('model m { from o join_left c on o.cid == c.cid expect many_to_one }')['m']
        js = tm.plan.joins[0]
        self.assertEqual((js.expect, js.left_keys, js.right_keys),
                         ('many_to_one', ['cid'], ['cid']))
        tm = check('model m { from o join_inner c on cid == c.cid and v > 1 '
                   'expect one_to_one }')['m']
        js = tm.plan.joins[0]
        self.assertEqual((js.expect, js.left_keys, js.right_keys),
                         ('one_to_one', ['cid'], ['cid']))
        # left-side expressions are fine when the right key is plain
        tm = check('model m { from o join_left c on upper(s) == c.w '
                   'expect many_to_one }')['m']
        self.assertEqual(tm.plan.joins[0].right_keys, ['w'])
        tm = check('model m { from o let z = cid join_left c on z == c.cid '
                   'expect many_to_one }')['m']
        self.assertEqual(tm.plan.joins[0].right_keys, ['cid'])

    def test_unannotated_joins_unchanged(self):
        tm = check('model m { from o join_left c on o.cid == c.cid }')['m']
        js = tm.plan.joins[0]
        self.assertIsNone(js.expect)
        self.assertEqual((js.left_keys, js.right_keys), ([], []))

    def test_shape_violations_are_E079(self):
        cases = [
            'model m { from o join_anti c on o.cid == c.cid expect many_to_one }',
            'model m { from o join_semi c on o.cid == c.cid expect one_to_one }',
            'model m { from o join_left c on o.cid > c.cid expect many_to_one }',
            'model m { from o join_left c on o.cid == c.cid or id == c.cid expect many_to_one }',
            'model m { from o join_left c on c.cid == c.cid expect many_to_one }',
            'model m { from o join_left c on o.cid == c.cid + id expect many_to_one }',
            'model m { from o join_left c on o.cid == o.id expect many_to_one }',
            'model m { from o join_left c on id == 5 expect many_to_one }',
            'model m { from o join_left c on o.id == 1 and c.cid == 2 expect one_to_one }',
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(analysis.StrataError) as cm:
                    check(text)
                self.assertEqual(cm.exception.code, 'E079')

    def test_bad_cardinality_word_is_parse_error(self):
        with self.assertRaises(ParseError):
            parse_strata(SRC + 'model m { from o join_left c on o.cid == c.cid expect one_to_many }')

    def test_roundtrip_and_fingerprint(self):
        text = 'model m { from o join_left c on o.cid == c.cid expect many_to_one }'
        formatted = fmt.format_module(parse_strata(SRC + text))
        self.assertIn('expect many_to_one', formatted)
        self.assertEqual(fmt.format_module(parse_strata(formatted)), formatted)
        original = check(text)['m']
        again = check(formatted)['m']
        self.assertEqual(again.fingerprint, original.fingerprint)
        self.assertEqual(again.plan.joins[0].expect, 'many_to_one')

    def test_check_sql_shape(self):
        sql = sqlgen.join_check_sql('customers', ['customer_id'])
        self.assertIn('FROM customers WHERE customer_id IS NOT NULL', sql)
        self.assertIn('GROUP BY customer_id HAVING COUNT(*) > 1', sql)
        multi = sqlgen.join_check_sql('v_r', ['a', 'b'])
        self.assertIn('a IS NOT NULL AND b IS NOT NULL', multi)
        self.assertIn('GROUP BY a, b', multi)


@unittest.skipIf(duckdb is None, 'DuckDB not installed')
class TestExpectExecution(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE o(id BIGINT, cid BIGINT, v DOUBLE, s VARCHAR)')
        self.con.execute('CREATE TABLE c(cid BIGINT, w VARCHAR)')
        self.con.execute("INSERT INTO o VALUES (1, 10, 1.0, 'a'), (2, 20, 2.0, 'b'),"
                         " (3, 10, 3.0, 'c'), (4, NULL, 4.0, 'd')")
        self.con.execute("INSERT INTO c VALUES (10, 'x'), (20, 'y'), (NULL, 'z')")

    def project(self, text):
        mod = parse_strata(SRC + text)
        proj = analysis.Project(mod)
        return proj, analysis.Checker(proj).check_all()

    def test_pass_reports_pin_and_keeps_rows(self):
        proj, tms = self.project(
            'model m { from o join_left c on o.cid == c.cid expect many_to_one '
            'select { id = id, w = c.w } }')
        applied, pins = execmod.materialize(self.con, proj, tms, ['m'])
        self.assertEqual(applied, ['m'])
        self.assertTrue(any('many_to_one' in p for p in pins))
        self.assertEqual(self.con.execute('SELECT * FROM v_m ORDER BY id').fetchall(),
                         [(1, 'x'), (2, 'y'), (3, 'x'), (4, None)])

    def test_fanout_aborts_with_pin_error(self):
        self.con.execute("INSERT INTO c VALUES (10, 'dupe')")
        proj, tms = self.project(
            'model m { from o join_left c on o.cid == c.cid expect many_to_one }')
        with self.assertRaises(execmod.PinError) as cm:
            execmod.materialize(self.con, proj, tms, ['m'])
        self.assertIn('expected many_to_one', str(cm.exception))
        self.assertIn('duplicate key groups', str(cm.exception))

    def test_one_to_one_rejects_left_duplicates(self):
        proj, tms = self.project(
            'model m { from o join_left c on o.cid == c.cid expect one_to_one }')
        with self.assertRaises(execmod.PinError) as cm:
            execmod.materialize(self.con, proj, tms, ['m'])
        self.assertIn('expected one_to_one', str(cm.exception))

    def test_inner_fanout_aborts(self):
        self.con.execute("INSERT INTO c VALUES (20, 'dupe2')")
        proj, tms = self.project(
            'model m { from o join_inner c on o.cid == c.cid expect many_to_one }')
        with self.assertRaises(execmod.PinError):
            execmod.materialize(self.con, proj, tms, ['m'])

    def test_unannotated_join_never_checked(self):
        proj, tms = self.project('model m { from o join_left c on o.cid == c.cid }')
        applied, pins = execmod.materialize(self.con, proj, tms, ['m'])
        self.assertEqual(applied, ['m'])
        self.assertFalse(any('many_to_one' in p or 'one_to_one' in p for p in pins))


if __name__ == '__main__':
    unittest.main()