# Referencia de sintaxis

Cada bloque de este documento se corrió contra `strata check` (o `build`)
antes de escribirse — el código que ves aquí es el mismo que se verificó,
no una reconstrucción a partir de la especificación. Donde el compilador
rechazó algo que parecía razonable, se anota explícitamente: son los
límites reales del lenguaje hoy, no una omisión del documento.

## 1. Estructura del módulo

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: {
    order_id:         int64 nonnull,
    customer_id:      int64 nonnull,
    country:          string nonnull,
    gross_amount_usd: money nonnull,
    is_test:          bool,
    order_day:        date nonnull,
  }
}

contract PaidOrders {
  order_id:   int64 nonnull
  country:    string nonnull
  net_amount: money nonnull
}

model paid_orders -> contract PaidOrders {
  from orders
  filter coalesce(is_test, false) == false
  select { order_id = order_id, country = upper(country), net_amount = gross_amount_usd }
}

pipeline prod {
  env: prod,
  models: [paid_orders],
}
```

- `source`: declara una tabla del warehouse. `ns`/`dataset` son metadatos
  de catálogo; la tabla SQL real leída es el nombre de la declaración
  (`orders` arriba), no esas claves.
- `contract`: fija el esquema de salida esperado de un modelo. Es
  opcional (`model m { ... }` sin `-> contract X` compila igual).
- `model`: la unidad de transformación.
- `pipeline`: agrupa modelos para un entorno (`env:`) y overrides de
  fuente por ambiente.

## 2. Cuerpo del modelo

### `from` / `join_*` / `expect`

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, customer_id: int64 nonnull, country: string nonnull }
}
source refunds(ns: "crm", dataset: "refunds") {
  columns: { order_id: int64 nonnull, discount_usd: money }
}

model with_refund {
  from orders
  join_left refunds on orders.order_id == refunds.order_id
  select { order_id = orders.order_id, discount = coalesce(refunds.discount_usd, 0) }
}
```

`join_left` / `join_inner` / `join_anti` / `join_semi` están implementados.
`join_anti` (filas del lado izquierdo sin match) y `join_semi` (filas del
lado izquierdo CON match, sin duplicar por multi-match) no tienen
palabra clave de valor a la derecha del `on`, solo la condición. Ejemplo
con `join_anti`:

```strata
source orders(ns: "crm", dataset: "orders") { columns: { order_id: int64 nonnull, customer_id: int64 nonnull } }
source refunds(ns: "crm", dataset: "refunds") { columns: { order_id: int64 nonnull } }

model orders_without_refund {
  from orders
  join_anti refunds on orders.order_id == refunds.order_id
  select { order_id = orders.order_id }
}
```

`expect many_to_one`/`one_to_one` valida en `materialize()` (contra datos
reales, no solo en `check`) que el lado marcado sea único en las claves
del `on`; una violación aborta la publicación (`docs/join-cardinality.md`):

```strata
source orders(ns: "crm", dataset: "orders") { columns: { order_id: int64 nonnull, customer_id: int64 nonnull } }
source customers(ns: "crm", dataset: "customers") { columns: { customer_id: int64 nonnull, name: string nonnull } }

model orders_with_customer {
  from orders
  join_inner customers on orders.customer_id == customers.customer_id expect many_to_one
  select { order_id = orders.order_id, name = customers.name }
}
```

### `filter`, `let`

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, country: string nonnull, gross_amount_usd: money nonnull, order_day: date nonnull }
}

model daily {
  from orders
  let gross = gross_amount_usd
  filter order_id > 0
  group { country, order_day } (
    aggregate { orders = count(order_id), total = sum(gross) }
  )
  sort { order_day, country }
  take 10
}
```

`let` define una columna intermedia (no aparece en la salida a menos que
se re-liste en `select`/`derive`/`aggregate`); `group { keys } (aggregate
{ ... })` agrupa; `sort`/`take` ordenan y limitan.

### `select` / `derive`

**`select` y `derive` son hoy exactamente el mismo mecanismo** —ambos
llaman a la misma rutina interna que registra columnas de salida
explícitas—, así que no hay ninguna diferencia de comportamiento entre
usar uno u otro; son dos nombres para la misma cosa. **Importante**: en
cuanto CUALQUIERA de los dos aparece (con al menos una asignación), el
passthrough implícito de todas las columnas base se apaga — la salida
del modelo pasa a ser exactamente lo que `select`/`derive` listó, ni una
columna más. Si quieres una columna nueva Y conservar las originales,
tienes que re-listarlas explícitamente:

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, gross_amount_usd: money nonnull }
}

model m {
  from orders
  derive { order_id = order_id, doubled = gross_amount_usd + gross_amount_usd }
}
```

(Sin el `order_id = order_id`, la salida de este modelo sería solo
`doubled` — verificado: es lo que da `strata check` si se omite.)

### `expand`

Una fila por elemento de una columna `array(T)` del `from` (no de una
derivada):

