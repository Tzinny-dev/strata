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
import re
from typing import Callable, Dict, List, Optional, Tuple

from .types import (
    Inf, StrataType, INT64, FLOAT64, STRING, BOOL, JSON, UNKNOWN, array, unify,
)

# error codes owned by this module
E_ARITY = "E062"
E_ARG_TYPE = "E063"
E_STRAY_STAR = "E064"
E_WINDOW_PLACEMENT = "E065"
E_DATE_UNIT = "E071"
E_DATE_ARG = "E072"
E_DATE_TYPE = "E073"
E_JSON_KEY = "E074"
E_COND_TYPE = "E090"


def valid_json_key(value) -> bool:
    """Portable first slice: literal ASCII object keys, not path expressions."""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


# A JSONPath filter step: `$[?(@ > 1)]` or `$[*] ? (@ > 1)` (SQL/JSON spelling).
_JSONPATH_FILTER = re.compile(r"\?\s*\(")

# The portable subset: the root, then member steps (`.name`) and index steps
# (`[0]`). Everything else is engine-specific: `"a.b"`-style quoted keys are
# SQL/JSON only, `['a.b']` is BigQuery-only (DuckDB and PostgreSQL reject it),
# and `[*]` returns a list of matches in DuckDB but the first match in
# PostgreSQL. Verified against a real DuckDB and a real PostgreSQL 16 server.
_JSONPATH_STEP = re.compile(r"(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])")


def json_path_problem(path) -> Optional[str]:
    """Why a literal ``json_path()`` path cannot be emitted, or None if it can.

    Lives next to the catalog so the checker and the code generator reject the
    same paths for the same reason instead of each keeping its own list. Only
    the subset that behaves the same on DuckDB, PostgreSQL, BigQuery and
    Snowflake is accepted: the root `$` plus member and index steps.
    """
    if not isinstance(path, str) or not path.startswith("$"):
        return "path must be a string literal starting with $"
    if _JSONPATH_FILTER.search(path):
        return "JSONPath filter expressions (`$[?(...)]`) are not supported yet"
    if ".." in path:
        return ("recursive descent (`$..`) is not supported yet: its result shape "
                "differs per warehouse (DuckDB returns an array of matches)")
    if path == "$":
        return None
    pos, rest = 0, path[1:]
    while pos < len(rest):
        step = _JSONPATH_STEP.match(rest, pos)
        if step is None:
            return (f"JSONPath step {rest[pos:]!r} is not portable: only member "
                    "steps (`.name`) and index steps (`[0]`) are supported, and "
                    "quoted keys (`$['a.b']`, `$.\"a.b\"`) and wildcards (`[*]`) "
                    "behave differently or fail on each warehouse")
        pos = step.end()
    return None


def _numeric(a: Inf) -> bool:
    return a.t.is_numeric() or a.t.is_money()


def _kind_label(fn: Fn, i: int) -> str:
    return fn.arg_kinds[i - 1] if i <= len(fn.arg_kinds) else fn.kind


