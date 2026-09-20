"""Connection-type dispatch so strata/exec.py's engine works against DuckDB
and Postgres without changing any of its function signatures.

Every exec.py function still receives whatever connection object its caller
opened — a raw `duckdb.Connection`, or a `PGConn` (below) wrapping a real
psycopg2 connection. `PGConn` gives a psycopg2 connection the same
`con.execute(sql, params).fetchone()/.fetchall()` chaining DuckDB's
connection already provides natively, which is what lets every existing
`con.execute(...)` call site in exec.py keep working completely unmodified.
Only the handful of places that ask the *warehouse catalog itself*
something dialect-specific (default schema name, view definitions, or
physical column types) call the dispatched helpers below, keyed off the
connection's own type — never a separately threaded "dialect" parameter,
so nothing here can drift out of sync with the connection actually in use.

Every dialect-specific mapping in this module (Postgres physical type
spellings, the array `udt_name` table, `pg_views` vs `duckdb_views()`) was
measured against a real Postgres 16 (see tests/pg_harness.py's ephemeral
cluster), not guessed from documentation.
"""
from __future__ import annotations

from typing import Dict

from .types import StrataType


class PGConn:
    """Wraps a psycopg2 connection so `con.execute(sql, params).fetchone()`
    chains the same way DuckDB's connection already does natively. `?`
    placeholders (DuckDB's style, used throughout exec.py) are translated to
    psycopg2's `%s` — safe here because every `?` in exec.py's own SQL is a
    bind-marker in code this project controls, never user data reaching a
    query as text."""

    def __init__(self, raw):
        raw.autocommit = False
        self.raw = raw
        self._cur = raw.cursor()

    def execute(self, sql: str, params=None) -> "PGConn":
        self._cur.execute(sql.replace("?", "%s"), params or None)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, n: int):
        return self._cur.fetchmany(n)

    @property
    def description(self):
        return self._cur.description

    def close(self) -> None:
        self._cur.close()
        self.raw.close()


def is_postgres(con) -> bool:
    return isinstance(con, PGConn)


def db_schema(con) -> str:
    """Default schema Strata's engine tables/views live in."""
    return "public" if is_postgres(con) else "main"


def live_view_defs(con) -> Dict[str, str]:
    """{view_name: definition_text}, used only for substring/regex search
    for a snapshot table name (see exec._current_snapshot_table,
    recover_metadata, protected_runs, gc_plan, gc_snapshots, run()).

    Measured against a real Postgres 16: `pg_views.definition` reconstructs
    the view's SQL through Postgres's own deparser and expands `SELECT *`
    into an explicit column list, unlike DuckDB's `duckdb_views().sql`
    (which stores the original text verbatim) — but the referenced table
    name still appears as a substring either way, which is all any caller
    here relies on."""
    if is_postgres(con):
        rows = con.execute(
            "SELECT viewname, definition FROM pg_views WHERE schemaname = ?",
            [db_schema(con)]).fetchall()
        return {name: defn for name, defn in rows}
    rows = con.execute(
        "SELECT view_name, sql FROM duckdb_views() WHERE schema_name='main'"
    ).fetchall()
    return {name: sql for name, sql in rows}


# Postgres reports an array column's own data_type as the bare literal
# "ARRAY" (information_schema.columns has no element-type column); the
# element type lives in udt_name instead, as its internal array-type name.
# Scoped to the element types Strata's type system actually declares today
# (types.py), not a general PostgreSQL type-name mapping.
_PG_ARRAY_ELEM = {
    "_int8": "BIGINT", "_int4": "BIGINT", "_float8": "DOUBLE PRECISION",
    "_text": "TEXT", "_varchar": "TEXT", "_bool": "BOOLEAN", "_date": "DATE",
    "_timestamp": "TIMESTAMP WITHOUT TIME ZONE", "_uuid": "UUID",
    "_jsonb": "JSONB", "_json": "JSONB", "_numeric": "NUMERIC",
}


def physical_schema(con, view: str) -> Dict[str, str]:
    """Actual physical column types of a live table/view, normalized into
    the same convention `physical_types()` below compares against.

    DuckDB already embeds precision/scale/element type directly in
    `information_schema.columns.data_type` (e.g. "DECIMAL(10,2)",
    "BIGINT[]"). Postgres does not: measured against a real Postgres 16,
    `numeric` columns never carry scale/precision in `data_type` itself
    (they're in the separate `numeric_precision`/`numeric_scale` columns)
    and array columns report the literal `data_type='ARRAY'` (element type
    in `udt_name`) — so this reconstructs the equivalent embedded string
    instead of trusting the bare `data_type`."""
    schema = db_schema(con)
    if is_postgres(con):
        rows = con.execute(
            "SELECT column_name, data_type, numeric_precision, "
            "numeric_scale, udt_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position", [schema, view]).fetchall()
        out: Dict[str, str] = {}
        for name, dtype, prec, scale, udt in rows:
            if dtype == "numeric" and prec is not None:
                out[name] = f"NUMERIC({prec},{scale or 0})"
            elif dtype == "ARRAY":
                out[name] = _PG_ARRAY_ELEM.get(udt, udt) + "[]"
            else:
                out[name] = dtype.upper()
        return out
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? "
        "ORDER BY ordinal_position", [schema, view]).fetchall()
    return {name: dtype for name, dtype in rows}


# DuckDB integral storage types that widen losslessly into a declared
# int64. Postgres has no HUGEINT; BIGINT/INTEGER cover what this project's
# own dialect emission (strata/dialects.py) ever produces for int64.
_DUCKDB_INT64_PHYSICAL = {"BIGINT", "INTEGER", "HUGEINT"}
_PG_INT64_PHYSICAL = {"BIGINT", "INTEGER"}


def physical_types(con, t: StrataType) -> set:
    """Acceptable warehouse storage types for a declared Strata type,
    dialect-aware via the connection actually in use. Narrowing (e.g. a
    BIGINT column promised, VARCHAR/TEXT found) is never accepted
    implicitly, in either dialect."""
    pg = is_postgres(con)
    if t.name == "int64":
        return set(_PG_INT64_PHYSICAL if pg else _DUCKDB_INT64_PHYSICAL)
    if t.name == "float64":
        return {"DOUBLE PRECISION"} if pg else {"DOUBLE", "REAL"}
    if t.name == "string":
        return {"TEXT"} if pg else {"VARCHAR"}
    if t.name == "bool":
        return {"BOOLEAN"}
    if t.name == "date":
        return {"DATE"}
    if t.name == "timestamp":
        return {"TIMESTAMP WITHOUT TIME ZONE"} if pg else \
            {"TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE"}
    if t.name == "uuid":
        return {"UUID"}
    if t.name == "json":
        return {"JSONB"} if pg else {"JSON"}
    if t.name == "decimal":
        type_str = f"NUMERIC({t.precision},{t.scale})" if pg \
            else f"DECIMAL({t.precision},{t.scale})"
        return {type_str}
    if t.name == "money":
        return {"NUMERIC(38,2)"} if pg else {"DECIMAL(38,2)"}
    if t.name == "array" and t.elem is not None:
        return {elem + "[]" for elem in physical_types(con, t.elem)}
    return set()
