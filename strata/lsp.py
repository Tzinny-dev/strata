"""Minimal LSP server for Strata (diagnostics, hover, completion, definition).

Reads JSON-RPC over stdin, writes over stdout. No external dependencies:
uses only stdlib (json, sys, ast, re).

Supported methods:
    initialize / initialized
    textDocument/didOpen, textDocument/didChange
    textDocument/completion
    textDocument/hover
    textDocument/definition
    textDocument/publishDiagnostics (internal)

Diagnostics are produced by compiling the document with the existing
Checker; hover/completion/definition use the parsed AST and typed
model schema.
"""
from __future__ import annotations

import json
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .parser import parse_strata
from .analysis import Checker, Project, StrataError
from .functions import FUNCTIONS, Fn


@dataclass
class Position:
    line: int
    character: int


@dataclass
class Range:
    start: Position
    end: Position


@dataclass
class Location:
    uri: str
    range: Range


@dataclass
class Diagnostic:
    range: Range
    severity: int
    code: str
    message: str
    source: str = "strata"


SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFORMATION = 3
SEVERITY_HINT = 4


def diag_from_error(e: Exception) -> Optional[Diagnostic]:
    """Map an exception to an LSP Diagnostic, or None when it carries no source span."""
    file = getattr(e, "file", None)
    line = getattr(e, "line", 1) - 1
    col = getattr(e, "col", 1) - 1
    end_line = getattr(e, "end_line", line + 1) - 1
    end_col = getattr(e, "end_col", col + 1) - 1
    code = getattr(e, "code", "")
    msg = str(e)
    if not file:
        return None
    return Diagnostic(
        range=Range(start=Position(line=line, character=col),
                      end=Position(line=end_line, character=end_col)),
        severity=SEVERITY_ERROR, code=code, message=msg)


def _uri(text: str) -> str:
    import urllib.parse
    return "file://" + urllib.parse.quote(text, safe="/")


class LSPContext:
    """Holds the current document state for LSP operations."""
    def __init__(self) -> None:
        self.uri: Optional[str] = None
        self.text: str = ""
        self.path: str = "<strata>"
        self.modules: Dict[str, Project] = {}
        self.diagnostics: List[Diagnostic] = []
        self.typed: Dict[str, Any] = {}
        self.models: Dict[str, Any] = {}
        self.sources: List[str] = []
        self.parsed: Optional[Any] = None

    def reload(self, text: str, uri: str) -> None:
        """Re-parse `text` for `uri` and refresh the cached project state."""
        self.text = text
        self.uri = uri
        self.path = uri.replace("file://", "")
        import tempfile, os
        from pathlib import Path
        self.diagnostics = []
        self.typed = {}
        self.models = {}
        self.parsed = None
        proj = None
        try:
            mod = parse_strata(text, self.path)
            self.parsed = mod
            proj = Project(mod)
            ck = Checker(proj)
            ck.check_all()
            ck.check_tests()
            self.typed = proj.typed
            self.models = {n: tm for n, tm in proj.typed.items()}
            self.sources = list(proj.sources)
        except StrataError as e:
            d = diag_from_error(e)
            if d:
                self.diagnostics.append(d)
        except Exception:
            pass
        self._proj = proj  # keep for completion even on error


