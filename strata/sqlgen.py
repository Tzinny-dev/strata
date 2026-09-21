"""SQL codegen from the typed Plan IR, parameterized by a warehouse dialect.

Dialect work is not "prettier SQL": it is the Strata guarantee that the same
typed DAG compiles and runs on any warehouse you pin to. Where a dialect
cannot express something the plan declares (e.g. ANTI/SEMI JOIN outside
DuckDB), the translator FAILS LOUDLY instead of emitting a silently-wrong
query — the same philosophy as the 3-phase pins.
"""
from __future__ import annotations

import datetime
from typing import List

from . import ast
from . import functions
from .analysis import TypedModel
from .types import StrataType, INT64, FLOAT64, STRING, BOOL, DATE, TIMESTAMP, UUID, JSON
from .dialects import Dialect, DUCKDB

_RAW, _OUTER = "raw", "outer"

SQL_TYPE = {
    "int64": "BIGINT", "float64": "DOUBLE", "string": "VARCHAR", "bool": "BOOLEAN",
    "date": "DATE", "timestamp": "TIMESTAMP", "uuid": "UUID", "json": "JSON",
}

JOIN_SQL = {"left": "LEFT JOIN", "inner": "JOIN", "anti": "ANTI JOIN", "semi": "SEMI JOIN"}
BINOP_SQL = {"==": "=", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">=",
             "and": "AND", "or": "OR", "+": "+", "-": "-", "*": "*", "/": "/",
             "%": "%", "||": "||", "in": "IN"}


