"""Compile-time analysis: fn evaluation, model typecheck, lineage, contracts.

Builds a typed semantic graph (the `TypedModel`) whose provenance (`origin`)
feeds blast-radius analysis, and whose `plan` is the IR consumed by `sqlgen`
and the executor.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import ast
from .types import (
    StrataType, INT64, FLOAT64, STRING, BOOL, DATE, TIMESTAMP, UUID, JSON,
    UNKNOWN, decimal, money, binary_type, Col,
)


class StrataError(Exception):
    def __init__(self, msg, code="E099", span=None):
        super().__init__(msg)
        self.code = code
        self.span = span


def err(code: str, msg: str, span=None):
    return StrataError(msg, code=code, span=span)


AGGREGATES = {"count", "sum", "avg", "max", "min"}


def _literal_type(value):
    """Inferred StrataType for a test literal (E094)."""
    if isinstance(value, bool):
        from .types import BOOL
        return BOOL
    if value is None:
        from .types import UNKNOWN
        return UNKNOWN
    if isinstance(value, int):
        from .types import INT64
        return INT64
    if isinstance(value, float):
        from .types import FLOAT64
        return FLOAT64
    from .types import STRING
    return STRING


@dataclass
class Inf:
    t: StrataType = UNKNOWN
    nullable: bool = True


@dataclass
class Origin:
    node: str
    col: str
    kind: str = "passthrough"

    def key(self):
        return (self.node, self.col)


# ------------------------------------------------------------------ IR plan

@dataclass
class InputSpec:
    alias: str
    node: str
    is_source: bool
    cols: "OrderedDict[str, Col]"


@dataclass
class JoinSpec:
    index: int
    alias: str
    node: str
    on: ast.Node
    kind: str = "left"


@dataclass
class BaseCol:
    name: str
    expr: Optional[ast.Node]


@dataclass
class PlanOut:
    name: str
    expr: ast.Node
    group_key: bool = False


@dataclass
class Plan:
    inputs: List[InputSpec] = field(default_factory=list)
    joins: List[JoinSpec] = field(default_factory=list)
    base_cols: List[BaseCol] = field(default_factory=list)
    preds: List[ast.Node] = field(default_factory=list)
    having: List[ast.Node] = field(default_factory=list)
    outputs: List[PlanOut] = field(default_factory=list)
    group_exprs: List[ast.Node] = field(default_factory=list)
    sorts: List[Tuple[ast.Node, bool]] = field(default_factory=list)
    limit: Optional[Tuple[Optional[int], Optional[int]]] = None
    grouped: bool = False


@dataclass
class TypedModel:
    name: str
    contract: Optional[str]
    attrs: Dict[str, str]
    schema: "OrderedDict[str, Col]" = field(default_factory=OrderedDict)
    lineage: Dict[str, List[Origin]] = field(default_factory=dict)
    reads: Set[Tuple[str, str]] = field(default_factory=set)
    plan: Optional[Plan] = None
    deps: List[str] = field(default_factory=list)
    fingerprint: str = ""
    diags: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ schemas

TYPE_FROM_KW = {
    "int64": INT64, "float64": FLOAT64, "string": STRING, "bool": BOOL,
    "date": DATE, "timestamp": TIMESTAMP, "uuid": UUID, "json": JSON,
}


def type_from_spec(spec: str, params: List[object]) -> StrataType:
    if spec in TYPE_FROM_KW:
        return TYPE_FROM_KW[spec]
    if spec == "decimal":
        return decimal(params[0], params[1])
    if spec == "money":
        return money(params[0] if params else "USD")
    if spec == "array":
        return array(TYPE_FROM_KW[params[0]])
    return UNKNOWN


def contract_field_col(f: ast.ContractField) -> Col:
    return Col(
        name=f.name,
        t=type_from_spec(f.type_spec, f.params),
        nullable=not f.nonnull,
        unique=f.unique,
        primary=f.primary,
        protected=f.protected,
        enum=frozenset(f.enum),
        classification=f.classification,
    )


def source_decl_cols(decl: ast.SourceDecl) -> List[Col]:
    for kind, val in decl.props:
        if kind == "columns":
            return [contract_field_col(f) for f in val]
    return []


# ------------------------------------------------------------------ fn evaluation (definition domain)

class FnEvaluator:
    def __init__(self, project: "Project"):
        self.project = project

    def call(self, decl: ast.FnDecl, args: List[object]) -> object:
        env = dict(zip([p for p, _ in decl.params], args))
        return self._val(decl.body, env)

    def _val(self, e, env: Dict[str, object]) -> object:
        if isinstance(e, ast.Literal):
            return e.value
        if isinstance(e, ast.TemplateStr):
            out = []
            for kind, v in e.parts:
                out.append(str(env[v]) if kind == "var" else v)
            return "".join(out)
        if isinstance(e, ast.ColumnRef):
            if e.name in env:
                return env[e.name]
            raise err("F040", f"unknown identifier {e.name!r} in fn body", e.span)
        if isinstance(e, ast.ListExpr):
            return [self._val(i, env) for i in e.items]
        if isinstance(e, ast.ListComprehension):
            out = []
            for v in self._val(e.iterable, env):
                env2 = dict(env)
                env2[e.var] = v
                out.append(self._val(e.body, env2))
            return out
        if isinstance(e, ast.Call):
            if e.name == "concat":
                return "".join(str(self._val(a, env)) for a in e.args)
            if e.name == "length":
                return len(self._val(e.args[0], env))
            if e.name in self.project.fns:
                return self.call(self.project.fns[e.name], [self._val(a, env) for a in e.args])
            raise err("F041", f"unknown fn call {e.name!r}", e.span)
        if isinstance(e, ast.BinOp):
            lval = self._val(e.left, env)
            rval = self._val(e.right, env)
            if e.op == "+":
                return lval + rval
            if e.op == "==":
                return lval == rval
            return (lval, e.op, rval)
        if isinstance(e, ast.ModelValue):
            return self._model_value(e, env)
        raise err("F042", f"unsupported expression in fn body: {type(e).__name__}", e.span)

    def _model_value(self, mv: ast.ModelValue, env) -> ast.ModelDecl:
        import copy
        return ast.ModelDecl(
            name=str(self._val(mv.name, env)),
            contract=mv.contract,
            attrs={k: v for k, v in mv.attrs.items()},
            stmts=[self._subst_stmt(copy.deepcopy(s), env) for s in mv.stmts],
            generated=True,
            span=mv.span,
        )

    def _subst_stmt(self, stmt: ast.Stmt, env) -> ast.Stmt:
        if isinstance(stmt, ast.FilterStmt):
            stmt.cond = self._subst_expr(stmt.cond, env)
        elif isinstance(stmt, ast.LetStmt):
            stmt.expr = self._subst_expr(stmt.expr, env)
        elif isinstance(stmt, ast.JoinStmt):
            stmt.on = self._subst_expr(stmt.on, env)
        elif isinstance(stmt, ast.DeriveStmt):
            for a in stmt.assigns:
                a.expr = self._subst_expr(a.expr, env)
        return stmt

    def _subst_expr(self, e, env):
        if isinstance(e, ast.ColumnRef):
            if e.name in env:
                return ast.Literal(value=env[e.name], span=e.span)
            return e
        if isinstance(e, ast.BinOp):
            e.left = self._subst_expr(e.left, env)
            e.right = self._subst_expr(e.right, env)
            return e
        if isinstance(e, ast.UnOp):
            e.operand = self._subst_expr(e.operand, env)
            return e
        if isinstance(e, ast.Call):
            e.args = [self._subst_expr(a, env) for a in e.args]
            return e
        return e


# ------------------------------------------------------------------ project

class Project:
    def __init__(self, module: ast.Module, search_dirs: Optional[List[str]] = None,
                 _seen: Optional[Set[str]] = None):
        self.module = module
        self.search_dirs = [str(p) for p in (search_dirs or [])]
        self.expansions: List[ast.GeneratorDecl] = []
        self.sources: Dict[str, ast.SourceDecl] = {}
        self.contracts: Dict[str, ast.ContractDecl] = {}
        self.models: Dict[str, ast.ModelDecl] = {}
        self.fns: Dict[str, ast.FnDecl] = {}
        self.pipelines: List[ast.PipelineDecl] = []
        self.tests: Dict[str, List[ast.TestDecl]] = {}
        self.typed: Dict[str, TypedModel] = {}
        self.modules: Dict[str, ast.Module] = {module.path or "<strata>": module}
        self.imports: List[str] = []
        self._resolve()
        self._expand_fns()

    # -- multi-file imports (spec/grammar.md: `import a.b` -> a/b.strata) ----
    def _resolve_import(self, path: str, seen: Set[str]) -> Optional[ast.Module]:
        rel = Path(*path.split(".")).with_suffix(".strata")
        candidates: List[Path] = []
        cur = Path(self.module.path or "<strata>")
        if str(cur) not in ("<strata>", "") and cur.parent != Path("."):
            candidates.append(cur.parent / rel)
        for d in self.search_dirs:
            candidates.append(Path(d) / rel)
        candidates.append(Path.cwd() / rel)
        for cand in candidates:
            if cand.is_file():
                key = str(cand.resolve())
                if key in seen:
                    raise err("F045", f"circular import {path!r} ({cand})")
                from .parser import parse_strata as _parse
                seen.add(key)
                sub = _parse(cand.read_text(), str(cand))
                self.modules[str(cand)] = sub
                self.imports.append(path)
                for d in sub.decls:
                    if isinstance(d, ast.ImportDecl):
                        self._resolve_import(d.path, seen)
                    else:
                        self._merge_decl(d)
                return sub
        raise err("E022", f"cannot resolve import {path!r} (tried {', '.join(str(c) for c in candidates)})")

    def _merge_decl(self, d: ast.Node) -> None:
        if isinstance(d, ast.SourceDecl):
            self.sources.setdefault(d.name, d)
        elif isinstance(d, ast.ContractDecl):
            self.contracts.setdefault(d.name, d)
        elif isinstance(d, ast.ModelDecl):
            self.models.setdefault(d.name, d)
        elif isinstance(d, ast.TestDecl):
            self.tests.setdefault(d.model, []).append(d)
        elif isinstance(d, ast.FnDecl):
            self.fns.setdefault(d.name, d)
        elif isinstance(d, ast.PipelineDecl):
            self.pipelines.append(d)
        elif isinstance(d, ast.GeneratorDecl):
            self.expansions.append(d)

    def _resolve(self):
        seen: Set[str] = set()
        try:
            seen.add(str(Path(self.module.path).resolve()))
        except Exception:
            pass
        for d in self.module.decls:
            if isinstance(d, ast.ImportDecl):
                self._resolve_import(d.path, seen)
            else:
                self._merge_decl(d)

    def _expand_fns(self):
        ev = FnEvaluator(self)
        for g in self.expansions:
            call = g.call
            if not isinstance(call, ast.Call):
                raise err("F043", "generator decl must be a fn call", g.span)
            if call.name not in self.fns:
                raise err("F044", f"unknown generator fn {call.name!r}", g.span)
            values = ev.call(self.fns[call.name], [ev._val(a, {}) for a in call.args])
            self._add_generated(values)
        for p in self.pipelines:
            for i, item in enumerate(p.models):
                if isinstance(item, ast.Call):
                    if item.name not in self.fns:
                        raise err("F044", f"unknown fn {item.name!r} in pipeline", item.span)
                    values = ev.call(self.fns[item.name], [ev._val(a, {}) for a in item.args])
                    names = [str(m.name) for m in self._flatten_models(values)]
                    p.models[i] = ast.ListExpr(items=[ast.Literal(value=n) for n in names])

    def _add_generated(self, values):
        for v in self._flatten_models(values):
            self.models.setdefault(v.name, v)

    def _flatten_models(self, values):
        out = []
        for v in values:
            if isinstance(v, list):
                out.extend(self._flatten_models(v))
            elif isinstance(v, ast.ModelDecl):
                out.append(v)
        return out

    def source_schema(self, name: str) -> "OrderedDict[str, Col]":
        decl = self.sources.get(name)
        if decl is None:
            raise err("E020", f"unknown source {name!r}")
        cols = source_decl_cols(decl)
        if not cols:
            raise err("E021", f"source {name!r} has no declared columns (add columns: {{...}})")
        return OrderedDict((c.name, c) for c in cols)

    def input_schema(self, name: str) -> Tuple["OrderedDict[str, Col]", bool, str]:
        if name in self.sources:
            return self.source_schema(name), True, name
        if name in self.models:
            tm = self.typed.get(name)
            if tm is None:
                raise err("E024", f"model {name!r} not yet typechecked (dependency order?)")
            return tm.schema, False, name
        raise err("E020", f"unknown input {name!r} (not a source or model)")

    def model_names_for(self, pipeline: Optional[ast.PipelineDecl], include_generated=False) -> List[str]:
        if pipeline is None:
            return [n for n in self.models if include_generated or not self.models[n].generated]
        names = []
        for item in pipeline.models:
            if isinstance(item, ast.Literal):
                names.append(str(item.value))
            elif isinstance(item, ast.ListExpr):
                names.extend(str(it.value) for it in item.items if isinstance(it, ast.Literal))
            elif isinstance(item, ast.ColumnRef):
                names.append(item.name)
            elif isinstance(item, ast.Call):  # unresolved fn hook
                raise err("F046", f"pipeline has unexpanded fn {item.name!r}", item.span)
        return names

    def pipeline_by_name(self, name: Optional[str]) -> Optional[ast.PipelineDecl]:
        if not self.pipelines:
            return None
        if name is None:
            return self.pipelines[0]
        for p in self.pipelines:
            if p.name == name:
                return p
        raise err("E023", f"unknown pipeline {name!r}")

    def pipeline_sources(self, pipeline: Optional[str]) -> Dict[str, Dict[str, str]]:
        """Per-env source resource overrides for `run --pipeline`.

        `pipeline prod { sources: { orders: from(ns: "x", dataset: "y") } }`
        returns {"orders": {"ns": "x", "dataset": "y"}}. Empty when the
        pipeline declares no `sources:` — `exec.run` then uses the compiled
        `source(ns,dataset)` defaults. (spec/grammar.md §5, propuesta Fase 2.)
        """
        p = self.pipeline_by_name(pipeline)
        if p is None:
            return {}
        return {src: dict(kv) for src, kv in (p.sources or {}).items()}


# ------------------------------------------------------------------ typing helpers

TYPE_FROM_KW_EXT = dict(TYPE_FROM_KW)


def types_compat(exp: StrataType, got: StrataType) -> bool:
    if got.name == "unknown":
        return False
    if exp == got:
        return True
    # narrowing never implicit; widening int -> float/decimal allowed
    if exp.is_numeric() and got.name == "int64":
        return True
    return False


def unify(t1: StrataType, t2: StrataType) -> StrataType:
    """Least-upper-bound used by coalesce/case; numeric literals coerce into money/decimal."""
    if t1 == t2:
        return t1
    if t1.name in ("decimal", "money") and t2.is_numeric():
        return t1
    if t2.name in ("decimal", "money") and t1.is_numeric():
        return t2
    if t1.name == "money" and t2.name == "money":
        return t1
    if t1.is_numeric() and t2.is_numeric():
        if t1.name == "float64" or t2.name == "float64":
            return FLOAT64
        if t1.name == "decimal" or t2.name == "decimal":
            return decimal()
        return INT64
    return UNKNOWN


AGG_TYPE = {
    "count": lambda a: Inf(INT64, False),
    "sum": lambda a: Inf(a.t, a.nullable),
    "avg": lambda a: Inf(FLOAT64, a.nullable),
    "max": lambda a: a,
    "min": lambda a: a,
}


def infer_binary(op: str, lt: Inf, rt: Inf, span=None) -> Inf:
    if op in ("==", "!=", "<", "<=", ">", ">=", "in"):
        if lt.t.is_numeric() and rt.t.is_numeric():
            return Inf(BOOL, lt.nullable or rt.nullable)
        if lt.t == rt.t:
            return Inf(BOOL, lt.nullable or rt.nullable)
        if lt.t.name == "unknown" or rt.t.name == "unknown":
            return Inf(BOOL, True)
        raise err("E051", f"cannot compare {lt.t} with {rt.t}", span)
    if op in ("and", "or"):
        if lt.t == BOOL and rt.t == BOOL:
            return Inf(BOOL, lt.nullable or rt.nullable)
        raise err("E052", f"'{op}' requires bool operands, got {lt.t} and {rt.t}", span)
    if op == "+" and lt.t == STRING and rt.t == STRING:
        return Inf(STRING, lt.nullable or rt.nullable)
    try:
        t = binary_type(op, lt.t, rt.t)
    except TypeError as te:
        raise err("E053", str(te), span)
    if t.name == "unknown":
        raise err("E053", f"unsupported operation {op} on {lt.t} and {rt.t}", span)
    return Inf(t, lt.nullable or rt.nullable)


# ------------------------------------------------------------------ the checker

class Checker:
    def __init__(self, project: Project):
        self.p = project
        self.all_reads: Dict[str, Set[Tuple[str, str]]] = {}

    def check_all(self, model_names: Optional[List[str]] = None) -> Dict[str, TypedModel]:
        names = model_names if (model_names is not None and model_names) else list(self.p.models)
        order = self._topo(names)
        for n in order:
            tm = self._check_model(self.p.models[n])
            self.p.typed[n] = tm
            self.all_reads[n] = set(tm.reads)
        return self.p.typed

    def check_tests(self, model_names=None):
        """Validate declarative tests (E091-E094). Must be called after check_all."""
        for tds in self.p.tests.values():
            for td in tds:
                mname = td.model
                if mname not in self.p.typed:
                    raise err("E091", f"test references non-existent model {mname!r}", td.span)
                tm = self.p.typed[mname]
                for c in td.checks:
                    if c.kind == "row_count":
                        continue
                    if c.col and c.col not in tm.schema:
                        raise err("E092",
                                  f"test references unknown column {c.col!r} in model {mname!r}",
                                  c.span)
                    if c.op not in ("==", "!=", "<", ">", "<=", ">="):
                        raise err("E093", f"test uses unsupported operator {c.op!r}", c.span)
                    if c.col is None:
                        continue
                    col = tm.schema[c.col]
                    # literal value must be comparable to column type
                    lit_type = _literal_type(c.value)
                    if lit_type != col.t:
                        raise err("E094",
                                  f"test value {c.value!r} has type {lit_type.name} "
                                  f"but column {mname}.{c.col} is {col.t.name}",
                                  c.span)
        return sum(len(tds) for tds in self.p.tests.values())

    def _topo(self, names: List[str]) -> List[str]:
        visiting, done, out = set(), set(), []

        def visit(n):
            if n in done:
                return
            if n in visiting:
                raise err("F001", f"model dependency cycle involving {n!r}")
            visiting.add(n)
            decl = self.p.models.get(n)
            if decl is not None:
                for dep in self._deps(decl):
                    if dep in self.p.models:
                        visit(dep)
            visiting.discard(n)
            done.add(n)
            out.append(n)

        for n in names:
            visit(n)
        return out

    def _deps(self, decl: ast.ModelDecl) -> List[str]:
        deps = []
        for s in decl.stmts:
            if isinstance(s, ast.FromStmt):
                deps.append(s.table)
            elif isinstance(s, ast.JoinStmt):
                deps.append(s.table)
        return deps

    def _check_model(self, decl: ast.ModelDecl) -> TypedModel:
        return _ModelState(decl, self).run()


class _ModelState:
    def __init__(self, decl: ast.ModelDecl, checker: Checker):
        self.decl = decl
        self.c = checker
        tm = TypedModel(name=decl.name, contract=decl.contract, attrs=dict(decl.attrs),
                        deps=[d for d in checker._deps(decl)])
        tm.plan = Plan()
        self.tm = tm
        self.inputs: List[InputSpec] = []
        self.base_cols: List[BaseCol] = []
        self.own: Dict[str, str] = {}
        self.cols: "OrderedDict[str, Col]" = OrderedDict()
        self.origins: Dict[str, List[Origin]] = {}
        self.preds: List[ast.Node] = []
        self.outputs: List[PlanOut] = []
        self.group_keys: Set[str] = set()
        self.in_group = False

    def run(self) -> TypedModel:
        for s in self.decl.stmts:
            self.stmt(s)
        self.finish()
        return self.tm

    # -- column lookups ----------------------------------------------
    def lookup(self, e: ast.ColumnRef) -> Col:
        if e.qualifier:
            for inp in self.inputs:
                if inp.alias == e.qualifier:
                    col = inp.cols.get(e.name)
                    if col is None:
                        raise err("E040", f"no column {e.name!r} in input {e.qualifier!r}", e.span)
                    return col
            raise err("E041", f"unknown input qualifier {e.qualifier!r}", e.span)
        col = self.cols.get(e.name)
        if col is None:
            where = "group output" if self.in_group else "input columns"
            raise err("E040", f"unknown column {e.name!r} in {where}", e.span)
        return col

    def origin_of(self, e: ast.ColumnRef) -> List[Origin]:
        if e.qualifier:
            for i, inp in enumerate(self.inputs):
                if inp.alias == e.qualifier:
                    if i == 0:
                        return [Origin(inp.node, e.name, "passthrough")]
                    return [Origin(inp.node, e.name, "joined")]
        return list(self.origins.get(e.name, [Origin(self.tm.name, e.name, "derived")]))

    # -- expressions ---------------------------------------------------
    def infer(self, e: ast.Node) -> Inf:
        if isinstance(e, ast.Literal):
            v = e.value
            if isinstance(v, bool):
                return Inf(BOOL, False)
            if isinstance(v, int):
                return Inf(INT64, False)
            if isinstance(v, float):
                return Inf(FLOAT64, False)
            if v is None:
                return Inf(UNKNOWN, True)
            return Inf(STRING, False)
        if isinstance(e, ast.ColumnRef):
            col = self.lookup(e)
            if e.qualifier:
                for inp in self.inputs:
                    if inp.alias == e.qualifier:
                        self.tm.reads.add((inp.node, e.name))
            else:
                owner = self.own.get(e.name)
                if owner is not None:
                    self.tm.reads.add((owner, e.name))
            return Inf(col.t, col.nullable)
        if isinstance(e, ast.UnOp):
            inner = self.infer(e.operand)
            t = BOOL if e.op == "not" else inner.t
            return Inf(t, inner.nullable)
        if isinstance(e, ast.BinOp):
            return infer_binary(e.op, self.infer(e.left), self.infer(e.right), e.span)
        if isinstance(e, ast.Call):
            return self.infer_call(e)
        raise err("E055", f"unsupported expression {type(e).__name__}", e.span)

    def infer_call(self, e: ast.Call) -> Inf:
        name = e.name
        if name in AGGREGATES:
            if not self.in_group:
                raise err("E056", f"aggregate {name}() only allowed inside group body", e.span)
            if not e.args:
                raise err("E057", f"{name}() requires an argument", e.span)
            return AGG_TYPE[name](self.infer(e.args[0]))
        if name == "coalesce":
            infs = [self.infer(a) for a in e.args]
            t = infs[0].t if infs else UNKNOWN
            for i in infs[1:]:
                t = unify(t, i.t)
                if t.name == "unknown":
                    raise err("E058", f"coalesce type mismatch {unify(t, i.t)}", e.span)
            return Inf(t, all(i.nullable for i in infs))
        if name in ("upper", "lower"):
            return Inf(STRING, self.infer(e.args[0]).nullable)
        if name == "cast":
            a = self.infer(e.args[0])
            spec = str(e.args[1].value) if len(e.args) > 1 and isinstance(e.args[1], ast.Literal) else "string"
            return Inf(type_from_spec(spec, []), a.nullable)
        raise err("E059", f"unknown function {name!r}", e.span)

    # -- statements -----------------------------------------------------
    def stmt(self, s: ast.Stmt):
        if isinstance(s, ast.FromStmt):
            self.do_from(s)
        elif isinstance(s, ast.JoinStmt):
            self.do_join(s)
        elif isinstance(s, ast.FilterStmt):
            if self.in_group:
                self.tm.plan.having.append(s.cond)
            else:
                self.tm.plan.preds.append(s.cond)
            self.infer(s.cond)
        elif isinstance(s, ast.LetStmt):
            self.do_let(s)
        elif isinstance(s, ast.DeriveStmt):
            for a in s.assigns:
                self.do_output(a)
        elif isinstance(s, ast.AggregateStmt):
            for a in s.assigns:
                self.do_output(a)
        elif isinstance(s, ast.GroupStmt):
            self.do_group(s)
        elif isinstance(s, ast.SortStmt):
            for e, desc in s.keys:
                self.infer(e)
                self.tm.plan.sorts.append((e, desc))
        elif isinstance(s, ast.TakeStmt):
            self.tm.plan.limit = (s.start, s.end)
        elif isinstance(s, ast.SelectStmt):
            for a in s.assigns:
                self.do_output(a)
        else:
            raise err("E060", f"unsupported statement {type(s).__name__}", s.span)

    def do_from(self, s: ast.FromStmt):
        cols, is_src, node = self.c.p.input_schema(s.table)
        inp = InputSpec(alias=s.table, node=node, is_source=is_src,
                        cols=OrderedDict((k, c.clone()) for k, c in cols.items()))
        self.inputs.append(inp)
        self.tm.plan.inputs.append(inp)
        for name, col in inp.cols.items():
            self.cols[name] = col
            self.own[name] = node
            self.origins[name] = [Origin(node, name, "passthrough")]
            self.base_cols.append(BaseCol(name=name, expr=None))

    def do_join(self, s: ast.JoinStmt):
        idx = len(self.inputs)
        cols, is_src, node = self.c.p.input_schema(s.table)
        inp = InputSpec(alias=s.table, node=node, is_source=is_src,
                        cols=OrderedDict((k, c.clone()) for k, c in cols.items()))
        self.inputs.append(inp)
        js = JoinSpec(index=idx, alias=s.table, node=node, kind=s.kind, on=s.on)
        self.tm.plan.inputs.append(inp)
        self.tm.plan.joins.append(js)
        self.infer(s.on)
        for name, col in inp.cols.items():
            key = f"__j{idx}_{name}"
            self.cols[key] = col
            self.own[key] = node
            self.origins[key] = [Origin(node, name, "joined")]
            self.base_cols.append(BaseCol(name=key, expr=None))

    def do_let(self, s: ast.LetStmt):
        inf = self.infer(s.expr)
        self.cols[s.name] = Col(name=s.name, t=inf.t, nullable=inf.nullable)
        self.own[s.name] = self.tm.name
        self.origins[s.name] = self._origin_of_expr(s.expr)
        self.base_cols.append(BaseCol(name=s.name, expr=s.expr))

    def do_output(self, a: ast.OutAssign):
        inf = self.infer(a.expr)
        col = Col(name=a.name, t=inf.t, nullable=inf.nullable)
        if self.in_group:
            if isinstance(a.expr, ast.ColumnRef) and a.expr.qualifier is None \
                    and a.expr.name in self.group_keys:
                self.outputs.append(PlanOut(name=a.name, expr=a.expr, group_key=True))
                self.cols[a.name] = self.cols[a.expr.name].clone(name=a.name)
                self.origins[a.name] = [Origin(o.node, o.col, "grouped")
                                        for o in self.origins.get(a.expr.name, [])]
                return
            if not (isinstance(a.expr, ast.Call) and a.expr.name in AGGREGATES):
                raise err("E050", f"output {a.name!r} in group body must be an aggregate "
                                  f"or reference a group key", a.span)
            self.outputs.append(PlanOut(name=a.name, expr=a.expr))
        else:
            self.outputs.append(PlanOut(name=a.name, expr=a.expr))
        self.cols[a.name] = col
        self.origins[a.name] = self._origin_of_expr(a.expr)

    def _origin_of_expr(self, e: ast.Node) -> List[Origin]:
        if isinstance(e, ast.ColumnRef):
            return self.origin_of(e)
        if isinstance(e, ast.Call):
            kind = "aggregated" if e.name in AGGREGATES else "derived"
            out: List[Origin] = []
            for a in e.args:
                out.extend(self._origin_of_expr(a))
            if not out:
                out = [Origin(self.tm.name, f"<{e.name}>", "derived")]
            return [Origin(o.node, o.col, kind) for o in out]
        if isinstance(e, ast.BinOp):
            return self._origin_of_expr(e.left) + self._origin_of_expr(e.right)
        if isinstance(e, ast.UnOp):
            return self._origin_of_expr(e.operand)
        return []

    def do_group(self, s: ast.GroupStmt):
        self.in_group = True
        for k in s.keys:
            inf = self.infer(k)
            if isinstance(k, ast.ColumnRef):
                name = k.name
            else:
                name = f"g{len(self.group_keys)}"
            self.group_keys.add(name)
            self.outputs.append(PlanOut(name=name, expr=k, group_key=True))
            self.cols[name] = Col(name=name, t=inf.t, nullable=inf.nullable)
            if isinstance(k, ast.ColumnRef):
                self.origins[name] = [Origin(o.node, o.col, "grouped")
                                      for o in self._origin_of_expr(k)]
            else:
                self.origins[name] = [Origin(self.tm.name, name, "grouped")]
            self.tm.plan.group_exprs.append(k)
        for b in s.body:
            self.stmt(b)
        self.in_group = False

    # -- finalization -----------------------------------------------------
    def finish(self):
        plan = self.tm.plan
        plan.base_cols = list(self.base_cols)
        plan.grouped = bool(self.group_keys)
        if self.outputs:
            plan.outputs = list(self.outputs)
        else:
            plan.outputs = [PlanOut(name=n, expr=ast.ColumnRef(name=n)) for n in self.cols]
        for name, col in self.cols.items():
            if any(o.name == name for o in plan.outputs):
                self.tm.schema[name] = col
                self.tm.lineage[name] = list(self.origins.get(name, []))
        if plan.grouped and len(plan.outputs) < len(self.cols):
            pass  # passthrough cols after group not auto-added (keys enforce SQL grouping)
        self.verify_contract()
        self.fingerprint()

    def verify_contract(self):
        model = self.tm
        if not model.contract:
            return
        cd = self.c.p.contracts.get(model.contract)
        if cd is None:
            raise err("E061", f"unknown contract {model.contract!r}")
        for f in cd.fields:
            col = self.cols.get(f.name)
            exp = contract_field_col(f)
            if col is None:
                raise err("E010", f"model {model.name} missing contract column {f.name!r}")
            if not types_compat(exp.t, col.t):
                raise err("E011", f"{model.name}.{f.name}: contract {exp.t} but inferred {col.t}")
            if not exp.nullable and col.nullable:
                raise err("E012", f"{model.name}.{f.name}: contract nonnull but value nullable")
            if (exp.enum or exp.classification) and col.t != STRING:
                raise err("E013", f"{model.name}.{f.name}: contract enum/classification requires string, got {col.t}")

    def fingerprint(self):
        # Canonical fingerprint: AST-shape, not source-whitespace. `str(decl)`
        # embeds raw spans/whitespace, so `fmt` (which only re-emits the same
        # AST) would spuriously mark everything stale. Canonicalize instead.
        from . import fmt as _fmt
        text = _fmt.format_module(__import__("strata.ast", fromlist=["Module"]).Module(
            path="<fp>", decls=[self.decl]))
        parts = [self.decl.name, self.decl.contract or "", re.sub(r"\s+", " ", text)]
        for d in self.tm.deps:
            up = self.c.p.typed.get(d)
            if up:
                parts.append(up.fingerprint)
        self.tm.fingerprint = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ blast radius

def build_down_edges(tms: Dict[str, TypedModel]) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """(upstream_node, col) -> [(model, output_col)]"""
    down: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for m, tm in tms.items():
        for out_name, origins in tm.lineage.items():
            for o in origins:
                down.setdefault(o.key(), []).append((m, out_name))
    for key in down:
        down[key] = sorted(set(down[key]))
    return down


def blast_radius(tms: Dict[str, TypedModel], changes: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    down = build_down_edges(tms)
    seen, stack = set(), list(changes)
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        for child in down.get(c, []):
            stack.append(child)
    return [c for c in seen]