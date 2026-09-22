# Strata — prototype

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
pip install strata
strata --help
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
| BigQuery | ✅ | ❌ stub E095 | `pip install strata[bigquery]` |
| Snowflake | ✅ | ❌ stub E095 | `pip install strata[snowflake]` |

## Experimental modules

| Module | Status | Notes |
|---|---|---|
| `strata.integrations` | scaffolding only | `airflow_dag_factory`/`prefect_flow_factory` return *strings* of Python source; `airflow`/`prefect` not installed by default (`pip install strata[airflow]` / `[prefect]`). No real DAG/flow is instantiated — see `plan-hito-2.md` §B. |
| `strata.testing` | fixture helpers only | `create_test_source`/`FreshnessTestHelper` emit `.strata` text or temp tables; not wired to the executor for e2e. Use `strata.exec` for real freshness gates. |
| `strata.observability` | connected | `MetricsCollector` is imported by `strata.exec._run_locked` and exposed via `get_metrics()`; exporters are formatters (Prometheus/StatsD/JSON) without external deps. |

## Development

```bash
.venv/bin/pytest -q
.venv/bin/pytest --cov=strata --cov-fail-under=80
```

Coverage gate: 80% (`pyproject.toml` / `pytest.ini`).
