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
| `json_path(doc, "$...")` | `json` nullable | Consulta por ruta de texto `$...` o `@...` con pasos de miembro e índice. Base JSON null, ruta inexistente o estructura no coincidente → SQL NULL. La ruta debe ser un literal string: los filtros `$[?(...)]` y el descenso recursivo `$..` se rechazan en compilación con E074. |
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

Solo se emite donde el dialecto puede expresar esa búsqueda exacta (DuckDB y
PostgreSQL); en BigQuery y Snowflake la compilación falla loud (ver dialectos).
No se valida en runtime que la clave sea un nombre de miembro simple: una clave
que empiece por `$` conserva el comportamiento de ruta de DuckDB, y
`json_get(doc, key)` con la clave `'$'` devuelve el documento completo en ese motor.

`json_path` emite siempre la ruta **entrecomillada** como literal de string (o de
`jsonpath` en PostgreSQL): emitirla sin comillas es SQL inválido en los cuatro
motores. La ruta se valida en compilación con `functions.json_path_problem`, la
misma definición que usa el generador: se admiten pasos de miembro e índice
(`$.a.b`, `$.xs[0]`), y se rechazan con E074 los filtros `$[?(...)]` / `$[*] ? (@ ...)`
y el descenso recursivo `$..`, porque la forma del resultado de `$..` no coincide
entre motores (en DuckDB devuelve un array de coincidencias, no un valor JSON) y
este catálogo declara `json`.

El contenedor debe tener tipo conocido: `array_get(null, 0)` se rechaza; una
columna nullable `array(int64)` sí es válida. Los elementos del array pueden
ser NULL aunque el array sea `nonnull`. Se aceptan los tipos escalares simples
actuales: int64, float64, string, bool, date, timestamp, uuid y json.

Errores: E062 aridad, E063 tipos (incluido contenedor NULL sin tipo y clave
dinámica que no sea de tipo string), E064 asterisco indebido, E065 uso como
función de ventana, E074 clave literal que no es un nombre de miembro simple y
ruta de `json_path` no expresable (no literal, sin `$`/`@`, filtros o `$..`).
Las restricciones de contratos y el lineage siguen pasando por el checker.
Se corrigió además el NameError que impedía tipar `array(int64)`.

## Dialectos y límites

- DuckDB: `JSON_EXTRACT(doc, '<path>')`; se admite el subconjunto de rutas
  expresable (miembro e índice). Clave dinámica:
  `JSON_EXTRACT(doc, NULLIF(key, ''))`. Comprobado contra DuckDB: una ruta sin `$`
  es una clave exacta (ni `'a.b'` traversa ni `'x[0]'` indexa), la ruta vacía se
  pliega a NULL porque DuckDB la resuelve al documento completo, y `$..` devuelve
  un array de coincidencias (por eso se rechaza antes de emitir).
- PostgreSQL: `jsonb_path_query_first(doc::jsonb, '<path>', '{}'::jsonb, TRUE)`.
  El objeto `vars` vacío se pasa explícitamente porque un `vars` NULL hace que la
  función devuelva NULL siempre, y `silent = TRUE` suprime los errores
  estructurales (documentado en PostgreSQL) para devolver NULL como los demás
  dialectos en lugar de elevar error. Clave dinámica: `(doc -> NULLIF(key, ''))`
  para `json_get` y `->>` dentro del `CASE` de `json_value`; `jsonb -> text` acepta
  cualquier expresión de texto y siempre es una clave exacta.
- BigQuery: `JSON_QUERY(doc, '<path>')`. Clave dinámica: **no se emite**.
  `JSON_QUERY`/`JSON_VALUE` exigen que el `json_path` sea un literal de string (o
  un parámetro de consulta), así que la compilación falla loud con el motivo en
  lugar de emitir SQL que el motor rechaza.
- Snowflake: `GET_PATH(doc, '<path>')`. La firma documentada es
  `GET_PATH(<column_identifier>, '<path_name>')`, con nombres de ruta al estilo
  `'array2[0].id3'`; `$..` no está documentado para `GET_PATH`, así que se rechaza
  antes de emitir (open issue). Clave dinámica: **no se emite**, porque el path
  debe ser un literal entrecomillado.

Ejecución real y tipos físicos comprobados en DuckDB. PostgreSQL, BigQuery y
Snowflake tienen pruebas de emisión, no ejecución contra servicios reales.
No se garantiza idéntica representación textual de números JSON entre motores.
Las fuentes deben respetar el esquema declarado: arrays unidimensionales,
homogéneos y densos. No se valida todavía esa garantía física fuera de DuckDB;
BigQuery tiene además restricciones propias al almacenar arrays con elementos NULL.

Pendiente: constructores, concatenación, búsqueda, agregación de arrays,
expansión a filas y arrays anidados o de tipos parametrizados; refinamiento de
`json_path` para correlacionar el modo recursivo y los filtros con la semántica
real de cada warehouse y, si corresponde, con una sintaxis de ruta en el lenguaje.
En claves dinámicas queda pendiente llegar a BigQuery y Snowflake (hoy exigen un
literal o un parámetro de consulta) y validar en runtime que la clave no sea
sintaxis de ruta, que es lo único que DuckDB sigue interpretando como tal.
No se agregan stubs para estas funciones.
