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

## Límites

- Granularidad mínima: el **modelo**, siempre recomputo total (`CREATE TABLE
  snap AS SELECT *`). Incrementalidad por filas (watermarks, merge de
  deltas, rangos de fechas como capas) requiere `partition_by`/`freshness`
  con semántica efectiva (§2, abierto): sin particiones el motor no sabe
  qué es "lo nuevo".
- `check`/`build`/`plan` no ejecutan: la staleness por datos solo decide en
  `run --only-stale` y `backfill`. `test` y `replay --execute` materializan
  completo (verificación, no ahorro).
- Los runs sin snapshots en el historial (p. ej. `run --stage-only`)
  no sirven de base para staleness: el primer run con snapshots lo
  reconstruye todo.

Errores: E085 backfill (ver arriba); el resto del vocabulario de runs no
cambia. Pendiente: incrementalidad por filas sobre `partition_by`,
eximir prueba de cardinalidad con unicidad ya pineada, y GC por antigüedad.
