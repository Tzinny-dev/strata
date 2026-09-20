"""Ephemeral, hermetic Postgres cluster for tests that need a real engine.

Same approach as tests/test_sqlgen.py::TestPostgresLiveE2E (initdb/pg_ctl in
a tempdir, custom port, listen_addresses="" so nothing but the local unix
socket is reachable — the system-wide Postgres service is never touched),
but this yields a live `dbcompat.PGConn` (wrapping a real psycopg2
connection) instead of shelling out to `psql`, because strata.exec needs
Python objects to call `.execute()/.fetchone()/.fetchall()` on, not text
output. `TestPostgresLiveE2E` itself is left on its own subprocess-based
path for now (migrating a green test isn't worth the churn on its own).
"""
from __future__ import annotations

import contextlib
import glob
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def find_pgbin() -> Optional[Path]:
    import shutil
    for cand in (shutil.which("initdb"),
                 *[str(p) for p in sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))]):
        if cand and Path(cand).exists():
            return Path(cand).resolve().parent
    return None


@contextlib.contextmanager
def ephemeral_postgres(port: int = 55434, dbname: str = "strata_test"):
    """Yields a `dbcompat.PGConn` to a throwaway database on a private,
    just-started Postgres cluster, or `None` if no Postgres server
    installation is found (same skip criterion as TestPostgresLiveE2E).
    Tears the whole cluster down on exit, however the test finished."""
    pgbin = find_pgbin()
    if pgbin is None:
        yield None
        return
    import psycopg2
    from strata.dbcompat import PGConn

    with tempfile.TemporaryDirectory() as td:
        data, sock = Path(td) / "pg", Path(td) / "sock"
        sock.mkdir()
        env = dict(os.environ)
        # pg_ctl spawns the postmaster, which inherits our stdio pipes and
        # keeps them open forever -> a plain subprocess.run(...) would hang
        # waiting for EOF. Detach the server: DEVNULL streams + own session.
        run = lambda *a: subprocess.run(
            *a, check=True, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run([str(pgbin / "initdb"), "-U", "strata", "-A", "trust",
             "-E", "UTF8", "--no-locale", str(data)])
        subprocess.run(
            [str(pgbin / "pg_ctl"), "-D", str(data), "-w", "-s",
             "-o", f"-p {port} -k {sock} -c listen_addresses=", "start"],
            check=True, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            run([str(pgbin / "createdb"), "-h", str(sock), "-p", str(port),
                 "-U", "strata", dbname])
            raw = psycopg2.connect(host=str(sock), port=port, user="strata",
                                   dbname=dbname)
            con = PGConn(raw)
            # A URL DSN equivalent to the keyword-args connection above, for
            # tests that need to hand a `-o` value to the CLI itself
            # (strata.cli.open_warehouse only accepts a DSN string, not a
            # ready-made connection).
            con.dsn = f"postgresql://strata@/{dbname}?host={sock}&port={port}"
            try:
                yield con
            finally:
                con.close()
        finally:
            subprocess.run(
                [str(pgbin / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
