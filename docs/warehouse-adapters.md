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

`strata/importdbt.py` ya provee `import_dbt_schema()` que traduce
un esquema dbt a una declaración `source`. No reimplementa
transformaciones dbt (eso es proyecto para una herramienta de
migración dedicada).

## WASM/playground

Opcional; no implementado en este hito.
