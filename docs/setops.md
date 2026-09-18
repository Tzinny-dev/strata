# Operaciones de conjuntos y deduplicación

Un modelo combina sus filas actuales con las de otro modelo del mismo
proyecto (`union`, `intersect`, `except`), o elimina duplicados exactos
(`dedup`). Las ramas deben tener las mismas columnas en el mismo orden con
tipos compatibles; la nulabilidad del resultado es la OR de ambas ramas.

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

Las sentencias antes del set-op dan forma a la **rama izquierda** (`filter`
y `let` aplican solo a ella); las sentencias después ven las **filas
combinadas** (`filter`, `select`, `derive`, `group`, `sort`, `take`):

```strata
model recent { from a filter x > 1 union b }
model labelled { from a union b select { z = x * 10, y = y } }
model per_y { from a union b group { y } ( aggregate { c = count() } ) }
```

Reglas (E076 si se incumplen): un solo set-op por modelo (encadenar pasa por
un modelo downstream); sin `join` en modelos con set-op (se hace downstream);
sin segundo `from`; el lado derecho es un **modelo**, no una source (envolver
la source en un modelo); el set-op precede a cualquier
`select`/`derive`/`aggregate`/`group`/`sort`/`take`. `expand` puede ir antes
(corre dentro de la rama izquierda) pero no después. Una auto-referencia es
un ciclo de dependencias (F001) y un modelo inexistente es E020.

## Compatibilidad de esquemas

La rama derecha debe llevar las mismas columnas en el mismo orden; el tipo de
cada columna es la unificación de ambas (`int64` + `float64` → `float64`, como
en `coalesce`). Nombres u orden distintos, o tipos no unificables (`string`
con `int64`), se rechazan con E077. Ambas ramas emiten los mismos alias en el
mismo orden con casts explícitos al tipo unificado: DuckDB casa las ramas de
un `UNION` **por nombre** (comprobado: `SELECT y,x ... UNION SELECT x,y`
mezcla columnas) mientras el resto casa por posición, así que solo los alias
idénticos son portables.

## Los cuatro operadores

| Sentencia | SQL | Duplicados |
| --- | --- | --- |
| `union m` | `UNION` | Elimina duplicados |
| `union all m` | `UNION ALL` | Los conserva |
| `intersect m` | `INTERSECT` | Siempre distintos |
| `except m` | `EXCEPT` | Siempre distintos |

`intersect`/`except` no aceptan `all`: BigQuery y Snowflake no tienen las
variantes `INTERSECT ALL`/`EXCEPT ALL`, así que el lenguaje no las ofrece en
ningún dialecto. El lineage registra ambas ramas (orígenes de la izquierda
más `(nodo, col, "set")` de la derecha) y los `reads` incluyen las columnas
consumidas de cada lado.

## `dedup`

`dedup` es `SELECT DISTINCT` sobre el conjunto final de filas (antes de
`sort`/`take`): sin argumentos, sin forma por clave. Deduplicar por clave sin
desempate es dependiente del motor, así que conservar una fila por clave se
escribe como un `group` explícito. `DISTINCT` funciona sobre `json` y arrays
en los cuatro motores (verificado en DuckDB, incluido `null` JSON y arrays
NULL). `union all ... dedup` equivale a `union`.

## Dialectos y límites

- DuckDB/PostgreSQL/BigQuery/Snowflake: los cuatro operadores existen con la
  misma semántica DISTINCT/ALL descrita. Ejecución real solo en DuckDB; los
  otros tres verificados por patrón de emisión.
- Ramas con tipos ensanchados emiten `CAST(... AS <tipo>)` en el lado que
  difiere (`CAST(x AS DOUBLE)` en DuckDB/Postgres, `FLOAT64` en BigQuery).
- El `money` solo se alinea consigo mismo exacto: mezclar `money` con otro
  tipo numérico es E077 en lugar de un cast de divisa silencioso.

Errores: E076 forma/colocación del set-op (segundo set-op, joins, segundo
`from`, source como rama derecha, set-op tras salidas/orden/límite/grupo,
`expand` posterior), E077 ramas incompatibles, F001 ciclo, E020 modelo
inexistente. Los contratos y el lineage pasan por el checker; `fmt` hace
round-trip de las tres sentencias y el fingerprint es estable.

Pendiente: varios set-ops en un mismo modelo, joins mezclados con set-ops,
referencias calificadas al modelo derecho (`b.x`), `count(distinct x)` y
deduplicación por clave.
