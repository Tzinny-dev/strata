# Cardinalidad de joins

Un join puede anotarse con la cardinalidad que promete (`expect many_to_one`
o `expect one_to_one`). La forma se valida en compilación (E079) y los datos
la verifican al materializar: contar grupos de claves duplicadas en las
tablas upstream, y abortar como un pin si aparecen.

```strata
source orders(ns: "app", dataset: "orders") {
  columns: { order_id: int64, customer_id: int64, amount: float64 }
}
source customers(ns: "app", dataset: "customers") {
  columns: { customer_id: int64 nonnull, country: string }
}
model enriched {
  from orders
  join_left customers on orders.customer_id == customers.customer_id expect many_to_one
  select { order_id = order_id, country = customers.country }
}
model profiles {
  from orders
  join_left customers on orders.customer_id == customers.customer_id expect one_to_one
}
```

## Semántica

- `many_to_one`: cada fila izquierda casa como mucho con una derecha. Se
  verifica probando que las claves de la derecha son únicas; los duplicados
  de la izquierda son "los muchos" y están permitidos.
- `one_to_one`: además, las claves de la izquierda son únicas.
- Solo `join_left` y `join_inner`: `anti`/`semi` nunca multiplican filas y
  la anotación allí es E079. La expectativa limita la *multiplicidad* de los
  matches, no la preservación (eso lo hace el tipo de join: un `inner`
  puede seguir descartando filas).
- Sin anotación no hay comprobación: los joins existentes compilan y se
  ejecutan exactamente igual que antes.

## Qué claves valen

La condición `on` debe ser un AND de `==` entre columnas base planas (o una
columna y un literal). Conjunciones que solo filtran filas izquierdas se
ignoran con seguridad (quitan matches, no los crean); cualquier referencia
a la tabla derecha fuera de una clave ecuacional limpia es E079, igual que
las condiciones no-ecuacionales (`>`, `or`, llamadas sobre columnas
derechas), las comparaciones del mismo lado y la falta de claves. Las claves
pueden ser compuestas (`on a.x == b.x and a.y == b.y` agrupa por ambas).

Las claves de una expresión del lado izquierdo (`upper(email)`, un `let`)
valen para `many_to_one` — la unicidad de la derecha sigue acotando los
matches — pero `one_to_one` exige al menos un par que relacione una columna
izquierda plana con una derecha plana. Lo que no se puede probar contando
claves se reescribe upstream, en voz alta.

## Verificación runtime

Al materializar, por cada join anotado se ejecuta contra las mismas tablas
que ve el modelo (vistas staged/live según el run, con `source_overrides`
aplicados):

```sql
SELECT COUNT(*) FROM (
  SELECT 1 FROM <tabla> WHERE <k> IS NOT NULL [AND ...]
  GROUP BY <claves> HAVING COUNT(*) > 1
) t
```

Cero es pasar; cualquier otra cosa aborta con `PinError` (`join cardinality
FAILED [modelo join tabla]: expected many_to_one but ... duplicate key
groups (...)`) y deja vivo el último dato bueno, como los pins. Las claves
todas-NULL se excluyen: en un equi-join nunca casan y no pueden explotar
filas. El éxito se reporta en el informe de pins (`ok m left customers:
many_to_one (...)`).

La comprobación corre en `materialize` (cubre `run`, `test` y `replay`
si re-ejecutan); `check`/`build` solo validan la forma (E079). Ejecución
real solo en DuckDB, como el resto de `exec`.

## Dialectos y límites

- La sintaxis no añade palabras clave: `expect` ya existía y
  `many_to_one`/`one_to_one` son identificadores contextuales (una errata
  es `ParseError`). La gramática GBNF los acepta y el muestreador sigue
  verde.
- No hay `many_to_many`/`one_to_many`: describirían fanout, no lo
  impedirían.
- Un `unique`/`primary_key` declarado no exime la prueba: los flags son
  declaraciones y los datos pueden violarlos (por eso los pins re chequean
  `unique` en runtime); la anotación siempre ejecuta su conteo.
- Coste: una agregación por lado chequeado y por materialización sobre la
  tabla upstream completa.

Errores: E079 forma/colocación (anti/semi, condición no-ecuacional,
referencias exóticas a la derecha, sin claves, `one_to_one` sin par
izquierda-derecha), `ParseError` en la palabra de cardinalidad, `PinError`
en la violación de datos. Pendiente: eximir la prueba cuando la unicidad ya
está pineada en el mismo run, y muestreo acotado para dimensiones enormes.
