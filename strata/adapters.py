"""Warehouse adapters: abstract interface for real execution.

DuckDB has a real `Warehouse` implementation (`DuckDBWarehouse`). Postgres
has real *engine* execution via `strata.cli.open_warehouse("postgres://...")`
which opens a `strata.dbcompat.PGConn` (psycopg2, tested against ephemeral
Postgres 16 in `tests/pg_harness.py`) — the engine (`strata.exec`) works
against either connection type without code changes (`dbcompat.is_postgres`).

BigQuery and Snowflake now have real Warehouse implementations
(`BigQueryWarehouse` / `SnowflakeWarehouse`) backed by
`strata.dbcompat.BigQueryConn` / `SnowflakeConn` — same chaining
`con.execute(sql, params).fetchall()` as DuckDB/PGConn, so `exec.py`
works without changes (`dbcompat.is_bigquery` / `is_snowflake`).

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
    "wired". BigQuery/Snowflake return real warehouses when their driver
    is installed, otherwise raise with the ``pip install strata[...]`` hint.
    """
    if dialect == "duckdb":
        import duckdb
        return DuckDBWarehouse(duckdb.connect(kw.get("database", ":memory:")))
    if dialect == "bigquery":
        pkg, mod = _MISSING["bigquery"]
        try:
            __import__(mod)
        except ImportError:
            raise AdapterNotAvailable(
                dialect, pkg,
                f"warehouse adapter for {dialect!r} requires {pkg}; "
                f"real execution is unavailable (pip install strata[{dialect}])") from None
        # project/dataset from kwargs or env — BigQuery client picks defaults if omitted
        return BigQueryWarehouse(
            project=kw.get("project"),
            dataset=kw.get("dataset", kw.get("database", "")),
            location=kw.get("location"),
            credentials=kw.get("credentials"),
        )
    if dialect == "snowflake":
        pkg, mod = _MISSING["snowflake"]
        try:
            __import__(mod)
        except ImportError:
            raise AdapterNotAvailable(
                dialect, pkg,
                f"warehouse adapter for {dialect!r} requires {pkg}; "
                f"real execution is unavailable (pip install strata[{dialect}])") from None
        return SnowflakeWarehouse(
            account=kw.get("account"),
            user=kw.get("user"),
            password=kw.get("password"),
            warehouse=kw.get("warehouse"),
            database=kw.get("database"),
            schema=kw.get("schema"),
            role=kw.get("role"),
        )
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


class BigQueryWarehouse(Warehouse):
    """Warehouse backed by google-cloud-bigquery. Also acts as a DB-API-like
    connection for exec.py (execute/fetchone/fetchall chaining via BigQueryConn)."""

    def __init__(self, project: Optional[str] = None, dataset: str = "", location: Optional[str] = None, credentials: Any = None) -> None:
        from google.cloud import bigquery as bq  # type: ignore

        # bigquery.Client picks project from env/credentials if not given
        self.client = bq.Client(project=project, location=location, credentials=credentials) if project or credentials or location else bq.Client()
        # dataset may be "project.dataset" or just "dataset"
        if dataset and "." in dataset and not project:
            # split project.dataset
            proj, ds = dataset.split(".", 1)
            self.dataset = ds
            # recreate client with project if needed
            if not project:
                try:
                    self.client = bq.Client(project=proj, location=location, credentials=credentials)
                except Exception:
                    pass
        else:
            self.dataset = dataset or getattr(self.client, "dataset", "") or ""
        # BigQueryConn for exec.py dispatch
        from .dbcompat import BigQueryConn

        self._conn = BigQueryConn(self.client, self.dataset)
        self.con = self._conn  # alias for exec.py is_* checks (is_bigquery checks BigQueryConn, but also handle Warehouse)

    def connect(self) -> None:
        pass

    # Warehouse ABC
    def execute(self, sql: str, params: Optional[Any] = None) -> Any:  # type: ignore
        """Warehouse execute (no params) or Conn execute (with params) — both chain."""
        return self._conn.execute(sql, params)

    def fetch(self, sql: str) -> List[tuple]:
        return self._conn.execute(sql).fetchall()

    def fetchone(self) -> Optional[tuple]:
        return self._conn.fetchone()

    def fetchall(self) -> List[tuple]:
        return self._conn.fetchall()

    @property
    def description(self) -> Optional[Any]:
        return self._conn.description

    def close(self) -> None:
        self._conn.close()

    def materialize(self, name: str, sql: str, partition_by: Optional[List[str]] = None) -> None:
        # BigQuery CREATE OR REPLACE TABLE `dataset.name` AS (sql) — partition_by ignored for now (requires PARTITION BY clause)
        tbl = f"`{self.dataset}.{name}`" if self.dataset and "." not in name else f"`{name}`"
        # Use backticks, handle already-qualified name
        if self.dataset and "." not in name:
            tbl = f"`{self.dataset}.{name}`"
        else:
            tbl = name if "." in name else f"`{name}`"
        self._conn.execute(f"CREATE OR REPLACE TABLE {tbl} AS {sql}")

    def drop(self, name: str) -> None:
        tbl = f"`{self.dataset}.{name}`" if self.dataset and "." not in name else f"`{name}`" if "." not in name else name
        try:
            self._conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass

    def list_views(self) -> List[str]:
        try:
            return list(self._conn.execute(
                f"SELECT table_name FROM `{self.dataset}.INFORMATION_SCHEMA.VIEWS`"
            ).fetchall())
        except Exception:
            return []


class SnowflakeWarehouse(Warehouse):
    """Warehouse backed by snowflake-connector-python. Also acts as DB-API conn."""

    def __init__(self, account: Optional[str] = None, user: Optional[str] = None, password: Optional[str] = None, warehouse: Optional[str] = None, database: Optional[str] = None, schema: Optional[str] = None, role: Optional[str] = None) -> None:
        import snowflake.connector  # type: ignore

        # snowflake.connector.connect requires account/user/password — let it raise if missing
        self.raw = snowflake.connector.connect(
            account=account or "",
            user=user or "",
            password=password or "",
            warehouse=warehouse or "",
            database=database or "",
            schema=schema or "PUBLIC",
            role=role or "",
        )
        from .dbcompat import SnowflakeConn

        self._conn = SnowflakeConn(self.raw)
        self.con = self._conn

    def connect(self) -> None:
        pass

    def execute(self, sql: str, params: Optional[Any] = None) -> Any:  # type: ignore
        return self._conn.execute(sql, params)

    def fetch(self, sql: str) -> List[tuple]:
        return self._conn.execute(sql).fetchall()

    def fetchone(self) -> Optional[tuple]:
        return self._conn.fetchone()

    def fetchall(self) -> List[tuple]:
        return self._conn.fetchall()

    @property
    def description(self) -> Optional[Any]:
        return self._conn.description

    def close(self) -> None:
        self._conn.close()

    def materialize(self, name: str, sql: str, partition_by: Optional[List[str]] = None) -> None:
        # Snowflake CREATE OR REPLACE TABLE name AS sql
        self._conn.execute(f"CREATE OR REPLACE TABLE {name} AS {sql}")

    def drop(self, name: str) -> None:
        try:
            self._conn.execute(f"DROP TABLE IF EXISTS {name}")
        except Exception:
            pass

    def list_views(self) -> List[str]:
        try:
            rows = self._conn.execute(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA = CURRENT_SCHEMA()"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []