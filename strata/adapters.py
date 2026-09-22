"""Warehouse adapters: abstract interface for real execution.

DuckDB has a real `Warehouse` implementation (`DuckDBWarehouse`). Postgres
has real *engine* execution via `strata.cli.open_warehouse("postgres://...")`
which opens a `strata.dbcompat.PGConn` (psycopg2, tested against ephemeral
Postgres 16 in `tests/pg_harness.py`) — the engine (`strata.exec`) works
against either connection type without code changes (`dbcompat.is_postgres`).

`strata.adapters.Warehouse` / `get_adapter("postgres")` is still a stub:
even when `psycopg2-binary` is installed, `get_adapter("postgres")` raises
`AdapterNotAvailable` (E095) because no `PostgresWarehouse` is wired to
the ABC yet — use the CLI path or `dbcompat.PGConn` directly for real
Postgres runs. BigQuery/Snowflake are SQL-emit only (no driver in base
env, stub raises with install hint `pip install strata[bigquery]` etc.).

`sqlgen` already produces dialect-specific SQL for all four; the adapter's
job is transport, not translation.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class AdapterNotAvailable(Exception):
    def __init__(self, dialect: str, package: str, msg: str) -> None:
        super().__init__(msg)
        self.dialect = dialect
        self.package = package
        self.help = (f"warehouse adapter for {dialect} is not available; "
                     f"install {package} to enable real execution")


class Warehouse(abc.ABC):
    """Minimal interface the Strata engine needs from a warehouse."""

    @abc.abstractmethod
    def connect(self) -> None:
        """Open the warehouse connection."""
        ...

    @abc.abstractmethod
    def execute(self, sql: str) -> None:
        """Run a statement that returns no rows."""
        ...

    @abc.abstractmethod
    def fetch(self, sql: str) -> List[tuple]:
        """Run a query and return all result rows."""
        ...

    @abc.abstractmethod
    def materialize(self, name: str, sql: str, partition_by: Optional[List[str]] = None) -> None:
        """CREATE TABLE <name> AS <sql> atomically.

        If partition_by is provided and dialect supports it, the table
        will be partitioned by those columns.
        """
        ...

    @abc.abstractmethod
    def drop(self, name: str) -> None:
        """Drop the physical table if it exists."""
        ...

    @abc.abstractmethod
    def list_views(self) -> List[str]:
        """Names of live views in the warehouse default schema."""
        ...


_MISSING: Dict[str, tuple] = {
    "postgres": ("psycopg2-binary", "psycopg2"),
    "bigquery": ("google-cloud-bigquery", "google.cloud.bigquery"),
    "snowflake": ("snowflake-connector-python", "snowflake.connector"),
}


def get_adapter(dialect: str, **kw) -> Warehouse:
    """Return a real Warehouse or raise AdapterNotAvailable (E095).

    ``duckdb`` returns a live ``DuckDBWarehouse``. ``postgres`` has a real
    engine path (`strata.cli.open_warehouse("postgres://...")` →
    ``dbcompat.PGConn``) but no ``Warehouse`` ABC implementation yet — this
    function raises ``AdapterNotAvailable`` with a direct hint even when
    ``psycopg2`` is importable, so callers don't misread "installed" as
    "wired". BigQuery/Snowflake raise with the ``pip install strata[...]``
    hint when their driver is absent, and "not yet implemented" when present
    but unwired.
    """
    if dialect == "duckdb":
        import duckdb
        return DuckDBWarehouse(duckdb.connect(kw.get("database", ":memory:")))
    pkg, mod = _MISSING.get(dialect, ("", ""))
    try:
        __import__(mod)
    except ImportError:
        raise AdapterNotAvailable(
            dialect, pkg,
            f"warehouse adapter for {dialect!r} requires {pkg}; "
            f"real execution is unavailable (pip install strata[{dialect}])") from None
    if dialect == "postgres":
        raise AdapterNotAvailable(
            dialect, pkg,
            f"warehouse adapter for {dialect!r} has no Warehouse ABC implementation yet; "
            f"use strata.cli.open_warehouse('postgres://...') / dbcompat.PGConn for real Postgres runs "
            f"(driver {pkg} is installed but adapters.Warehouse is not wired)") from None
    raise AdapterNotAvailable(
        dialect, pkg,
        f"warehouse adapter for {dialect!r} not yet implemented; "
        f"install {pkg} and open a feature request (SQL emit via --dialect {dialect} works)") from None


class DuckDBWarehouse(Warehouse):
    """Thin wrapper around a duckdb.Connection."""

    def __init__(self, con: Any) -> None:
        self.con = con

    def connect(self) -> None:
        """Open the warehouse connection."""
        pass

    def execute(self, sql: str) -> None:
        """Run a statement that returns no rows."""
        self.con.execute(sql)

    def fetch(self, sql: str) -> List[tuple]:
        """Run a query and return all result rows."""
        return self.con.execute(sql).fetchall()

    def materialize(self, name: str, sql: str, partition_by: Optional[List[str]] = None) -> None:
        # DuckDB doesn't support partitioning in CTAS, so we ignore it
        """Create the table atomically (partitioning ignored where unsupported)."""
        self.con.execute(f"CREATE TABLE {name} AS {sql}")

    def drop(self, name: str) -> None:
        """Drop the physical table if it exists."""
        self.con.execute(f"DROP TABLE IF EXISTS {name}")

    def list_views(self) -> List[str]:
        """Names of live views in the warehouse default schema."""
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='VIEW'").fetchall()
        return [r[0] for r in rows]