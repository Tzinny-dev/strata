# Set operations and deduplication

A model combines its current rows with those of other models in the same
project (`union`, `intersect`, `except`), or removes duplicates (`dedup`).
The branches must have the same columns in the same order with compatible
types; the result's nullability is the OR of all the branches.

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

## Pipeline semantics

Set-ops are **consecutive**: `from a union b union c` chains
(`(a union b) union c`, same precedence as the engines). The statements
**before the first set-op** shape the **left branch** (`filter` and
`let` apply only to it); the statements **after the chain** see the
**combined rows** (`filter`, `select`, `derive`, `group`, `sort`,
`take`) and even **joins** (they append columns prefixed `__j{k}_{col}` to
the combined rows):

```strata
model recent { from a filter x > 1 union b }
model labelled { from a union b select { z = x * 10, y = y } }
model per_y { from a union b group { y } ( aggregate { c = count() } ) }
model enriched { from a union b join_left c on x == c.x }
```

Rules (E076 if violated): a `let`/`filter`/`join` **between** two set-ops
breaks the chain (siblings must be consecutive); a `join` **before** the
first set-op is illegal (join post-set-op or downstream); no second `from`;
the right-hand side is a **model**, not a source (wrap the source
in a model); the set-op precedes any
`select`/`derive`/`aggregate`/`group`/`sort`/`take`. `expand` can go before
(it runs inside the left branch) but not after. A self-reference is
a dependency cycle (F001) and a nonexistent model is E020.

## Qualified references to the right model

Since each set-op records its right-hand model, a qualified reference to it
(`b.x`) resolves against the **combined** column of that name — useful in
expressions after the chain:

```strata
model doubled { from a union b select { z = b.x * 10, y = y } }
```

In SQL the references collapse to the unqualified combined column in the
outer query (and to `t0.{col}` inside the body, so that a post-join does not
make it ambiguous).

## Schema compatibility

Each column records the types of **all** the branches, in chain order; the
model's type is their unification (`int64` + `float64` →
`float64`, as in `coalesce`). Different names or order, or types that cannot
be unified (`string` with `int64`), are rejected with E077. All the branches
emit the same aliases in the same order with explicit casts to the unified
type in the branches that differ: DuckDB matches the branches of a `UNION`
**by name** (checked: `SELECT y,x ... UNION SELECT x,y` mixes up columns)
while the rest match by position, so only identical aliases are
portable.

## The four operators

| Statement | SQL | Duplicates |
| --- | --- | --- |
| `union m` | `UNION` | Removes duplicates |
| `union all m` | `UNION ALL` | Keeps them |
| `intersect m` | `INTERSECT` | Always distinct |
| `except m` | `EXCEPT` | Always distinct |

`intersect`/`except` do not accept `all`: BigQuery and Snowflake do not have
the `INTERSECT ALL`/`EXCEPT ALL` variants, so the language does not offer them
in any dialect. The lineage records all the branches (the left's origins
plus `(node, col, "set")` from each right) and the `reads` include
the columns consumed from each side.

## `dedup`

`dedup` is `SELECT DISTINCT` over the final set of rows (before
`sort`/`take`): with no arguments it removes identical rows;
`dedup by k1, k2` keeps one row per key in a **deterministic** and portable
way:

```strata
model latest { from a union all b dedup by y }
model keyed { from a union all b select { x = x, y = y } dedup by x }
```

`dedup by` is implemented as `ROW_NUMBER() OVER (PARTITION BY keys ORDER BY
the rest of the output columns)` keeping `rn = 1`: same tie-breaking on any
engine (NULLs follow each warehouse's order; for an explicit order,
a later `sort` referencing output columns). The keys must be unqualified
output columns; `select`/`sort` after `dedup by` only see the selected
columns (E076 if a key or a `sort` references something
outside the output). `dedup` and `dedup by` are mutually exclusive.
`DISTINCT` works on `json` and arrays in the four engines (verified on
DuckDB, including JSON `null` and NULL arrays). `union all ... dedup` is
equivalent to `union`.

## `count(distinct x)`

`count(distinct expr)` is the only DISTINCT form of aggregation: it emits
`COUNT(DISTINCT expr)` and is available in aggregation and in `group`:

```strata
model per_y { from a group { y } ( aggregate { n = count(distinct x) } ) }
```

`distinct` in any other function, in `count(distinct *)`, or inside an
`over (...)` is E096.

## Dialects and limits

- DuckDB/PostgreSQL/BigQuery/Snowflake: the four operators exist with the
  same DISTINCT/ALL semantics described and the chain is emitted as nested
  parentheses (`((a op b) op c)`). The `ROW_NUMBER` of `dedup by` is
  standard in the four engines. Real execution only on DuckDB; the other
  three verified by emission pattern.
- Branches with widened types emit `CAST(... AS <type>)` on the sides that
  differ (`CAST(x AS DOUBLE)` in DuckDB/Postgres, `FLOAT64` in BigQuery).
- `money` only unifies with exact `money`: mixing `money` with another
  numeric type is E077 instead of a silent currency cast.

Errors: E076 set-op form/placement (chain broken by
`let`/`filter`/`join`, joins before the first set-op, second `from`, source
as the right branch, set-op after outputs/order/limit/group, later `expand`,
keys or `sort` of `dedup by` outside the output), E077 incompatible
branches, E096 `distinct` in a non-count/window form, F001 cycle,
E020 nonexistent model. Contracts and lineage pass through the checker;
`fmt` round-trips all the forms (including chains, `dedup by` and
`count(distinct x)`) and the fingerprint is stable.
