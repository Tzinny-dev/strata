"""Strata AST nodes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict

from .types import Col


@dataclass
class Node:
    span: tuple = None


# ------------------------------------------------------------------ expressions

@dataclass
class Literal(Node):
    value: object = None


@dataclass
class ColumnRef(Node):
    name: str = ""
    qualifier: Optional[str] = None


@dataclass
class Call(Node):
    name: str = ""
    args: List[Node] = field(default_factory=list)


@dataclass
class Kwarg(Node):
    """`name: value` argument inside a call: e.g. `date_add(d, years: 1)`.

    Not a general keyword-argument mechanism — the callee declares which
    keyword names it accepts (the typechecker rejects the rest).
    """
    name: str = ""
    value: Node = None


@dataclass
class Star(Node):
    """`*` as a call argument: only valid inside count(*)."""
    pass


@dataclass
class WindowSpec(Node):
    """The `over (...)` clause of a window call: partition columns plus the
    ordering inside each partition, reusing the `sort` (expr, desc) shape."""
    partition_by: List[Node] = field(default_factory=list)
    sort: List[Tuple[Node, bool]] = field(default_factory=list)


@dataclass
class WindowCall(Node):
    """`fn(args) over (partition_by: [...], sort: [...])`: a function applied
    over a window, evaluated after grouping/aggregation in the outer query."""
    name: str = ""
    args: List[Node] = field(default_factory=list)
    over: WindowSpec = None


@dataclass
class BinOp(Node):
    op: str = ""
    left: Node = None
    right: Node = None


@dataclass
class UnOp(Node):
    op: str = ""
    operand: Node = None


@dataclass
class TemplateStr(Node):
    # "prefix{id}suffix{id2}" -> parts
    parts: List[Tuple[str, Optional[str]]] = field(default_factory=list)


@dataclass
class ListExpr(Node):
    items: List[Node] = field(default_factory=list)


@dataclass
class ListComprehension(Node):
    body: Node = None
    var: str = ""
    iterable: Node = None


@dataclass
class ModelValue(Node):
    """model literal used inside fn bodies (compile-time value constructor)."""
    name: Node = None
    contract: Optional[str] = None
    attrs: Dict[str, str] = field(default_factory=dict)
    stmts: List["Stmt"] = field(default_factory=list)


# ------------------------------------------------------------------ statements

@dataclass
class Stmt(Node):
    pass


@dataclass
class FromStmt(Stmt):
    table: str = ""


@dataclass
class JoinStmt(Stmt):
    kind: str = ""          # left inner anti semi
    table: str = ""
    on: Node = None


@dataclass
class FilterStmt(Stmt):
    cond: Node = None


@dataclass
class LetStmt(Stmt):
    name: str = ""
    expr: Node = None


@dataclass
class OutAssign(Node):
    name: str = ""
    expr: Node = None


@dataclass
class DeriveStmt(Stmt):
    assigns: List[OutAssign] = field(default_factory=list)


@dataclass
class AggregateStmt(Stmt):
    assigns: List[OutAssign] = field(default_factory=list)


@dataclass
class GroupStmt(Stmt):
    keys: List[Node] = field(default_factory=list)
    body: List[Stmt] = field(default_factory=list)


@dataclass
class SortStmt(Stmt):
    keys: List[Tuple[Node, bool]] = field(default_factory=list)  # (expr, desc)


@dataclass
class TakeStmt(Stmt):
    start: Optional[int] = None
    end: Optional[int] = None
    limit: Optional[int] = None


@dataclass
class ExpandStmt(Stmt):
    """One row per element of a typed array column of the primary input.

    ``expand xs`` replaces ``xs`` with its (nullable) element column;
    ``expand xs as e`` keeps ``xs`` and adds ``e``. Element type is the
    array's, the output column is nullable, and the expansion runs in the
    base (pre-aggregation) subquery as a lateral unnest per dialect.
    """
    name: str = ""
    as_name: str = ""
    span: Any = None


@dataclass
class SetOpStmt(Stmt):
    """Combine the current rows with a same-shaped upstream model.

    ``op`` is union (DISTINCT unless ``all``), intersect or except (always
    DISTINCT: the ALL variants are not portable). The right side is a model
    name whose schema must carry the same columns in the same order with
    compatible types; statements before the set-op shape the left branch,
    statements after it see the combined rows.
    """
    op: str = ""
    table: str = ""
    all: bool = False
    span: Any = None


@dataclass
class DedupStmt(Stmt):
    """Duplicate-row elimination over the final row set (SELECT DISTINCT).

    Full-row only: key-based dedup without a tiebreak is engine-dependent,
    so keeping one row per key must be written as an explicit group-by.
    """
    span: Any = None


@dataclass
class SelectStmt(Stmt):
    assigns: List[OutAssign] = field(default_factory=list)


# ------------------------------------------------------------------ declarations

@dataclass
class SourceDecl(Node):
    name: str = ""
    resource: Dict[str, str] = field(default_factory=dict)
    props: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class ContractField(Node):
    name: str = ""
    type_spec: str = ""
    params: List[object] = field(default_factory=list)
    nonnull: bool = False
    unique: bool = False
    primary: bool = False
    protected: bool = False
    enum: list = field(default_factory=list)
    classification: Optional[str] = None


@dataclass
class ContractDecl(Node):
    name: str = ""
    fields: List[ContractField] = field(default_factory=list)


@dataclass
class DomainDecl(Node):
    """Transparent type alias: `domain user_id = int64`.

    Usable anywhere a type is written (sources, contracts, casts, nested
    element types, other domains). Structural, not nominal: compatibility
    and physical types follow the underlying type.
    """
    name: str = ""
    type_spec: str = ""
    params: List[object] = field(default_factory=list)


@dataclass
class ModelDecl(Node):
    name: str = ""
    contract: Optional[str] = None
    attrs: Dict[str, str] = field(default_factory=dict)
    stmts: List[Stmt] = field(default_factory=list)
    generated: bool = False       # produced by fn expansion


@dataclass
class FnDecl(Node):
    name: str = ""
    params: List[Tuple[str, str]] = field(default_factory=list)
    body: Node = None
    return_type: str = ""


@dataclass
class PipelineDecl(Node):
    name: str = ""
    env: Optional[str] = None
    models: List[Node] = field(default_factory=list)
    sources: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class ImportDecl(Node):
    path: str = ""


@dataclass
class GeneratorDecl(Node):
    """Top-level `by_country(countries())` -- compile-time model generation."""
    call: Node = None


@dataclass
class TestDecl(Node):
    """Declarative test on a model: `test <model> { expect <expr>; ... }`.
    Compile-time checked (column exists, comparable types, literal rhs) and
    evaluated at run-time against the staged view of the model — a failing
    test aborts the swap (fail-closed blue-green)."""
    model: str = ""
    checks: List["TestCheck"] = field(default_factory=list)


@dataclass
class TestCheck(Node):
    kind: str = "expect"   # reserved: 'row_count'
    col: Optional[str] = None
    op: Optional[str] = None
    value: object = None


@dataclass
class Module:
    path: str = ""
    decls: List[Node] = field(default_factory=list)

    def find(self, kind, name=None):
        for d in self.decls:
            if kind(d) and (name is None or d.name == name):
                return d
        return None

    def all(self, kind):
        return [d for d in self.decls if kind(d)]