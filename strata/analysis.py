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
from . import functions
from .types import (
    StrataType, Inf, INT64, FLOAT64, STRING, BOOL, DATE, TIMESTAMP, UUID, JSON,
    UNKNOWN, decimal, money, array, binary_type, unify, Col,
)


class StrataError(Exception):
    def __init__(self, msg, code="E099", span=None, file=None,
                 severity="error", help=None):
        super().__init__(msg)
        self.code = code
        self.span = span
        self.file = file
        self.severity = severity
        self.help = help


def err(code: str, msg: str, span=None, file=None,
        severity="error", help=None):
    """Build a StrataError with a code, optional span/file, severity and help."""
    return StrataError(msg, code=code, span=span, file=file,
                       severity=severity, help=help)


# Function classification and signatures live in functions.py: one declaration,
# read by both the typechecker (below) and the code generator (sqlgen).
AGGREGATES = functions.AGGREGATES


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
class Origin:
    node: str
    col: str
    kind: str = "passthrough"

    def key(self):
        """Identity key (node, col) used as the edge label in lineage maps."""
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
    # Cardinality expectation (None | "many_to_one" | "one_to_one") with the
    # equi-join key columns per side, extracted at check time; enforced at
    # materialize time by counting duplicate key groups on the upstream tables.
    expect: Optional[str] = None
    left_keys: List[str] = field(default_factory=list)
    right_keys: List[str] = field(default_factory=list)


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
    partition_by: List[Node] = field(default_factory=list)
    freshness: Optional[List[str]] = None  # e.g. ['incremental'], ['1h', 'daily']
    freshness_column: Optional[str] = None  # event-time column for freshness check
    # Incremental model configuration
    incremental: bool = False  # True if this is an incremental model
    merge_keys: List[Node] = field(default_factory=list)  # Keys for upsert/merge
    merge_strategy: Optional[str] = None  # 'upsert', 'append', 'replace'
    cdc_column: Optional[str] = None  # Change Data Capture column
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
    # id(ast.Call) -> StrataType of the temporal base argument, filled by the
    # date-function typing path: sqlgen needs it to know where DATE must be
    # preserved (DuckDB/Postgres promote to TIMESTAMP on month/year math).
    date_arg_types: Dict[int, StrataType] = field(default_factory=dict)
    # Container type for collection calls; Snowflake GET needs an element cast.
    collection_arg_types: Dict[int, StrataType] = field(default_factory=dict)
    # One expand per model: (source_col, output_col, element_type_name) of the
    # lateral array unnest that runs in the base subquery.
    expand: Optional[Tuple[str, str, str]] = None
    # Set operation combining the current rows with a same-shaped model:
    # (op, all, right_node). Statements before it shape the left branch;
    # setop_base_split/setop_pred_split record how many base_cols/preds
    # belong to the left branch at union time.
    set_op: Optional[Tuple[str, bool, str]] = None
    setop_base_split: int = 0
    setop_pred_split: int = 0
    # Union branch column types, in positional order: (name, left_type,
    # right_type, unified_type) for the casts in both branches.
    setop_cols: List[Tuple[str, "StrataType", "StrataType", "StrataType"]] = field(default_factory=list)
    # Full-row duplicate elimination (SELECT DISTINCT over the final rows).
    distinct: bool = False


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


def type_from_spec(spec: str, params: List[object],
                   domains: Optional[Dict[str, StrataType]] = None) -> StrataType:
    """Resolve a declared type: builtins (array elements recurse to any depth
    over scalars, parameterized types and nested arrays), or a bare `domain`
    alias looked up in the project's pre-resolved domain table."""
    if spec in TYPE_FROM_KW:
        return TYPE_FROM_KW[spec]
    if spec == "decimal":
        return decimal(params[0], params[1])
    if spec == "money":
        return money(params[0] if params else "USD")
    if spec == "array":
        if len(params) != 1:
            raise err("E063", "array() takes exactly one element type")
        return array(_elem_type(params[0], domains))
    if domains is not None and spec in domains:
        return domains[spec]
    return UNKNOWN


def _elem_type(p: object, domains: Optional[Dict[str, StrataType]]) -> StrataType:
    # Array element params: bare names stay bare strings (scalars, or domain
    # aliases resolved here); parameterized or nested elements are
    # (spec, subparams) tuples resolved recursively.
    if isinstance(p, tuple):
        t = type_from_spec(p[0], p[1], domains)
        if t.name == "unknown":
            raise err("E063", f"array elements must be a supported scalar, "
                              f"parameterized or nested array type, or a declared "
                              f"domain (got {p[0]!r})")
        return t
    if p in TYPE_FROM_KW:
        return TYPE_FROM_KW[p]
    if p == "money":
        # money() without parens means money(USD), like the top level
        return money("USD")
    if domains is not None and p in domains:
        return domains[p]
    raise err("E063", f"array elements must be a supported scalar, parameterized "
                      f"or nested array type, or a declared domain (got {p!r})")


