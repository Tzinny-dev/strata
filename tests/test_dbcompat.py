"""Unit tests for strata/dbcompat.py connection wrappers (pure parts).

No live PostgreSQL/BigQuery needed: the placeholder/inline translation
logic is exercised directly (M3: `?` -> `%s`, string-literal aware, plus
BigQuery literal inlining).
"""
import unittest

from strata.dbcompat import BigQueryConn, PGConn


class TestConvertPlaceholders(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(PGConn._convert_placeholders("WHERE a = ?", False),
                         "WHERE a = %s")
        self.assertEqual(PGConn._convert_placeholders("SELECT ?, ?", False),
                         "SELECT %s, %s")

    def test_skips_question_marks_inside_literals(self):
        self.assertEqual(
            PGConn._convert_placeholders("SELECT 'a?b', ?", False),
            "SELECT 'a?b', %s")

    def test_escaped_quotes_kept(self):
        self.assertEqual(
            PGConn._convert_placeholders("SELECT 'it''s ?', ?", False),
            "SELECT 'it''s ?', %s")

    def test_percent_escape_only_with_params(self):
        sql = "SELECT 'LIKE %foo%', ?"
        self.assertEqual(PGConn._convert_placeholders(sql, False),
                         "SELECT 'LIKE %foo%', %s")
        self.assertEqual(PGConn._convert_placeholders(sql, True),
                         "SELECT 'LIKE %%foo%%', %s")


class TestBigQueryInline(unittest.TestCase):
    def setUp(self):
        self.conn = BigQueryConn.__new__(BigQueryConn)

    def test_no_params_passthrough(self):
        self.assertEqual(self.conn._inline_params("SELECT 1", None),
                         "SELECT 1")

    def test_inlines_literals(self):
        self.assertEqual(
            self.conn._inline_params("SELECT ? WHERE x = ?", ["a", 3]),
            "SELECT 'a' WHERE x = '3'")

    def test_escapes_quotes_and_backslashes(self):
        self.assertEqual(
            self.conn._inline_params("SELECT ?", ["it's \\ x"]),
            "SELECT 'it''s \\\\ x'")


if __name__ == "__main__":
    unittest.main()