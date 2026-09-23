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

from typing import Any, Dict, List, Optional, Set

from .types import StrataType


class PGConn:
    """Wraps a psycopg2 connection so `con.execute(sql, params).fetchone()`
    chains the same way DuckDB's connection already does natively. `?`
    placeholders (DuckDB's style, used throughout exec.py) are translated to
    psycopg2's `%s` — safe here because every `?` in exec.py's own SQL is a
    bind-marker in code this project controls, never user data reaching a
    query as text."""

    def __init__(self, raw: Any) -> None:
        raw.autocommit = False
        self.raw = raw
        self._cur = raw.cursor()

    def execute(self, sql: str, params: Optional[Any] = None) -> "PGConn":
        """Run SQL (with `?` bind markers) and return self for chaining."""
        self._cur.execute(sql.replace("?", "%s"), params or None)
        return self

    def fetchone(self) -> Optional[tuple]:
        """Return the next result row, or None."""
        return self._cur.fetchone()

    def fetchall(self) -> List[tuple]:
        """Return all remaining result rows as a list."""
        return self._cur.fetchall()

    def fetchmany(self, n: int) -> List[tuple]:
        """Return up to n remaining result rows."""
        return self._cur.fetchmany(n)

    @property
    def description(self) -> Optional[Any]:
        """DB-API description of the most recent result set."""
        return self._cur.description

    def close(self) -> None:
        """Close the cursor and the underlying raw connection."""
        self._cur.close()
        self.raw.close()


class BigQueryConn:
    """Wraps a bigquery.Client so `con.execute(sql, params).fetchone()` chains like DuckDB/PGConn.

    BigQuery has no `?` placeholder — params are inlined as quoted literals
    (only schema/table names from exec.py, never user data as free text).
    """

    def __init__(self, client: Any, dataset: str = "") -> None:
        self.client = client
        self.dataset = dataset
        self._rows: List[tuple] = []
        self._description: Optional[Any] = None
        self._cur_index = 0

    def _inline_params(self, sql: str, params: Optional[Any]) -> str:
        if not params:
            return sql
        # exec.py only uses `?` for schema/table names — safe to inline as 'literal'
        out = sql
        for p in params:  # type: ignore
            lit = "'" + str(p).replace("'", "''") + "'"
            out = out.replace("?", lit, 1)
        return out

    def execute(self, sql: str, params: Optional[Any] = None) -> "BigQueryConn":
        """Run SQL and store result for fetch* chaining."""
        q = self._inline_params(sql, params)
        # BigQuery `client.query` is async — wait for result
        job = self.client.query(q)
        rows = list(job.result())
        # job.result() yields Row objects — convert to tuple, capture description
        self._rows = [tuple(r.values()) if hasattr(r, "values") else tuple(r) for r in rows]
        self._cur_index = 0
        # description from job.schema
        try:
            self._description = [(f.name, f.field_type) for f in (job.schema or [])]
        except Exception:
            self._description = None
        return self

    def fetchone(self) -> Optional[tuple]:
        if self._cur_index >= len(self._rows):
            return None
        row = self._rows[self._cur_index]
        self._cur_index += 1
        return row

    def fetchall(self) -> List[tuple]:
        rows = self._rows[self._cur_index :]
        self._cur_index = len(self._rows)
        return rows

    def fetchmany(self, n: int) -> List[tuple]:
        rows = self._rows[self._cur_index : self._cur_index + n]
        self._cur_index += len(rows)
        return rows

    @property
    def description(self) -> Optional[Any]:
        return self._description

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


class SnowflakeConn:
    """Wraps a snowflake.connector connection like PGConn ( ? → %s )."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self._cur = raw.cursor()

    def execute(self, sql: str, params: Optional[Any] = None) -> "SnowflakeConn":
        # snowflake-connector uses %s or ? depending, accept both — translate ? → %s
        q = sql.replace("?", "%s") if params else sql
        self._cur.execute(q, params or None)
        return self

    def fetchone(self) -> Optional[tuple]:
        return self._cur.fetchone()

    def fetchall(self) -> List[tuple]:
        return self._cur.fetchall()

    def fetchmany(self, n: int) -> List[tuple]:
        return self._cur.fetchmany(n)

    @property
    def description(self) -> Optional[Any]:
        return self._cur.description

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass
        try:
            self.raw.close()
        except Exception:
            pass


def is_postgres(con: Any) -> bool:
    """True when `con` is a PGConn wrapper (Postgres), not a DuckDB connection."""
    return isinstance(con, PGConn) or type(con).__name__ == "PostgresWarehouse"


def is_bigquery(con: Any) -> bool:
    """True when `con` is a BigQueryConn wrapper or BigQueryWarehouse."""
    return isinstance(con, BigQueryConn) or type(con).__name__ == "BigQueryWarehouse" or hasattr(con, "_conn") and isinstance(getattr(con, "_conn", None), BigQueryConn)


def is_snowflake(con: Any) -> bool:
    """True when `con` is a SnowflakeConn wrapper or SnowflakeWarehouse."""
    return isinstance(con, SnowflakeConn) or type(con).__name__ == "SnowflakeWarehouse" or hasattr(con, "_conn") and isinstance(getattr(con, "_conn", None), SnowflakeConn)


def db_schema(con: Any) -> str:
    """Default schema Strata's engine tables/views live in."""
    if is_postgres(con):
        return "public"
    if is_bigquery(con):
        return con.dataset or "default"
    if is_snowflake(con):
        # Snowflake current schema — use session value or PUBLIC as fallback
        try:
            row = con.execute("SELECT CURRENT_SCHEMA()").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return "PUBLIC"
    return "main"