def contract_field_col(f: ast.ContractField,
                       domains: Optional[Dict[str, StrataType]] = None) -> Col:
    """Resolve a ContractField into a typed Col (resolving domain aliases, E078 on unknowns)."""
    t = type_from_spec(f.type_spec, f.params, domains)
    if t.name == "unknown":
        raise err("E078", f"unknown type {f.type_spec!r} for column {f.name!r} "
                          f"(declare it with `domain {f.type_spec} = <type>`)")
    return Col(
        name=f.name,
        t=t,
        nullable=not f.nonnull,
        unique=f.unique,
        primary=f.primary,
        protected=f.protected,
        enum=frozenset(f.enum),
        classification=f.classification,
    )


def source_decl_cols(decl: ast.SourceDecl,
                     domains: Optional[Dict[str, StrataType]] = None) -> List[Col]:
    """Column list declared by a source declaration (`columns: {...}`), empty when absent."""
    for kind, val in decl.props:
        if kind == "columns":
            return [contract_field_col(f, domains) for f in val]
    return []


# ------------------------------------------------------------------ fn evaluation (definition domain)

class FnEvaluator:
    def __init__(self, project: "Project"):
        self.project = project

    def call(self, decl: ast.FnDecl, args: List[object]) -> object:
        """Evaluate a fn call with the given argument values."""
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
            raise self._err("F040", f"unknown identifier {e.name!r} in fn body", e.span)
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
            raise self._err("F041", f"unknown fn call {e.name!r}", e.span)
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
        raise self._err("F042", f"unsupported expression in fn body: {type(e).__name__}", e.span)

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
        if isinstance(e, ast.Kwarg):
            e.value = self._subst_expr(e.value, env)
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
        self.domains: Dict[str, ast.DomainDecl] = {}
        self.domain_types: Dict[str, StrataType] = {}
        self.fns: Dict[str, ast.FnDecl] = {}
        self.pipelines: List[ast.PipelineDecl] = []
        self.tests: Dict[str, List[ast.TestDecl]] = {}
        self.typed: Dict[str, TypedModel] = {}
        self.modules: Dict[str, ast.Module] = {module.path or "<strata>": module}
        self.imports: List[str] = []
        self._resolve()
        self._resolve_domains()
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
        elif isinstance(d, ast.DomainDecl):
            self.domains.setdefault(d.name, d)
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

    def _resolve_domains(self):
        """Pre-resolve `domain` aliases to StrataTypes (fail fast on cycles
        and unknown names, even when the alias is never used)."""
        visiting: Set[str] = set()

        def expand(spec: str, params: List[object]) -> StrataType:
            if spec in TYPE_FROM_KW:
                return TYPE_FROM_KW[spec]
            if spec == "decimal":
                return decimal(params[0], params[1])
            if spec == "money":
                return money(params[0] if params else "USD")
            if spec == "array":
                if len(params) != 1:
                    raise err("E063", "array() takes exactly one element type")
                p = params[0]
                if isinstance(p, tuple):
                    return array(expand(p[0], p[1]))
                if p in TYPE_FROM_KW:
                    return array(TYPE_FROM_KW[p])
                if p == "money":
                    return array(money("USD"))
                return array(rec(p))
            return rec(spec)

        def rec(name: str) -> StrataType:
            if name in self.domain_types:
                return self.domain_types[name]
            if name in visiting:
                raise err("E078", f"domain cycle involving {name!r}")
            d = self.domains.get(name)
            if d is None:
                raise err("E078", f"unknown domain {name!r} "
                                  f"(declare it with `domain {name} = <type>`)")
            visiting.add(name)
            t = expand(d.type_spec, d.params)
            visiting.discard(name)
            self.domain_types[name] = t
            return t

        for name in list(self.domains):
            rec(name)

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
        """Ordered column schema of a source declaration by name (E020/E021 when unknown/empty)."""
        decl = self.sources.get(name)
        if decl is None:
            raise err("E020", f"unknown source {name!r}")
        cols = source_decl_cols(decl, self.domain_types)
        if not cols:
            raise err("E021", f"source {name!r} has no declared columns (add columns: {{...}})")
        return OrderedDict((c.name, c) for c in cols)

    def input_schema(self, name: str) -> Tuple["OrderedDict[str, Col]", bool, str]:
        """(cols, is_source, node) where the input name is a source or an already-typed model."""
        if name in self.sources:
            return self.source_schema(name), True, name
        if name in self.models:
            tm = self.typed.get(name)
            if tm is None:
                raise err("E024", f"model {name!r} not yet typechecked (dependency order?)")
            return tm.schema, False, name
        raise err("E020", f"unknown input {name!r} (not a source or model)")

    def model_names_for(self, pipeline: Optional[ast.PipelineDecl], include_generated=False) -> List[str]:
        """Names of the models a pipeline selects (or all non-generated models when None)."""
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
        """Pipeline declaration by name, or the first one when name is None."""
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
    """Whether a value of type `got` satisfies a contract type `exp` (widening: int64 into any numeric)."""
    if got.name == "unknown":
        return False
    if exp == got:
        return True
    # narrowing never implicit; widening int -> float/decimal allowed
    if exp.is_numeric() and got.name == "int64":
        return True
    return False


