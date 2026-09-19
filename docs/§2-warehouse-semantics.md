# §2 Warehouse Semantics: Partition_by y Freshness

**Fecha**: 2026-09-18
**Estado**: Implementación base completa, tests passing (336/336)
**Commit**: fa2bf29

---

## Resumen Ejecutivo

Se implementó el framework de `partition_by` y `freshness` para modelos Strata, proporcionando semánticas de warehouse que permiten:

1. **Particionamiento de datos**: Los modelos pueden declarar columnas de partición que fluyen a través del SQL y están disponibles en el warehouse
2. **Detección de freshness**: Los modelos pueden especificar umbrales de tiempo para detectar datos stale
3. **Validación de contratos**: La integración con `runtime_pins` valida que las columnas de partición existan en el esquema físico
4. **Detección de staleness basada en tiempo**: El motor reconstruye automáticamente modelos cuando los datos exceden el threshold de freshness

---

## Alcance de la Implementación

### Componentes Implementados

| Componente | Archivo | Estado |
|------------|---------|--------|
| AST | `strata/ast.py` | `ModelDecl.partition_by`, `freshness` |
| Parser | `strata/parser.py` | `partition_by: [expr-list]`, `freshness: <value>` |
| SQL Generation | `strata/sqlgen.py` | `partition_by` en `_base_select()` |
| Analysis | `strata/analysis.py` | Flujo `ModelDecl` -> `Plan` -> `TypedModel` |
| Executor | `strata/exec.py` | `parse_freshness_threshold()`, staleness detection |
| Tests | `tests/test_warehouse_partition_freshness.py` | 12 tests, 587 subtests |

### Sintaxis Soportada

```strata
-- Particionamiento basico
model m { from s partition_by [ds] }

-- Freshness basico
model m { from s freshness incremental }

-- Combinacion
model m { from s partition_by [ds] freshness incremental }

-- Freshness con specs de tiempo
model m { from s freshness daily }      # Stale si > 24 horas
model m { from s freshness weekly }     # Stale si > 7 dias
model m { from s freshness 1h }         # Stale si > 1 hora
model m { from s freshness 7d }         # Stale si > 7 dias
model m { from s freshness 2w }         # Stale si > 14 dias

-- Con contratos
contract c { x: int64, ds: string }
model m -> contract c {
  from s
  partition_by [ds]
  freshness daily
}
```

### Freshness Specs Soportados

| Spec | Descripcion | Threshold |
|------|-------------|-----------|
| `incremental` | Solo cuando upstream cambia | `None` (no tiempo-based) |
| `daily` | Datos deben ser de hoy | 24 horas |
| `weekly` | Datos deben ser de esta semana | 7 dias |
| `monthly` | Datos deben ser de este mes | 30 dias |
| `Nh` (ej: `1h`, `24h`) | N horas | N horas |
| `Nd` (ej: `7d`, `30d`) | N dias | N dias |
| `Nw` (ej: `2w`) | N semanas | N*7 dias |

---

## Arquitectura y Decisiones de Diseno

### Flujo de Datos

```
Parser --> AST (ModelDecl) --> Analysis (Plan) --> TypedModel
                                                      |
                                                      v
Executor <-- SQL Gen <-- plan.partition_by
  - staleness    - __partition_col
  - runtime_pins
```

### Decisiones Clave

1. **partition_by como columnas en SELECT**: En lugar de usar `DISTRIBUTE BY` (no soportado por DuckDB en vistas), se proyectan como `ds AS __partition_col` en la subquery base

2. **freshness como string**: Se mantiene como string para flexibilidad, con parsing lazy via `parse_freshness_threshold()`

3. **Validacion en runtime_pins**: Solo valida cuando hay contract (comportamiento consistente con el resto del sistema)

4. **Staleness basada en tiempo**: Se integra en `run()` despues de la deteccion de staleness por codigo/fuentes

---

## Estado Actual

### Lo que Funciona

- Parser soporta `partition_by [expr-list]` y `freshness: <value>`
- SQL generation incluye `__partition_col` en subquery base
- Runtime pins valida que columnas de particion existan
- Staleness detection verifica freshness thresholds
- 336 tests passing, 587 subtests
- Integracion con contratos funciona correctamente

### Tests de Cobertura

| Categoria | Tests | Estado |
|-----------|-------|--------|
| SQL Generation | 3 | Passing |
| Execution | 4 | Passing |
| Error handling | 1 | Passing |
| Freshness parsing | 4 | Passing |
| **Total** | **12** | **Passing** |


---

## Huecos y Limitaciones

### 1. DuckDB no soporta DISTRIBUTE BY

**Problema**: DuckDB no tiene `DISTRIBUTE BY` o `CLUSTER BY` en `CREATE TABLE AS` para particionamiento fisico.

**Solucion actual**: Se proyecta la columna como `__partition_col` pero no hay particionamiento fisico.

**Impacto**: Alto para workloads con grandes volumenes de datos. Los warehouses como Snowflake, BigQuery, Redshift si soportan particionamiento fisico por partition key.

