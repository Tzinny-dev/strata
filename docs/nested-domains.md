# Nested types and domain aliases

Arrays nest to any depth over scalars, `decimal`/`money`
and other arrays, and `domain` declares a transparent alias usable wherever a
type is written. Compatibility remains structural and invariant
(`array(T)` only matches an exact `array(T)`, as `array_concat` already required).

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

## Nested and parameterized arrays

- Declaration: `array(array(int64))`, `array(decimal(10, 2))`,
  `array(money(USD))`, to any depth. The parser accepts it recursively and
  the checker resolves it the same way (`type_from_spec`).
- Homogeneous construction: `array_construct` accepts elements of any
  supported type (including arrays, which must be identical to each other);
  at least one element with a known type, as before. Each element is emitted
  with a `CAST` to its type (`BIGINT[][]` in DuckDB/Postgres,
  `ARRAY<ARRAY<INT64>>` in BigQuery, `ARRAY` in Snowflake).
- Access and measurement: `array_get(xss, 0)` returns the element (including an
  array) and `array_length` counts positions in any array.
- Concatenation: `array_concat` requires identical types, nested ones included.
- `union`/`dedup`/`group` and contracts work on nested columns
  without changes (unification only matches equal types; `DISTINCT` verified
  in DuckDB on nested arrays).

What keeps being rejected, loudly and on purpose:

- `array_contains`, `array_sort`, `array_append`/`prepend`/`remove`/`index_of`
  on non-simple-scalar elements (E063): equality and ordering of
  composite elements, and the rectangular multi-dimensional arrays that
  Postgres requires, are not portable without per-engine verification.
- `array_agg` of an array (E063): it would aggregate ragged arrays that Postgres
  cannot represent.
- `expand` of a nested array (E075): expansion declares columns of
  scalar elements.
- `array(array)` or `array(decimal)` without parameters are not types: a parse
  error, not a checking error.

## Domains

```strata
domain user_id = int64
domain ids = array(user_id)
```

- `domain <name> = <type>` at the top level; the type can be anything
  (including another domain). Used in `source`, `contract`, `cast` and array
  elements. Transparent: contract compatibility, the `reads`/lineage
  and the physical types follow the underlying type (`user_id` is `int64` for
  all purposes from checking onward).
- Domains are resolved when the project loads, failing early: a cycle
  (`a = b`, `b = a`, or self-reference) and an undeclared name are E078,
  even if the alias is never used. A `source` with no models reading it is
  not checked (lazy, as before), so its E078 shows up when it is used.
- `fmt` round-trips (`domain user_id = int64`) and the fingerprint is
  stable. The GBNF grammar accepts the declaration and the references (an
  identifier in type position); the deterministic sampler stays
  green because everything it generates parses.

## Dialects and limits

- Real execution only in DuckDB (literals `[[1,2],[3]]`, one-level
  `UNNEST`, `DISTINCT`/`UNION` on nested values, `BIGINT[][]` and
  `DECIMAL(10,2)[]` casts, all verified); the other three engines verified by
  emission pattern.
- The physical types of pins (`exec`) reuse the recursive
  mapping (`BIGINT[][]`, `DECIMAL(10,2)[]`); `money` is `DECIMAL(38,2)` also
  when nested.
- `decimal`/`money` as array elements can be declared, measured,
  accessed and concatenated, but element-wise operations
  reject them just like nested ones (E063): they are left for a delivery with
  per-engine verification.
- `fn` signature types (`List<...>`) remain opaque and do not
  resolve domains; nor is there predicate validation (that is
  constraints/masking territory, not aliases).

Errors: E063 unsupported array elements, E078 unknown or
cyclic domain, plus the existing ones of each function. Pending: `struct`/`map`
(only mentioned in the proposal, no design), element-wise
operations over `decimal`/`money`/nested values with per-engine verification,
nested `expand`/`array_agg` and predicate validation on domains.
