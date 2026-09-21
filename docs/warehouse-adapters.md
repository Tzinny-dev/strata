# Warehouse adapters

El motor Strata se ejecuta hoy contra **DuckDB** (único driver instalado
en el entorno). Otros warehouses — Postgres, BigQuery, Snowflake —
pasan por `strata/adapters.py` que expone un `Warehouse` ABC con
seis operaciones (`connect`, `execute`, `fetch`, `materialize`,
`drop`, `list_views`).

## Estado actual

| Warehouse | Adapter | Ejecución real |
| --- | --- | --- |
| DuckDB | `DuckDBWarehouse` (incluido) | ✅ `duckdb` |
| Postgres | stub E095 | ❌ requiere `psycopg2-binary` |
| BigQuery | stub E095 | ❌ requiere `google-cloud-bigquery` |
| Snowflake | stub E095 | ❌ requiere `snowflake-connector-python` |

`sqlgen` ya produce SQL por dialecto; el adapter solo transporta
(transacción, materialización, listado). No hay traducción de
semántica en el adapter — eso vive en `dialects.py`.

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
cuerpo Strata. El subconjunto traducible en el paso 1 es de **una sola
tabla**: `SELECT` de columnas/alias + `count/sum/avg/min/max`, `WHERE`
de columna-vs-literal con `IS [NOT] NULL` y `AND`, y `GROUP BY` sobre
esas agregaciones. Todo lo demás (joins, CTEs, macros, `select *`,
`ORDER BY`, `LIMIT`, `DISTINCT`, expresiones calculadas) es **E042
fail-loud** — la filosofía de no adivinar: un modelo que no traduce, no
se importa con una versión aproximada. Contratos `not_null` solo se
emiten si la nulabilidad es demostrable en Strata (un `filter` no
refina nulabilidad todavía: el `nonnull` tiene que venir del upstream o
de un `coalesce`).

## WASM/playground

Opcional; no implementado en este hito.
