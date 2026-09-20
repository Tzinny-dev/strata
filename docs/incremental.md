# Incrementalidad y backfills

Sin sintaxis nueva del autor: la incrementalidad la decide el motor, sobre
capas versionadas, nunca con `INSERT` que mute lo publicado (propuesta §43 y
§179). Esta entrega cierra §4 con dos piezas:

1. `run --only-stale` reconstruye el conjunto mínimo por código **y** por
   datos (antes, cualquier cambio de datos reconstruía todo).
2. `strata backfill` re-ejecuta con datos corregidos bajo una razón
   explícita, auditada en el historial junto al run corregido.

## Staleness por fuente

Cada run congela `source_fingerprints` (hash de esquema + filas por source).
En `--only-stale`, el run actual compara fuente a fuente contra el último
run con snapshots de la rama:

- Solo los modelos downstream de las fuentes cambiadas se reconstruyen,
  más los que cambiaron de código (transitivo vía fingerprints, como antes).
- Las ramas intactas conservan sus snapshots sin tocarse.
- Sin cambios: `everything up to date (nothing to do)`, sin escribir nada.
- El parche de rollback/warehouse distinto se mantiene: si la vista live no
  expone el snapshot del último run, el modelo se reconstruye.

El conjunto reconstruido incluye las dependencias de los stale (el
materializador siempre construye dependencias transitivas: leer una vista
live rancia sería peor que recomputar; si el contenido coincide, el snapshot
existente se reutiliza por identidad, nunca se sobrescribe). Cambiar un
override a una tabla de contenido idéntico no reconstruye: los hashes se
resuelven a través de los overrides, así que el contenido manda, no los
nombres.

## Backfill

```
strata backfill module.strata <run_id> --source orders=orders_fixed \
    --reason "corrected upstream extract" -o warehouse.duckdb
```

- Parte del contexto del run indicado (modelos, rama, overrides) y lo
  corrige: `--models m1,m2` recorta la selección, `--source s=t` repetible
  apunta fuentes a tablas corregidas (gana al override del run).
- `--reason` es obligatorio: sin razón no hay backfill, solo un run.
- Registra `backfill_of` + `reason` en la entrada del historial; `strata
  replay <id>` los muestra. La selección se reconstruye en mínimo (stale),
  así que corregir una fuente solo toca su downstream.
- Errores E085: run desconocido, modelo desconocido, `--source` mal
  formado o con source desconocida, y cualquier `PinError` de la
  materialización (incluidas violaciones de cardinalidad o de contrato).

Un backfill no reescribe historia: el run corregido sigue intacto para
`rollback`/`replay`/`verify`, y el nuevo run tiene su propio id de
contenido (mismos datos + misma selección = mismo id, idempotente).

## Merge por fila (`incremental merge_strategy`)

Distinto del punto anterior (que decide qué **modelos** reconstruir):
`merge_strategy: append`/`upsert` decide qué **filas**, dentro de un modelo,
entran a la nueva snapshot sin releer todo lo ya procesado.

```strata
model m {
  from orders
  incremental
  merge_strategy: upsert      -- o "append"; "replace" (default) es full rebuild
  merge_keys: [id]            -- solo para upsert
  cdc_column: updated_at      -- obligatorio para append/upsert
}
```

En cada run que no sea el primero para ese modelo, `exec.materialize` ubica
la snapshot previa (`snap_<run_id>_<modelo>` detrás de la vista `v_<modelo>`
actual) y calcula el delta como las filas del recómputo completo con
`cdc_column > MAX(cdc_column)` de esa snapshot:

- `append`: `snapshot_previa UNION ALL delta`. No deduplica: una fila con la
  misma `merge_keys` que una anterior queda duplicada a propósito.
- `upsert`: filas de la snapshot previa cuyas `merge_keys` **no** aparecen en
  el delta, más el delta completo (antijoin + union, nunca `NOT IN` para
  evitar la semántica de `NULL`). Una fila del delta reemplaza cualquier fila
  previa con las mismas claves.

Restricciones verificadas en compilación (no en ejecución):
`merge_strategy` desconocido (E086), `upsert`/`append` sobre un modelo con
`group`/`aggregate` (E087 — reagregar solo el delta sin releer todo lo ya
fusionado no es sonante, así que se rechaza en vez de producir un agregado
incorrecto en silencio), falta de `cdc_column` como columna de salida real
(E088), y `upsert` sin `merge_keys` válidas (E089). `replace` (o ningún
`merge_strategy`) no tiene requisitos extra: sigue siendo el rebuild
completo de siempre.

Límite medido, no supuesto: el recómputo completo (`__full`) se sigue
ejecutando cada run — el filtro por `cdc_column` recorta qué filas del
resultado entran al delta, no cuánto de las fuentes se escanea. Es correcto
(las pruebas verifican que una fila ya fusionada, mutada sin adelantar su
`cdc_column`, nunca reaparece) pero no reduce el escaneo de las fuentes; eso
requeriría empujar el filtro dentro de `gen_base_subquery`, pendiente.
La primera ejecución de un modelo (sin snapshot previa) siempre es un
recómputo completo, sea cual sea `merge_strategy`. Tests:
`tests/test_incremental.py` (validación) y `tests/test_incremental_merge.py`
(ejecución real en DuckDB, incluida la prueba que distingue esto de un
rebuild completo: mutar una fila ya fusionada sin tocar `cdc_column` no debe
verse en el siguiente run).

## Límites

- Granularidad mínima efectiva de la staleness por modelo (`run --only-stale`):
  el **modelo** completo, salvo que declare `incremental merge_strategy:
  append|upsert` (ver arriba), en cuyo caso el propio modelo decide por fila
  vía `cdc_column`.
- `check`/`build`/`plan` no ejecutan: la staleness por datos solo decide en
  `run --only-stale` y `backfill`. `test` y `replay --execute` materializan
  completo (verificación, no ahorro).
- Los runs sin snapshots en el historial (p. ej. `run --stage-only`)
  no sirven de base para staleness: el primer run con snapshots lo
  reconstruye todo.

Errores: E085 backfill (ver arriba), E086-E089 `merge_strategy` (ver arriba);
el resto del vocabulario de runs no cambia. Pendiente: empujar el filtro de
`cdc_column` a las fuentes (hoy solo recorta el resultado ya recomputado),
eximir prueba de cardinalidad con unicidad ya pineada, y GC por antigüedad.