def infer_binary(op: str, lt: Inf, rt: Inf, span=None) -> Inf:
    """Infer the result type of a binary operator, raising E05x on invalid combinations."""
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
        self.file = project.module.path or "<strata>"

    def _err(self, code: str, msg: str, span=None, help: str = None):
        raise err(code, msg, span=span, file=self.file, help=help)

    def check_all(self, model_names: Optional[List[str]] = None) -> Dict[str, TypedModel]:
        """Typecheck every model in topological order and return the typed graph."""
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
                    raise self._err("E091",
                                    f"test references non-existent model {mname!r}",
                                    td.span, help="declare the model or check the test name")
                tm = self.p.typed[mname]
                for c in td.checks:
                    if c.kind == "row_count":
                        continue
                    if c.col and c.col not in tm.schema:
                        raise self._err("E092",
                                        f"test references unknown column {c.col!r} in model {mname!r}",
                                        c.span, help="check the column name and spelling")
                    if c.op not in ("==", "!=", "<", ">", "<=", ">="):
                        raise self._err("E093",
                                        f"test uses unsupported operator {c.op!r}",
                                        c.span, help="use ==, !=, <, >, <= or >=")
                    if c.col is None:
                        continue
                    col = tm.schema[c.col]
                    # literal value must be comparable to column type
                    lit_type = _literal_type(c.value)
                    if lit_type != col.t:
                        raise self._err("E094",
                                          f"test value {c.value!r} has type {lit_type.name} "
                                          f"but column {mname}.{c.col} is {col.t.name}",
                                          c.span,
                                          help="check the literal type matches the column type")
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
            elif isinstance(s, ast.SetOpStmt):
                deps.append(s.table)
        return deps

    def _check_model(self, decl: ast.ModelDecl) -> TypedModel:
        return _ModelState(decl, self).run()


