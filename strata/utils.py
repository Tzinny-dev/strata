"""Shared CLI utilities and helpers."""

import re
from pathlib import Path
from typing import Any

from strata import analysis
from strata.analysis import (
    Checker,
    Project,
    TypedModel,
)
from strata.dialects import get_dialect as get_dialect  # re-export: commands import it here
from strata.parser import parse_strata


def _mask_dsn(dsn: str) -> str:
    """Mask password in DSN for safe logging."""
    # postgres://user:password@host/db -> postgres://user:***@host/db
    return re.sub(r'(postgres(?:ql)?://[^:]+:)([^@]+)(@)', r'\1***\3', dsn)


def _is_dsn(output: str) -> bool:
    """Check if output is a postgres DSN."""
    return output.startswith(("postgres://", "postgresql://"))


# Valid SQL identifier pattern: starts with letter/underscore, followed by alphanumeric/underscore
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _valid_ident(name: str) -> bool:
    """Check if a string is a valid SQL identifier."""
    return bool(_IDENT_RE.match(name))


def load(path: str,
         search_dirs: list[str] | None = None) -> Project:
    """Parse a `.strata` file and build its analysis Project (parse errors raise)."""
    src = Path(path).read_text()
    module = parse_strata(src, path)
    proj = analysis.Project(module, search_dirs=search_dirs)
    return proj


def check(proj: Project,
          model_names: list[str] | None = None) -> dict[str, TypedModel]:
    """Typecheck a Project; returns {model_name: TypedModel}, raising StrataError on failure."""
    ck = Checker(proj)
    tms = ck.check_all(model_names)
    return tms


def open_warehouse(output: str | None, read_only: bool = False) -> Any:
    """Open a warehouse connection for `-o`/`--output`.

    `postgres://...`/`postgresql://...` connects via psycopg2 (wrapped in
    dbcompat.PGConn, see strata/dbcompat.py); `bigquery://project/dataset`
    via google-cloud-bigquery; `snowflake://user:pass@account/db/schema?...`
    via snowflake-connector-python; anything else is a DuckDB file path,
    or `:memory:` when `output` is falsy. `--dialect` (SQL emission) and
    `-o` (which engine to connect to) are independent and both explicit on
    purpose — no scheme-sniffing to infer one from the other. Raises
    RuntimeError with an actionable message if the needed driver isn't
    installed."""
    if output and output.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError(
                "postgres driver not available; pip install psycopg2-binary")
        from urllib.parse import parse_qs, urlparse

        from strata import dbcompat

        # Parse TLS parameters from query string
        # Default to sslmode=verify-full for security (fail-loud if TLS not available)
        parsed = urlparse(output)
        qs = parse_qs(parsed.query)
        connect_params: dict[str, str | int] = {}
        if parsed.username:
            connect_params["user"] = parsed.username
        if parsed.password:
            connect_params["password"] = parsed.password
        # Handle host: can be in hostname (TCP) or in query string (Unix socket)
        host = parsed.hostname or qs.get("host", [None])[0]
        if host:
            connect_params["host"] = host
        # Handle port: can be in port or in query string
        port: str | int | None = parsed.port or qs.get("port", [None])[0]
        if port:
            connect_params["port"] = int(port) if isinstance(port, str) else port
        if parsed.path:
            connect_params["dbname"] = parsed.path.lstrip("/")

        # TLS configuration - default to verify-full for security
        # For Unix socket connections, default to disable TLS since it's local
        default_sslmode = "disable" if host and host.startswith("/") else "verify-full"
        sslmode = qs.get("sslmode", [default_sslmode])[0]
        connect_params["sslmode"] = sslmode
        if "sslrootcert" in qs:
            connect_params["sslrootcert"] = qs["sslrootcert"][0]
        if "sslcert" in qs:
            connect_params["sslcert"] = qs["sslcert"][0]
        if "sslkey" in qs:
            connect_params["sslkey"] = qs["sslkey"][0]

        return dbcompat.PGConn(psycopg2.connect(**connect_params))
    if output and output.startswith("bigquery://"):
        try:
            from google.cloud import bigquery as bq  # type: ignore
        except ImportError:
            raise RuntimeError(
                "bigquery driver not available; pip install google-cloud-bigquery")
        from urllib.parse import parse_qs, unquote, urlparse

        from strata.dbcompat import BigQueryConn

        # bigquery://project/dataset?location=US  or bigquery://project.dataset
        parsed = urlparse(output)
        # netloc is project, path is /dataset
        project = unquote(parsed.netloc) if parsed.netloc else None
        dataset = unquote(parsed.path.lstrip("/")) if parsed.path else ""
        if not dataset and project and "." in project:
            # allow bigquery://project.dataset
            proj, ds = project.split(".", 1)
            project, dataset = proj, ds
        qs = parse_qs(parsed.query)
        location = qs.get("location", [None])[0]
        # bigquery.Client handles default project/credentials from env if project is None
        client = bq.Client(project=project, location=location) if project or location else bq.Client()
        return BigQueryConn(client, dataset)
    if output and output.startswith("snowflake://"):
        try:
            import snowflake.connector  # type: ignore
        except ImportError:
            raise RuntimeError(
                "snowflake driver not available; pip install snowflake-connector-python")
        from urllib.parse import parse_qs, unquote, urlparse

        from strata.dbcompat import SnowflakeConn

        # snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE
        parsed = urlparse(output)
        user = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
        account = parsed.hostname or ""
        # path is /database/schema
        parts = [unquote(p) for p in parsed.path.lstrip("/").split("/") if p]
        database = parts[0] if len(parts) > 0 else ""
        schema = parts[1] if len(parts) > 1 else "PUBLIC"
        qs = parse_qs(parsed.query)
        warehouse = qs.get("warehouse", [None])[0]
        role = qs.get("role", [None])[0]
        raw = snowflake.connector.connect(
            account=account,
            user=user or "",
            password=password or "",
            database=database or "",
            schema=schema,
            warehouse=warehouse or "",
            role=role or "",
        )
        return SnowflakeConn(raw)
    try:
        import duckdb
    except ImportError:
        raise RuntimeError(
            "duckdb not available; run with the venv interpreter "
            "(prototype/.venv/bin/python)")
    if read_only:
        return duckdb.connect(output or ":memory:", read_only=True)
    return duckdb.connect(output or ":memory:")


def render_build(proj: Project, tms: dict[str, TypedModel],
                 names: list[str]) -> str:
    """Deterministic build report: typed contracts + fingerprints + column
    lineage. Shared by `cmd_build` and the bench golden runner (Fase 4)."""
    out = []
    for name in names:
        tm = tms[name]
        cols = ", ".join(c.describe() for c in tm.schema.values())
        out.append(f"model {name}" + (f" -> contract {tm.contract}" if tm.contract else ""))
        out.append(f"  fingerprint  {tm.fingerprint}")
        out.append(f"  outputs      {cols}")
        for cname, origins in tm.lineage.items():
            o = ", ".join(f"{o.node}.{o.col} [{o.kind}]" for o in origins)
            out.append(f"  lineage {cname} <- {o}")
        out.append(f"  reads        {sorted(tm.reads)}")
        out.append("")
    return "\n".join(out).rstrip()