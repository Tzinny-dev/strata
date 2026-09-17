"""SQL codegen from the typed Plan IR, parameterized by a warehouse dialect.

Dialect work is not "prettier SQL": it is the Strata guarantee that the same
typed DAG compiles and runs on any warehouse you pin to. Where a dialect
cannot express something the plan declares (e.g. ANTI/SEMI JOIN outside
DuckDB), the translator FAILS LOUDLY instead of emitting a silently-wrong
query — the same philosophy as the 3-phase pins.
"""
from __future__ import annotations

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
    return str(value)


def _sql_type_stub(t: StrataType) -> str:
    if t.name == "decimal":
        return f"DECIMAL({t.precision},{t.scale})"
    if t.name == "money":
        return "DECIMAL(38,2)"
    if t.name == "array":
        return f"{SQL_TYPE.get(t.elem.name, 'VARCHAR')}[]"
    return SQL_TYPE.get(t.name, "VARCHAR")


class Translator:
    def __init__(self, plan, mode: str, dialect=DUCKDB):
        self.dialect = dialect
        self.plan = plan
        self.mode = mode

    def lookup_input(self, qualifier):
        for i, inp in enumerate(self.plan.inputs):
            if inp.alias == qualifier:
                return i
        raise KeyError(qualifier)

    def col(self, name: str, qualifier=None) -> str:
        if qualifier:
            i = self.lookup_input(qualifier)
            if i == 0:
                return name if self.mode == _OUTER else f"t0.{name}"
            return f"__j{i}_{name}" if self.mode == _OUTER else f"t{i}.{name}"
        return name

    def expr(self, e: ast.Node) -> str:
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
        if len(e.args) != fn.min_args:
            raise RuntimeError(f"malformed {name}() reached codegen")
        base = self.expr(e.args[0])
        if fn.literal_key:
            key = e.args[1]
            if not isinstance(key, ast.Literal) or not functions.valid_json_key(key.value):
                raise RuntimeError(f"{name}() requires a simple literal object key")
            path = _lit("$." + key.value)
            if d == "bigquery":
                return f"{'JSON_QUERY' if name == 'json_get' else 'JSON_VALUE'}({base}, {path})"
            if d == "duckdb":
                value = f"JSON_EXTRACT({base}, {path})"
                scalar = f"JSON_EXTRACT_STRING({base}, {path})"
                kind = f"JSON_TYPE({value})"
                allowed = "'VARCHAR', 'BOOLEAN', 'BIGINT', 'UBIGINT', 'DOUBLE'"
            elif d == "postgres":
                value = f"({base} -> {_lit(key.value)})"
                scalar = f"({base} ->> {_lit(key.value)})"
                kind = f"JSONB_TYPEOF({value})"
                allowed = "'string', 'boolean', 'number'"
            else:
                value = f"GET({base}, {_lit(key.value)})"
                scalar = f"CAST({value} AS VARCHAR)"
                kind = f"TYPEOF({value})"
                allowed = "'VARCHAR', 'BOOLEAN', 'INTEGER', 'DECIMAL', 'DOUBLE'"
            if name == "json_get":
                return value
            return f"CASE WHEN {kind} IN ({allowed}) THEN {scalar} ELSE NULL END"
        length = (f"CARDINALITY({base})" if d == "postgres" else
                  f"ARRAY_SIZE({base})" if d == "snowflake" else f"ARRAY_LENGTH({base})")
        if name == "array_length":
            return f"CAST({length} AS {self.dialect.sql_type('int64')})"
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
                target = self.dialect.sql_type(base_t.elem.name)
                if target is None:
                    raise RuntimeError(f"dialect {d!r} cannot express array_get of {base_t.elem}")
                value = f"CAST({value} AS {target})"
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


def gen_base_subquery(plan, dialect=DUCKDB, upstream_prefix: str = "v_") -> str:
    t = Translator(plan, _RAW, dialect=dialect)
    # explicit projection over the left table (avoids name clashes with computed columns)
    computed = {bc.name for bc in plan.base_cols if bc.expr is not None}
    left_input = plan.inputs[0]
    left_table = f"{upstream_prefix}{left_input.node}" if not left_input.is_source else left_input.node
    selects = [f"t0.{cname}" for cname in left_input.cols if cname not in computed]
    for j in plan.joins:
        inp = plan.inputs[j.index]
        for cname in inp.cols:
            selects.append(f"t{j.index}.{cname} AS __j{j.index}_{cname}")
    for bc in plan.base_cols:
        if bc.expr is None:
            continue
        selects.append(f"{t.expr(bc.expr)} AS {bc.name}")

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
    base = "SELECT " + ", ".join(selects) + "\nFROM " + "\n  ".join(froms)
    if plan.preds:  # pre-aggregation filters live in base subquery for cleanliness
        t2 = Translator(plan, _RAW, dialect=dialect)
        base += "\nWHERE " + " AND ".join(t2.expr(p) for p in plan.preds)
    
    return base


def gen_outer(plan, dialect=DUCKDB) -> str:
    t = Translator(plan, _OUTER, dialect=dialect)
    parts = []
    for out in plan.outputs:
        sql = t.expr(out.expr)
        parts.append(f"{sql} AS {out.name}")
    sql = "SELECT " + ", ".join(parts) + "\nFROM base"
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
    plan = tm.plan
    if plan is None:
        return "-- no plan"
    base = gen_base_subquery(plan, dialect=dialect, upstream_prefix=upstream_prefix)
    outer = gen_outer(plan, dialect=dialect)
    return f"-- model {tm.name}" + (f" -> contract {tm.contract}" if tm.contract else "") + "\nWITH base AS (\n" + base + "\n)\n" + outer + "\n"


def full_sql(tms: List[TypedModel], names: List[str], dialect=DUCKDB,
           view_prefix: str = "v_", upstream_prefix: str = "v_") -> str:
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