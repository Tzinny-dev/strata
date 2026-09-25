# JSON and arrays: basic access

First delivery of the collection catalog. It requires no new call syntax; the
prototype schemas write `array(int64)`, not `array<int64>` (the latter is the
type representation in the reports).

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

## Semantics

| Function | Return | Rules |
| --- | --- | --- |
| `json_get(doc, key)` | `json` nullable | Object member by literal key or by dynamic key (text expression). Preserves JSON null and containers. Missing member, SQL NULL base, or non-object base → SQL NULL. |
| `json_value(doc, key)` | `string` nullable | Scalar as text without JSON quotes, by literal or dynamic key. Missing member, JSON null, object, or array → SQL NULL. |
| `json_path(doc, "$...")` | `json` nullable | Query by path `$...` with member and index steps (`$.a.b`, `$.xs[0]`, `$` alone). JSON null base, nonexistent path, or non-matching structure → SQL NULL. The path must be a string literal: filters, recursive descent `$..`, wildcards, and quoted keys are rejected at compile time with E074. |
| `array_length(xs)` | `int64` | Counts positions, including NULL elements. Empty array → 0; SQL NULL array → SQL NULL. Inherits nullability from the array. |
| `array_get(xs, index)` | element type, nullable | Integer index starting at **zero**, also dynamic. NULL, negative, or out-of-range index → SQL NULL. |

The literal keys of `json_get`/`json_value` are ASCII matching
`[A-Za-z_][A-Za-z0-9_]*`, case-sensitive. They are not paths: `"$.x"`,
`"x.y"` and `""` are rejected with E074 (for paths there is `json_path`). You
can compose `json_value(json_get(payload, "detail"), "name")`.

A **non-literal** key (`string` column or text expression) is a dynamic key:
it is resolved at runtime and is always an **exact** member lookup, never a
path. With `key = "a.b"` the member named `a.b` is read, not the nested
`a → b`; an empty `key` or SQL NULL returns SQL NULL.

```strata
model by_field { from events select { pick = json_get(payload, field) } }
```

It is only emitted where the dialect can express that exact lookup (DuckDB,
PostgreSQL and Snowflake); in BigQuery compilation fails loud (see dialects).
It is not validated at runtime that the key is a simple member name: a key
starting with `$` keeps DuckDB's path behavior, and
`json_get(doc, key)` with the key `'$'` returns the full document in that engine.

`json_path` always emits the path **quoted** as a string literal (or as a
`jsonpath` literal in PostgreSQL): emitting it unquoted is invalid SQL in the
four engines. The path is validated at compile time with
`functions.json_path_problem`, the same definition the generator uses: only
the subset that behaves the same in the four engines is allowed (root `$`
plus member and index steps).
Filters, recursive descent `$..`, wildcards
(`$[*]`) and quoted keys (`$['a.b']`, `$."a.b"`) are rejected with E074:
checked against real DuckDB and PostgreSQL, `$['a.b']` is a syntax error in
both (only BigQuery accepts it), `$."a.b"` is SQL/JSON only (DuckDB and
PostgreSQL) and `$[*]` returns an array of matches in DuckDB but the first
match in PostgreSQL, so it cannot be declared `json` in both.

The container must have a known type: `array_get(null, 0)` is rejected; a
nullable `array(int64)` column is valid. Array elements can be NULL even if
the array is `nonnull`. The current simple scalar types are accepted: int64,
float64, string, bool, date, timestamp, uuid and json.

Errors: E062 arity, E063 types (including untyped NULL container and a
dynamic key that is not of type string), E064 improper asterisk, E065 use as
a window function, E074 literal key that is not a simple member name and
inexpressible `json_path` path (non-literal, no root `$`, filters, `$..`,
wildcards or quoted steps).
Contract constraints and lineage continue to go through the checker.
The NameError that prevented typing `array(int64)` was also fixed.

## Aggregation and row expansion (second delivery)

Two operations that do require new syntax/functions over arrays:

### `array_agg(expr)` — aggregation inside `group`

An aggregate function (`aggregate = true`, `collection = true`; outside a
`group` body it is E056) that returns **an array with the group's NON-NULL
values**, in encounter order:

```strata
model history {
  from events
  group { field } ( aggregate { indices = array_agg(index) } )
}
```

- Element = the type of the argument (simple scalar, including `json`). An
  argument that is already an array would produce a nested array and is
  rejected with E063; an untyped `null` has no element either.
  Result: `array(elem)` **nullable**.
- A group with no rows, or with all values NULL, returns **NULL** in the
  four engines, and NULLs are excluded so that the semantics do not depend on
  the engine: DuckDB/PostgreSQL `ARRAY_AGG(x) FILTER (WHERE x IS NOT NULL)`,
  BigQuery `ARRAY_AGG(x IGNORE NULLS)`, Snowflake `ARRAY_AGG(x)` (discards
  the NULLs).
  Verified on DuckDB 1.5.5: the native `ARRAY_AGG` keeps the NULLs, which is
  why they are filtered explicitly.

### `expand` — one row per array element

