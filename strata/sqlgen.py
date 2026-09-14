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
            args = ", ".join(self.expr(a) for a in e.args)
            if name == "cast":
                spec = e.args[1].value if len(e.args) > 1 and isinstance(e.args[1], ast.Literal) else "string"
                return f"CAST({self.expr(e.args[0])} AS {self.dialect.cast_target(spec)})"
            if name == "in":
                return self._in(e)
            if name in ("count", "sum", "avg", "max", "min", "coalesce", "upper", "lower"):
                return f"{name.upper()}({args})"
            return self.fn_sql(name, args)
        raise ValueError(f"cannot codegen {type(e).__name__}")

    def fn_sql(self, name: str, args: str) -> str:
        mapped = self.dialect.function_map.get(name)
        if mapped is None:
            raise RuntimeError(
                f"dialect {self.dialect.name!r} cannot express function {name}()"
                f" (add an alias to dialects.py or rewrite the model)")
        return f"{mapped}({args})"

    def _in(self, e: ast.Call):
        # IN is represented as Call('in', [x, [a,b,c]])
        lhs = self.expr(e.args[0])
        items = e.args[1].items if isinstance(e.args[1], ast.ListExpr) else e.args[1:]
        parts = ", ".join(self.expr(i) for i in items)
        return f"({lhs} IN ({parts}))"


def gen_base_subquery(plan, dialect=DUCKDB) -> str:
    t = Translator(plan, _RAW, dialect=dialect)
    # explicit projection over the left table (avoids name clashes with computed columns)
    computed = {bc.name for bc in plan.base_cols if bc.expr is not None}
    left_input = plan.inputs[0]
    selects = [f"t0.{cname}" for cname in left_input.cols if cname not in computed]
    for j in plan.joins:
        inp = plan.inputs[j.index]
        for cname in inp.cols:
            selects.append(f"t{j.index}.{cname} AS __j{j.index}_{cname}")
    for bc in plan.base_cols:
        if bc.expr is None:
            continue
        selects.append(f"{t.expr(bc.expr)} AS {bc.name}")

    froms = [f"{left_input.node} t0"]
    for j in plan.joins:
        on_sql = t.expr(j.on)
        kind = JOIN_SQL[j.kind]
        if j.kind in ("anti", "semi") and not t.dialect.supports_anti_semi:
            raise RuntimeError(f"dialect {t.dialect.name!r} cannot express ANTI/{j.kind.upper()} JOIN"
                             f" (emit NOT EXISTS/EXISTS instead via an equivalent pipeline)")
        froms.append(f"{kind} {plan.inputs[j.index].node} t{j.index} ON {on_sql}")
    base = "SELECT " + ", ".join(selects) + "\nFROM " + "\n  ".join(froms)
    if plan.preds:  # pre-aggregation filters live in base subquery for cleanliness
        t2 = Translator(plan, _RAW)
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


def model_sql(tm: TypedModel, dialect=DUCKDB) -> str:
    plan = tm.plan
    if plan is None:
        return "-- no plan"
    base = gen_base_subquery(plan, dialect=dialect)
    outer = gen_outer(plan, dialect=dialect)
    return f"-- model {tm.name}" + (f" -> contract {tm.contract}" if tm.contract else "") + "\nWITH base AS (\n" + base + "\n)\n" + outer + "\n"


def full_sql(tms: List[TypedModel], names: List[str], dialect=DUCKDB) -> str:
    view_sqls = []
    for name in names:
        tm = tms[name]
        sql = model_sql(tm, dialect=dialect)
        view_sqls.append(f"CREATE OR REPLACE VIEW v_{name} AS\n{sql}")
    return "\n".join(view_sqls)