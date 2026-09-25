# Warehouse adapters

The Strata engine today runs against **DuckDB** (the only driver installed
in the environment). Other warehouses — Postgres, BigQuery, Snowflake —
go through `strata/adapters.py`, which exposes a `Warehouse` ABC with
six operations (`connect`, `execute`, `fetch`, `materialize`,
`drop`, `list_views`).

## Current status

| Warehouse | Adapter ABC (`strata.adapters`) | Real engine execution (`strata.exec`) | Driver |
| --- | --- | --- | --- |
| DuckDB | `DuckDBWarehouse` ✅ | ✅ `duckdb` (default dep) | `duckdb` |
| Postgres | stub E095 — no `Warehouse` ABC yet | ✅ `cli.open_warehouse("postgres://...")` → `dbcompat.PGConn` (psycopg2, tested against ephemeral Postgres 16 in `tests/pg_harness.py`) | `pip install strata[postgres]` |
| BigQuery | `BigQueryWarehouse` ✅ | ✅ `BigQueryConn` (`bigquery://project/dataset?location=US`) + `cli.open_warehouse` / `get_adapter` | `pip install strata[bigquery]` |
| Snowflake | `SnowflakeWarehouse` ✅ | ✅ `SnowflakeConn` (`snowflake://user:pass@account/db/schema?warehouse=WH`) + `cli.open_warehouse` / `get_adapter` | `pip install strata[snowflake]` |
| Iceberg | not applicable — not a SQL dialect | post-run: `--iceberg-dir` copies snapshots to real Iceberg tables (`strata/iceberg.py`, `propuesta-iceberg.md` L1-L3) | DuckDB extension `iceberg`; optional read via `pip install strata[iceberg]` (`pyiceberg`) |

`sqlgen` already produces SQL per dialect; the adapter only transports
(transaction, materialization, listing). There is no semantic
translation in the adapter — that lives in `dialects.py`. The Postgres
fork (real engine via `dbcompat.PGConn`, adapter ABC still a stub) is
intentional and is tested in `tests/test_exec_postgres.py` (6 tests
against real Postgres); `get_adapter("postgres")` keeps throwing E095
on purpose with a hint toward the CLI path.

**Iceberg is not a SQL dialect**: the engine keeps computing in DuckDB
and freezing snapshot tables; `--iceberg-dir` copies each snapshot to a
real Apache Iceberg table under a local catalog (`runs/<run_id>/<model>`)
and records `run_id → models` in `_strata_manifest.json`. rollback/gc/
replay operate on the manifest; independent reading (without DuckDB)
uses `pyiceberg` via `replay --verify --verify-reader pyiceberg`
(optional extra `strata[iceberg]`).

The catalog is inspected with **`strata catalog <dir>`**: it lists the
published runs, their models, and marks the `default` with `*`; `--json`
dumps the manifest; `--run <id> [--verify-reader duckdb|pyiceberg]`
shows the rows per model without re-running (the two readers serve as an
independent cross-check that the catalog is standard Iceberg).
Fail-loud: no manifest `E084`, foreign run `E081`, broken read `E083`.

## Usage

```python
from strata.adapters import get_adapter, AdapterNotAvailable
try:
    wh = get_adapter("postgres", conn_string="...")
except AdapterNotAvailable as e:
    print(e.help)   # -> "pip install psycopg2-binary"
wh.execute(sql)
wh.materialize("snap_abc_m", sql)
```

## The `Warehouse` interface

- `materialize(name, sql)` → atomic `CREATE TABLE name AS sql`.
- `list_views()` returns the published `v_*` (views, not tables).
- The `snap_*` are tables; the GC deletes them separately.

## To enable a real warehouse

1. Install the driver (`pip install psycopg2-binary` /
   `google-cloud-bigquery` / `snowflake-connector-python`).
2. Implement the `Warehouse` subclass that opens the connection
   with the environment credentials.
3. Add the dialect to `_MISSING` in `adapters.py` so that the
   stub stops raising.
4. Add real execution tests in `tests/test_warehouse_<dialect>.py`
   (the pattern: the same model in DuckDB and in the warehouse → the
   same values, as was done for JSON/arrays and PostgreSQL 16).

## dbt adaptation

`strata/importdbt.py` provides `import_dbt_schema()` (translates the dbt
schema — sources and column contracts of the models — into a deterministic
`.strata` artifact) and, with `strata import-dbt schema.yml --models
models/`, `import_dbt_project()` additionally translates each `.sql` model
into its Strata body. The translatable subset covers:

- **a single table**: `SELECT` of columns/aliases + `count/sum/avg/min/max`,
  `WHERE` of column-vs-literal with `IS [NOT] NULL` and `AND`, and `GROUP BY`
  over those aggregations;
- **`JOIN`** (INNER/LEFT/RIGHT/FULL) with equi-`ON` `a.col = b.col` and
  **`CASE WHEN ... THEN ... ELSE ... END`** in the `SELECT` (with an alias);
- **`WITH cte AS (...)`** → helper model without a contract `{model}__{cte}`
  that the main model reads via `from` (forward references or
  unused CTEs remain as inert helpers);
- **dbt-iceberg (L4)**: `INSERT [OVERWRITE] INTO <t> <select>` → the
  select (the overwrite IS the model: deterministic recompute);
  `MERGE INTO <t> USING (<select>) ON <equi-keys> WHEN ...` → the select
  of the USING + `dedup by <keys>`; `config(materialized/unique_key/
  partition_by/ttl_days)` → `// dbt-iceberg ...` annotations (v1 writes
  unpartitioned; ttl → `strata gc --iceberg-dir --keep-days`).

Everything else — macros, `select *` without a contract, `ORDER BY`, `LIMIT`,
`DISTINCT`, `HAVING`, `UNION`, subqueries, `MERGE` with `DELETE` or a
non-equi-join `ON` — is **E042 fail-loud**. The philosophy is not to guess:
a model that does not translate is not imported with an approximate version.
`not_null` contracts are only emitted if nullability is demonstrable in
Strata (a `filter` does not refine nullability yet: the `nonnull` has to
come from upstream or from a `coalesce`).

## WASM/playground

Optional; not implemented in this milestone.
