"""Ephemeral BigQuery emulator or real project for integration tests.

Supports two modes:
1. Local emulator: Uses `ghcr.io/goccy/bigquery-emulator` via Docker
   (the only open-source BigQuery emulator; `gcloud emulators bigquery`
   does not exist in the Cloud SDK).
2. Real project: Uses GCP credentials from environment (GOOGLE_APPLICATION_CREDENTIALS).

Returns a BigQueryConn that works with strata.exec (same API as DuckDB/PGConn).
Skips tests if neither emulator nor real credentials are available.
"""
from __future__ import annotations

import os
import contextlib
import shutil
import subprocess
from typing import Optional

from strata.adapters import get_adapter
from strata.dbcompat import BigQueryConn

EMULATOR_IMAGE = "ghcr.io/goccy/bigquery-emulator:latest"
EMULATOR_NAME = "strata-bq-emulator"


class EmulatorBigQueryConn(BigQueryConn):
    """BigQueryConn that talks to the emulator over the jobs.query REST endpoint.

    google-cloud-bigquery executes query jobs via ``POST /jobs`` + job polling,
    which goccy/bigquery-emulator answers with 409 for DDL/DML — that made the
    original harness hang forever. The emulator's ``jobs.query`` endpoint handles
    SELECT and DDL/DML alike, so we bypass the client for execution and keep the
    exact BigQueryConn API exec.py expects (isinstance checks included).
    """

    def __init__(
        self,
        client,
        dataset: str = "",
        endpoint: str = "http://localhost:9050",
        timeout: float = 60,
    ) -> None:
        super().__init__(client, dataset)
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    def _coerce(self, v):
        if v is None:
            return None
        if isinstance(v, list):
            return [self._coerce(x.get("v") if isinstance(x, dict) else x) for x in v]
        if isinstance(v, dict):
            return {k: self._coerce(x) for k, x in v.items()}
        s = str(v)
        if s == "":
            return None
        if s == "true":
            return True
        if s == "false":
            return False
        try:
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except ValueError:
            return s

    def _rows_for(self, data: dict) -> list[tuple]:
        return [tuple(self._coerce(f.get("v")) for f in (row.get("f") or [])) for row in (data.get("rows") or [])]

    def execute(self, sql: str, params=None) -> "EmulatorBigQueryConn":
        import re
        import time

        import requests

        # goccy's ZetaSQL analyzer only accepts NUMERIC(P,2) with P<=31, while
        # real BigQuery uses NUMERIC(38,2). Shrink it so DDL and emitted model
        # SQL both land. NUMERIC (bare) stays in physical_types()'s allowed set,
        # and a NUMERIC(10,2) pin check still fails as designed.
        sql = sql.replace("NUMERIC(38,2)", "NUMERIC").replace("DECIMAL(38,2)", "DECIMAL")

        project = self.client.project
        # goccy cannot resolve 3-part backticked references (`project.dataset`.t):
        # DDL "succeeds" but later SELECT/INSERT against the same name says
        # "Table not found". Tests write `{project}.{dataset}` names (valid on
        # real BigQuery); downgrade them to the 2-part form the emulator resolves.
        sql = sql.replace(f"`{project}.{self.dataset}`.", f"`{self.dataset}`.")

        # goccy's DQL/DML resolve bare names against the default dataset, but a
        # bare DDL target (`CREATE OR REPLACE VIEW v`) is mangled into
        # `{project} v` and rejected. Qualify bare DDL targets with the dataset.
        sql = re.sub(
            r"(?i)\b(CREATE\s+OR\s+REPLACE\s+(?:TABLE|VIEW))\s+([`\w][^\s;,]*)",
            lambda m: m.group(0) if "." in m.group(2) else (
                f"{m.group(1)} `{self.dataset}`.{m.group(2)}"
            ),
            sql,
        )

        url = f"{self._endpoint}/bigquery/v2/projects/{project}/queries"
        body = {"query": self._inline_params(sql, params), "useLegacySql": False}

        # Startup race: the port forward can register a beat after the readiness
        # probe succeeded, so retry connection refused briefly before giving up.
        resp = None
        for _ in range(6):
            try:
                resp = requests.post(url, json=body, timeout=self._timeout)
                break
            except requests.exceptions.ConnectionError:
                time.sleep(0.5)
        if resp is None:
            raise RuntimeError(
                f"BigQuery emulator at {url} not reachable for SQL:\n{sql!r}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"BigQuery emulator returned HTTP {resp.status_code} for SQL:\n{sql!r}\n{resp.text[:500]}"
            )
        data = resp.json()
        job_id = (data.get("jobReference") or {}).get("jobId")

        rows = list(self._rows_for(data))
        page_token = data.get("pageToken")
        # Paginate remaining rows via getQueryResults if the token is present
        while page_token and job_id:
            get_url = f"{self._endpoint}/bigquery/v2/projects/{project}/queries/{job_id}"
            data = requests.get(
                get_url,
                params={"pageToken": page_token, "maxResults": 0, "prettyPrint": False},
                timeout=self._timeout,
            ).json()
            rows += self._rows_for(data)
            page_token = data.get("pageToken")

        self._rows = rows
        self._cur_index = 0
        fields = (data.get("schema") or {}).get("fields") or []
        self._description = [(f.get("name"), f.get("type")) for f in fields]
        return self

    def fetch(self, sql: str | None = None):
        if sql is not None:
            self.execute(sql)
        return self._rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._cur_index < len(self._rows):
            row = self._rows[self._cur_index]
            self._cur_index += 1
            return row
        return None

    def __iter__(self):
        return iter(self._rows)


