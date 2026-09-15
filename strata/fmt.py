"""Strata canonical formatter: AST -> text (idempotent)."""
from __future__ import annotations
from . import ast

def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def _field(f: ast.ContractField) -> str:
    t = f.type_spec
    if f.type_spec == "decimal":
        t = f"decimal({f.params[0]}, {f.params[1]})"
    elif f.type_spec == "array":
        t = f"array<{f.params[0]}>"
    elif f.type_spec == "money" and f.params:
        t = f"money({f.params[0]})"
    bits = [t]
    if f.nonnull and not f.primary:
        bits.append("nonnull")
    if f.primary:
        bits.append("primary_key")
    elif f.unique:
        bits.append("unique")
    if f.protected:
        bits.append("protected")
    if f.enum:
        bits.append("enum {" + ", ".join(f.enum) + "}")
    if f.classification:
        bits.append(f"classification: {_q(f.classification)}")
    return f"{f.name} : {' '.join(bits)}"

def _expr(e: ast.Node) -> str:
    if isinstance(e, ast.Literal):
        if isinstance(e.value, bool):
            return "true" if e.value else "false"
        if e.value is None:
            return "none"
        if isinstance(e.value, str):
            return _q(e.value)
        return str(e.value)
    if isinstance(e, ast.ColumnRef):
        return f"{e.qualifier}.{e.name}" if e.qualifier else e.name
    if isinstance(e, ast.Call):
        if e.name == "in" and len(e.args) == 2 and isinstance(e.args[1], ast.ListExpr):
            items = ", ".join(_expr(i) for i in e.args[1].items)
            return f"{_expr(e.args[0])} in [{items}]"
        return f"{e.name}({', '.join(_expr(a) for a in e.args)})"
    if isinstance(e, ast.BinOp):
        return f"({_expr(e.left)} {e.op} {_expr(e.right)})"
    if isinstance(e, ast.UnOp):
        return "(not " + _expr(e.operand) + ")" if e.op == "not" else "(-" + _expr(e.operand) + ")"
    if isinstance(e, ast.ListExpr):
        return "[" + ", ".join(_expr(i) for i in e.items) + "]"
    return "..."

def _stmts(stmts, ind: str):
    out = []
    for s in stmts:
        if isinstance(s, ast.FromStmt):
            out.append(f"{ind}from {s.table}")
        elif isinstance(s, ast.JoinStmt):
            out.append(f"{ind}join_{s.kind} {s.table} on {_expr(s.on)}")
        elif isinstance(s, ast.FilterStmt):
            out.append(f"{ind}filter {_expr(s.cond)}")
        elif isinstance(s, ast.LetStmt):
            out.append(f"{ind}let {s.name} = {_expr(s.expr)}")
        elif isinstance(s, ast.DeriveStmt):
            assigns = ", ".join(f"{a.name} = {_expr(a.expr)}" for a in s.assigns)
            out.append(f"{ind}derive {{ {assigns} }}")
        elif isinstance(s, ast.SelectStmt):
            assigns = ", ".join(f"{a.name} = {_expr(a.expr)}" for a in s.assigns)
            out.append(f"{ind}select {{ {assigns} }}")
        elif isinstance(s, ast.AggregateStmt):
            assigns = ", ".join(f"{a.name} = {_expr(a.expr)}" for a in s.assigns)
            out.append(f"{ind}aggregate {{ {assigns} }}")
        elif isinstance(s, ast.GroupStmt):
            keys = ", ".join(_expr(k) for k in s.keys)
            out.append(f"{ind}group {{{keys}}} (")
            out.extend(_stmts(s.body, ind + "  "))
            out.append(f"{ind})")
        elif isinstance(s, ast.SortStmt):
            keys = ", ".join(("-" if d else "") + _expr(e) for e, d in s.keys)
            out.append(f"{ind}sort {{{keys}}}")
        elif isinstance(s, ast.TakeStmt):
            out.append(f"{ind}take {s.limit}" if s.limit is not None else f"{ind}take {s.start}..{s.end}")
    return out

def format_module(mod: ast.Module) -> str:
    out = []
    for d in mod.decls:
        if isinstance(d, ast.ImportDecl):
            out.append(f"import {d.path}")
        elif isinstance(d, ast.SourceDecl):
            res = ", ".join(f'{k}: {_q(v)}' for k, v in d.resource.items())
            out.append(f"source {d.name}({res}) {{")
            for k, v in d.props:
                if k == "columns":
                    out.append("  columns: {")
                    for f in v:
                        out.append(f"    {_field(f)},")
                    out.append("  }")
                elif isinstance(v, str):
                    out.append(f"  {k}: {_q(v)}")
                else:
                    out.append(f"  {k}: {v}")
            out.append("}")
        elif isinstance(d, ast.ContractDecl):
            out.append(f"contract {d.name} {{")
            for f in d.fields:
                out.append(f"  {_field(f)},")
            out.append("}")
        elif isinstance(d, ast.ModelDecl):
            head = f"model {d.name}" + (f" -> contract {d.contract}" if d.contract else "")
            out.append(head + " {")
            for k, v in d.attrs.items():
                out.append(f'  {k}: {_q(v)}')
            out.extend(_stmts(d.stmts, "  "))
            out.append("}")
        elif isinstance(d, ast.FnDecl):
            out.append(f"fn {d.name}(...) -> ... {{ {_expr(d.body)} }}")
        elif isinstance(d, ast.PipelineDecl):
            out.append(f"pipeline {d.name} {{")
            if d.env:
                out.append(f"  env: {d.env},")
            out.append(f"  models: [{', '.join(_expr(m) for m in d.models)}],")
            if d.sources:
                out.append("  sources: {")
                for src, kv in d.sources.items():
                    inner = ", ".join(f'{k}: {_q(v)}' for k, v in kv.items())
                    out.append(f"    {src}: from({inner}),")
                out.append("  }")
            out.append("}")
        elif isinstance(d, ast.GeneratorDecl):
            out.append(_expr(d.call))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
