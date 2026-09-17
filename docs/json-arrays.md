# JSON y arrays: acceso básico

Primera entrega del catálogo de colecciones. No requiere sintaxis nueva de
llamadas; los esquemas del prototipo escriben `array(int64)`, no `array<int64>`
(esta última es la representación del tipo en los informes).

```strata
source events(ns: "app", dataset: "events") {
  columns: { payload: json, scores: array(int64), index: int64 }
}
contract Result {
  detail: json
  name: string
  size: int64
  score: int64
}
model summary -> contract Result {
  from events
  select {
    detail = json_get(payload, "detail"),
    name = json_value(payload, "name"),
    size = array_length(scores),
    score = array_get(scores, index)
  }
}
```

## Semántica

| Función | Retorno | Reglas |
| --- | --- | --- |
| `json_get(doc, "key")` | `json` nullable | Miembro de objeto; conserva JSON null y contenedores. Miembro ausente, base SQL NULL o base no-objeto → SQL NULL. |
| `json_value(doc, "key")` | `string` nullable | Escalar como texto sin comillas JSON. Miembro ausente, JSON null, objeto o array → SQL NULL. |
| `array_length(xs)` | `int64` | Cuenta posiciones, incluidos elementos NULL. Array vacío → 0; array SQL NULL → SQL NULL. Hereda nulabilidad del array. |
| `array_get(xs, index)` | tipo del elemento, nullable | Índice entero desde **cero**, también dinámico. Índice NULL, negativo o fuera de rango → SQL NULL. |

Las claves son literales ASCII que coinciden con `[A-Za-z_][A-Za-z0-9_]*`,
con distinción de mayúsculas. No son rutas: `"$.x"`, `"x.y"`, claves vacías
ni columnas usadas como claves están admitidas en esta entrega.
Se puede componer `json_value(json_get(payload, "detail"), "name")`.

El contenedor debe tener tipo conocido: `array_get(null, 0)` se rechaza; una
columna nullable `array(int64)` sí es válida. Los elementos del array pueden
ser NULL aunque el array sea `nonnull`. Se aceptan los tipos escalares simples
actuales: int64, float64, string, bool, date, timestamp, uuid y json.

Errores: E062 aridad, E063 tipos (incluido contenedor NULL sin tipo), E064
asterisco indebido, E065 uso como función de ventana, E074 clave no admitida.
Las restricciones de contratos y el lineage siguen pasando por el checker.
Se corrigió además el NameError que impedía tipar `array(int64)`.

## Dialectos y límites

- DuckDB: JSON_EXTRACT/JSON_EXTRACT_STRING con guardia de tipo para escalares;
  ARRAY_LENGTH/LIST_EXTRACT con traducción y límites de índice.
- PostgreSQL: JSONB, operadores de miembro y JSONB_TYPEOF; CARDINALITY y
  ARRAY_LOWER para respetar el origen físico del array. Longitud convertida a BIGINT.
- BigQuery: JSON_QUERY/JSON_VALUE, ARRAY_LENGTH y SAFE_OFFSET.
- Snowflake: GET/TYPEOF y ARRAY_SIZE; el acceso al array convierte el VARIANT
  al tipo de elemento declarado (json conserva VARIANT).

Ejecución real y tipos físicos comprobados en DuckDB. PostgreSQL, BigQuery y
Snowflake tienen pruebas de emisión, no ejecución contra servicios reales.
No se garantiza idéntica representación textual de números JSON entre motores.
Las fuentes deben respetar el esquema declarado: arrays unidimensionales,
homogéneos y densos. No se valida todavía esa garantía física fuera de DuckDB;
BigQuery tiene además restricciones propias al almacenar arrays con elementos NULL.

Pendiente: JSONPath, claves arbitrarias/dinámicas, parseo tolerante de texto JSON,
constructores, concatenación, búsqueda, agregación de arrays, expansión a filas
y arrays anidados o de tipos parametrizados. No se agregan stubs para estas funciones.