def _client(project: str, port: int = 9050):
    """Anonymous bigquery.Client pointed at the local emulator."""
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    return bigquery.Client(
        project=project,
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": f"http://localhost:{port}"},
    )


def _start_emulator(project: str, dataset: str, port: int = 9050) -> Optional[subprocess.Popen]:
    """Start the goccy BigQuery emulator container.

    Returns the process if successful, None if Docker is unavailable.
    """
    if not shutil.which("docker"):
        return None
    try:
        subprocess.run(
            ["docker", "pull", EMULATOR_IMAGE],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except Exception:
        return None
    # Guarantee a fresh instance: a stale container from a killed run would
    # otherwise hold the port and silently serve a different dataset to us.
    subprocess.run(
        ["docker", "rm", "-f", EMULATOR_NAME],
        capture_output=True,
        check=False,
    )
    return subprocess.Popen(
        [
            "docker", "run", "--rm", "--name", EMULATOR_NAME,
            "-p", f"{port}:9050",
            "-p", "9060:9060",
            EMULATOR_IMAGE,
            "--project", project,
            "--dataset", dataset,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_client(project: str, port: int = 9050):
    """Poll the emulator until the API answers, raising TimeoutError if not."""
    import time

    client = _client(project, port)
    for _ in range(60):
        try:
            client.list_datasets(max_results=1)
            return client
        except Exception:
            time.sleep(1)
    raise TimeoutError("BigQuery emulator did not become ready")


def has_bq_credentials() -> bool:
    """Check if real BigQuery credentials are available."""
    return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


@contextlib.contextmanager
def ephemeral_bigquery(
    project: str = "strata-test",
    dataset: str = "strata_test",
    location: str = "US",
    port: int = 9050,
) -> BigQueryConn | None:
    """Yield a BigQueryConn to an ephemeral BigQuery instance.

    Tries in order:
    1. Local BigQuery emulator (goccy/bigquery-emulator via Docker)
    2. Real BigQuery project via GOOGLE_APPLICATION_CREDENTIALS

    Yields None if neither is available (tests should skip).
    """
    # Try 1: Local emulator
    proc = _start_emulator(project, dataset, port)
    if proc:
        try:
            # NOTE: do NOT call create_dataset here — the goccy emulator HANGS
            # on datasets.insert; the dataset is already created at startup via
            # the `--dataset` flag.
            client = _wait_client(project, port)
            endpoint = f"http://localhost:{port}"
            yield EmulatorBigQueryConn(client, dataset, endpoint=endpoint)
        except TimeoutError:
            yield None
        finally:
            proc.terminate()
        return

    # Try 2: Real BigQuery with credentials
    if has_bq_credentials():
        try:
            from google.cloud import bigquery

            real_project = os.environ.get("BIGQUERY_PROJECT", project)
            client = bigquery.Client(project=real_project, location=location)
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
    proc = _start_emulator(project, dataset, port)
    if proc:
        try:
            client = _wait_client(project, port)
            endpoint = f"http://localhost:{port}"
            wh = get_adapter("bigquery", project=project, dataset=dataset, location=location)
            wh.client = client  # type: ignore[attr-defined]
            wh._conn.client = client  # type: ignore[attr-defined]
            wh._conn = EmulatorBigQueryConn(client, dataset, endpoint=endpoint)
            yield wh
        except TimeoutError:
            yield None
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