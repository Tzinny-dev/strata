# Operaciones de conjuntos y deduplicación

Un modelo combina sus filas actuales con las de otros modelos del mismo
proyecto (`union`, `intersect`, `except`), o elimina duplicados (`dedup`).
Las ramas deben tener las mismas columnas en el mismo orden con tipos
compatibles; la nulabilidad del resultado es la OR de todas las ramas.

```strata
source s(ns: "app", dataset: "s") { columns: { x: int64, y: string } }
source t(ns: "app", dataset: "t") { columns: { x: int64, y: string } }
model a { from s }
model b { from t }
model combined { from a union b }
model everything { from a union all b }
model shared { from a intersect b }
model only_a { from a except b }
model unique_a { from a dedup }
```

## Semántica de pipeline

Los set-ops son **consecutivos**: `from a union b union c` encadena
(`(a union b) union c`, mismo precedencia que los motores). Las sentencias
**antes del primer set-op** dan forma a la **rama izquierda** (`filter` y
`let` aplican solo a ella); las sentencias **después de la cadena** ven las
**filas combinadas** (`filter`, `select`, `derive`, `group`, `sort`,
`take`) e incluso **joins** (anexan columnas prefijadas `__j{k}_{col}` a las
filas combinadas):

```strata
model recent { from a filter x > 1 union b }
model labelled { from a union b select { z = x * 10, y = y } }
model per_y { from a union b group { y } ( aggregate { c = count() } ) }
model enriched { from a union b join_left c on x == c.x }
```

Reglas (E076 si se incumplen): un `let`/`filter`/`join` **entre** dos set-ops
rompe la cadena (los hermanos deben ser consecutivos); un `join` **antes**
del primer set-op es ilegal (se une post-set-op o downstream); sin segundo
`from`; el lado derecho es un **modelo**, no una source (envolver la source
en un modelo); el set-op precede a cualquier
`select`/`derive`/`aggregate`/`group`/`sort`/`take`. `expand` puede ir antes
(corre dentro de la rama izquierda) pero no después. Una auto-referencia es
un ciclo de dependencias (F001) y un modelo inexistente es E020.

## Referencias calificadas al modelo derecho

Como cada set-op registra a su modelo derecho, una referencia calificada a él
(`b.x`) se resuelve contra la columna **combinada** de ese nombre — útil en
expresiones posteriores a la cadena:

```strata
model doubled { from a union b select { z = b.x * 10, y = y } }
```

En SQL la referencias colapsan a la columna combinada sin cualificador en la
consulta exterior (y a `t0.{col}` dentro del cuerpo, para que un post-join no
la vuelva ambigua).

## Compatibilidad de esquemas

Cada columna se registra con los tipos de **todas** las ramas, en orden de
cadena; el tipo del modelo es su unificación (`int64` + `float64` →
`float64`, como en `coalesce`). Nombres u orden distintos, o tipos no
unificables (`string` con `int64`), se rechazan con E077. Todas las ramas
emiten los mismos alias en el mismo orden con casts explícitos al tipo
unificado en las ramas que difieren: DuckDB casa las ramas de un `UNION`
**por nombre** (comprobado: `SELECT y,x ... UNION SELECT x,y` mezcla columnas)
mientras el resto casa por posición, así que solo los alias idénticos son
portables.

## Los cuatro operadores

| Sentencia | SQL | Duplicados |
| --- | --- | --- |
| `union m` | `UNION` | Elimina duplicados |
| `union all m` | `UNION ALL` | Los conserva |
| `intersect m` | `INTERSECT` | Siempre distintos |
| `except m` | `EXCEPT` | Siempre distintos |

`intersect`/`except` no aceptan `all`: BigQuery y Snowflake no tienen las
variantes `INTERSECT ALL`/`EXCEPT ALL`, así que el lenguaje no las ofrece en
ningún dialecto. El lineage registra todas las ramas (orígenes de la
izquierda más `(nodo, col, "set")` de cada derecha) y los `reads` incluyen
las columnas consumidas de cada lado.

## `dedup`

`dedup` es `SELECT DISTINCT` sobre el conjunto final de filas (antes de
`sort`/`take`): sin argumentos elimina filas idénticas; `dedup by k1, k2`
conserva una fila por clave de forma **determinista** y portable:

```strata
model latest { from a union all b dedup by y }
model keyed { from a union all b select { x = x, y = y } dedup by x }
```

`dedup by` se implementa como `ROW_NUMBER() OVER (PARTITION BY claves ORDER BY
resto de las columnas de salida)` y queda con `rn = 1`: mismo desempate en
cualquier motor (NULLs al orden de cada warehouse; para un orden explícito,
un `sort` posterior referenciando columnas de salida). Las claves deben ser
columnas de salida sin cualificar; `select`/`sort` tras `dedup by` solo ven
las columnas seleccionadas (E076 si una clave o un `sort` referencia algo
fuera de la salida). `dedup` y `dedup by` son mutuamente excluyentes. `DISTINCT`
funciona sobre `json` y arrays en los cuatro motores (verificado en DuckDB,
incluido `null` JSON y arrays NULL). `union all ... dedup` equivale a `union`.

## `count(distinct x)`

`count(distinct expr)` es la única forma DISTINCT de agregación: emite
`COUNT(DISTINCT expr)` y está disponible en agregación y en `group`:

```strata
model per_y { from a group { y } ( aggregate { n = count(distinct x) } ) }
```

`distinct` en cualquier otra función, en `count(distinct *)` o dentro de un
`over (...)` es E096.

## Dialectos y límites

- DuckDB/PostgreSQL/BigQuery/Snowflake: los cuatro operadores existen con la
  misma semántica DISTINCT/ALL descrita y la cadena se emite como
  paréntesis anidados (`((a op b) op c)`). El `ROW_NUMBER` de `dedup by` es
  estándar en los cuatro motores. Ejecución real solo en DuckDB; los otros
  tres verificados por patrón de emisión.
- Ramas con tipos ensanchados emiten `CAST(... AS <tipo>)` en los lados que
  difieren (`CAST(x AS DOUBLE)` en DuckDB/Postgres, `FLOAT64` en BigQuery).
- El `money` solo se alinea consigo mismo exacto: mezclar `money` con otro
  tipo numérico es E077 en lugar de un cast de divisa silencioso.

Errores: E076 forma/colocación del set-op (cadena rota por
`let`/`filter`/`join`, joins antes del primer set-op, segundo `from`, source
como rama derecha, set-op tras salidas/orden/límite/grupo, `expand`
posterior, claves o `sort` de `dedup by` fuera de la salida), E077 ramas
incompatibles, E096 `distinct` en una forma no count/ventana, F001 ciclo,
E020 modelo inexistente. Los contratos y el lineage pasan por el checker;
`fmt` hace round-trip de todas las formas (incluidas cadenas, `dedup by` y
`count(distinct x)`) y el fingerprint es estable.