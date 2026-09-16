"""Single source of truth for function signatures.

Function names used to live in three places — ``analysis.infer_call``,
``sqlgen.expr`` and ``dialects.function_map`` — and nothing checked arity, so
``upper(a, b)`` typechecked and emitted ``UPPER(a, b)``: invalid SQL that only
failed at the warehouse instead of at compile time. Here every function is
declared once with its arity, accepted argument kind, return type/nullability,
aggregate classification and SQL spelling, and the typechecker and the code
generator both read that same descriptor.

The dialect layer keeps the last word on *spelling* (see
``Dialect.function_map``), which is how a warehouse that names a function
differently overrides this catalog; the catalog is the default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .types import (
    Inf, StrataType, INT64, FLOAT64, STRING, BOOL, UNKNOWN, unify,
)

# error codes owned by this module
E_ARITY = "E062"
E_ARG_TYPE = "E063"
E_STRAY_STAR = "E064"
E_WINDOW_PLACEMENT = "E065"


def _numeric(a: Inf) -> bool:
    return a.t.is_numeric() or a.t.is_money()


def _kind_label(fn: Fn, i: int) -> str:
    return fn.arg_kinds[i - 1] if i <= len(fn.arg_kinds) else fn.kind


def _kind_label(fn: Fn, i: int) -> str:
    return fn.arg_kinds[i - 1] if i <= len(fn.arg_kinds) else fn.kind


_KINDS: Dict[str, Callable[[Inf], bool]] = {
    "any": lambda a: True,
    "numeric": _numeric,
    "string": lambda a: a.t == STRING,
    "int": lambda a: a.t == INT64,
}


@dataclass
class Fn:
    """One declared function: what it accepts and what it produces.

    ``window=True`` means the function may be applied over a window with an
    ``over (...)`` clause (v0.2): the checker applies the same arity/type
    rules, and codegen emits ``FN(args) OVER (PARTITION BY ... ORDER BY ...)``.
    An aggregate is windowable by the same SQL rule that lets windows see one
    row per group (they run after ``GROUP BY``).
    """
    name: str
    min_args: int
    ret: Callable[[List[Inf]], Inf]
    max_args: int = -1                 # -1 = unbounded
    kind: str = "any"                  # default per-argument kind (see _KINDS)
    arg_kinds: Tuple[str, ...] = ()    # per-position kind override, 1-based
    ret_label: str = ""                # result-type label for the E063 message
    aggregate: bool = False            # only legal inside a group body
    window: bool = False               # legal under over(...)
    accepts_star: bool = False         # count(*)
    sql: Optional[str] = None          # SQL spelling; default upper(name)
    doc: str = ""

    @property
    def sql_name(self) -> str:
        return self.sql or self.name.upper()


def _unify_all(args: List[Inf]) -> StrataType:
    """Least upper bound across arguments; `null` (unknown) arguments adapt to
    the rest, so coalesce(a, null) is a string, not a mismatch."""
    t = UNKNOWN
    for a in args:
        if a.t.name == "unknown":
            continue
        if t.name == "unknown":
            t = a.t
            continue
        t = unify(t, a.t)
    return t


def _arity_msg(fn: Fn) -> str:
    if fn.max_args < 0:
        want = f"at least {fn.min_args}"
    elif fn.min_args == fn.max_args:
        want = f"exactly {fn.min_args}"
    else:
        want = f"{fn.min_args}..{fn.max_args}"
    return f"{fn.name}() takes {want} argument(s)"


def check(fn: Fn, args: List[Inf], has_star: bool = False) -> Optional[Tuple[str, str]]:
    """Return (code, message) when the call is malformed, else None.

    Pure on purpose: the checker owns error construction and spans, so this
    module stays free of the analysis layer.
    """
    if has_star:
        if not fn.accepts_star:
            return (E_STRAY_STAR, f"'*' is only valid as count(*), not in {fn.name}()")
        if len(args) != 1:
            return (E_ARITY, _arity_msg(fn))
        return None
    n = len(args)
    if n < fn.min_args or (fn.max_args >= 0 and n > fn.max_args):
        return (E_ARITY, _arity_msg(fn))
    if fn.kind == "coalesce":
        t = _unify_all(args)
        if t.name == "unknown":
            seen = ", ".join(str(a.t) for a in args)
            return ("E058", f"coalesce type mismatch ({seen})")
        return None
    for i, a in enumerate(args, 1):
        if a.t.name == "unknown":
            continue                   # NULL literal adapts to the other args
        kind = _kind_label(fn, i)
        if not _KINDS[kind](a):
            label = fn.ret_label or kind
            return (E_ARG_TYPE,
                    f"{fn.name}() argument {i} must be {label}, got {a.t}")
    return None


# ---------------------------------------------------------------- the catalog

def _same(a: List[Inf]) -> Inf:
    return Inf(a[0].t, a[0].nullable)


def _unified(a: List[Inf]) -> Inf:
    return Inf(_unify_all(a), all(i.nullable for i in a))


FUNCTIONS: List[Fn] = [
    Fn("count", 0, lambda a: Inf(INT64, False), max_args=1,
       aggregate=True, window=True, accepts_star=True,
       doc="rows in the group; count() and count(*) count rows, count(col) skips NULL"),
    Fn("sum", 1, lambda a: Inf(a[0].t, a[0].nullable), max_args=1, kind="numeric",
       aggregate=True, window=True,
       doc="grouped total, preserving the argument type"),
    Fn("avg", 1, lambda a: Inf(FLOAT64, a[0].nullable), max_args=1, kind="numeric",
       aggregate=True, window=True,
       doc="grouped mean as float64"),
    Fn("max", 1, _same, max_args=1, aggregate=True, window=True,
       doc="largest value in the group"),
    Fn("min", 1, _same, max_args=1, aggregate=True, window=True,
       doc="smallest value in the group"),
    Fn("coalesce", 1, _unified, kind="coalesce",
       doc="first non-NULL argument; result type is the least upper bound"),
    Fn("upper", 1, lambda a: Inf(STRING, a[0].nullable), max_args=1, kind="string",
       doc="uppercase string"),
    Fn("lower", 1, lambda a: Inf(STRING, a[0].nullable), max_args=1, kind="string",
       doc="lowercase string"),
    Fn("concat", 1, lambda a: Inf(STRING, any(i.nullable for i in a)),
       doc="string concatenation; any argument NULLs the result"),
    Fn("length", 1, lambda a: Inf(INT64, a[0].nullable), max_args=1, kind="string",
       doc="length of the string in characters (INT64)"),
    Fn("substring", 2, lambda a: Inf(STRING, a[0].nullable), max_args=3, kind="string",
       arg_kinds=("string", "int", "int"),
       doc="substring(s, start[, length]); 1-based start, length optional"),
    Fn("trim", 1, lambda a: Inf(STRING, a[0].nullable), max_args=1, kind="string",
       doc="strip spaces from both ends"),
    Fn("ltrim", 1, lambda a: Inf(STRING, a[0].nullable), max_args=1, kind="string",
       doc="strip spaces from the left end"),
    Fn("rtrim", 1, lambda a: Inf(STRING, a[0].nullable), max_args=1, kind="string",
       doc="strip spaces from the right end"),
    Fn("replace", 3, lambda a: Inf(STRING, any(i.nullable for i in a)), max_args=3,
       kind="string", doc="replace every occurrence of arg 2 with arg 3"),
    Fn("lpad", 3, lambda a: Inf(STRING, any(i.nullable for i in a)), max_args=3,
       kind="string", arg_kinds=("string", "int", "string"),
       doc="pad arg 1 on the left to arg 2 length with arg 3"),
    Fn("rpad", 3, lambda a: Inf(STRING, any(i.nullable for i in a)), max_args=3,
       kind="string", arg_kinds=("string", "int", "string"),
       doc="pad arg 1 on the right to arg 2 length with arg 3"),
    Fn("startswith", 2, lambda a: Inf(BOOL, any(i.nullable for i in a)), max_args=2,
       kind="string", ret_label="bool",
       doc="true when arg 1 starts with arg 2 (nullable if any argument is)"),
    Fn("split_part", 3, lambda a: Inf(STRING, a[0].nullable), max_args=3, kind="string",
       arg_kinds=("string", "string", "int"),
       doc="arg 1 split by arg 2, part arg 3 (1-based; 0/out-of-range = empty string)"),
    Fn("regexp_replace", 3, lambda a: Inf(STRING, any(i.nullable for i in a)),
       max_args=3, kind="string", doc="replace regex arg 2 matches with arg 3"),
    Fn("left", 2, lambda a: Inf(STRING, a[0].nullable), max_args=2, kind="string",
       arg_kinds=("string", "int"),
       doc="first arg 2 characters of arg 1"),
    Fn("right", 2, lambda a: Inf(STRING, a[0].nullable), max_args=2, kind="string",
       arg_kinds=("string", "int"),
       doc="last arg 2 characters of arg 1"),
    Fn("row_number", 0, lambda a: Inf(INT64, False), max_args=0, window=True,
       doc="row number inside the window partition (1-based)"),
    Fn("rank", 0, lambda a: Inf(INT64, False), max_args=0, window=True,
       doc="rank inside the partition, with gaps on ties"),
    Fn("dense_rank", 0, lambda a: Inf(INT64, False), max_args=0, window=True,
       doc="rank inside the partition, without gaps on ties"),
    Fn("lag", 1, lambda a: Inf(a[0].t, True), max_args=3, kind="any",
       window=True,
       doc="value of the argument n rows before (nullable: no row may exist)"),
    Fn("lead", 1, lambda a: Inf(a[0].t, True), max_args=3, kind="any",
       window=True,
       doc="value of the argument n rows after (nullable: no row may exist)"),
    Fn("first_value", 1, _same, max_args=1, window=True,
       doc="first argument value inside the partition"),
    Fn("last_value", 1, _same, max_args=1, window=True,
       doc="last argument value inside the partition"),
]

_BY_NAME: Dict[str, Fn] = {f.name: f for f in FUNCTIONS}

AGGREGATES = frozenset(f.name for f in FUNCTIONS if f.aggregate)

WINDOW_FUNCTIONS = frozenset(f.name for f in FUNCTIONS if f.window)


def is_windowable(name: str) -> bool:
    """True when a function may appear under an `over (...)` clause.

    In v0.2 this is exactly the window-flagged catalog rows (pure ranking /
    offset / frame functions); aggregates take `over` only at the SQL level
    through windowed models, never stacked inside one output.
    """
    fn = get(name)
    return bool(fn) and fn.window


def get(name: str) -> Optional[Fn]:
    """Look up a function, or None when the language does not declare it."""
    return _BY_NAME.get(name)


def is_aggregate(name: str) -> bool:
    return name in AGGREGATES


def emit_sql(name: str, args_sql: str, dialect=None) -> str:
    """SQL for a declared function: dialect spelling wins over the default.

    A no-argument call to a star-accepting function (``count()``) emits
    ``COUNT(*)``: the knowledge that an empty count counts rows lives here,
    with the signature, not in the code generator.

    Some catalog entries do not map 1:1 onto a spelled CALL: ``split_part``
    has no native BigQuery function (emulated with SPLIT there, which returns
    NULL for an out-of-range part instead of ''), so its dialect spelling is
    handled here; ``substring`` is a keyword form shaped by ``sqlgen``. A
    dialect that cannot express a function maps it to a ``*_UNAVAILABLE``
    spelling, which fails loud here instead of emitting broken SQL.
    """
    fn = get(name)
    if fn is None:
        raise RuntimeError(f"undeclared function {name}() reached codegen")
    if not args_sql and fn.accepts_star:
        args_sql = "*"
    spelling = None
    if dialect is not None:
        if name == "split_part":
            if dialect.function_map.get(name) == "SPLIT":
                return "SPLIT(" + args_sql + ")"
            return "SPLIT_PART(" + args_sql + ")"
        spelling = dialect.function_map.get(name)
    if spelling is not None and spelling.endswith("_UNAVAILABLE"):
        raise RuntimeError(
            f"dialect {dialect.name!r} cannot express function {name}()"
            f" (rewrite the model with expressible calls)")
    return f"{spelling or fn.sql_name}({args_sql})"