class LSPServer:
    def __init__(self, ctx: LSPContext) -> None:
        self.ctx = ctx
        self.capabilities: Dict[str, Any] = {
            "textDocumentSync": {"openClose": True, "change": 2},
            "completionProvider": {"resolveProvider": False,
                                    "triggerCharacters": ["."]},
            "hoverProvider": True,
            "definitionProvider": True,
        }

    def _find_model_at(self, line: int, col: int) -> Optional[Tuple[str, int, int]]:
        """Return (model_name, col_start, col_end) or None."""
        text = self.ctx.text
        lines = text.split("\n")
        if line >= len(lines):
            return None
        ln = lines[line]
        # Find model keyword nearby
        m = re.search(r"model\s+(\w+)", ln[:col + 1])
        if m:
            name = m.group(1)
            if name in self.ctx.models:
                return (name, m.start(1), m.end(1))
        return None

    def completion(self, line: int, col: int) -> List[Dict[str, Any]]:
        """Completion items at (line, col): model names and source names."""
        items: List[Dict[str, Any]] = []
        # Model names
        proj = getattr(self.ctx, '_proj', None)
        if proj is not None:
            for name in proj.models:
                items.append({"label": name, "kind": 6,
                              "detail": "model",
                              "insertText": name})
        # Sources
        if self.ctx.parsed:
            for src_name in self.ctx.sources:
                items.append({"label": src_name, "kind": 20,
                              "detail": "source",
                              "insertText": src_name})
        # Functions from catalog
        for fn in FUNCTIONS:
            items.append({"label": fn.name, "kind": 13,
                          "detail": fn.sig if hasattr(fn, "sig") else fn.name,
                          "insertText": fn.name})
        # Column names from current model's schema
        model = self._find_model_at(line, col)
        if model and model[0] in self.ctx.models:
            tm = self.models[model[0]]
            if hasattr(tm, "schema"):
                for cname in tm.schema:
                    items.append({"label": cname, "kind": 6,
                                  "detail": "column",
                                  "insertText": cname})
        return items

    def hover(self, line: int, col: int) -> Optional[Dict[str, Any]]:
        """Hover text for the token at (line, col), or None."""
        text = self.ctx.text
        lines = text.split("\n")
        if line >= len(lines):
            return None
        ln = lines[line]
        # Hover over a column reference in the current model
        model = self._find_model_at(line, col)
        if model and model[0] in self.ctx.models:
            tm = self.ctx.models[model[0]]
            if hasattr(tm, "schema"):
                for cname, ccol in tm.schema.items():
                    if cname in ln and cname:
                        from .types import StrataType
                        t = ccol.t
                        tstr = str(t) if t else "unknown"
                        if not ccol.nullable:
                            tstr += " nonnull"
                        return {
                            "contents": {
                                "kind": "markdown",
                                "value": f"**{cname}**: `{tstr}`"
                            },
                            "range": {
                                "start": {"line": line,
                                          "character": ln.index(cname)},
                                "end": {"line": line,
                                        "character": ln.index(cname) + len(cname)}
                            }
                        }
        return None

    def definition(self, line: int, col: int) -> Optional[Dict[str, Any]]:
        """Definition location for the token at (line, col), or None."""
        text = self.ctx.text
        lines = text.split("\n")
        if line >= len(lines):
            return None
        ln = lines[line]
        # Go to model declaration
        m = re.search(r"model\s+(\w+)", ln)
        if m:
            return {
                "uri": _uri(self.ctx.path),
                "range": {
                    "start": {"line": line, "character": m.start(1)},
                    "end": {"line": line,
                            "character": m.end(1)}
                }
            }
        return None


def run() -> None:
    """Run the LSP server loop on stdin/stdout."""
    ctx = LSPContext()
    server = LSPServer(ctx)
    req_id = 0
    initialized = False

    def send(method: str, params: Dict[str, Any] = {}, id: Optional[int] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if id is not None:
            msg["id"] = id
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    def reply(id: Any, result: Any) -> None:
        send("result", result, id=id)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        pid = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            initialized = True
            send("initialized", {})
            reply(pid, {"capabilities": server.capabilities})
        elif method == "textDocument/didOpen":
            doc = params.get("textDocument", {})
            uri = doc.get("uri", "")
            text = doc.get("text", "")
            ctx.reload(text, uri)
            send("textDocument/publishDiagnostics", {
                "uri": uri,
                "diagnostics": _diag_list(ctx.diagnostics)
            })
        elif method == "textDocument/didChange":
            text = params.get("textDocument", {}).get("text", "")
            ctx.text = text
            ctx.reload(text, ctx.uri or "<strata>")
            send("textDocument/publishDiagnostics", {
                "uri": ctx.uri or "<strata>",
                "diagnostics": _diag_list(ctx.diagnostics)
            })
        elif method == "textDocument/completion":
            pos = params.get("position", {})
            line = pos.get("line", 0)
            col = pos.get("character", 0)
            items = server.completion(line, col)
            reply(pid, {"isIncomplete": False, "items": items})
        elif method == "textDocument/hover":
            pos = params.get("position", {})
            line = pos.get("line", 0)
            col = pos.get("character", 0)
            result = server.hover(line, col)
            reply(pid, result or {})
        elif method == "textDocument/definition":
            pos = params.get("position", {})
            line = pos.get("line", 0)
            col = pos.get("character", 0)
            result = server.definition(line, col)
            reply(pid, result or {"uris": [], "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 0}}})
        elif method == "shutdown":
            reply(pid, {})
        elif method == "exit":
            break


def _diag_list(diagnostics: List[Diagnostic]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for d in diagnostics:
        out.append({
            "range": {
                "start": {"line": d.range.start.line,
                          "character": d.range.start.character},
                "end": {"line": d.range.end.line,
                        "character": d.range.end.character}
            },
            "severity": d.severity,
            "code": d.code,
            "message": d.message,
            "source": d.source,
        })
    return out


if __name__ == "__main__":
    run()