```strata
source events(ns: "crm", dataset: "events") {
  columns: { event_id: int64 nonnull, tags: array(string) }
}

model tags_exploded {
  from events
  expand tags as tag
  select { event_id = event_id, tag = tag }
}
```

Detalles y límites (arrays anidados, columnas JSON) en
`docs/json-arrays.md`.

## 3. Operaciones de conjunto

`union [all]` / `intersect` / `except` combinan modelos, **no fuentes
directamente** — el lado derecho de un set-op debe ser un `model`, aunque
sea uno trivial que solo hace `from`:

```strata
source es_orders(ns: "crm", dataset: "es_orders") { columns: { order_id: int64 nonnull, country: string nonnull } }
source mx_orders(ns: "crm", dataset: "mx_orders") { columns: { order_id: int64 nonnull, country: string nonnull } }

model mx { from mx_orders }
model all_orders {
  from es_orders
  union mx      // o: union all mx / intersect mx / except mx
  union more    // set-ops consecutivos encadenan
  dedup
  // o: dedup by order_id → conserva una fila por clave, determinista
}
```

Los set-ops son consecutivos (`from a union b union c`); lo que haya antes
del primero da forma a la rama izquierda y lo que haya después ve las filas
combinadas (`filter`, `select`, `derive`, `group`, `sort`, `take` y también
`join_*`). Una referencia calificada al modelo derecho (`b.x`) se resuelve
contra la columna combinada. El esquema de todas las ramas debe alinear por
nombre y tipo (unificado, como `coalesce`). `dedup` es `SELECT DISTINCT`
sobre las columnas de salida; `dedup by k1, k2` conserva una fila por clave
vía `ROW_NUMBER` determinista. En agregación, `count(distinct x)` emite
`COUNT(DISTINCT x)`; `distinct` en otra función o en ventanas es E096
(ver `docs/setops.md`).

## 4. Tipos

Escalares: `int64`, `float64`, `string`, `bool`, `date`, `timestamp`,
`uuid`, `json`. Además:

- `decimal(precision, scale)`: `decimal(10, 2)`.
- `money`: `money`, o `money(EUR)`/`money(USD)` fijando la divisa (por
  defecto `USD`).
- `array(T)`: recursivo, `array(array(string))` es válido.
- `domain nombre = <tipo>`: alias transparente, resuelto al cargar el
  proyecto — un `domain country_code = string` se comporta exactamente
  como `string` en contratos, casts y elementos de array.

```strata
domain country_code = string

source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, country: country_code nonnull, tags: array(array(string)) }
}

model m {
  from orders
  select { order_id = order_id, country = country, tags = tags }
}
```

**Límite no obvio verificado**: `money` NO es un tipo "numérico" a efectos
de comparación (`is_numeric()` en `strata/types.py` solo incluye
`int64`/`float64`/`decimal`) — comparar una columna `money` contra un
literal entero falla con `E051 cannot compare money(USD) with int64`. Hay
que envolver el literal: `cast(100, "money")` (el segundo argumento de
`cast` es siempre un literal string con el nombre del tipo, nunca un tipo
sin comillas).

## 5. Funciones (catálogo único, `strata/functions.py`)

### Agregadas

`count`, `sum`, `avg`, `max`, `min`, `array_agg` — legales solo dentro de
`aggregate { }` (E056 si no).

### Ventanas: `fn(args) over (partition_by: [...], sort: [...])`

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, country: string nonnull, gross_amount_usd: money nonnull, order_day: date nonnull }
}

model m {
  from orders
  select {
    order_id = order_id,
    country = country,
    rn = row_number() over (partition_by: [country], sort: [order_day]),
    country_total = sum(gross_amount_usd) over (partition_by: [country]),
  }
}
```

`row_number`, `rank`, `dense_rank`, `lag`, `lead`, `first_value`,
`last_value`, y cualquier agregado (`sum`, `avg`, ...) son ventaneables.
Colocación (E065): solo en salidas de `select`/`derive`/`aggregate`, nunca
en `let`/`filter`/`sort`/claves de `group`/condiciones de `join`, y sin
anidar ventanas.

### String

`upper`, `lower`, `concat`, `length`, `substring`, `trim`/`ltrim`/`rtrim`,
`replace`, `lpad`/`rpad`, `startswith`, `split_part`, `regexp_replace`,
`left`, `right`.

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, country: string nonnull }
}

model m {
  from orders
  select {
    order_id = order_id,
    country_upper = upper(country),
    initial = left(country, 1),
    padded = lpad(country, 4, "-"),
  }
}
```

### Fecha

`date_add`/`date_sub` (unidad como kwarg: `years:`/`months:`/`weeks:`/
`days:`), `date_trunc`/`date_diff` (unidad como símbolo o string):

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, order_day: date nonnull }
}