_KINDS: Dict[str, Callable[[Inf], bool]] = {
    "any": lambda a: True,
    "numeric": _numeric,
    "string": lambda a: a.t == STRING,
    "int": lambda a: a.t == INT64,
    "temporal": lambda a: a.t.name in ("date", "timestamp"),
    "json": lambda a: a.t == JSON,
    "array": lambda a: a.t.name == "array" and a.t.elem is not None,
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
    unit_names: Optional[frozenset] = None   # date fns: legal unit spellings
    collection: bool = False          # requires typed collection codegen

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


def _if_ret(args: List[Inf]) -> Inf:
    """if(cond, then, else): exactly one branch runs per row, and either one
    could be the row that runs, so either being nullable makes the result
    nullable — unlike coalesce, which only turns non-nullable once EVERY
    candidate has been tried."""
    branches = args[1:]
    return Inf(_unify_all(branches), any(b.nullable for b in branches))


def _case_values(args: List[Inf]) -> List[Inf]:
    """The THEN/ELSE value slots of a case(cond, val, [cond, val, ...],
    [else]) call: every odd-indexed arg, plus a trailing ELSE if the
    argument count is odd."""
    pairs = len(args) // 2
    values = [args[2 * i + 1] for i in range(pairs)]
    if len(args) % 2 == 1:
        values.append(args[-1])
    return values


def _case_ret(args: List[Inf]) -> Inf:
    """case(...): nullable whenever there's no ELSE (an unmatched row falls
    through to SQL NULL regardless of branch types) or any value branch is
    itself nullable."""
    values = _case_values(args)
    has_else = len(args) % 2 == 1
    return Inf(_unify_all(values), (not has_else) or any(v.nullable for v in values))


def _check_if(fn: Fn, args: List[Inf]) -> Optional[Tuple[str, str]]:
    cond = args[0]
    if cond.t.name not in ("unknown", "bool"):
        return (E_COND_TYPE, f"if() condition must be bool, got {cond.t}")
    t = _unify_all(args[1:])
    if t.name == "unknown":
        seen = ", ".join(str(a.t) for a in args[1:])
        return ("E058", f"if() type mismatch ({seen})")
    return None


def _check_case(fn: Fn, args: List[Inf]) -> Optional[Tuple[str, str]]:
    pairs = len(args) // 2
    for i in range(pairs):
        cond = args[2 * i]
        if cond.t.name not in ("unknown", "bool"):
            return (E_COND_TYPE, f"case() condition {i + 1} must be bool, got {cond.t}")
    values = _case_values(args)
    t = _unify_all(values)
    if t.name == "unknown":
        seen = ", ".join(str(v.t) for v in values)
        return ("E058", f"case() type mismatch ({seen})")
    return None


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
    module stays free of the analysis layer. Date functions (``date_add``/
    ``date_sub``/``date_trunc``/``date_diff``) carry a ``unit_names`` table;
    their symbolic-unit validation lives in ``check_date_call`` because a
    unit is not an argument kind — it is a keyword name (``years: 1``) or a
    unit literal (``month``) spelled per warehouse at codegen.
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
            return ("E058", f"coalesce() type mismatch ({seen})")
        return None
    if fn.name == "if":
        return _check_if(fn, args)
    if fn.name == "case":
        return _check_case(fn, args)
    if fn.name in ("array_construct", "array_contains", "array_concat",
                   "array_append", "array_prepend", "array_remove",
                   "array_index_of", "array_sort"):
        return _check_array_operation(fn, args)
    if fn.name == "json_build":
        return _check_json_build(fn, args)
    if fn.name == "array_agg":
        # The element type is the aggregate's return (array of it); nested
        # arrays are unsupported, and an untyped NULL has no element type.
        t = args[0].t
        if t.name == "array":
            return E_ARG_TYPE, "array_agg() of an array would nest collections (unsupported)"
        if t == UNKNOWN:
            return E_ARG_TYPE, "array_agg() requires a known element type, not an untyped NULL"
        return None
    # Collection access needs a known container type (especially array_get's
    # element return type). A nullable typed column is fine, an untyped NULL is not.
    if fn.collection and args[0].t == UNKNOWN:
        return (E_ARG_TYPE, f"{fn.name}() requires a typed container, not an untyped NULL")
    for i, a in enumerate(args, 1):
        if a.t.name == "unknown":
            continue                   # NULL literal adapts to the other args
        kind = _kind_label(fn, i)
        if not _KINDS[kind](a):
            label = fn.ret_label or kind
            return (E_ARG_TYPE,
                    f"{fn.name}() argument {i} must be {label}, got {a.t}")
    return None


# Keep this slice aligned with the simple element types supported in schemas.
ARRAY_ELEMENTS = frozenset({"int64", "float64", "string", "bool", "date",
                            "timestamp", "uuid", "json"})


# Element types an array may carry: the simple scalars, decimal/money, and
# nested arrays to any depth (homogeneous by construction). Element-wise
# operations with per-engine equality/ordering semantics (contains, sort,
# append/prepend/remove/index_of) still require simple scalar elements and
# reject the rest loudly at check time.
def _valid_elem(t: StrataType) -> bool:
    if t.name in ARRAY_ELEMENTS or t.name in ("decimal", "money"):
        return True
    return t.name == "array" and t.elem is not None and _valid_elem(t.elem)


def _constructed_type(args: List[Inf]) -> StrataType:
    return array(next(a.t for a in args if a.t != UNKNOWN))


def _check_json_build(fn: Fn, args: List[Inf]) -> Optional[Tuple[str, str]]:
    if len(args) % 2 != 0:
        return (E_ARITY,
                "json_build() requires key/value pairs (even argument count)")
    for i in range(0, len(args), 2):
        # NULL keys are rejected by the literal-key check in infer_call; here
        # we only validate that non-NULL keys are string-typed.
        if args[i].t.name == "unknown":
            continue
        if args[i].t != STRING:
            return E_ARG_TYPE, "json_build() keys must be strings"
    return None


def _check_array_operation(fn: Fn, args: List[Inf]) -> Optional[Tuple[str, str]]:
    if fn.name in ("array_append", "array_prepend", "array_remove", "array_index_of"):
        base = args[0].t if fn.name != "array_prepend" else args[1].t
        needle = args[1] if fn.name != "array_prepend" else args[0]
        if base.name != "array" or base.elem is None or base.elem.name not in ARRAY_ELEMENTS:
            return E_ARG_TYPE, f"{fn.name}() requires a typed one-dimensional array"
        if base.elem == JSON:
            return E_ARG_TYPE, f"{fn.name}() does not support JSON-typed arrays"
        if needle.t not in (base.elem, UNKNOWN):
            return E_ARG_TYPE, f"{fn.name}() needle type {needle.t} does not match array element {base.elem}"
        return None
    if fn.name in ("array_construct", "array_contains", "array_concat"):
        known = [a.t for a in args if a.t != UNKNOWN]
        if fn.name == "array_construct":
            if not known or not _valid_elem(known[0]) or any(t != known[0] for t in known):
                return E_ARG_TYPE, "array_construct() requires homogeneous elements of a supported type and at least one known type"
        else:
            base = args[0].t
            if base.name != "array" or base.elem is None or not _valid_elem(base.elem):
                return E_ARG_TYPE, f"{fn.name}() requires a typed array with supported element types"
            other = args[1].t
            if fn.name == "array_concat":
                if other != base:
                    return E_ARG_TYPE, f"array_concat() requires identical array types, got {base} and {other}"
            elif base.elem.name not in ("int64", "float64", "string", "bool", "date", "timestamp", "uuid"):
                return E_ARG_TYPE, "array_contains() requires a one-dimensional array of comparable scalar elements (no JSON, decimal, money or nested arrays)"
            elif other not in (base.elem, UNKNOWN):
                return E_ARG_TYPE, "array_contains() requires a matching scalar value; JSON equality is not supported"
        return None
    if fn.name in ("array_sort",):
        base = args[0].t
        if base.name != "array" or base.elem is None or base.elem.name not in ("int64", "float64", "string", "bool", "date", "timestamp"):
            return E_ARG_TYPE, "array_sort() requires a typed one-dimensional array of comparable scalar elements"
        return None
    return None


def check_date_call(fn: Fn, unit: str) -> Optional[Tuple[str, str]]:
    """Validate the symbolic unit of a date call (`years: 1` kwarg name or
    `month` unit literal). Split from ``check`` because the unit is not an
    expression argument — the caller (analysis) extracts it from the AST."""
    if fn.unit_names is not None and unit not in fn.unit_names:
        return (E_DATE_UNIT,
                f"{fn.name}() unit {unit!r} is not supported (have: "
                f"{', '.join(sorted(fn.unit_names))})")
    return None


# ---------------------------------------------------------------- the catalog

def _same(a: List[Inf]) -> Inf:
    return Inf(a[0].t, a[0].nullable)


def _unified(a: List[Inf]) -> Inf:
    return Inf(_unify_all(a), all(i.nullable for i in a))


# Legal units per date function. `date_add`/`date_sub` take the unit as a
# kwarg name (`date_add(d, years: 1)`); `date_trunc`/`date_diff` take it as
# the unit literal (`date_trunc(d, month)`, `date_diff(a, b, day)`). One
# language surface per family; per-dialect spelling lives in sqlgen.
DATE_ADD_UNITS = frozenset({"years", "quarters", "months", "weeks", "days"})
DATE_TRUNC_UNITS = frozenset({"year", "quarter", "month", "week", "day"})


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
    Fn("if", 3, _if_ret, max_args=3,
       doc="if(cond, then, else): cond must be bool; result type is the "
           "least upper bound of then/else"),
    Fn("case", 2, _case_ret, max_args=-1,
       doc="case(cond, val, [cond, val, ...], [else]): first matching "
           "condition wins; no else means NULL when none match"),
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
    Fn("array_construct", 1, lambda a: Inf(_constructed_type(a), False),
       collection=True, doc="homogeneous scalar array; NULL elements preserved, at least one typed element"),
    Fn("array_contains", 2, lambda a: Inf(BOOL, any(i.nullable for i in a)),
       max_args=2, collection=True,
       doc="membership by scalar equality; NULL array or needle returns NULL; NULL elements do not match"),
    Fn("array_concat", 2, lambda a: Inf(a[0].t, any(i.nullable for i in a)),
       max_args=2, collection=True,
       doc="concatenate same-typed arrays in order, preserving duplicates; NULL array returns NULL"),
    Fn("array_append", 2, lambda a: Inf(a[0].t, True),
       max_args=2, collection=True,
       doc="append a scalar element to the end of a typed array; NULL tip/NULL array returns NULL"),
    Fn("array_prepend", 2, lambda a: Inf(a[1].t, True),
       max_args=2, collection=True,
       doc="prepend a scalar element to the start of a typed array; NULL value/NULL array returns NULL"),
    Fn("array_remove", 2, lambda a: Inf(a[0].t, True),
       max_args=2, collection=True,
       doc="remove all matching elements from a typed array; NULL needle has no effect, NULL array returns NULL"),
    Fn("array_index_of", 2, lambda a: Inf(INT64, True),
       max_args=2, collection=True,
       doc="0-based position of the needle, normal across warehouses; NULL array/needle/no match returns NULL; NULL elements interrupt the scan"),
    Fn("array_sort", 1, lambda a: Inf(a[0].t, True), max_args=1,
       collection=True,
       doc="stable ascending sort of a typed scalar array; NULL array returns NULL, NULL elements remain in unspecified order"),
    Fn("array_agg", 1, lambda a: Inf(array(a[0].t), True),
       max_args=1, aggregate=True, collection=True,
       doc="one array of the group's non-NULL argument values, in encounter order; "
           "an empty or all-NULL group yields NULL; element type follows the argument (no nested arrays)"),
        Fn("json_build", 2, lambda a: Inf(JSON, True), max_args=-1,
       collection=True, arg_kinds=("string", "any"),
       doc="construct a JSON object from key/value pairs; simple ASCII keys; NULL values become JSON null, NULL keys are rejected"),
    Fn("json_is_null", 1, lambda a: Inf(BOOL, True),
       kind="json", collection=True, doc="true only for a JSON null value; SQL NULL returns NULL"),
    Fn("json_get", 2, lambda a: Inf(JSON, True), max_args=2,
       arg_kinds=("json", "string"), collection=True,
       doc="JSON member by a literal simple key or by a runtime string expression; "
           "a runtime key is an exact-key lookup emitted only where the dialect can "
           "express one (DuckDB, PostgreSQL); missing member is SQL NULL, JSON null preserved"),
    Fn("json_value", 2, lambda a: Inf(STRING, True), max_args=2,
       arg_kinds=("json", "string"), collection=True,
       doc="JSON scalar member as text, by a literal simple key or by a runtime string "
           "expression where the dialect can express an exact-key lookup; missing, JSON "
           "null and containers return SQL NULL"),
    Fn("json_path", 2, lambda a: Inf(JSON, True), max_args=2,
       arg_kinds=("json", "string"), collection=True,
       doc="JSON value selected by a literal path string with the root $ and member/"
           "index steps; base JSON null or path absent returns SQL NULL; filters, "
           "recursive descent, wildcards and quoted keys are rejected in compilation"),
    Fn("array_length", 1, lambda a: Inf(INT64, a[0].nullable), max_args=1,
       kind="array", collection=True,
       doc="number of elements, including NULL elements; empty array is zero"),
    Fn("array_get", 2, lambda a: Inf(a[0].t.elem, True), max_args=2,
       arg_kinds=("array", "int"), collection=True,
       doc="zero-based element access; NULL, negative and out-of-range indices return NULL"),
    Fn("date_add", 2, lambda a: Inf(a[0].t, any(i.nullable for i in a)), max_args=2, kind="temporal",
       arg_kinds=("temporal", "int"),
       unit_names=DATE_ADD_UNITS,
       doc="arg 1 shifted by arg 2 units (keyword arg: date_add(d, years: 1)); "
           "unit keywords: years, quarters, months, weeks, days; returns the argument type"),
    Fn("date_sub", 2, lambda a: Inf(a[0].t, any(i.nullable for i in a)), max_args=2, kind="temporal",
       arg_kinds=("temporal", "int"),
       unit_names=DATE_ADD_UNITS,
       doc="arg 1 shifted back by arg 2 units (keyword arg: date_sub(d, days: 3)); "
           "same unit keywords as date_add; returns the argument type"),
    Fn("date_trunc", 2, _same,
       max_args=2, kind="temporal", arg_kinds=("temporal", "string"),
       unit_names=DATE_TRUNC_UNITS,
       doc="arg 1 truncated to the arg 2 granularity (unit literal: "
           "date_trunc(d, month)); units: year, quarter, month, week, day; "
           "a DATE stays a DATE"),
    Fn("date_diff", 3, lambda a: Inf(INT64, a[0].nullable or a[1].nullable), max_args=3,
       kind="temporal", arg_kinds=("temporal", "temporal", "string"),
       unit_names=DATE_TRUNC_UNITS | {"weeks"},
       doc="calendar boundaries crossed from arg 1 to arg 2 (date_diff(a, b, day)); "
           "weeks start Monday; result INT64"),
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
    if fn.collection:
        raise RuntimeError(f"{name}() requires typed collection codegen, not a plain SQL call")
    if fn.unit_names is not None:
        raise RuntimeError(f"{name}() requires typed date codegen, not a plain SQL call")
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
