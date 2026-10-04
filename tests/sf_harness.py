"""Ephemeral Snowflake emulator (LocalStack) or real account for integration tests.

Supports two modes:
1. Local emulator: Uses LocalStack Snowflake emulator via Docker
2. Real account: Uses Snowflake credentials from environment

Returns a SnowflakeConn that works with strata.exec (same API as DuckDB/PGConn).
Skips tests if neither emulator nor real credentials are available.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from strata.adapters import get_adapter
from strata.dbcompat import SnowflakeConn


def has_snowflake_credentials() -> bool:
    """Check if real Snowflake credentials are available."""
    return all(
        os.environ.get(v)
        for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
    )


def _start_localstack_snowflake(port: int = 4444) -> Optional[subprocess.Popen]:
    """Start LocalStack with Snowflake emulator.

    Returns the process if successful, None if Docker not available.
    """
    if not shutil.which("docker"):
        return None

    # Check if LocalStack image exists locally or can be pulled
    try:
        subprocess.run(
            ["docker", "pull", "localstack/localstack:latest"],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        return None

    # Start LocalStack with Snowflake service
    proc = subprocess.Popen(
        [
            "docker", "run", "--rm",
            "-p", f"{port}:443",
            "-e", "SERVICES=snowflake",
            "-e", "DEBUG=1",
            "localstack/localstack:latest",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


@contextlib.contextmanager
def ephemeral_snowflake(
    account: str = "test",
    user: str = "test",
    password: str = "test",
    warehouse: str = "TEST_WH",
    database: str = "STRATA_TEST",
    schema: str = "PUBLIC",
    port: int = 4444,
) -> SnowflakeConn | None:
    """Yield a SnowflakeConn to an ephemeral Snowflake instance.

    Tries in order:
    1. LocalStack Snowflake emulator via Docker
    2. Real Snowflake account via credentials

    Yields None if neither is available (tests should skip).
    """
    # Try 1: LocalStack emulator
    proc = _start_localstack_snowflake(port)
    if proc:
        try:
            # Wait for LocalStack to be ready
            for _ in range(60):
                try:
                    import snowflake.connector
                    conn = snowflake.connector.connect(
                        account=account,
                        user=user,
                        password=password,
                        warehouse=warehouse,
                        database=database,
                        schema=schema,
                        host="localhost",
                        port=port,
                        protocol="https",
                    )
                    conn.close()
                    break
                except Exception:
                    time.sleep(1)
            else:
                proc.terminate()
                yield None
                return

            # Return connection via SnowflakeConn
            import snowflake.connector
            raw = snowflake.connector.connect(
                account=account,
                user=user,
                password=password,
                warehouse=warehouse,
                database=database,
                schema=schema,
                host="localhost",
                port=port,
                protocol="https",
            )
            yield SnowflakeConn(raw)
        finally:
            proc.terminate()
        return

    # Try 2: Real Snowflake with credentials
    if has_snowflake_credentials():
        try:
            import snowflake.connector
            raw = snowflake.connector.connect(
                account=os.environ["SNOWFLAKE_ACCOUNT"],
                user=os.environ["SNOWFLAKE_USER"],
                password=os.environ["SNOWFLAKE_PASSWORD"],
                warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", warehouse),
                database=os.environ.get("SNOWFLAKE_DATABASE", database),
                schema=os.environ.get("SNOWFLAKE_SCHEMA", schema),
                role=os.environ.get("SNOWFLAKE_ROLE"),
            )
            yield SnowflakeConn(raw)
            return
        except Exception:
            pass

    # Neither available
    yield None


@contextlib.contextmanager
def snowflake_adapter(
    account: str = "test",
    user: str = "test",
    password: str = "test",
    warehouse: str = "TEST_WH",
    database: str = "STRATA_TEST",
    schema: str = "PUBLIC",
    port: int = 4444,
):
    """Yield a SnowflakeWarehouse adapter for CLI integration tests.

    Same credential logic as ephemeral_snowflake.
    """
    proc = _start_localstack_snowflake(port)
    if proc:
        try:
            for _ in range(60):
                try:
                    import snowflake.connector
                    conn = snowflake.connector.connect(
                        account=account,
                        user=user,
                        password=password,
                        warehouse=warehouse,
                        database=database,
                        schema=schema,
                        host="localhost",
                        port=port,
                        protocol="https",
                    )
                    conn.close()
                    break
                except Exception:
                    time.sleep(1)
            else:
                proc.terminate()
                yield None
                return
            yield get_adapter(
                "snowflake",
                account=account,
                user=user,
                password=password,
                warehouse=warehouse,
                database=database,
                schema=schema,
            )
        finally:
            proc.terminate()
        return

    if has_snowflake_credentials():
        try:
            yield get_adapter(
                "snowflake",
                account=os.environ["SNOWFLAKE_ACCOUNT"],
                user=os.environ["SNOWFLAKE_USER"],
                password=os.environ["SNOWFLAKE_PASSWORD"],
                warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", warehouse),
                database=os.environ.get("SNOWFLAKE_DATABASE", database),
                schema=os.environ.get("SNOWFLAKE_SCHEMA", schema),
                role=os.environ.get("SNOWFLAKE_ROLE"),
            )
            return
        except Exception:
            pass

    yield None