# Tipos anidados y aliases de dominio

Los arrays anidan a cualquier profundidad sobre escalares, `decimal`/`money`
y otros arrays, y `domain` declara un alias transparente usable donde se
escriba un tipo. La compatibilidad sigue siendo estructural e invariante
(`array(T)` solo casa con `array(T)` exacto, como ya exigía `array_concat`).

```strata
domain user_id = int64
domain matrix = array(array(int64))
domain amount = decimal(10, 2)
source events(ns: "app", dataset: "events") {
  columns: { id: user_id, m: matrix, p: array(amount), xs: array(int64) }
}
model nested {
  from events
  select {
    deep = array_construct(m),
    first = array_get(m, 0),
    size = array_length(m),
    both = array_concat(m, m)
  }
}
```

## Arrays anidados y parametrizados

- Declaración: `array(array(int64))`, `array(decimal(10, 2))`,
  `array(money(USD))`, a cualquier profundidad. El parser lo acepta de forma
  recursiva y el checker lo resuelve igual (`type_from_spec`).
- Construcción homogénea: `array_construct` acepta elementos de cualquier
  tipo soportado (incluidos arrays, que deben ser idénticos entre sí);
  al menos un elemento con tipo conocido, como antes. Cada elemento se emite
  con `CAST` a su tipo (`BIGINT[][]` en DuckDB/Postgres,
  `ARRAY<ARRAY<INT64>>` en BigQuery, `ARRAY` en Snowflake).
- Acceso y medida: `array_get(xss, 0)` devuelve el elemento (incluido un
  array) y `array_length` cuenta posiciones en cualquier array.
- Concatenación: `array_concat` exige tipos idénticos, anidados incluidos.
- `union`/`dedup`/`group` y los contratos funcionan sobre columnas anidadas
  sin cambios (la unificación solo casa tipos iguales; `DISTINCT` verificado
  en DuckDB sobre arrays anidados).

Lo que sigue rechazándose, ruidosamente y a propósito:

- `array_contains`, `array_sort`, `array_append`/`prepend`/`remove`/`index_of`
  sobre elementos no escalares simples (E063): la igualdad y el orden de
  elementos compuestos, y los arrays multidimensionales rectangulares que
  Postgres exige, no son portables sin verificación por motor.
- `array_agg` de un array (E063): agregaría arrays irregulares que Postgres
  no puede representar.
- `expand` de un array anidado (E075): la expansión declara columnas de
  elementos escalares.
- `array(array)` o `array(decimal)` sin parámetros no son tipos: error de
  parseo, no de chequeo.

## Dominios

```strata
domain user_id = int64
domain ids = array(user_id)
```

- `domain <nombre> = <tipo>` de primer nivel; el tipo puede ser cualquiera
  (incluido otro dominio). Uso en `source`, `contract`, `cast` y elementos
  de array. Transparente: la compatibilidad de contratos, los `reads`/lineage
  y los tipos físicos siguen al tipo subyacente (`user_id` es `int64` a
  todos los efectos a partir del chequeo).
- Los dominios se resuelven al cargar el proyecto, fallen pronto: un ciclo
  (`a = b`, `b = a`, o auto-referencia) y un nombre no declarado son E078,
  aunque el alias no llegue a usarse. Una `source` sin modelos que la lean
  no se chequea (lazy, como antes), así que su E078 aparece al usarla.
- `fmt` hace round-trip (`domain user_id = int64`) y el fingerprint es
  estable. La gramática GBNF acepta la declaración y las referencias (un
  identificador en posición de tipo); el muestreador determinista sigue
  verde porque todo lo que genera parsea.

## Dialectos y límites

- Ejecución real solo en DuckDB (literales `[[1,2],[3]]`, `UNNEST` de un
  nivel, `DISTINCT`/`UNION` sobre anidados, casts `BIGINT[][]` y
  `DECIMAL(10,2)[]`, todo verificado); los otros tres motores verificados
  por patrón de emisión.
- Los tipos físicos de pins (`exec`) reutilizan el mapeo recursivo
  (`BIGINT[][]`, `DECIMAL(10,2)[]`); `money` es `DECIMAL(38,2)` también
  anidado.
- `decimal`/`money` como elementos de array se pueden declarar, medir,
  acceder y concatenar, pero las operaciones elemento a elemento los
  rechazan igual que a los anidados (E063): quedan para una entrega con
  verificación por motor.
- Los tipos de firma de `fn` (`List<...>`) siguen siendo opacos y no
  resuelven dominios; tampoco hay validación por predicado (eso es
  territorio de constraints/masking, no de aliases).

Errores: E063 elementos de array no soportados, E078 dominio desconocido o
cíclico, más los existentes de cada función. Pendiente: `struct`/`map`
(solo mencionados en la propuesta, sin diseño), operaciones elemento a
elemento sobre `decimal`/`money`/anidados con verificación por motor,
`expand`/`array_agg` anidados y validación por predicado en dominios.
