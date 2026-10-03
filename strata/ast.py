"""Strata AST nodes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


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
    qualifier: str | None = None


@dataclass
class Call(Node):
    name: str = ""
    args: list[Node] = field(default_factory=list)
    # `count(distinct x)`: the only DISTINCT aggregate form the language
    # offers (validated by the typechecker against the catalog).
    distinct: bool = False


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


@dataclass
class WindowSpec(Node):
    """The `over (...)` clause of a window call: partition columns plus the
    ordering inside each partition, reusing the `sort` (expr, desc) shape."""
    partition_by: list[Node] = field(default_factory=list)
    sort: list[tuple[Node, bool]] = field(default_factory=list)


@dataclass
class WindowCall(Node):
    """`fn(args) over (partition_by: [...], sort: [...])`: a function applied
    over a window, evaluated after grouping/aggregation in the outer query."""
    name: str = ""
    args: list[Node] = field(default_factory=list)
    over: WindowSpec = None
    # count(distinct x) inside a window: parsed but rejected by the
    # typechecker (the DISTINCT aggregate form is only supported in the
    # plain, non-windowed position).
    distinct: bool = False


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
    parts: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class ListExpr(Node):
    items: list[Node] = field(default_factory=list)


@dataclass
class ListComprehension(Node):
    body: Node = None
    var: str = ""
    iterable: Node = None


@dataclass
class ModelValue(Node):
    """model literal used inside fn bodies (compile-time value constructor)."""
    name: Node = None
    contract: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    stmts: list[Stmt] = field(default_factory=list)


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
    expect: str | None = None  # None | "many_to_one" | "one_to_one"


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
    assigns: list[OutAssign] = field(default_factory=list)


@dataclass
class AggregateStmt(Stmt):
    assigns: list[OutAssign] = field(default_factory=list)


@dataclass
class GroupStmt(Stmt):
    keys: list[Node] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)


@dataclass
class SortStmt(Stmt):
    keys: list[tuple[Node, bool]] = field(default_factory=list)  # (expr, desc)


@dataclass
class TakeStmt(Stmt):
    start: int | None = None
    end: int | None = None
    limit: int | None = None


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
    """Duplicate-row elimination over the final row set.

    No keys: full-row SELECT DISTINCT. With `by` keys: deterministic
    keep-one-row-per-key (ROW_NUMBER partitioned by the keys, ordered by the
    remaining output columns, rn = 1), portable across all four engines.
    """
    by: list[Node] = field(default_factory=list)
    span: Any = None


@dataclass
class SelectStmt(Stmt):
    assigns: list[OutAssign] = field(default_factory=list)


# ------------------------------------------------------------------ declarations

@dataclass
class SourceDecl(Node):
    name: str = ""
    resource: dict[str, str] = field(default_factory=dict)
    props: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ContractField(Node):
    name: str = ""
    type_spec: str = ""
    params: list[object] = field(default_factory=list)
    nonnull: bool = False
    unique: bool = False
    primary: bool = False
    protected: bool = False
    enum: list = field(default_factory=list)
    classification: str | None = None


@dataclass
class ContractDecl(Node):
    name: str = ""
    fields: list[ContractField] = field(default_factory=list)


@dataclass
class DomainDecl(Node):
    """Transparent type alias: `domain user_id = int64`.

    Usable anywhere a type is written (sources, contracts, casts, nested
    element types, other domains). Structural, not nominal: compatibility
    and physical types follow the underlying type.
    """
    name: str = ""
    type_spec: str = ""
    params: list[object] = field(default_factory=list)


@dataclass
class ModelDecl(Node):
    name: str = ""
    contract: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    partition_by: list[Node] = field(default_factory=list)
    freshness: list[str] | None = None  # e.g. ['incremental'], ['1h', 'daily']
    freshness_column: str | None = None  # event-time column for freshness check
    # Incremental model configuration
    incremental: bool = False  # True if this is an incremental model
    merge_keys: list[Node] = field(default_factory=list)  # Keys for upsert/merge
    merge_strategy: str | None = None  # 'upsert', 'append', 'replace'
    cdc_column: str | None = None  # Change Data Capture column
    stmts: list[Stmt] = field(default_factory=list)
    generated: bool = False       # produced by fn expansion


@dataclass
class FnDecl(Node):
    name: str = ""
    params: list[tuple[str, str]] = field(default_factory=list)
    body: Node = None
    return_type: str = ""


@dataclass
class PipelineDecl(Node):
    name: str = ""
    env: str | None = None
    models: list[Node] = field(default_factory=list)
    sources: dict[str, dict[str, str]] = field(default_factory=dict)


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
    checks: list[TestCheck] = field(default_factory=list)


@dataclass
class TestCheck(Node):
    kind: str = "expect"   # reserved: 'row_count'
    col: str | None = None
    op: str | None = None
    value: object = None


@dataclass
class Module:
    path: str = ""
    decls: list[Node] = field(default_factory=list)

    def find(self, kind: Callable[[Node], bool], name: str | None = None) -> Node | None:
        """First declaration whose kind matches and, if given, whose name matches; None otherwise."""
        for d in self.decls:
            if kind(d) and (name is None or d.name == name):
                return d
        return None

    def all(self, kind: Callable[[Node], bool]) -> list[Node]:
        """Every declaration whose kind matches."""
        return [d for d in self.decls if kind(d)]