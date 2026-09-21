"""Unified diagnostic formatting: file, range, severity, help.

All public error types (ParseError, LexError, StrataError) expose
position + severity + help so the CLI can render a single canonical
one-line diagnostic:

    file:line:col-end_line:end_col [ERROR|WARN] code: message
      help: <suggestion>

The caller controls stdout vs stderr and exit code; this module only
formats.
"""
from __future__ import annotations

import os
from typing import Any


def _rel(file: str) -> str:
    """Relative path when under cwd, else basename for readability."""
    if os.path.isabs(file):
        try:
            rel = os.path.relpath(file)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass
    return os.path.basename(file) or file


def format_diagnostic(e: Any) -> str:
    """Render a ParseError, LexError or StrataError as a canonical line."""
    file = getattr(e, "file", None) or "<strata>"
    line = getattr(e, "line", 1)
    col = getattr(e, "col", 1)
    end_line = getattr(e, "end_line", line)
    end_col = getattr(e, "end_col", col)
    code = getattr(e, "code", "")
    severity = getattr(e, "severity", "error")
    help_msg = getattr(e, "help", None)
    msg = str(e)

    if code:
        head = f"{_rel(file)}:{line}:{col}-{end_line}:{end_col} {severity.upper()} {code}: {msg}"
    else:
        head = f"{_rel(file)}:{line}:{col}-{end_line}:{end_col} {severity.upper()}: {msg}"
    if help_msg:
        head += f"\n  help: {help_msg}"
    return head