```strata
model per_score {
  from events
  expand scores
  select { score = scores }
}
model per_score_e {
  from events
  expand scores as s
  filter s > 0
}
```

- Syntax: `expand <col> [as <name>]`. Only `expand` is added to the
  vocabulary (grammar + lexer, with an `expand-stmt` rule); `as` is parsed
  contextually as an identifier, so it does not break existing schemas.
- `expand xs` shadows: the array column disappears from the schema and `xs`
  becomes the element column (nullable, element type). `expand xs as e`
  keeps `xs` and adds `e`.
- The source must be a column **of the `from`** of type `array(elem)` with a
  scalar element (no nested arrays). A `let`/`derive` or join column does not
  work as a source (it lost the row context). At most one `expand` per model
  — a second lateral unnest would multiply rows. Errors: E075.
- The expansion is emitted in the base subquery, before any grouping, as a
  lateral unnest: DuckDB/PostgreSQL
  `CROSS JOIN LATERAL UNNEST(t0.xs) AS u0(e)`,
  BigQuery `CROSS JOIN UNNEST(t0.xs) AS e`, Snowflake
  `CROSS JOIN LATERAL FLATTEN(input => t0.xs) AS u0` with
  `CAST(u0.VALUE AS <type>)` for scalar elements (the `json` ones stay as
  VARIANT, Snowflake's native json). Row with a NULL or empty array → 0 rows.
  The `AS u0(e)` alias is required in DuckDB/PostgreSQL: `unnest(xs) AS e`
  would expose the element as a STRUCT. Verified on DuckDB 1.5.5.
- After `expand` you can group (the element is already a scalar value) and
  derive/filter like any column.

## Dialects and limits

- DuckDB: `JSON_EXTRACT(doc, '<path>')`; the expressible path subset (member
  and index) is allowed. Dynamic key:
  `JSON_EXTRACT(doc, NULLIF(key, ''))`. Checked against DuckDB: a path without
  `$` is an exact key (neither does `'a.b'` traverse nor `'x[0]'` index), the
  empty path folds to NULL because DuckDB resolves it to the full document,
  and `$..` and `$[*]` return arrays of matches (hence they are rejected
  before emitting).
- PostgreSQL: `jsonb_path_query_first(doc::jsonb, '<path>', '{}'::jsonb, TRUE)` —
  **verified against a real PostgreSQL 16 server**: the same model run in
  DuckDB and in PostgreSQL returns the same values for the whole JSON/arrays
  catalog (22 expressions, including dynamic keys, `json_path`,
  `json_build` and all the `array_*`). The empty `vars` object is passed
  explicitly because a NULL `vars` makes the function always return NULL
  (checked), and `silent = TRUE` suppresses structural errors (documented
  and checked: without `silent`, `$.key.deep` on a scalar raises an error) so
  it returns NULL like the other dialects. Dynamic key:
  `(doc -> NULLIF(key, ''))`
  for `json_get` and `->>` inside the `CASE` of `json_value`; `jsonb -> text`
  accepts any text expression and is always an exact key. Representation:
  `jsonb` sorts the keys when serializing (insertion order is not preserved)
  and adds spaces, so portable JSON equality is by value, not by text.
- BigQuery: `JSON_QUERY(doc, '<path>')`. Dynamic key: **not emitted**.
  `JSON_QUERY`/`JSON_VALUE` require the `json_path` to be a string literal (or
  a query parameter), so compilation fails loud with the reason instead of
  emitting SQL the engine rejects.
- Snowflake: `GET_PATH(doc, '<path without $>')` — its notation is JavaScript
  without a root (`'a.b'`, `'xs[0]'`, documented; `$` does not exist). A lone
  `$` has no translation (`GET_PATH` requires a member or index step) and
  fails loud.
  Dynamic key: `GET(doc, NULLIF(key, ''))` — `GET` is a **key lookup**,
  not a path, and for VARIANT `field_name` accepts a VARCHAR expression
  (the constant requirement is only for structured OBJECT); an empty key
  returns NULL by specification. Only BigQuery is left without a dynamic key.

Real execution and physical types checked on DuckDB, and real execution of
the JSON/arrays catalog against a local PostgreSQL 16 server (value parity
with DuckDB on the 22 covered expressions). BigQuery and Snowflake have
emission tests, not execution against real services. Identical textual
representation across engines is not guaranteed (`jsonb` reorders keys and
adds spaces; JSON numbers may serialize differently).
Sources must respect the declared schema: one-dimensional, homogeneous and
dense arrays. That physical guarantee is not yet validated outside DuckDB;
BigQuery additionally has its own restrictions when storing arrays with NULL
elements.

Pending: nested arrays or parameterized types; expansion of `json` columns
that contain an array (requires verifying `unnest`/array-elements per engine);
filters and recursive descent in `json_path` correlated with the result
shape of each warehouse (and, if applicable, a path syntax in the language
that includes quoted keys); dynamic key in BigQuery
(the engine requires a literal or query parameter) and runtime validation
that a dynamic key is not path syntax (the only thing DuckDB still
interprets as such). No stubs are added for these functions.