def _lit(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    # datetime is a date subclass; check it first or a timestamp watermark
    # (e.g. from an incremental cdc_column pushdown predicate) would lose
    # its time component and silently become a bare DATE literal.
    if isinstance(value, datetime.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, datetime.date):
        return f"DATE '{value.isoformat()}'"
    return str(value)


def _sql_type_stub(t: StrataType) -> str:
    if t.name == "decimal":
        return f"DECIMAL({t.precision},{t.scale})"
    if t.name == "money":
        return "DECIMAL(38,2)"
    if t.name == "array":
        return f"{SQL_TYPE.get(t.elem.name, 'VARCHAR')}[]"
    return SQL_TYPE.get(t.name, "VARCHAR")


def _elem_target(dialect, t: StrataType) -> str:
    """CAST target for an array element type, recursing through nested
    arrays (BIGINT[][], ARRAY<ARRAY<...>>, ...), decimals and money."""
    target = dialect.sql_type(t.name)
    if target is not None:
        return target
    if t.name == "decimal":
        return dialect.decimal_sql(t.precision, t.scale)
    if t.name == "money":
        return dialect.money
    if t.name == "array" and t.elem is not None:
        return dialect.array_sql(_elem_target(dialect, t.elem))
    raise RuntimeError(f"dialect {dialect.name!r} cannot target element type {t}")


class Translator:
    """Compile a TypedModel's plan into dialect-specific SQL expressions."""

    def __init__(self, plan, mode: str, dialect=DUCKDB):
        self.dialect = dialect
        self.plan = plan
        self.mode = mode

    def lookup_input(self, qualifier):
        """Index of the plan input with the given alias; raises KeyError."""
        for i, inp in enumerate(self.plan.inputs):
            if inp.alias == qualifier:
                return i
        raise KeyError(qualifier)

    def col(self, name: str, qualifier=None) -> str:
        """Qualified SQL column reference for an identifier."""
        if qualifier:
            i = self.lookup_input(qualifier)
            if i == 0:
                return name if self.mode == _OUTER else f"t0.{name}"
            return f"__j{i}_{name}" if self.mode == _OUTER else f"t{i}.{name}"
        return name

    def expr(self, e: ast.Node) -> str:
        """Compile one expression AST node into dialect SQL."""
        if isinstance(e, ast.Literal):
            return _lit(e.value)
        if isinstance(e, ast.ColumnRef):
            return self.col(e.name, e.qualifier)
        if isinstance(e, ast.UnOp):
            inner = self.expr(e.operand)
            return f"NOT ({inner})" if e.op == "not" else f"-({inner})"
        if isinstance(e, ast.BinOp):
            op = BINOP_SQL[e.op]
            return f"({self.expr(e.left)} {op} {self.expr(e.right)})"
        if isinstance(e, ast.Call):
            name = e.name
            fn = functions.get(name)
            if fn is not None and fn.collection:
                return self._collection_call(e, fn)
            if fn is not None and fn.unit_names is not None:
                return self._date_call(e, name)
            if name in ("if", "case"):
                return self._case_sql(e)
            args = ", ".join(self.expr(a) for a in e.args)
            if name == "cast":
                spec = e.args[1].value if len(e.args) > 1 and isinstance(e.args[1], ast.Literal) else "string"
                return f"CAST({self.expr(e.args[0])} AS {self.dialect.cast_target(spec)})"
            if name == "in":
                return self._in(e)
            # Signature and spelling come from the one catalog both the
            # typechecker and this generator read (functions.py). Two
            # catalog entries do not map 1:1 onto a spelled CALL:
            #  - substring is the SQL keyword form SUBSTRING(s FROM a [FOR
            #    len]), not the comma form (not standard SQL);
            #  - BigQuery has no SPLIT_PART: SPLIT(x, d)[SAFE_OFFSET(n - 1)]
            #    degrades out-of-range parts to NULL instead of '' (fail-loud
            #    instead of silently-wrong, like the ANTI/SEMI JOIN stance).
            if name == "substring":
                pieces = [self.expr(a) for a in e.args]
                if len(pieces) == 3:
                    return f"SUBSTRING({pieces[0]} FROM {pieces[1]} FOR {pieces[2]})"
                return f"SUBSTRING({pieces[0]} FROM {pieces[1]})"
            if name == "split_part" and self.dialect.function_map.get(name) == "SPLIT":
                part = self.expr(e.args[2])
                return (f"SPLIT({self.expr(e.args[0])}, {self.expr(e.args[1])})"
                        f"[SAFE_OFFSET({part} - 1)]")
            if functions.get(name) is None:
                raise RuntimeError(
                    f"dialect {self.dialect.name!r} cannot express function {name}()"
                    f" (declare it in functions.py or rewrite the model)")
            return functions.emit_sql(name, args, self.dialect)
        if isinstance(e, ast.WindowCall):
            return self.window_sql(e)
        if isinstance(e, ast.Star):
            return "*"
        raise ValueError(f"cannot codegen {type(e).__name__}")

    def _collection_call(self, e: ast.Call, fn: functions.Fn) -> str:
        """Portable object-member and one-dimensional array access."""
        d, name = self.dialect.name, fn.name
        if d not in ("duckdb", "postgres", "bigquery", "snowflake"):
            raise RuntimeError(f"dialect {d!r} cannot express {name}()")
        base_t = self.plan.collection_arg_types.get(id(e))
        if base_t is None:
            raise RuntimeError(f"{name}() requires typed collection codegen")
        if len(e.args) < fn.min_args or (fn.max_args >= 0 and len(e.args) > fn.max_args):
            raise RuntimeError(f"malformed {name}() reached codegen")

        # --- json_build: construct a JSON object from key/value pairs ---
        if name == "json_build":
            parts = []
            for i in range(0, len(e.args), 2):
                key = e.args[i]
                val = e.args[i + 1]
                if not isinstance(key, ast.Literal) or not isinstance(key.value, str):
                    raise RuntimeError(f"json_build() key must be a string literal")
                if not functions.valid_json_key(key.value):
                    raise RuntimeError(f"json_build() invalid key: {key.value!r}")
                parts.append(f"'{key.value}', {self.expr(val)}")
            inner = ", ".join(parts)
            if d == "duckdb":
                return f"json_object({inner})"
            elif d == "postgres":
                return f"json_build_object({inner})"
            elif d == "bigquery":
                return f"JSON_OBJECT({inner})"
            else:
                return f"OBJECT_CONSTRUCT({inner})"

        # --- json_is_null: distinguish JSON null from SQL NULL ---
        if name == "json_is_null":
            value = self.expr(e.args[0])
            if d == "duckdb":
                return (f"CASE WHEN {value} IS NULL THEN NULL "
                        f"WHEN JSON_TYPE({value}) = 'NULL' THEN TRUE ELSE FALSE END")
            elif d == "postgres":
                return (f"CASE WHEN {value} IS NULL THEN NULL "
                        f"WHEN {value}::jsonb = 'null'::jsonb THEN TRUE ELSE FALSE END")
            elif d == "bigquery":
                return (f"CASE WHEN {value} IS NULL THEN NULL "
                        f"WHEN JSON_TYPE({value}) = 'NULL' THEN TRUE ELSE FALSE END")
            else:
                return (f"CASE WHEN {value} IS NULL THEN NULL "
                        f"WHEN IS_NULL_VALUE({value}) THEN TRUE ELSE FALSE END")

        # --- array_agg: collect the group's non-NULL values into a typed array.
        # Nullability is portable across warehouses only if NULLs are excluded
        # (DuckDB/Postgres filter, BigQuery IGNORE NULLS, Snowflake ARRAY_AGG
        # always drops NULLs when elements are not object role): an empty group
        # then yields NULL everywhere and element counts agree.
        if name == "array_agg":
            arg = self.expr(e.args[0])
            if d == "bigquery":
                return f"ARRAY_AGG({arg} IGNORE NULLS)"
            if d == "snowflake":
                return f"ARRAY_AGG({arg})"
            return f"ARRAY_AGG({arg}) FILTER (WHERE {arg} IS NOT NULL)"

        # --- array_construct: homogeneous typed array literal ---
        if name == "array_construct":
            target = _elem_target(self.dialect, base_t.elem)
            # Cast each element, including NULL, so warehouse inference cannot
            # disagree with the homogeneous Strata element type.
            args = ", ".join(f"CAST({self.expr(a)} AS {target})" for a in e.args)
            if d == "snowflake":
                return f"ARRAY_CONSTRUCT({args})"
            return f"{'ARRAY' if d == 'postgres' else ''}[{args}]"

        # --- json_get / json_value: object member by literal key, or by an
        # expression that the checker has already proven to be a string ---
        # A literal key becomes a compile-time path/member. A dynamic key is a
        # runtime value, so it is an *exact-key lookup* and is emitted only
        # where the dialect can express one: DuckDB (a `$`-less path is an
        # exact key: it neither traverses dots nor indexes brackets) and
        # PostgreSQL (`jsonb -> text` takes any text expression, never a
        # path). BigQuery requires the JSONPath to be a string literal or
        # query parameter and Snowflake's GET_PATH requires a quoted
        # path-name literal, so both fail loud instead of emitting SQL the
        # engine will reject or that reads a different member than the same
        # expression on another warehouse.
        #
        # `NULLIF(key, '')` gives both warehouses the same answer for the one
        # degenerate runtime key: DuckDB resolves the *empty* path to the whole
        # document, and folding it to NULL avoids returning the entire row's
        # payload for what the language calls a member lookup.
        if name in ("json_get", "json_value"):
            base = self.expr(e.args[0])
            key = e.args[1]
            if isinstance(key, ast.Literal):
                # Analysis rejects a bad literal key as E074; guard codegen too.
                if not functions.valid_json_key(key.value):
                    raise RuntimeError(f"{name}() requires a simple literal object key")
                literal, key_sql = key.value, None
            else:
                literal, key_sql = None, self.expr(key)
            if d == "duckdb":
                if literal is None:
                    path = f"NULLIF({key_sql}, '')"
                else:
                    path = _lit("$." + literal)
                value = f"JSON_EXTRACT({base}, {path})"
                scalar = f"JSON_EXTRACT_STRING({base}, {path})"
                kind = f"JSON_TYPE({value})"
                allowed = "'VARCHAR', 'BOOLEAN', 'BIGINT', 'UBIGINT', 'DOUBLE'"
            elif d == "postgres":
                if literal is None:
                    value = f"({base} -> NULLIF({key_sql}, ''))"
                    scalar = f"({base} ->> NULLIF({key_sql}, ''))"
                else:
                    value = f"({base} -> {_lit(literal)})"
                    scalar = f"({base} ->> {_lit(literal)})"
                kind = f"JSONB_TYPEOF({value})"
                allowed = "'string', 'boolean', 'number'"
            elif d == "bigquery":
                if literal is None:
                    raise RuntimeError(
                        f"dialect 'bigquery' cannot take a dynamic key in {name}(): the "
                        "engine requires the JSONPath to be a string literal or query "
                        "parameter; use a literal key or json_path()")
                return f"{'JSON_QUERY' if name == 'json_get' else 'JSON_VALUE'}({base}, {_lit('$.' + literal)})"
            else:
                # Snowflake GET(<variant>, <field_name>) is a *key* lookup, not a
                # path, which is exactly the contract of json_get/json_value:
                # field_name accepts any VARCHAR expression for VARIANT input
                # (only structured OBJECTs require a constant) and "must not be
                # an empty string", so NULLIF maps the empty key to NULL as in
                # the other warehouses.
                key_expr = f"NULLIF({key_sql}, '')" if literal is None else _lit(literal)
                value = f"GET({base}, {key_expr})"
                scalar = f"CAST({value} AS VARCHAR)"
                kind = f"TYPEOF({value})"
                allowed = "'VARCHAR', 'BOOLEAN', 'INTEGER', 'DECIMAL', 'DOUBLE'"
            if name == "json_get":
                return value
            return f"CASE WHEN {kind} IN ({allowed}) THEN {scalar} ELSE NULL END"

        # --- json_path: JSONPath-style query over a JSON value ---
        # The path is a string literal starting with $ and must be emitted
        # *quoted*: every dialect takes the path as a string/jsonpath literal,
        # never as raw SQL. `functions.json_path_problem` defines which paths are
        # expressible (the checker rejects them earlier with E074); the guard here
        # keeps a hand-built plan from emitting SQL with the wrong shape.
        if name == "json_path":
            path_arg = e.args[1]
            if not isinstance(path_arg, ast.Literal) or not isinstance(path_arg.value, str):
                raise RuntimeError(f"{name}() requires a string literal path expression")
            path = path_arg.value
            problem = functions.json_path_problem(path)
            if problem is not None:
                raise RuntimeError(f"{name}(): {problem}")
            base = self.expr(e.args[0])
            d = self.dialect.name
            if d == "duckdb":
                return f"JSON_EXTRACT({base}, {_lit(path)})"
            if d == "bigquery":
                return f"JSON_QUERY({base}, {_lit(path)})"
            if d == "postgres":
                # SQL/JSON path syntax. An empty vars object is passed explicitly
                # because a NULL vars would make the whole function return NULL;
                # `silent => true` suppresses the structural errors (a scalar
                # where the path expects an object) so Postgres returns NULL like
                # the other dialects instead of raising.
                return (f"jsonb_path_query_first({base}::jsonb, {_lit(path)}, "
                        f"'{{}}'::jsonb, TRUE)")
            if d == "snowflake":
                # GET_PATH takes a JavaScript-notation path *without* the
                # JSONPath '$' root: the documented forms are 'a.b' and
                # 'xs[0]' (its own syntax has no '$'). A bare '$' has no
                # GET_PATH spelling — the column already is the whole document —
                # so it fails loud instead of guessing an empty path.
                if path == "$":
                    raise RuntimeError(
                        "dialect 'snowflake' cannot express the whole document in "
                        "json_path() (GET_PATH needs a member or index step); use "
                        "the column itself")
                sub = path[1:]
                if sub.startswith("."):
                    sub = sub[1:]
                return f"GET_PATH({base}, {_lit(sub)})"
            raise RuntimeError(f"dialect {d!r} cannot express {name}()")

        # --- array functions: determine base (array) and other (non-array) ---
        # array_prepend(scalar, array): array is args[1], scalar is args[0]
        # all others: array is args[0], other is args[1]
        if name == "array_prepend":
            base = self.expr(e.args[1])
            other = self.expr(e.args[0])
        else:
            base = self.expr(e.args[0])
            other = self.expr(e.args[1]) if len(e.args) > 1 else None
        target = self.dialect.sql_type(base_t.elem.name)

        # --- array_concat: concatenate same-typed arrays ---
        if name == "array_concat":
            if d == "duckdb":
                value = f"LIST_CONCAT({base}, {other})"
            elif d == "postgres":
                value = f"ARRAY_CAT({base}, {other})"
            elif d == "bigquery":
                value = f"ARRAY_CONCAT({base}, {other})"
            else:
                value = f"ARRAY_CAT({base}, {other})"
            return (f"CASE WHEN ({base}) IS NULL OR ({other}) IS NULL "
                    f"THEN NULL ELSE {value} END")

        # --- array_contains: membership test ---
        if name == "array_contains":
            # Give an untyped NULL needle its element type for native
            # polymorphic functions (notably Snowflake TO_VARIANT).
            needle = f"CAST({other} AS {target})"
            if d == "duckdb":
                value = f"LIST_CONTAINS({base}, {needle})"
            elif d == "postgres":
                value = f"COALESCE({needle} = ANY({base}), FALSE)"
            elif d == "bigquery":
                value = f"COALESCE({needle} IN UNNEST({base}), FALSE)"
            else:
                value = f"ARRAY_CONTAINS(TO_VARIANT({needle}), {base})"
            return (f"CASE WHEN ({base}) IS NULL OR ({other}) IS NULL "
                    f"THEN NULL ELSE {value} END")

        # --- array_append / array_prepend: scalar concat to array end/start ---
        if name in ("array_append", "array_prepend"):
            needle = f"CAST({other} AS {target})"
            if name == "array_append":
                if d == "duckdb":
                    value = f"LIST_APPEND({base}, {needle})"
                elif d == "postgres":
                    value = f"ARRAY_APPEND({base}, {needle})"
                elif d == "bigquery":
                    value = f"ARRAY_CONCAT({base}, [CAST({other} AS {target})])"
                else:
                    value = f"ARRAY_APPEND({base}, TO_VARIANT({other}))"
            else:  # array_prepend
                if d == "duckdb":
                    value = f"LIST_PREPEND({needle}, {base})"
                elif d == "postgres":
                    value = f"ARRAY_PREPEND({needle}, {base})"
                elif d == "bigquery":
                    value = f"ARRAY_CONCAT([CAST({other} AS {target})], {base})"
                else:
                    value = f"ARRAY_PREPEND(TO_VARIANT({other}), {base})"
            return (f"CASE WHEN ({base}) IS NULL OR ({other}) IS NULL "
                    f"THEN NULL ELSE {value} END")

        # --- array_remove: remove all matching elements ---
        if name == "array_remove":
            needle = f"CAST({other} AS {target})"
            if d == "duckdb":
                # IS DISTINCT FROM keeps NULL elements when needle is non-NULL;
                # the CASE wrapper makes NULL needle a no-op.
                value = f"LIST_FILTER({base}, x -> x IS DISTINCT FROM {needle})"
            elif d == "postgres":
                value = f"ARRAY_REMOVE({base}, {needle})"
            elif d == "bigquery":
                value = f"ARRAY_FILTER({base}, x -> x IS NOT DISTINCT FROM {needle})"
            else:
                value = f"ARRAY_REMOVE({base}, TO_VARIANT({other}))"
            # NULL base -> NULL; NULL needle -> no effect (return base)
            return (f"CASE WHEN ({base}) IS NULL THEN NULL "
                    f"WHEN ({other}) IS NULL THEN ({base}) "
                    f"ELSE {value} END")

        # --- array_index_of: 0-based position, NULL if not found ---
        if name == "array_index_of":
            needle = f"CAST({other} AS {target})"
            if d == "duckdb":
                value = f"list_position({base}, {needle}) - 1"
            elif d == "postgres":
                value = f"array_position({base}, {needle}) - 1"
            elif d == "bigquery":
                value = (f"(SELECT MIN(o) FROM UNNEST({base}) AS v WITH OFFSET o "
                         f"WHERE v = {needle})")
            else:
                value = f"ARRAY_POSITION({base}, TO_VARIANT({needle}))"
            # list_position / array_position returns NULL when not found,
            # so the -1 naturally produces NULL.
            return (f"CASE WHEN ({base}) IS NULL OR ({other}) IS NULL "
                    f"THEN NULL ELSE {value} END")

        # --- array_sort: stable ascending ---
        if name == "array_sort":
            if d == "duckdb":
                value = f"ARRAY_SORT({base})"
            elif d == "postgres":
                # ARRAY_AGG over zero rows returns NULL, so the empty array has
                # to be rebuilt explicitly: sorting [] must stay [] (verified
                # against a real PostgreSQL 16 server).
                value = (f"COALESCE((SELECT ARRAY_AGG(v ORDER BY v) FROM UNNEST({base}) AS v), "
                         f"ARRAY[]::{target}[])")
            elif d == "bigquery":
                value = f"ARRAY(SELECT x FROM UNNEST({base}) AS x ORDER BY x)"
            else:
                value = f"ARRAY_SORT({base}, TRUE, FALSE)"
            return f"CASE WHEN ({base}) IS NULL THEN NULL ELSE {value} END"

        # --- array_length ---
        length = (f"CARDINALITY({base})" if d == "postgres" else
                  f"ARRAY_SIZE({base})" if d == "snowflake" else f"ARRAY_LENGTH({base})")
        if name == "array_length":
            return f"CAST({length} AS {self.dialect.sql_type('int64')})"

        # --- array_get: 0-based element access ---
        index = self.expr(e.args[1])
        # Guard before translating to a one-based index: no negative indexing
        # and no int64 overflow for e.g. 9223372036854775807 + 1.
        if d == "duckdb":
            value = f"LIST_EXTRACT({base}, ({index}) + 1)"
        elif d == "postgres":
            value = f"({base})[CAST(({index}) + ARRAY_LOWER({base}, 1) AS INTEGER)]"
        elif d == "bigquery":
            value = f"({base})[SAFE_OFFSET({index})]"
        else:
            value = f"GET({base}, {index})"
            if base_t.elem != JSON:
                value = f"CAST({value} AS {_elem_target(self.dialect, base_t.elem)})"
        return (f"CASE WHEN ({index}) >= 0 AND ({index}) < {length} "
                f"THEN {value} ELSE NULL END")

    def _date_call(self, e: ast.Call, name: str) -> str:
        """Calendar arithmetic; timestamps are UTC civil time, weeks start Monday."""
        d = self.dialect
        if d.name not in ("duckdb", "postgres", "bigquery", "snowflake"):
            raise RuntimeError(f"dialect {d.name!r} cannot express {name}()")
        base_t = self.plan.date_arg_types.get(id(e))
        if base_t is None:
            raise RuntimeError(
                f"{name}() reached codegen untyped (plan.date_arg_types is "
                f"missing the call); the typechecker must run first")
        args = e.args
        shifting = name in ("date_add", "date_sub")
        if len(args) != (3 if name == "date_diff" else 2):
            raise RuntimeError(f"malformed {name}() reached codegen")
        unit_arg = args[-1]
        if shifting:
            if not isinstance(unit_arg, ast.Kwarg):
                raise RuntimeError(f"{name}() requires a unit keyword and amount")
            unit = unit_arg.name
        else:
            if not isinstance(unit_arg, ast.Literal) or not isinstance(unit_arg.value, str):
                raise RuntimeError(f"{name}() requires a literal unit")
            unit = unit_arg.value
        problem = functions.check_date_call(functions.get(name), unit)
        if problem is not None:
            raise RuntimeError(problem[1])
        if shifting:
            return self._shift(name, args[0], unit, self.expr(unit_arg.value), base_t)
        unit = "week" if unit == "weeks" else unit
        base = self.expr(args[0])
        if name == "date_trunc":
            if d.name == "bigquery":
                part = "WEEK(MONDAY)" if unit == "week" else unit.upper()
                if base_t.name == "timestamp":
                    return f"TIMESTAMP_TRUNC({base}, {part}, 'UTC')"
                return f"DATE_TRUNC({base}, {part})"
            sql = (self._monday(base) if d.name == "snowflake" and unit == "week"
                   else f"DATE_TRUNC('{unit}', {base})")
            # DuckDB can return DATE even for TIMESTAMP truncation.
            return f"CAST({sql} AS {d.cast_target(base_t.name)})"
        if name != "date_diff":
            raise RuntimeError(f"no date emitter for {name}()")
        return self._date_diff(base, self.expr(args[1]), unit)

    def _monday(self, sql: str) -> str:
        """Monday midnight, independent of Snowflake's WEEK_START setting."""
        if self.dialect.name == "snowflake":
            return f"DATEADD(day, 1 - DAYOFWEEKISO({sql}), DATE_TRUNC('day', {sql}))"
        return f"DATE_TRUNC('week', {sql})"

    def _date_diff(self, start: str, end: str, unit: str) -> str:
        """Count crossed calendar boundaries, not elapsed whole durations."""
        d = self.dialect
        if d.name == "bigquery":
            part = "WEEK(MONDAY)" if unit == "week" else unit.upper()
            # BigQuery TIMESTAMP -> DATE uses UTC; DATE_DIFF counts civil boundaries.
            sql = f"DATE_DIFF(CAST({end} AS DATE), CAST({start} AS DATE), {part})"
        elif unit == "week" and d.name in ("duckdb", "postgres", "snowflake"):
            a, b = (f"CAST({self._monday(s)} AS DATE)" for s in (start, end))
            days = f"DATEDIFF(day, {a}, {b})" if d.name == "snowflake" else f"({b} - {a})"
            sql = f"({days} / 7)"
        elif d.name == "duckdb":
            sql = f"DATE_DIFF('{unit}', {start}, {end})"
        elif d.name == "snowflake":
            sql = f"DATEDIFF({unit}, {start}, {end})"
        elif d.name == "postgres":
            if unit == "day":
                sql = f"(CAST({end} AS DATE) - CAST({start} AS DATE))"
            else:
                def index(s):
                    year = f"EXTRACT(YEAR FROM {s})"
                    if unit == "year":
                        return year
                    scale = 12 if unit == "month" else 4
                    return f"({year} * {scale} + EXTRACT({unit.upper()} FROM {s}))"
                sql = f"({index(end)} - {index(start)})"
        else:
            raise RuntimeError(f"dialect {d.name!r} cannot express date_diff()")
        return f"CAST({sql} AS {d.cast_target('int64')})"

    def _shift(self, name: str, base: ast.Node, unit: str, amount: str, base_t) -> str:
        """Shift by a calendar interval, preserving the analyzed base type."""
        d = self.dialect
        u = unit[:-1].upper()  # plural kwarg -> singular interval unit
        op = "-" if name == "date_sub" else "+"
        base_sql = self.expr(base)
        if d.name == "duckdb":
            sql = f"({base_sql} {op} ({amount}) * INTERVAL 1 {u})"
        elif d.name == "postgres":
            interval = "3 MONTH" if u == "QUARTER" else f"1 {u}"
            sql = f"({base_sql} {op} ({amount}) * INTERVAL '{interval}')"
        elif d.name == "bigquery":
            suffix = "SUB" if name == "date_sub" else "ADD"
            if base_t.name == "timestamp":
                return (f"TIMESTAMP(DATETIME_{suffix}(DATETIME({base_sql}, 'UTC'), "
                        f"INTERVAL ({amount}) {u}), 'UTC')")
            sql = f"DATE_{suffix}({base_sql}, INTERVAL ({amount}) {u})"
        elif d.name == "snowflake":
            n = f"-({amount})" if name == "date_sub" else f"({amount})"
            sql = f"DATEADD({u}, {n}, {base_sql})"
        else:
            raise RuntimeError(f"dialect {d.name!r} cannot express {name}()")
        return self._as_date_if_base(sql, base_t)

    def _as_date_if_base(self, sql: str, base_t) -> str:
        """DuckDB/Postgres interval arithmetic promotes DATE; restore its type."""
        if base_t.name == "date" and self.dialect.name in ("duckdb", "postgres"):
            return f"CAST({sql} AS DATE)"
        return sql

    def window_sql(self, e: ast.WindowCall) -> str:
        """`FN(args) OVER (PARTITION BY ... ORDER BY ...)` — standard in all
        four dialects, so no dialect override is needed (the catalog default).
        The typechecker owns legality; codegen only defends its invariant."""
        if not functions.is_windowable(e.name):
            raise RuntimeError(f"non-window function {e.name}() with over(...) reached codegen")
        head = functions.emit_sql(
            e.name, ", ".join(self.expr(a) for a in e.args), self.dialect)
        frame: List[str] = []
        if e.over.partition_by:
            frame.append("PARTITION BY " + ", ".join(self.expr(p) for p in e.over.partition_by))
        if e.over.sort:
            frame.append("ORDER BY " + ", ".join(
                self.expr(k) + (" DESC" if desc else "") for k, desc in e.over.sort))
        return f"{head} OVER ({' '.join(frame)})" if frame else f"{head} OVER ()"

    def _in(self, e: ast.Call):
        # IN is represented as Call('in', [x, [a,b,c]])
        lhs = self.expr(e.args[0])
        items = e.args[1].items if isinstance(e.args[1], ast.ListExpr) else e.args[1:]
        parts = ", ".join(self.expr(i) for i in items)
        return f"({lhs} IN ({parts}))"

    def _case_sql(self, e: ast.Call) -> str:
        """if(cond, then, else) / case(cond, val, [cond, val, ...], [else])
        both compile to CASE WHEN...END, identical across DuckDB/Postgres/
        BigQuery/Snowflake (no dialect branching needed at this level — the
        two existing CASE WHEN emitters elsewhere in this file, array_get's
        bounds check and json_is_null, already share one shape across all
        four; only nested sub-expressions ever need per-dialect treatment,
        and those already get it via the recursive self.expr(...) calls
        below). Omitting ELSE when case() has none lets SQL's own implicit
        NULL-on-no-match do the work, matching functions._case_ret's
        nullability."""
        args = e.args
        if e.name == "if":
            cond, then, els = args
            return (f"CASE WHEN {self.expr(cond)} THEN {self.expr(then)} "
                    f"ELSE {self.expr(els)} END")
        pairs = len(args) // 2
        parts = ["CASE"]
        for i in range(pairs):
            parts.append(f"WHEN {self.expr(args[2 * i])} THEN {self.expr(args[2 * i + 1])}")
        if len(args) % 2 == 1:
            parts.append(f"ELSE {self.expr(args[-1])}")
        parts.append("END")
        return " ".join(parts)


def _base_select(plan, dialect, base_cols, preds, upstream_prefix: str = "v_") -> str:
    """SELECT...FROM...[WHERE] over the left table (the left branch when the
    model combines rows with a set operation, the whole base otherwise)."""
    t = Translator(plan, _RAW, dialect=dialect)
    # explicit projection over the left table (avoids name clashes with computed columns)
    computed = {bc.name for bc in base_cols if bc.expr is not None}
    left_input = plan.inputs[0]
    left_table = f"{upstream_prefix}{left_input.node}" if not left_input.is_source else left_input.node
    expand, expand_shadow = plan.expand, None
    if expand is not None:
        # `expand xs` replaces the array column with its element column, so the
        # array itself is not projected under that name anymore.
        expand_shadow = expand[0] if expand[0] == expand[1] else None
    selects = [f"t0.{cname}" for cname in left_input.cols
               if cname not in computed and cname != expand_shadow]
    for j in plan.joins:
        inp = plan.inputs[j.index]
        for cname in inp.cols:
            selects.append(f"t{j.index}.{cname} AS __j{j.index}_{cname}")
    for bc in base_cols:
        if bc.expr is None:
            continue
        selects.append(f"{t.expr(bc.expr)} AS {bc.name}")
    # Add partition_by expressions to SELECT if defined
    if plan.partition_by:
        for idx, p_expr in enumerate(plan.partition_by):
            selects.append(f"{t.expr(p_expr)} AS __partition_col_{idx}")

    froms = [f"{left_table} t0"]
    for j in plan.joins:
        on_sql = t.expr(j.on)
        kind = JOIN_SQL[j.kind]
        if j.kind in ("anti", "semi") and not t.dialect.supports_anti_semi:
            raise RuntimeError(f"dialect {t.dialect.name!r} cannot express ANTI/{j.kind.upper()} JOIN"
                             f" (emit NOT EXISTS/EXISTS instead via an equivalent pipeline)")
        jnode = plan.inputs[j.index]
        jtable = f"{upstream_prefix}{jnode.node}" if not jnode.is_source else jnode.node
        froms.append(f"{kind} {jtable} t{j.index} ON {on_sql}")
    if expand is not None:
        src, out, elem = expand
        d = t.dialect.name
        if d in ("duckdb", "postgres"):
            # Alias rows shape: UNNEST(...) AS u0(e) gives a single column e;
            # the plain `AS e` alias would expose the whole element as a STRUCT.
            froms.append(f"CROSS JOIN LATERAL UNNEST(t0.{src}) AS u0(e)")
            selects.append(f"u0.e AS {out}")
        elif d == "bigquery":
            # GoogleSQL: UNNEST alias IS the element column name.
            froms.append(f"CROSS JOIN UNNEST(t0.{src}) AS {out}")
            selects.append(f"{out} AS {out}")
        else:  # snowflake
            froms.append(f"CROSS JOIN LATERAL FLATTEN(input => t0.{src}) AS u0")
            # FLATTEN yields the element as VARIANT; a JSON element stays
            # VARIANT (the warehouse's native json), a scalar gets cast to the
            # pinned element type so type agreements hold across dialects.
            value = "u0.VALUE" if elem == "json" else \
                f"CAST(u0.VALUE AS {t.dialect.sql_type(elem)})"
            selects.append(f"{value} AS {out}")
    base = "SELECT " + ", ".join(selects) + "\nFROM " + "\n  ".join(froms)
    if preds:  # pre-aggregation filters live in base subquery for cleanliness
        t2 = Translator(plan, _RAW, dialect=dialect)
        base += "\nWHERE " + " AND ".join(t2.expr(p) for p in preds)

    return base


def _union_cast(dialect, t: StrataType) -> str:
    """CAST target aligning a set-operation branch column to its unified type."""
    try:
        return _elem_target(dialect, t)
    except RuntimeError:
        raise RuntimeError(f"dialect {dialect.name!r} cannot align set column of type {t}")


def _setop_base(plan, dialect, upstream_prefix: str = "v_"):
    """(extra_ctes, base_body) for a model combining rows with a set operation.

    The left branch is the model's own base query (filters and lets before
    the set-op live there); the right branch projects the upstream model's
    view with positional casts to the unified types. Both branches spell the
    same column aliases in the same order, so by-name (DuckDB) and
    by-position (everywhere else) matching agree. Lets and filters after the
    set-op compile against the combined rows through a `t0`-aliased union
    subquery, so _RAW qualified references keep resolving unchanged.
    """
    op, all_, right_node = plan.set_op
    t = Translator(plan, _RAW, dialect=dialect)
    left_q = _base_select(plan, dialect, plan.base_cols[:plan.setop_base_split],
                          plan.preds[:plan.setop_pred_split], upstream_prefix)

    def branch(alias, table, idx):
        parts = []
        for name, left_t, right_t, unified in plan.setop_cols:
            side_t = (left_t, right_t)[idx]
            ref = f"{alias}.{name}"
            parts.append(ref if side_t == unified
                         else f"CAST({ref} AS {_union_cast(dialect, unified)})")
        return "SELECT " + ", ".join(parts) + f" FROM {table}"

    left_branch = branch("b_left", "b_left", 0)
    right_table = f"{upstream_prefix}{right_node}"  # the right side is always a model
    right_branch = branch(right_table, right_table, 1)
    op_sql = {"union": "UNION ALL" if all_ else "UNION",
              "intersect": "INTERSECT", "except": "EXCEPT"}[op]
    union_q = f"{left_branch}\n{op_sql}\n{right_branch}"

    post_lets = [bc for bc in plan.base_cols[plan.setop_base_split:] if bc.expr is not None]
    post_preds = plan.preds[plan.setop_pred_split:]
    if not post_lets and not post_preds:
        return [f"b_left AS (\n{left_q}\n)"], union_q
    shadowed = {bc.name for bc in post_lets}
    inner = ", ".join(n for n, _, _, _ in plan.setop_cols if n not in shadowed)
    lets = ", ".join(f"{t.expr(bc.expr)} AS {bc.name}" for bc in post_lets)
    sel = inner + (", " + lets if lets else "")
    base_body = f"SELECT {sel}\nFROM (\n{union_q}\n) t0"
    if post_preds:
        base_body += "\nWHERE " + " AND ".join(t.expr(p) for p in post_preds)
    return [f"b_left AS (\n{left_q}\n)"], base_body


def gen_base_subquery(plan, dialect=DUCKDB, upstream_prefix: str = "v_"):
    """Content of the `base` CTE plus any sibling CTEs it needs (set models
    need `b_left` for their left branch): returns (extra_ctes, base_body)."""
    if plan.set_op is None:
        return [], _base_select(plan, dialect, plan.base_cols, plan.preds, upstream_prefix)
    return _setop_base(plan, dialect, upstream_prefix)


def join_check_sql(table: str, keys) -> str:
    """Duplicate-key probe backing a join cardinality expectation: counts key
    groups occurring more than once, ignoring all-NULL keys (they never match
    in an equi-join, so they cannot fan out). Zero means the side is unique
    on those keys; anything else fails the expectation at materialize time."""
    cond = " AND ".join(f"{k} IS NOT NULL" for k in keys)
    group = ", ".join(keys)
    return (f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} WHERE {cond} "
            f"GROUP BY {group} HAVING COUNT(*) > 1) t")


