# Strata — prototype

[![CI](https://github.com/Tzinny-dev/strata/actions/workflows/ci.yml/badge.svg)](https://github.com/Tzinny-dev/strata/actions/workflows/ci.yml) [![Binary](https://github.com/Tzinny-dev/strata/actions/workflows/binary.yml/badge.svg)](https://github.com/Tzinny-dev/strata/actions/workflows/binary.yml) [![Publish](https://github.com/Tzinny-dev/strata/actions/workflows/publish.yml/badge.svg)](https://github.com/Tzinny-dev/strata/actions/workflows/publish.yml) [![PyPI](https://img.shields.io/pypi/v/strata-lang)](https://pypi.org/project/strata-lang/) [![Python](https://img.shields.io/pypi/pyversions/strata-lang)](https://pypi.org/project/strata-lang/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Coverage 81%](https://img.shields.io/badge/coverage-81%25-brightgreen)](htmlcov/index.html)

Declarative, versioned, immutable data transformations. Compiles to SQL (DuckDB/Postgres/BigQuery/Snowflake), with column-level lineage and 3-phase contract pins.

> Working title v0.1 — prototype lives in `prototype/`. Spec in `../spec/`.

## Install (dev)

```bash
cd prototype
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
strata --help
```

Or without venv after publish:

```bash
pip install strata-lang
strata --help
```

### uv (standalone tool, no venv)

```bash
# from PyPI (once published) — package is strata-lang, entry point stays `strata`
uv tool install strata-lang
strata --help

# from Git (before PyPI, or dev)
uv tool install git+https://github.com/Tzinny-dev/strata --from prototype
strata --help

# upgrade
uv tool upgrade strata-lang
pip install --upgrade strata-lang
```

## Quick start

```bash
# typecheck + contracts + lineage (no DB)
strata build examples/daily_orders.strata

# emit SQL for a dialect
strata compile examples/daily_orders.strata --dialect postgres

# materialize in DuckDB (with demo fixtures)
strata run examples/daily_orders.strata --seed -o /tmp/demo.duckdb
strata test examples/daily_orders.strata --seed
```

See `docs/getting-started.md` and `docs/tutorial.md` for the full walkthrough (every code block was run against the CLI before writing).

## Warehouses

| Warehouse | SQL emit (`compile --dialect`) | Engine run (`run -o` / `exec`) | Driver |
|---|---|---|---|
| DuckDB | ✅ | ✅ `DuckDBWarehouse` + `dbcompat` (`duckdb` default dep) | `duckdb` |
| Postgres | ✅ | ✅ `cli.open_warehouse("postgres://...")` → `dbcompat.PGConn` (probado vs Postgres 16 efímero) — `adapters.get_adapter("postgres")` sigue stub E095 a propósito | `pip install strata[postgres]` |
| BigQuery | ✅ | ✅ `BigQueryWarehouse` → `dbcompat.BigQueryConn` (`bigquery://project/dataset?location=US`) + `get_adapter("bigquery")` | `pip install strata[bigquery]` |
| Snowflake | ✅ | ✅ `SnowflakeWarehouse` → `dbcompat.SnowflakeConn` (`snowflake://user:pass@account/db/schema?warehouse=WH&role=ROLE`) + `get_adapter("snowflake")` | `pip install strata[snowflake]` |

## Experimental modules

| Module | Status | Notes |
|---|---|---|
| `strata.integrations` | scaffolding only | `airflow_dag_factory`/`prefect_flow_factory` return *strings* of Python source; `airflow`/`prefect` not installed by default (`pip install strata[airflow]` / `[prefect]`). No real DAG/flow is instantiated — see `plan-hito-2.md` §B. |
| `strata.testing` | fixture helpers only | `create_test_source`/`FreshnessTestHelper` emit `.strata` text or temp tables; not wired to the executor for e2e. Use `strata.exec` for real freshness gates. |
| `strata.observability` | connected | `MetricsCollector` is imported by `strata.exec._run_locked` and exposed via `get_metrics()`; exporters are formatters (Prometheus/StatsD/JSON) without external deps. |

## Standalone binary

```bash
# curl | bash (detects OS/arch, pulls from GH Releases)
curl -fsSL https://raw.githubusercontent.com/Tzinny-dev/strata/main/install.sh | bash
strata --help
# or pin version / custom dir
curl -fsSL .../install.sh | bash -s -- --version 0.1.5 --to /usr/local/bin
```

```bash
# Homebrew (tap — formula in homebrew/strata-lang.rb, sha256 replaced on release)
brew tap Tzinny-dev/strata  # or: brew tap Tzinny-dev/homebrew-strata if split
brew install strata-lang
strata --help
```

## Docker

```bash
# from GHCR (after tag push triggers docker.yml)
docker pull ghcr.io/tzinny-dev/strata:0.1.5
docker run --rm ghcr.io/tzinny-dev/strata:0.1.5 --help
docker run --rm -v $PWD:/work -w /work ghcr.io/tzinny-dev/strata:0.1.5 build examples/daily_orders.strata
docker run --rm -v $PWD:/work -w /work ghcr.io/tzinny-dev/strata:0.1.5 run examples/daily_orders.strata --seed -o /tmp/demo.duckdb

# local build (no docker daemon required on host for CI build via GHA)
docker build -t ghcr.io/tzinny-dev/strata:0.1.5 -f Dockerfile .
```

## Development

```bash
.venv/bin/pytest -q
.venv/bin/pytest --cov=strata --cov-fail-under=80
```

Coverage gate: 80% (`pyproject.toml` / `pytest.ini`).
