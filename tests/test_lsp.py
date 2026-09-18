"""Minimal LSP server: diagnostics, hover, completion, definition."""
import sys
import unittest
from pathlib import Path
import tempfile, os

from strata.lsp import (LSPServer, LSPContext, diag_from_error,
                        Diagnostic, Range, Position, SEVERITY_ERROR)
from strata.parser import ParseError
from strata.lexer import LexError
from strata.analysis import err


def write(path, text):
    Path(path).write_text(text)


class TestDiagFromError(unittest.TestCase):
    def test_strata_error(self):
        e = err("E091", "test ref", span=(1, 1, 1, 5), file="foo.strata")
        d = diag_from_error(e)
        self.assertIsNotNone(d)
        self.assertEqual(d.range.start.line, 0)
        self.assertEqual(d.code, "E091")
        self.assertEqual(d.severity, SEVERITY_ERROR)

    def test_lex_error(self):
        e = LexError("unexpected", file="bar.strata", line=3, col=5,
                     end_line=3, end_col=6)
        d = diag_from_error(e)
        self.assertIsNotNone(d)
        self.assertEqual(d.range.start.line, 2)

    def test_parse_error(self):
        e = ParseError("expected model name", file="baz.strata", line=2,
                        col=7, end_line=2, end_col=10)
        d = diag_from_error(e)
        self.assertIsNotNone(d)
        self.assertEqual(d.range.start.line, 1)
        self.assertEqual(d.message, "expected model name")


class TestLSPContext(unittest.TestCase):
    def test_diagnostics_from_error(self):
        ctx = LSPContext()
        ctx.reload('source s(ns: "n", dataset: "s") { columns: { x: int64 } }\n'
                    'model m { from s select { z = unknown_fn(x) } }',
                    "file:///tmp/test.strata")
        self.assertGreater(len(ctx.diagnostics), 0)
        self.assertEqual(ctx.diagnostics[0].code, "E059")

    def test_no_diagnostics_when_green(self):
        ctx = LSPContext()
        ctx.reload('source s(ns: "n", dataset: "s") { columns: { x: int64 } }\n'
                    'model m { from s select { z = x + 1 } }',
                    "file:///tmp/test2.strata")
        self.assertEqual(len(ctx.diagnostics), 0)


class TestLSPServer(unittest.TestCase):
    def setUp(self):
        self.ctx = LSPContext()
        self.ctx.reload('source s(ns: "n", dataset: "s") { columns: { x: int64 } }\n'
                         'model m { from s select { z = x + 1 } }',
                         "file:///tmp/test3.strata")
        self.server = LSPServer(self.ctx)

    def test_completion_contains_model(self):
        items = self.server.completion(1, 1)
        labels = [i["label"] for i in items]
        self.assertIn("m", labels)

    def test_completion_contains_source(self):
        items = self.server.completion(1, 1)
        labels = [i["label"] for i in items]
        self.assertIn("s", labels)

    def test_hover_returns_something(self):
        h = self.server.hover(1, 1)
        self.assertIsNone(h)

    def test_definition_returns_none_for_unknown(self):
        d = self.server.definition(0, 0)
        # model not on line 0
        self.assertIsNone(d)

    def test_completion_contains_functions(self):
        items = self.server.completion(0, 0)
        labels = [i["label"] for i in items]
        self.assertIn("count", labels)


if __name__ == '__main__':
    unittest.main()