def gen_outer(plan, dialect=DUCKDB) -> str:
    """Compile a plan's outer projection (SELECT/HAVING/ORDER BY/LIMIT) to SQL."""
    t = Translator(plan, _OUTER, dialect=dialect)
    parts = []
    for out in plan.outputs:
        sql = t.expr(out.expr)
        parts.append(f"{sql} AS {out.name}")
    sql = "SELECT " + ("DISTINCT " if plan.distinct else "") + ", ".join(parts) + "\nFROM base"
    if plan.grouped:
        if plan.group_exprs:
            sql += "\nGROUP BY " + ", ".join(t.expr(k) for k in plan.group_exprs)
    if plan.having:
        sql += "\nHAVING " + " AND ".join(t.expr(p) for p in plan.having)
    if plan.sorts:
        order = []
        for e, desc in plan.sorts:
            order.append(t.expr(e) + (" DESC" if desc else ""))
        sql += "\nORDER BY " + ", ".join(order)
    if plan.limit:
        start, end = plan.limit
        if end is not None:
            sql += f"\nLIMIT {end - start + 1} OFFSET {start - 1}"
        else:
            sql += f"\nLIMIT {start}"
    return sql


def model_sql(tm: TypedModel, dialect=DUCKDB, upstream_prefix: str = "v_") -> str:
    """Full SQL text for one typed model (CTEs + outer SELECT).

    Upstream inputs are referenced as ``<upstream_prefix><name>``; returns
    ``-- no plan`` for seed/identity models without a transformation plan."""
    plan = tm.plan
    if plan is None:
        return "-- no plan"
    extras, base = gen_base_subquery(plan, dialect=dialect, upstream_prefix=upstream_prefix)
    outer = gen_outer(plan, dialect=dialect)
    ctes = ",\n".join(extras + [f"base AS (\n{base}\n)"])
    return f"-- model {tm.name}" + (f" -> contract {tm.contract}" if tm.contract else "") + "\nWITH " + ctes + "\n" + outer + "\n"


def full_sql(tms: List[TypedModel], names: List[str], dialect=DUCKDB,
           view_prefix: str = "v_", upstream_prefix: str = "v_") -> str:
    """One CREATE-statement string per model in `names`, in given order."""
    # One statement per view: `materialize` executes them sequentially in
    # topological order, so upstream views (v_base) already exist when the
    # downstream view compiles. A single multi-CTE string would collide
    # (`WITH base ... FROM base` self-reference) — keep statements separate.
    view_sqls = []
    for name in names:
        tm = tms[name]
        sql = model_sql(tm, dialect=dialect, upstream_prefix=upstream_prefix)
        view_sqls.append(f"CREATE OR REPLACE VIEW {view_prefix}{name} AS\n{sql}")
    return ";\n".join(view_sqls)