# Warehouse adapters

El motor Strata se ejecuta hoy contra **DuckDB** (único driver instalado
en el entorno). Otros warehouses — Postgres, BigQuery, Snowflake —
pasan por `strata/adapters.py` que expone un `Warehouse` ABC con
seis operaciones (`connect`, `execute`, `fetch`, `materialize`,
`drop`, `list_views`).

## Estado actual

| Warehouse | Adapter ABC (`strata.adapters`) | Ejecución real del motor (`strata.exec`) | Driver |
| --- | --- | --- | --- |
| DuckDB | `DuckDBWarehouse` ✅ | ✅ `duckdb` (default dep) | `duckdb` |
| Postgres | stub E095 — sin `Warehouse` ABC aún | ✅ `cli.open_warehouse("postgres://...")` → `dbcompat.PGConn` (psycopg2, probado contra Postgres 16 efímero en `tests/pg_harness.py`) | `pip install strata[postgres]` |
| BigQuery | `BigQueryWarehouse` ✅ | ✅ `BigQueryConn` (`bigquery://project/dataset?location=US`) + `cli.open_warehouse` / `get_adapter` | `pip install strata[bigquery]` |
| Snowflake | `SnowflakeWarehouse` ✅ | ✅ `SnowflakeConn` (`snowflake://user:pass@account/db/schema?warehouse=WH`) + `cli.open_warehouse` / `get_adapter` | `pip install strata[snowflake]` |
| Iceberg | no aplica — no es un dialecto SQL | post-run: `--iceberg-dir` copia snapshots a tablas Iceberg reales (`strata/iceberg.py`, `propuesta-iceberg.md` L1-L3) | extensión DuckDB `iceberg`; lectura opcional `pip install strata[iceberg]` (`pyiceberg`) |

`sqlgen` ya produce SQL por dialecto; el adapter solo transporta
(transacción, materialización, listado). No hay traducción de
semántica en el adapter — eso vive en `dialects.py`. La bifurcación
Postgres (motor real vía `dbcompat.PGConn`, adapter ABC aún stub) es
intencional y está testeada en `tests/test_exec_postgres.py` (6 tests
contra Postgres real); `get_adapter("postgres")` sigue tirando E095
a propósito con hint a la ruta CLI.

**Iceberg no es un dialecto SQL**: el motor sigue computando en DuckDB
y congelando snapshot tables; `--iceberg-dir` copia cada snapshot a una
tabla Apache Iceberg real bajo un catálogo local (`runs/<run_id>/<model>`)
y registra `run_id → modelos` en `_strata_manifest.json`. rollback/gc/
replay operan sobre el manifest; la lectura independiente (sin DuckDB)
usa `pyiceberg` vía `replay --verify --verify-reader pyiceberg`
(extra opcional `strata[iceberg]`).

El catálogo se inspecciona con **`strata catalog <dir>`**: lista los
runs publicados, sus modelos y marca el `default` con `*`; `--json`
vuelca el manifest; `--run <id> [--verify-reader duckdb|pyiceberg]`
muestra las filas por modelo sin re-ejecutar (los dos readers sirven de
cross-check independiente de que el catálogo es Iceberg estándar).
Fail-loud: sin manifest `E084`, run ajeno `E081`, lectura rota `E083`.

## Uso

```python
from strata.adapters import get_adapter, AdapterNotAvailable
try:
    wh = get_adapter("postgres", conn_string="...")
except AdapterNotAvailable as e:
    print(e.help)   # -> "pip install psycopg2-binary"
wh.execute(sql)
wh.materialize("snap_abc_m", sql)
```

## Interfaz `Warehouse`

- `materialize(name, sql)` → `CREATE TABLE name AS sql` atómica.
- `list_views()` devuelve los `v_*` publicados (views, no tablas).
- Los `snap_*` son tablas; el GC los borra por separado.

## Para habilitar un warehouse real

1. Instalar el driver (`pip install psycopg2-binary` /
   `google-cloud-bigquery` / `snowflake-connector-python`).
2. Implementar la subclase de `Warehouse` que abra la conexión
   con las credenciales del entorno.
3. Añadir el dialecto a `_MISSING` en `adapters.py` para que el
   stub deje de lanzar.
4. Añadir tests de ejecución real en `tests/test_warehouse_<dialect>.py`
   (el patrón: mismo modelo en DuckDB y en el warehouse → mismos
   valores, como se hizo para JSON/arrays y PostgreSQL 16).

## Adaptación dbt

`strata/importdbt.py` provee `import_dbt_schema()` (traduce el esquema
dbt — sources y contratos de columnas de los modelos — a un artefacto
`.strata` determinista) y, con `strata import-dbt schema.yml --models
models/`, `import_dbt_project()` traduce además cada modelo `.sql` a su
cuerpo Strata. El subconjunto traducible cubre:

- **una sola tabla**: `SELECT` de columnas/alias + `count/sum/avg/min/max`,
  `WHERE` de columna-vs-literal con `IS [NOT] NULL` y `AND`, y `GROUP BY`
  sobre esas agregaciones;
- **`JOIN`** (INNER/LEFT/RIGHT/FULL) con equi-`ON` `a.col = b.col` y
  **`CASE WHEN ... THEN ... ELSE ... END`** en el `SELECT` (con alias);
- **`WITH cte AS (...)`** → modelo helper sin contrato `{model}__{cte}`
  que el modelo principal lee vía `from` (referencias hacia delante o
  CTEs sin usar quedan como helpers inertes);
- **dbt-iceberg (L4)**: `INSERT [OVERWRITE] INTO <t> <select>` → el
  select (el overwrite ES el modelo: recompute determinista);
  `MERGE INTO <t> USING (<select>) ON <equi-keys> WHEN ...` → el select
  del USING + `dedup by <keys>`; `config(materialized/unique_key/
  partition_by/ttl_days)` → anotaciones `// dbt-iceberg ...` (v1 escribe
  sin particionar; ttl → `strata gc --iceberg-dir --keep-days`).

Todo lo demás — macros, `select *` sin contrato, `ORDER BY`, `LIMIT`,
`DISTINCT`, `HAVING`, `UNION`, subqueries, `MERGE` con `DELETE` o `ON`
no equi-join — es **E042 fail-loud**. La filosofía es no adivinar: un
modelo que no traduce, no se importa con una versión aproximada.
Contratos `not_null` solo se emiten si la nulabilidad es demostrable en
Strata (un `filter` no refina nulabilidad todavía: el `nonnull` tiene
que venir del upstream o de un `coalesce`).

## WASM/playground

Opcional; no implementado en este hito.
