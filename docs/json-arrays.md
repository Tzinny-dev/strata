# JSON y arrays: acceso básico

Primera entrega del catálogo de colecciones. No requiere sintaxis nueva de
llamadas; los esquemas del prototipo escriben `array(int64)`, no `array<int64>`
(esta última es la representación del tipo en los informes).

```strata
source events(ns: "app", dataset: "events") {
  columns: { payload: json, scores: array(int64), index: int64, field: string }
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
| `json_get(doc, key)` | `json` nullable | Miembro de objeto por clave literal o por clave dinámica (expresión de texto). Conserva JSON null y contenedores. Miembro ausente, base SQL NULL o base no-objeto → SQL NULL. |
| `json_value(doc, key)` | `string` nullable | Escalar como texto sin comillas JSON, por clave literal o dinámica. Miembro ausente, JSON null, objeto o array → SQL NULL. |
| `json_path(doc, "$...")` | `json` nullable | Consulta por ruta `$...` con pasos de miembro e índice (`$.a.b`, `$.xs[0]`, `$` solo). Base JSON null, ruta inexistente o estructura no coincidente → SQL NULL. La ruta debe ser un literal string: los filtros, el descenso recursivo `$..`, los comodines y las claves entrecomilladas se rechazan en compilación con E074. |
| `array_length(xs)` | `int64` | Cuenta posiciones, incluidos elementos NULL. Array vacío → 0; array SQL NULL → SQL NULL. Hereda nulabilidad del array. |
| `array_get(xs, index)` | tipo del elemento, nullable | Índice entero desde **cero**, también dinámico. Índice NULL, negativo o fuera de rango → SQL NULL. |

Las claves literales de `json_get`/`json_value` son ASCII que coinciden con
`[A-Za-z_][A-Za-z0-9_]*`, con distinción de mayúsculas. No son rutas: `"$.x"`,
`"x.y"` y `""` se rechazan con E074 (para rutas está `json_path`). Se puede
componer `json_value(json_get(payload, "detail"), "name")`.

Una clave **no literal** (columna `string` o expresión de texto) es una clave
dinámica: se resuelve en runtime y siempre es una búsqueda de miembro **exacta**,
nunca una ruta. Con `key = "a.b"` se lee el miembro llamado `a.b`, no el anidado
`a → b`; `key` vacío o SQL NULL devuelve SQL NULL.

```strata
model by_field { from events select { pick = json_get(payload, field) } }
```

Solo se emite donde el dialecto puede expresar esa búsqueda exacta (DuckDB,
PostgreSQL y Snowflake); en BigQuery la compilación falla loud (ver dialectos).
No se valida en runtime que la clave sea un nombre de miembro simple: una clave
que empiece por `$` conserva el comportamiento de ruta de DuckDB, y
`json_get(doc, key)` con la clave `'$'` devuelve el documento completo en ese motor.

`json_path` emite siempre la ruta **entrecomillada** como literal de string (o de
`jsonpath` en PostgreSQL): emitirla sin comillas es SQL inválido en los cuatro
motores. La ruta se valida en compilación con `functions.json_path_problem`, la
misma definición que usa el generador: solo se admite el subconjunto que se
comporta igual en los cuatro motores (raíz `$` más pasos de miembro e índice).
Se rechazan con E074 los filtros, el descenso recursivo `$..`, los comodines
(`$[*]`) y las claves entrecomilladas (`$['a.b']`, `$."a.b"`): comprobado contra
DuckDB y PostgreSQL reales, `$['a.b']` es un error de sintaxis en ambos (solo
BigQuery lo admite), `$."a.b"` es solo SQL/JSON (DuckDB y PostgreSQL) y `$[*]`
devuelve un array de coincidencias en DuckDB pero la primera coincidencia en
PostgreSQL, así que no puede declarar `json` en ambos.

El contenedor debe tener tipo conocido: `array_get(null, 0)` se rechaza; una
columna nullable `array(int64)` sí es válida. Los elementos del array pueden
ser NULL aunque el array sea `nonnull`. Se aceptan los tipos escalares simples
actuales: int64, float64, string, bool, date, timestamp, uuid y json.

Errores: E062 aridad, E063 tipos (incluido contenedor NULL sin tipo y clave
dinámica que no sea de tipo string), E064 asterisco indebido, E065 uso como
función de ventana, E074 clave literal que no es un nombre de miembro simple y
ruta de `json_path` no expresable (no literal, sin raíz `$`, filtros, `$..`,
comodines o pasos entrecomillados).
Las restricciones de contratos y el lineage siguen pasando por el checker.
Se corrigió además el NameError que impedía tipar `array(int64)`.

## Dialectos y límites

- DuckDB: `JSON_EXTRACT(doc, '<path>')`; se admite el subconjunto de rutas
  expresable (miembro e índice). Clave dinámica:
  `JSON_EXTRACT(doc, NULLIF(key, ''))`. Comprobado contra DuckDB: una ruta sin `$`
  es una clave exacta (ni `'a.b'` traversa ni `'x[0]'` indexa), la ruta vacía se
  pliega a NULL porque DuckDB la resuelve al documento completo, y `$..` y `$[*]`
  devuelven arrays de coincidencias (por eso se rechazan antes de emitir).
- PostgreSQL: `jsonb_path_query_first(doc::jsonb, '<path>', '{}'::jsonb, TRUE)` —
  **verificado contra un servidor PostgreSQL 16 real**: el mismo modelo ejecutado
  en DuckDB y en PostgreSQL devuelve los mismos valores para todo el catálogo
  JSON/arrays (22 expresiones, incluyendo claves dinámicas, `json_path`,
  `json_build` y todos los `array_*`). El objeto `vars` vacío se pasa
  explícitamente porque un `vars` NULL hace que la función devuelva NULL siempre
  (comprobado), y `silent = TRUE` suprime los errores estructurales (documentado
  y comprobado: sin `silent`, `$.key.deep` sobre un escalar eleva error) para
  devolver NULL como los demás dialectos. Clave dinámica: `(doc -> NULLIF(key, ''))`
  para `json_get` y `->>` dentro del `CASE` de `json_value`; `jsonb -> text` acepta
  cualquier expresión de texto y siempre es una clave exacta. Representación:
  `jsonb` ordena las claves al serializar (no se preserva el orden de inserción)
  y añade espacios, así que la igualdad portable de JSON es por valor, no por texto.
- BigQuery: `JSON_QUERY(doc, '<path>')`. Clave dinámica: **no se emite**.
  `JSON_QUERY`/`JSON_VALUE` exigen que el `json_path` sea un literal de string (o
  un parámetro de consulta), así que la compilación falla loud con el motivo en
  lugar de emitir SQL que el motor rechaza.
- Snowflake: `GET_PATH(doc, '<ruta sin $>')` — su notación es JavaScript sin raíz
  (`'a.b'`, `'xs[0]'`, documentado; no existe `$`). Un `$` solo no tiene
  traducción (`GET_PATH` exige un paso de miembro o índice) y falla loud.
  Clave dinámica: `GET(doc, NULLIF(key, ''))` — `GET` es una **búsqueda de clave**,
  no una ruta, y para VARIANT `field_name` acepta una expresión VARCHAR
  (la exigencia de constante es solo para OBJECT estructurados); la clave vacía
  devuelve NULL por especificación. Solo BigQuery queda sin clave dinámica.

Ejecución real y tipos físicos comprobados en DuckDB, y ejecución real del
catálogo JSON/arrays contra un servidor PostgreSQL 16 local (paridad de valores
con DuckDB en las 22 expresiones cubiertas). BigQuery y Snowflake tienen pruebas
de emisión, no ejecución contra servicios reales. No se garantiza idéntica
representación textual entre motores (`jsonb` reordena claves y añade espacios;
los números JSON pueden serializarse distinto).
Las fuentes deben respetar el esquema declarado: arrays unidimensionales,
homogéneos y densos. No se valida todavía esa garantía física fuera de DuckDB;
BigQuery tiene además restricciones propias al almacenar arrays con elementos NULL.

Pendiente: agregación de arrays, expansión a filas y arrays anidados o de tipos
parametrizados; filtros y descenso recursivo en `json_path` correlacionados con
la forma de resultado de cada warehouse (y, si corresponde, una sintaxis de ruta
en el lenguaje que incluya claves entrecomilladas); clave dinámica en BigQuery
(el motor exige literal o parámetro de consulta) y validación en runtime de que
la clave dinámica no sea sintaxis de ruta (lo único que DuckDB sigue
interpretando como tal). No se agregan stubs para estas funciones.