class _ModelState:
    def __init__(self, decl: ast.ModelDecl, checker: Checker):
        self.decl = decl
        self.c = checker
        self.file = checker.p.module.path or "<strata>"
        tm = TypedModel(name=decl.name, contract=decl.contract, attrs=dict(decl.attrs),
                        deps=[d for d in checker._deps(decl)])
        tm.plan = Plan()
        tm.plan.partition_by = decl.partition_by
        tm.plan.freshness = decl.freshness
        tm.plan.freshness_column = decl.freshness_column
        tm.plan.incremental = decl.incremental
        tm.plan.merge_keys = decl.merge_keys
        tm.plan.merge_strategy = decl.merge_strategy
        tm.plan.cdc_column = decl.cdc_column
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

    def _err(self, code: str, msg: str, span=None, help: str = None):
        raise err(code, msg, span=span, file=self.file, help=help)

    def run(self) -> TypedModel:
        """Typecheck the model body and finalize its TypedModel."""
        for s in self.decl.stmts:
            self.stmt(s)
        self.finish()
        return self.tm

    # -- column lookups ----------------------------------------------
    def lookup(self, e: ast.ColumnRef) -> Col:
        """Resolve a column reference to its Col, raising E040/E041 when unknown."""
        if e.qualifier:
            for inp in self.inputs:
                if inp.alias == e.qualifier:
                    col = inp.cols.get(e.name)
                    if col is None:
                        raise self._err("E040", f"no column {e.name!r} in input {e.qualifier!r}", e.span)
                    return col
            raise self._err("E041", f"unknown input qualifier {e.qualifier!r}", e.span)
        col = self.cols.get(e.name)
        if col is None:
            where = "group output" if self.in_group else "input columns"
            raise self._err("E040", f"unknown column {e.name!r} in {where}", e.span)
        return col

    def origin_of(self, e: ast.ColumnRef) -> List[Origin]:
        """Lineage [Origin] entries backing a column reference."""
        if e.qualifier:
            for i, inp in enumerate(self.inputs):
                if inp.alias == e.qualifier:
                    if i == 0:
                        return [Origin(inp.node, e.name, "passthrough")]
                    return [Origin(inp.node, e.name, "joined")]
        return list(self.origins.get(e.name, [Origin(self.tm.name, e.name, "derived")]))

    # -- expressions ---------------------------------------------------
    def infer(self, e: ast.Node) -> Inf:
        """Infer the type/nullability of an expression, raising on ill-typed expressions."""
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
            return self.infer_call(e, window_allowed=True)
        if isinstance(e, ast.WindowCall):
            return self.infer_window(e)
        if isinstance(e, ast.Kwarg):
            raise self._err(functions.E_DATE_ARG,
                      "keyword arguments are only allowed as date_add/date_sub units", e.span)
        if isinstance(e, ast.Star):
            raise self._err(functions.E_STRAY_STAR, "'*' is only valid as count(*)", e.span)
        raise self._err("E055", f"unsupported expression {type(e).__name__}", e.span)

    def infer_window(self, e: ast.WindowCall) -> Inf:
        """Typecheck a window call against the catalog, including partition/sort keys."""
        fn = functions.get(e.name)
        if fn is None:
            raise self._err("E059", f"unknown function {e.name!r}", e.span)
        if not fn.window:
            raise self._err(functions.E_WINDOW_PLACEMENT,
                      f"{e.name}() is not a window function: it takes no over(...)", e.span)
        if fn.aggregate and self.in_group:
            # An aggregate already collapses the group; a window over it would
            # stack two reductions on the same column — write the aggregate in
            # an upstream model and window over that model instead.
            raise self._err(functions.E_WINDOW_PLACEMENT,
                      f"aggregate {e.name}() cannot take over(...) inside a group body", e.span)
        star = [a for a in e.args if isinstance(a, ast.Star)]
        if star:
            raise self._err(functions.E_STRAY_STAR,
                      f"'*' is only valid as count(*), not in {e.name}()", e.span)
        for sub in e.args:
            self._reject_nested_window(sub, e.span)
        for part in e.over.partition_by:
            self._reject_nested_window(part, e.span)
            self.infer(part)
        for key, _desc in e.over.sort:
            self._reject_nested_window(key, e.span)
            self.infer(key)
        args = [self.infer(a) for a in e.args]
        problem = functions.check(fn, args)
        if problem is not None:
            code, msg = problem
            raise self._err(code, msg, e.span)
        return fn.ret(args)

    def _reject_nested_window(self, e: ast.Node, span):
        if isinstance(e, ast.WindowCall):
            raise self._err(functions.E_WINDOW_PLACEMENT, "a window cannot appear inside a window", span)
        if isinstance(e, ast.Call):
            for a in e.args:
                self._reject_nested_window(a, span)
        elif isinstance(e, (ast.BinOp,)):
            self._reject_nested_window(e.left, span)
            self._reject_nested_window(e.right, span)
        elif isinstance(e, ast.Kwarg):
            self._reject_nested_window(e.value, span)
        elif isinstance(e, ast.UnOp):
            self._reject_nested_window(e.operand, span)

    def infer_call(self, e: ast.Call, window_allowed: bool = False) -> Inf:
        """Typecheck a function call (casts, date and json functions get special handling)."""
        name = e.name
        if name == "cast" and any(isinstance(a, ast.Kwarg) for a in e.args):
            raise self._err(functions.E_DATE_ARG, "cast() does not accept keyword arguments", e.span)
        if name == "cast":
            # cast() takes a type name (not an expression) as its second
            # argument, so it stays a language construct rather than a catalog
            # entry; its arity is checked here.
            if len(e.args) != 2:
                raise self._err("E062", "cast() takes exactly 2 arguments", e.span)
            a = self.infer(e.args[0])
            spec = str(e.args[1].value) if isinstance(e.args[1], ast.Literal) else "string"
            return Inf(type_from_spec(spec, [], self.c.p.domain_types), a.nullable)
        fn = functions.get(name)
        if fn is None:
            raise self._err("E059", f"unknown function {name!r}", e.span)
        if fn.aggregate and not self.in_group:
            raise self._err("E056", f"aggregate {name}() only allowed inside group body", e.span)
        # Star is not an expression: reject a stray one before inferring args.
        star = [a for a in e.args if isinstance(a, ast.Star)]
        if star and not fn.accepts_star:
            raise self._err(functions.E_STRAY_STAR,
                      f"'*' is only valid as count(*), not in {name}()", e.span)
        if name in ("date_add", "date_sub", "date_trunc", "date_diff"):
            return self.infer_date_call(e, fn)
        args = [Inf(INT64, False)] if star else [self.infer(a) for a in e.args]
        problem = functions.check(fn, args, has_star=bool(star))
        if problem is not None:
            code, msg = problem
            raise self._err(code, msg, e.span)
        if fn.collection:
            # array_prepend's array is the second argument; json_build has no
            # array base (its type is the return type).  Everything else uses
            # args[0] as the collection base.
            if name == "array_construct":
                base_t = fn.ret(args).t
            elif name == "json_build":
                base_t = fn.ret(args).t
            elif name == "array_agg":
                base_t = fn.ret(args).t
            elif name == "array_prepend":
                base_t = args[1].t
            else:
                base_t = args[0].t
            self.tm.plan.collection_arg_types[id(e)] = base_t
        if name in ("json_get", "json_value"):
            # A literal key must be a simple ASCII member name: it compiles to a
            # `$.key` path (DuckDB, BigQuery) or a quoted member (PostgreSQL,
            # Snowflake). A non-literal key is a runtime string and is emitted as
            # an exact-key lookup only where the dialect can express one; proving
            # it is a string is the job of the argument kind checked above.
            key = e.args[1]
            if isinstance(key, ast.Literal) and not functions.valid_json_key(key.value):
                raise self._err(functions.E_JSON_KEY,
                          f'{name}() literal key must match [A-Za-z_][A-Za-z0-9_]*; '
                          'use json_path() for path expressions',
                          key.span)
        if name == "json_path":
            # The path is checked here so an unusable one fails during
            # compilation with a span instead of during codegen;
            # `functions.json_path_problem` is the single definition shared with
            # the generator.
            path = e.args[1]
            if not isinstance(path, ast.Literal) or not isinstance(path.value, str):
                raise self._err(functions.E_JSON_KEY,
                          'json_path() requires a string literal path', path.span)
            problem = functions.json_path_problem(path.value)
            if problem is not None:
                raise self._err(functions.E_JSON_KEY, f'json_path(): {problem}', path.span)
        if name == "json_build":
            for i in range(0, len(e.args), 2):
                key = e.args[i]
                if not isinstance(key, ast.Literal) or not isinstance(key.value, str) \
                        or not functions.valid_json_key(key.value):
                    raise self._err(functions.E_JSON_KEY,
                              f"json_build() key at position {i + 1} must be a "
                              "simple ASCII identifier literal",
                              key.span)
        return fn.ret(args)

    # -- date functions (date_add/date_sub/date_trunc/date_diff) -----------
    # A date call carries a *symbolic unit* (kwarg name or unit literal) plus
    # a base argument whose type decides the SQL shape per dialect, so it is
    # typed apart from plain-argument functions: same arity/kind rules as the
    # catalog (functions.check), the unit checked against the function's
    # unit_names, and the base argument's type recorded on the Plan so codegen
    # knows where DATE must be preserved across dialects that promote to
    # TIMESTAMP with month/year arithmetic (DuckDB/Postgres).

    def infer_date_call(self, e: ast.Call, fn: "functions.Fn") -> Inf:
        # Check arity before indexing; symbolic units never resolve as columns.
        """Typecheck a date_add/date_sub/date_trunc/date_diff call with its symbolic unit."""
        if len(e.args) != fn.min_args:
            raise self._err(functions.E_ARITY,
                      f"{fn.name}() takes exactly {fn.min_args} arguments", e.span)
        base = self.infer(e.args[0])
        if base.t not in (DATE, TIMESTAMP):
            raise self._err(functions.E_ARG_TYPE,
                      f"{fn.name}() argument 1 must be date or timestamp, got {base.t}", e.span)
        if fn.name in ("date_add", "date_sub"):
            kw = e.args[1]
            if not isinstance(kw, ast.Kwarg):
                raise self._err(functions.E_DATE_ARG,
                          f"{fn.name}() requires a unit kwarg, e.g. days: 1", e.span)
            unit = kw.name
            args = [base, self.infer(kw.value)]
        else:
            unit_arg = e.args[-1]
            if not isinstance(unit_arg, ast.Literal) or not isinstance(unit_arg.value, str):
                raise self._err(functions.E_DATE_ARG,
                          f"{fn.name}() requires a symbolic unit or string literal", e.span)
            unit = unit_arg.value
            args = [base]
            if fn.name == "date_diff":
                second = self.infer(e.args[1])
                if base.t != second.t:
                    raise self._err(functions.E_DATE_TYPE,
                              "date_diff() arguments must share one temporal type", e.span)
                args.append(second)
            args.append(Inf(STRING, False))
        problem = functions.check_date_call(fn, unit) or functions.check(fn, args)
        if problem is not None:
            code, msg = problem
            raise self._err(code, msg, e.span)
        self.tm.plan.date_arg_types[id(e)] = base.t
        return fn.ret(args)

    # -- statements -----------------------------------------------------
    def stmt(self, s: ast.Stmt):
        """Dispatch one model-body statement to its handler."""
        if isinstance(s, ast.FromStmt):
            self.do_from(s)
        elif isinstance(s, ast.JoinStmt):
            self.do_join(s)
        elif isinstance(s, ast.FilterStmt):
            self._require_no_window(s.cond, s.span,
                                    "having" if self.in_group else "filter")
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
                self._require_no_window(e, s.span, "sort")
                self.infer(e)
                self.tm.plan.sorts.append((e, desc))
        elif isinstance(s, ast.TakeStmt):
            self.tm.plan.limit = (s.start, s.end)
        elif isinstance(s, ast.ExpandStmt):
            self.do_expand(s)
        elif isinstance(s, ast.SetOpStmt):
            self.do_setop(s)
        elif isinstance(s, ast.DedupStmt):
            self.tm.plan.distinct = True
        elif isinstance(s, ast.SelectStmt):
            for a in s.assigns:
                self.do_output(a)
        else:
            raise self._err("E060", f"unsupported statement {type(s).__name__}", s.span)

    def do_from(self, s: ast.FromStmt):
        """Register the from input: columns, ownership and passthrough lineage."""
        if self.tm.plan.set_op is not None:
            raise self._err("E076", "a set model combines exactly one from with one "
                              "named model; chain further inputs downstream", s.span)
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
        """Register a join input and extract expect-cardinality keys when annotated."""
        if self.tm.plan.set_op is not None:
            raise self._err("E076", f"{s.kind} join after a set operation is not supported; "
                              "join the combined rows in a downstream model", s.span)
        idx = len(self.inputs)
        cols, is_src, node = self.c.p.input_schema(s.table)
        inp = InputSpec(alias=s.table, node=node, is_source=is_src,
                        cols=OrderedDict((k, c.clone()) for k, c in cols.items()))
        self.inputs.append(inp)
        js = JoinSpec(index=idx, alias=s.table, node=node, kind=s.kind, on=s.on)
        self.tm.plan.inputs.append(inp)
        self.tm.plan.joins.append(js)
        self._require_no_window(s.on, s.span, "join condition")
        self.infer(s.on)
        if s.expect is not None:
            js.expect, js.left_keys, js.right_keys = self._join_cardinality(s, inp)
        for name, col in inp.cols.items():
            key = f"__j{idx}_{name}"
            self.cols[key] = col
            self.own[key] = node
            self.origins[key] = [Origin(node, name, "joined")]
            self.base_cols.append(BaseCol(name=key, expr=None))

    def _join_cardinality(self, s: ast.JoinStmt, inp: InputSpec):
        """Validate an `expect many_to_one|one_to_one` annotation and extract
        the equi-join key columns per side for the materialize-time check.

        many_to_one needs keys on the right side only (their uniqueness bounds
        every left row to at most one match); one_to_one needs at least one
        key pair relating a left column to a right column. Conjuncts that only
        filter left rows are safely ignored (AND-semantics: they remove
        matches, never create them); anything touching the right table outside
        a clean `right_col == <non-right-expr>` equi-pair fails loudly, since
        the upstream uniqueness probe cannot cover it.
        """
        if s.kind in ("anti", "semi"):
            raise self._err("E079", f"expect {s.expect} does not apply to a {s.kind} "
                              f"join (it never multiplies rows)", s.span)
        left_alias = self.inputs[0].alias
        true_pairs: List[Tuple[str, str]] = []
        right_only: List[str] = []

        def l_plain(e):
            if not isinstance(e, ast.ColumnRef):
                return None
            if e.qualifier:
                return e.name if e.qualifier == left_alias else None
            if e.name in self.inputs[0].cols and \
                    self.cols.get(e.name) is self.inputs[0].cols.get(e.name):
                return e.name
            return None

        def r_plain(e):
            if isinstance(e, ast.ColumnRef) and e.qualifier == inp.alias:
                return e.name
            return None

        def refs_right(e) -> bool:
            if isinstance(e, ast.ColumnRef):
                return e.qualifier == inp.alias
            if isinstance(e, ast.BinOp):
                return refs_right(e.left) or refs_right(e.right)
            if isinstance(e, ast.UnOp):
                return refs_right(e.operand)
            if isinstance(e, ast.Call):
                return any(refs_right(a) for a in e.args)
            if isinstance(e, ast.WindowCall):
                return any(refs_right(a) for a in e.args)
            if isinstance(e, ast.Kwarg):
                return refs_right(e.value)
            return False

        def walk(e):
            if isinstance(e, ast.BinOp) and e.op == "and":
                walk(e.left); walk(e.right); return
            if isinstance(e, ast.BinOp) and e.op == "==":
                ln, rn = l_plain(e.left), r_plain(e.right)
                if ln is not None and rn is not None:
                    true_pairs.append((ln, rn)); return
                ln, rn = l_plain(e.right), r_plain(e.left)
                if ln is not None and rn is not None:
                    true_pairs.append((ln, rn)); return
                rn = r_plain(e.left) or r_plain(e.right)
                if rn is not None:
                    other = e.right if r_plain(e.left) else e.left
                    if not refs_right(other):
                        right_only.append(rn); return
                if refs_right(e.left) or refs_right(e.right):
                    raise self._err("E079", f"expect {s.expect} needs the right side "
                                      f"referenced only through equi-join keys "
                                      f"(found an exotic condition)", s.span)
                return  # left-local filter or tautology: removes matches only
            if refs_right(e):
                raise self._err("E079", f"expect {s.expect} needs equi-join keys on plain "
                                  f"columns (top-level AND of col == col)", s.span)
            return  # left-local filter: ignore

        walk(s.on)

        def ordered(keys):
            out = []
            for k in keys:
                if k not in out:
                    out.append(k)
            return out

        left_keys = ordered([l for l, _ in true_pairs])
        right_keys = ordered([r for _, r in true_pairs] + right_only)
        if not right_keys:
            raise self._err("E079", f"expect {s.expect} needs at least one equi-join key "
                              f"on {inp.alias}", s.span)
        if s.expect == "one_to_one" and not true_pairs:
            raise self._err("E079", "expect one_to_one needs at least one equi-join key "
                              "pair relating a left column to a right column", s.span)
        return s.expect, left_keys, right_keys

    def do_let(self, s: ast.LetStmt):
        """Register a named let expression: infer, add col and lineage."""
        self._require_no_window(s.expr, s.span, "let")
        inf = self.infer(s.expr)
        self.cols[s.name] = Col(name=s.name, t=inf.t, nullable=inf.nullable)
        self.own[s.name] = self.tm.name
        self.origins[s.name] = self._origin_of_expr(s.expr)
        self.base_cols.append(BaseCol(name=s.name, expr=s.expr))

    def do_expand(self, s: ast.ExpandStmt):
        """One row per element of the primary input's typed array column.

        Expansion runs in the base (pre-aggregation) subquery as a lateral
        unnest, so it must come before any grouping, and the source must be a
        row-preserving column of the ``from`` table (joined columns already
        lost the row context; deriving over an array is a different shape).
        ``expand xs`` replaces ``xs`` with its nullable element column;
        ``expand xs as e`` keeps ``xs`` and adds ``e``.
        """
        if self.in_group:
            raise self._err("E075", "expand is only allowed before grouping, "
                              "not inside a group body", s.span)
        if self.tm.plan.expand is not None:
            raise self._err("E075", "only one expand per model (a second lateral "
                              "unnest would cross-multiply rows)", s.span)
        if self.tm.plan.set_op is not None:
            raise self._err("E076", "expand after a set operation is not supported; "
                              "expand a branch before combining, or the combined "
                              "rows in a downstream model", s.span)
        if not self.inputs:
            raise self._err("E075", "expand requires a from first", s.span)
        if s.name not in self.inputs[0].cols:
            raise self._err("E075", f"expand source {s.name!r} must be a column of "
                              "the from table", s.span)
        src = self.inputs[0].cols[s.name]
        if src.t.name != "array":
            raise self._err("E075", f"expand source {s.name!r} must be a typed array "
                              f"column, got {src.t}", s.span)
        elem = src.t.elem
        if elem is None or elem.name == "array":
            raise self._err("E075", f"expand source {s.name!r} must be a "
                              "one-dimensional array of scalar elements", s.span)
        if s.as_name in self.inputs[0].cols and s.as_name != s.name:
            raise self._err("E075", f"expand output {s.as_name!r} collides with an "
                              "existing column of the from table", s.span)
        self.tm.plan.expand = (s.name, s.as_name, elem.name)
        self.cols[s.as_name] = Col(name=s.as_name, t=elem, nullable=True)
        self.own[s.as_name] = self.tm.name
        self.origins[s.as_name] = [Origin(self.inputs[0].node, s.name, "expanded")]

    def do_setop(self, s: ast.SetOpStmt):
        """Combine the current rows with a same-shaped upstream model.

        Pipeline semantics: statements before the set-op shape the left
        branch (filters and lets apply there); statements after it see the
        combined rows. The union schema keeps the left column names in order
        with unified types and OR-ed nullability, so both SQL branch
        spellings (by-name DuckDB, by-position everywhere else) agree.
        """
        plan = self.tm.plan
        if plan.set_op is not None:
            raise self._err("E076", "only one set operation per model (chain them "
                              "through downstream models)", s.span)
        if not self.inputs:
            raise self._err("E076", f"{s.op} requires a from first", s.span)
        if plan.joins:
            raise self._err("E076", f"{s.op} combines single-table row sets; join "
                              "in a downstream model instead", s.span)
        if self.outputs or plan.sorts or plan.limit is not None or self.group_keys:
            raise self._err("E076", f"{s.op} must come before select/derive/aggregate/"
                              "group/sort/take (those see the combined rows)", s.span)
        cols, is_src, node = self.c.p.input_schema(s.table)
        if is_src:
            raise self._err("E076", f"{s.op} combines models, not sources; wrap "
                              f"{s.table!r} in a model first", s.span)
        names = list(self.cols)
        if list(cols) != names:
            raise self._err("E077", f"{s.op} {s.table!r} must carry the same columns "
                              f"in the same order (left {names}, "
                              f"right {list(cols)})", s.span)
        for n in names:
            lt, rt = self.cols[n].t, cols[n].t
            u = lt if lt == rt else unify(lt, rt)
            if u.name == "unknown" or (u.name == "money" and lt != rt):
                raise self._err("E077", f"{s.op} column {n!r} cannot align {lt} "
                                  f"with {rt}", s.span)
            self.cols[n] = Col(name=n, t=u, nullable=self.cols[n].nullable or cols[n].nullable)
            self.own[n] = self.tm.name
            self.origins[n] = list(self.origins.get(n, [])) + [Origin(node, n, "set")]
            self.tm.reads.add((self.inputs[0].node, n))
            self.tm.reads.add((node, n))
            plan.setop_cols.append((n, lt, rt, u))
        plan.set_op = (s.op, s.all, node)
        plan.setop_base_split = len(self.base_cols)
        plan.setop_pred_split = len(plan.preds)

    def _require_no_window(self, e: ast.Node, span, where: str):
        """Windows run after grouping in the outer query, so `let` (inner
        subquery), `filter`, group keys and `sort` must not contain them."""
        found = self._find_window(e)
        if found is not None:
            raise self._err(functions.E_WINDOW_PLACEMENT,
                      f"over(...) is only allowed in select/derive/aggregate "
                      f"outputs, not in {where} (found {found})", span)

    def _find_window(self, e: ast.Node) -> Optional[str]:
        if isinstance(e, ast.WindowCall):
            return e.name
        if isinstance(e, ast.Call):
            for a in e.args:
                hit = self._find_window(a)
                if hit:
                    return hit
        elif isinstance(e, ast.BinOp):
            return self._find_window(e.left) or self._find_window(e.right)
        elif isinstance(e, ast.Kwarg):
            return self._find_window(e.value)
        elif isinstance(e, ast.UnOp):
            return self._find_window(e.operand)
        return None

    def do_output(self, a: ast.OutAssign):
        """Handle a projection output assignment, enforcing group-body aggregate rules."""
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
                if isinstance(a.expr, ast.WindowCall):
                    # Aggregates already reduced the group here; window the
                    # upstream model's output instead (same rule as infer_window).
                    raise self._err(functions.E_WINDOW_PLACEMENT,
                              f"over(...) is not allowed inside a group body "
                              f"(found {a.expr.name}); window over an upstream model", a.span)
                raise self._err("E050", f"output {a.name!r} in group body must be an aggregate "
                                  f"or reference a group key", a.span)
            self.outputs.append(PlanOut(name=a.name, expr=a.expr))
        else:
            self.outputs.append(PlanOut(name=a.name, expr=a.expr))
        self.cols[a.name] = col
        self.origins[a.name] = self._origin_of_expr(a.expr)

    def _origin_of_expr(self, e: ast.Node) -> List[Origin]:
        if isinstance(e, ast.ColumnRef):
            return self.origin_of(e)
        if isinstance(e, ast.WindowCall):
            # Windows see one row per group (they run after GROUP BY), so the
            # windowed expression keeps its kind on the argument origins while
            # recording that a window produced them.
            kind = "windowed"
            out: List[Origin] = []
            for a in e.args:
                out.extend(self._origin_of_expr(a))
            for p in e.over.partition_by:
                out.extend(self._origin_of_expr(p))
            for k, _desc in e.over.sort:
                out.extend(self._origin_of_expr(k))
            if not out:
                out = [Origin(self.tm.name, f"<{e.name}>", "windowed")]
            return [Origin(o.node, o.col, kind) for o in out]
        if isinstance(e, ast.Call):
            kind = "aggregated" if e.name in AGGREGATES else "derived"
            out: List[Origin] = []
            for a in e.args:
                out.extend(self._origin_of_expr(a))
            if not out:
                out = [Origin(self.tm.name, f"<{e.name}>", "derived")]
            return [Origin(o.node, o.col, kind) for o in out]
        if isinstance(e, ast.Kwarg):
            # Keyword argument (date unit): lineage follows the value; the
            # unit name itself is compile-time vocabulary, not data.
            return self._origin_of_expr(e.value)
        if isinstance(e, ast.BinOp):
            return self._origin_of_expr(e.left) + self._origin_of_expr(e.right)
        if isinstance(e, ast.UnOp):
            return self._origin_of_expr(e.operand)
        return []

    def do_group(self, s: ast.GroupStmt):
        """Register group keys, infer the grouped body and record planned group expressions."""
        self.in_group = True
        for k in s.keys:
            self._require_no_window(k, s.span, "group keys")
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
        """Finalize the plan (outputs, schema, lineage), then incremental/contract checks and fingerprint."""
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
        self.verify_incremental()
        self.verify_contract()
        self.fingerprint()

    def verify_incremental(self):
        """`incremental merge_strategy: append|upsert` is executed for real
        (exec.materialize merges only cdc_column-new rows into the prior
        snapshot) — so it needs the same rigor as any other pin: reject a
        configuration the executor cannot honor instead of accepting it and
        producing silently wrong data. `merge_strategy: replace` (or no
        strategy at all) is still a plain full rebuild every run and has no
        extra requirements."""
        plan = self.tm.plan
        if not plan.incremental:
            return
        strategy = plan.merge_strategy or "replace"
        if strategy not in ("replace", "append", "upsert"):
            raise self._err(
                "E086",
                f"{self.tm.name}: unknown merge_strategy {strategy!r} "
                "(expected replace, append or upsert)", self.decl.span)
        if strategy == "replace":
            return
        if plan.grouped:
            raise self._err(
                "E087",
                f"{self.tm.name}: incremental merge_strategy {strategy!r} is not "
                "supported on a grouped/aggregate model (a cdc_column delta cannot "
                "re-aggregate rows already folded into a prior snapshot without "
                "rescanning everything, which defeats the point)", self.decl.span)
        if not plan.cdc_column or plan.cdc_column not in self.tm.schema:
            raise self._err(
                "E088",
                f"{self.tm.name}: incremental merge_strategy {strategy!r} requires "
                "cdc_column naming an output column of this model (used as the "
                "append/upsert watermark)", self.decl.span)
        if strategy == "upsert":
            keys = [getattr(k, "name", None) for k in plan.merge_keys]
            if not plan.merge_keys or any(k is None or k not in self.tm.schema for k in keys):
                raise self._err(
                    "E089",
                    f"{self.tm.name}: merge_strategy upsert requires merge_keys "
                    "naming one or more plain output columns of this model",
                    self.decl.span)

    def verify_contract(self):
        """Enforce the declared contract: presence, type, nullability, enum/classification."""
        model = self.tm
        if not model.contract:
            return
        cd = self.c.p.contracts.get(model.contract)
        if cd is None:
            raise self._err("E061", f"unknown contract {model.contract!r}")
        for f in cd.fields:
            col = self.cols.get(f.name)
            exp = contract_field_col(f, self.c.p.domain_types)
            if col is None:
                raise self._err("E010", f"model {model.name} missing contract column {f.name!r}")
            if not types_compat(exp.t, col.t):
                raise self._err("E011", f"{model.name}.{f.name}: contract {exp.t} but inferred {col.t}")
            if not exp.nullable and col.nullable:
                raise self._err("E012", f"{model.name}.{f.name}: contract nonnull but value nullable")
            if (exp.enum or exp.classification) and col.t != STRING:
                raise self._err("E013", f"{model.name}.{f.name}: contract enum/classification requires string, got {col.t}")

    def fingerprint(self):
        # Canonical fingerprint: AST-shape, not source-whitespace. `str(decl)`
        # embeds raw spans/whitespace, so `fmt` (which only re-emits the same
        # AST) would spuriously mark everything stale. Canonicalize instead.
        """Canonical SHA-256 fingerprint over fmt-canonicalized AST text and upstream fingerprints."""
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
    """All (node, col) descendants transitively affected by the given changed columns."""
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