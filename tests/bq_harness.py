"""Ephemeral BigQuery emulator or real project for integration tests.

Supports two modes:
1. Local emulator: Uses `gcloud emulators bigquery` (requires google-cloud-sdk)
2. Real project: Uses GCP credentials from environment (GOOGLE_APPLICATION_CREDENTIALS or gcloud auth)

Returns a BigQueryConn that works with strata.exec (same API as DuckDB/PGConn).
Skips tests if neither emulator nor real credentials are available.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from strata.adapters import get_adapter
from strata.dbcompat import BigQueryConn


def find_gcloud() -> Optional[Path]:
    """Find gcloud executable for BigQuery emulator."""
    return Path(shutil.which("gcloud")) if shutil.which("gcloud") else None


def has_bq_credentials() -> bool:
    """Check if real BigQuery credentials are available."""
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("BIGQUERY_PROJECT")
        or shutil.which("gcloud")
    )


@contextlib.contextmanager
def ephemeral_bigquery(
    project: str = "strata-test",
    dataset: str = "strata_test",
    location: str = "US",
    port: int = 9050,
) -> BigQueryConn | None:
    """Yield a BigQueryConn to an ephemeral BigQuery instance.

    Tries in order:
    1. Local BigQuery emulator via `gcloud emulators bigquery`
    2. Real BigQuery project via credentials

    Yields None if neither is available (tests should skip).
    """
    # Try 1: Local emulator
    gcloud = find_gcloud()
    if gcloud:
        with tempfile.TemporaryDirectory() as td:
            # Start BigQuery emulator
            env = dict(os.environ)
            # Use a dedicated data dir for emulator
            emulator_data = Path(td) / "bq_emulator"
            emulator_data.mkdir()

            proc = subprocess.Popen(
                [
                    str(gcloud), "emulators", "bigquery", "start",
                    f"--host-port=localhost:{port}",
                    f"--data-dir={emulator_data}",
                    "--project", project,
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                # Wait for emulator to be ready
                import time
                for _ in range(30):
                    try:
                        # Test connection
                        from google.cloud import bigquery
                        client = bigquery.Client(
                            project=project,
                            client_options={"api_endpoint": f"http://localhost:{port}"},
                        )
                        client.list_datasets(max_results=1)
                        break
                    except Exception:
                        time.sleep(0.5)
                else:
                    proc.terminate()
                    yield None
                    return

                # Create dataset
                try:
                    client.create_dataset(f"{project}.{dataset}", exists_ok=True)
                except Exception:
                    pass

                # Return connection
                from strata.dbcompat import BigQueryConn
                yield BigQueryConn(client, dataset)
            finally:
                proc.terminate()
            return

    # Try 2: Real BigQuery with credentials
    if has_bq_credentials():
        try:
            from google.cloud import bigquery
            # Use project from env or default
            real_project = os.environ.get("BIGQUERY_PROJECT", project)
            client = bigquery.Client(project=real_project, location=location)
            # Create dataset if needed
            try:
                client.create_dataset(f"{real_project}.{dataset}", exists_ok=True)
            except Exception:
                pass
            yield BigQueryConn(client, dataset)
            return
        except Exception:
            pass

    # Neither available
    yield None


@contextlib.contextmanager
def bigquery_adapter(
    project: str = "strata-test",
    dataset: str = "strata_test",
    location: str = "US",
    port: int = 9050,
):
    """Yield a BigQueryWarehouse adapter for CLI integration tests.

    Same credential logic as ephemeral_bigquery.
    """
    gcloud = find_gcloud()
    if gcloud:
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            emulator_data = Path(td) / "bq_emulator"
            emulator_data.mkdir()
            proc = subprocess.Popen(
                [
                    str(gcloud), "emulators", "bigquery", "start",
                    f"--host-port=localhost:{port}",
                    f"--data-dir={emulator_data}",
                    "--project", project,
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                import time
                for _ in range(30):
                    try:
                        from google.cloud import bigquery
                        client = bigquery.Client(
                            project=project,
                            client_options={"api_endpoint": f"http://localhost:{port}"},
                        )
                        client.list_datasets(max_results=1)
                        break
                    except Exception:
                        time.sleep(0.5)
                else:
                    proc.terminate()
                    yield None
                    return
                yield get_adapter("bigquery", project=project, dataset=dataset, location=location)
            finally:
                proc.terminate()
            return

    if has_bq_credentials():
        try:
            real_project = os.environ.get("BIGQUERY_PROJECT", project)
            yield get_adapter("bigquery", project=real_project, dataset=dataset, location=location)
            return
        except Exception:
            pass

    yield None