def live_view_defs(con: Any) -> Dict[str, str]:
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
    if is_bigquery(con):
        # BigQuery INFORMATION_SCHEMA.VIEWS in the dataset
        dataset = db_schema(con)
        try:
            rows = con.execute(
                f"SELECT table_name, view_definition FROM `{dataset}.INFORMATION_SCHEMA.VIEWS`"
            ).fetchall()
            return {name: defn for name, defn in rows}
        except Exception:
            return {}
    if is_snowflake(con):
        schema = db_schema(con)
        try:
            rows = con.execute(
                "SELECT TABLE_NAME, VIEW_DEFINITION FROM INFORMATION_SCHEMA.VIEWS "
                "WHERE TABLE_SCHEMA = ?", [schema]
            ).fetchall()
            return {name: defn for name, defn in rows}
        except Exception:
            return {}
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


def physical_schema(con: Any, view: str) -> Dict[str, str]:
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
    if is_bigquery(con):
        # BigQuery INFORMATION_SCHEMA.COLUMNS — data_type is STRING, INT64, FLOAT64, BOOL, DATE, TIMESTAMP, NUMERIC, JSON etc.
        try:
            dataset = db_schema(con)
            rows = con.execute(
                f"SELECT column_name, data_type FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS` "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [view],
            ).fetchall()
            return {name: dtype.upper() for name, dtype in rows}
        except Exception:
            return {}
    if is_snowflake(con):
        try:
            rows = con.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                [schema, view],
            ).fetchall()
            return {name: dtype.upper() for name, dtype in rows}
        except Exception:
            return {}
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


def physical_types(con: Any, t: StrataType) -> Set[str]:
    """Acceptable warehouse storage types for a declared Strata type,
    dialect-aware via the connection actually in use. Narrowing (e.g. a
    BIGINT column promised, VARCHAR/TEXT found) is never accepted
    implicitly, in either dialect."""
    pg = is_postgres(con)
    bq = is_bigquery(con)
    sf = is_snowflake(con)
    # BigQuery and Snowflake share Postgres-like spellings for most types (TEXT/BOOLEAN/DATE)
    is_pg_like = pg or bq or sf
    if t.name == "int64":
        if pg or bq or sf:
            return set(_PG_INT64_PHYSICAL)
        return set(_DUCKDB_INT64_PHYSICAL)
    if t.name == "float64":
        return {"DOUBLE PRECISION", "FLOAT64"} if is_pg_like else {"DOUBLE", "REAL", "FLOAT"}
    if t.name == "string":
        return {"TEXT", "STRING"} if is_pg_like else {"VARCHAR"}
    if t.name == "bool":
        return {"BOOLEAN", "BOOL"} if bq else {"BOOLEAN"}
    if t.name == "date":
        return {"DATE"}
    if t.name == "timestamp":
        if bq:
            return {"TIMESTAMP", "DATETIME"}
        return {"TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP_TZ", "TIMESTAMP_NTZ"} if sf else \
            ({"TIMESTAMP WITHOUT TIME ZONE"} if pg else {"TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE"})
    if t.name == "uuid":
        return {"UUID", "STRING"} if bq else {"UUID"}
    if t.name == "json":
        if bq:
            return {"JSON", "STRING"}
        if sf:
            return {"VARIANT", "OBJECT", "ARRAY"}
        return {"JSONB"} if pg else {"JSON"}
    if t.name == "decimal":
        if bq:
            return {f"NUMERIC", f"BIGNUMERIC", f"DECIMAL({t.precision},{t.scale})", f"NUMERIC({t.precision},{t.scale})"}
        type_str = f"NUMERIC({t.precision},{t.scale})" if is_pg_like \
            else f"DECIMAL({t.precision},{t.scale})"
        return {type_str}
    if t.name == "money":
        return {"NUMERIC", "NUMERIC(38,2)", "DECIMAL(38,2)"} if bq else \
            ({"NUMERIC(38,2)"} if is_pg_like else {"DECIMAL(38,2)"})
    if t.name == "array" and t.elem is not None:
        if bq:
            return {f"ARRAY<{elem}>" for elem in physical_types(con, t.elem)}
        if sf:
            return {"ARRAY", "VARIANT"}
        return {elem + "[]" for elem in physical_types(con, t.elem)}
    return set()