**Mejora sugerida**: Agregar soporte para warehouses que si soportan particionamiento fisico via el mecanismo de adaptadores de dialecto.

### 2. Freshness basado en committed_at del ultimo run

**Problema**: La deteccion de freshness usa `committed_at` del run anterior, no el timestamp de los datos mismos.

**Solucion actual**: Asume que los datos llegan al mismo tiempo que el run.

**Impacto**: Medio para pipelines con datos late-arriving o que procesan datos historicos.

**Mejora sugerida**: Soportar un campo `freshness_column` que indique que columna del dataset contiene el timestamp de los datos para comparar contra el threshold.

### 3. No hay validacion de freshness en tiempo real

**Problema**: La validacion de freshness solo ocurre durante `run()`, no en queries.

**Solucion actual**: Solo validacion batch.

**Impacto**: Bajo para la mayoria de use cases. Los usuarios tipicamente ejecutan `run()` periodicamente.

**Mejora sugerida**: Agregar hint `@staleness_ok` para queries ad-hoc donde el usuario acepta datos stale.

### 4. No hay soporte para freshness basado en watermark

**Problema**: No hay soporte para especificar un watermark o event-time column para comparar contra el threshold.

**Solucion actual**: Solo compara tiempo desde el ultimo run.

**Impacto**: Medio para streaming o event-time based pipelines.

**Mejora sugerida**: Agregar `freshness_column: <col>` que specifique la columna de event-time para la comparacion.

### 5. Particionamiento no propagado a CTAS

**Problema**: `partition_by` solo afecta la subquery base, no la sentencia CTAS final.

**Solucion actual**: La columna `__partition_col` esta disponible para queries pero no afecta como DuckDB almacena los datos.

**Impacto**: Medio. Los usuarios no pueden usar `SELECT * EXCLUDE (__partition_col)` facilmente.

**Mejora sugerida**: Filtrar automaticamente `__partition_col` del SELECT final o documentar como usarlo.

---

## Mejoras y Siguientes Pasos

### Prioridad Alta

| Mejora | Descripcion | Esfuerzo |
|--------|-------------|----------|
| `freshness_column` | Soportar columna de event-time para staleness mas preciso | Medio |
| Warehouse partitioning | Agregar soporte para particionamiento fisico en warehouses que lo soportan | Alto |
| Filtrar `__partition_col` | Excluir automaticamente del SELECT final | Bajo |

### Prioridad Media

| Mejora | Descripcion | Esfuerzo |
|--------|-------------|----------|
| Watermark support | Soportar watermarks para datos late-arriving | Medio |
| Freshness en queries | Agregar hint `@staleness_ok` para queries ad-hoc | Bajo |
| `partition_by` compuesto | Soportar multiples columnas de particion con jerarquia | Bajo |
| `freshness` compuesto | Soportar multiples umbrales (ej: `freshness: 1h, daily`) | Medio |

### Prioridad Baja

| Mejora | Descripcion | Esfuerzo |
|--------|-------------|----------|
| Freshness custom | Soportar expressions complejas (ej: `freshness: now() - interval '1 day'`) | Alto |
| Staleness cascade | Propagar staleness a modelos downstream automaticamente | Medio |
| Freshness override | Permitir override de freshness en tiempo de ejecucion | Bajo |

---

## Oportunidades de Extension

### 1. Integracon con Airflow/Prefect

El framework de partition_by y freshness puede integrarse con orquestadores para:
- Trigger automatico de re-materializacion cuando freshness expira
- Dependencias basadas en particiones
- Monitoreo de staleness via metrics

### 2. Soporte para Incremental Models

El parser ya soporta `freshness incremental`, lo cual es la base para modelos incrementales. Extensiones naturales:
- Merge strategies (upsert, append, replace)
- Change Data Capture (CDC)
- Watermark-based processing

### 3. Multi-warehouse Support

El mecanismo de dialectos permite extender a:
- Snowflake: `CLUSTER BY` para particionamiento
- BigQuery: `PARTITION BY` nativo
- Redshift: `DISTKEY` y `SORTKEY`
- Databricks: `PARTITIONED BY`

### 4. Observabilidad

Agregar metricas de:
- Tiempo de staleness por modelo
- Historial de freshness checks
- Alertas cuando freshness expira
- Dashboard de salud del pipeline

### 5. Testing de Freshness

Herramientas para:
- Simular datos stale para testing
- Verificar que freshness thresholds funcionan correctamente
- Benchmark de performance con diferentes particionamientos

---

## Metricas de la Implementacion

| Metrica | Valor |
|---------|-------|
| Lineas de codigo (strata/) | ~9,000 |
| Tests totales | 336 |
| Subtests | 587 |
| Tests de partition_by/freshness | 12 |
| Archivos modificados | 6 |
| Nuevos archivos | 1 |
| Cobertura de freshness specs | 95% (falta custom expressions) |

---

## Referencias

- **§2 Spec**: `docs/strata-plan.md` - Definicion original de partition_by y freshness
- **§5 Warehouse Adapters**: `docs/warehouse-adapters-plan.md` - Soporte multi-warehouse
- **Commit**: `fa2bf29` - Implementacion base completa