model m {
  from orders
  select {
    order_id = order_id,
    next_month = date_add(order_day, months: 1),
    month_start = date_trunc(order_day, month),
  }
}
```

### JSON / arrays

`json_get`/`json_value` (clave literal simple o dinámica; búsqueda exacta
de miembro, nunca ruta), `json_path` (JSONPath acotado a raíz `$` + pasos
de miembro/índice), `json_build`, `array_length`, `array_get` (índice
desde cero), `array_construct`, `array_concat`/`array_contains`/
`array_append`/`array_prepend`/`array_remove`/`array_sort`/
`array_index_of`, `array_agg`. Límites medidos por dialecto (BigQuery sin
clave dinámica, Snowflake con sintaxis propia) en `docs/json-arrays.md`.

```strata
source events(ns: "crm", dataset: "events") {
  columns: { event_id: int64 nonnull, payload: json, tags: array(string) }
}

model m {
  from events
  select {
    event_id  = event_id,
    user_id   = json_get(payload, "user_id"),
    tag_count = array_length(tags),
    first_tag = array_get(tags, 0),
  }
}
```

### Condicionales: `if` / `case`

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, gross_amount_usd: money nonnull }
}

model m {
  from orders
  select {
    order_id = order_id,
    size = case(gross_amount_usd >= cast(100, "money"), "large",
                gross_amount_usd >= cast(50, "money"), "medium",
                "small"),
    flagged = if(gross_amount_usd >= cast(100, "money"), true, false),
  }
}
```

`if(cond, then, else)`: aridad exactamente 3, `cond` debe ser `bool`.
`case(cond, val, [cond, val, ...], [else])`: aridad mínima 2, cualquier
número de pares; sin `else`, una fila sin match da `NULL`. Ambos se
emiten como `CASE WHEN...END`, idéntico en los cuatro dialectos. Detalle
completo: `docs/incremental.md` no, este es nuevo — ver el catálogo en
`strata/functions.py` (`if`/`case`) y `tests/test_conditionals.py`.

## 6. Contratos

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, country: string nonnull, email: string }
}

contract OrderContract {
  order_id : int64 nonnull primary_key
  country  : string nonnull enum {ES, MX, CO, BR}
  email    : string protected classification: "pii"
}

model m -> contract OrderContract {
  from orders
  select { order_id = order_id, country = country, email = email }
}
```

`nonnull`, `unique`, `primary_key` (`unique` implícito), `enum {A, B, ...}`
(valores sin comillas), `protected` (marca de sensibilidad; no implica
enmascaramiento automático — ver límite en `propuesta-lenguaje-strata.md`),
`classification: "texto"` (metadato libre, requiere las comillas y los
dos puntos).

`build <archivo> [modelos...] --strict` exige contrato a todo modelo
comprobado, incluidas dependencias transitivas:

```
$ python -m strata build sin_contrato.strata --strict
error: E014: strict mode requires every built model to declare -> contract: m
```

Detalle: `docs/strict-contracts.md`.

## 7. Semántica de warehouse

`partition_by [cols]`, `freshness <umbral>` (`1h`, `24h`, `daily`,
`weekly`, `monthly`, o una expresión SQL entre comillas),
`freshness_column: col` — framework real conectado a `run --only-stale`
(detección de staleness efectiva, no solo aceptado por el parser). Detalle
completo: `docs/§2-warehouse-semantics.md`.

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, order_day: date nonnull }
}

model m {
  from orders
  partition_by [order_day]
  freshness 1h
}
```

`incremental merge_strategy: append|upsert` con `cdc_column` (obligatorio)
y `merge_keys` (obligatorio solo para `upsert`) **ejecuta merge real**
—no es solo sintaxis aceptada—: en cada run que no sea el primero para ese
modelo, fusiona la snapshot anterior con las filas cuyo `cdc_column` es
mayor que su watermark, en vez de recomputar todo desde cero. No soportado
sobre modelos `group`/`aggregate` (rechazado en compilación, E087: no es
sonante reagregar solo el delta). Detalle completo, incluida la prueba
que distingue esto de un rebuild completo: `docs/incremental.md`, sección
"Merge por fila".

```strata
source events(ns: "crm", dataset: "events") {
  columns: { event_id: int64 nonnull, updated_at: timestamp nonnull }
}

model m {
  from events
  incremental
  merge_strategy: upsert
  merge_keys: [event_id]
  cdc_column: updated_at
}
```

## 8. No implementado hoy

Verificado que fallan (no es una omisión, es el estado real):

- `like`/`rlike`: `filter country like "E%"` → `ERROR: unexpected token
  'like' in model body` (ni siquiera es un error de tipos: el parser no
  reconoce la palabra).
- Constructores de colección `list`/`dict`/`map`: `list(1, 2, 3)` →
  `E059: unknown function 'list'`.
- Tipo `struct`: `meta: struct` en una columna → `E078: unknown type
  'struct'`.

Si necesitas alguna de estas, `spec/grammar.md` las deja documentadas
como fuera del subconjunto soportado, no como un error de esta versión.
