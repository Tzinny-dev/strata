"""Warehouse adapters: abstract interface for real execution.

The Strata engine today runs against DuckDB (the only warehouse with
a real Python driver). Other warehouses (Postgres, BigQuery, Snowflake)
are reachable through their official drivers (`psycopg2`,
`google-cloud-bigquery`, `snowflake-connector-python`) — none of which
is installed in the base environment, so their adapters raise
`AdapterNotAvailable` (E095) with the package to install.

The `Warehouse` ABC defines the six operations the engine needs:
`connect`, `execute`, `fetch`, `materialize` (CREATE TABLE snap),
`drop` and `list_views`. A concrete adapter implements only those
methods; `sqlgen` already produces dialect-specific SQL, so the
adapter's job is transport, not translation.

Usage:
    from strata.adapters import get_adapter, AdapterNotAvailable
    try:
        wh = get_adapter("postgres", conn_string="...")
    except AdapterNotAvailable as e:
        print(e.help)   # -> "pip install psycopg2-binary"
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional


class AdapterNotAvailable(Exception):
    def __init__(self, dialect: str, package: str, msg: str):
        super().__init__(msg)
        self.dialect = dialect
        self.package = package
        self.help = (f"warehouse adapter for {dialect} is not available; "
                     f"install {package} to enable real execution")


class Warehouse(abc.ABC):
    """Minimal interface the Strata engine needs from a warehouse."""

    @abc.abstractmethod
    def connect(self) -> None:
        ...

    @abc.abstractmethod
    def execute(self, sql: str) -> None:
        ...

    @abc.abstractmethod
    def fetch(self, sql: str) -> List[tuple]:
        ...

    @abc.abstractmethod
    def materialize(self, name: str, sql: str) -> None:
        """CREATE TABLE <name> AS <sql> atomically."""
        ...

    @abc.abstractmethod
    def drop(self, name: str) -> None:
        ...

    @abc.abstractmethod
    def list_views(self) -> List[str]:
        ...


_MISSING: Dict[str, tuple] = {
    "postgres": ("psycopg2-binary", "psycopg2"),
    "bigquery": ("google-cloud-bigquery", "google.cloud.bigquery"),
    "snowflake": ("snowflake-connector-python", "snowflake.connector"),
}


def get_adapter(dialect: str, **kw) -> Warehouse:
    """Return a real Warehouse or raise AdapterNotAvailable (E095).

    Only ``duckdb`` is available today (via ``duckdb``). Other
    dialects look for their driver in the environment and raise
    with the install instruction when absent.
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
            f"real execution is unavailable") from None
    raise AdapterNotAvailable(
        dialect, pkg,
        f"warehouse adapter for {dialect!r} not yet implemented; "
        f"install {pkg} and open a feature request") from None


class DuckDBWarehouse(Warehouse):
    """Thin wrapper around a duckdb.Connection."""

    def __init__(self, con):
        self.con = con

    def connect(self) -> None:
        pass

    def execute(self, sql: str) -> None:
        self.con.execute(sql)

    def fetch(self, sql: str) -> List[tuple]:
        return self.con.execute(sql).fetchall()

    def materialize(self, name: str, sql: str) -> None:
        self.con.execute(f"CREATE TABLE {name} AS {sql}")

    def drop(self, name: str) -> None:
        self.con.execute(f"DROP TABLE IF EXISTS {name}")

    def list_views(self) -> List[str]:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type='VIEW'").fetchall()
        return [r[0] for r in rows]
