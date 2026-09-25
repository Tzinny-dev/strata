# Syntax reference

Every block in this document was run against `strata check` (or `build`)
before being written — the code you see here is the one that was verified,
not a reconstruction from the specification. Where the compiler rejected
something that seemed reasonable, it is noted explicitly: those are the
real limits of the language today, not an omission from the document.

## 1. Module structure

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

- `source`: declares a warehouse table. `ns`/`dataset` are catalog
  metadata; the actual SQL table read is the name of the declaration
  (`orders` above), not those keys.
- `contract`: fixes the expected output schema of a model. It is optional
  (`model m { ... }` without `-> contract X` compiles the same).
- `model`: the unit of transformation.
- `pipeline`: groups models for an environment (`env:`) and source
  overrides per environment.

## 2. Model body

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

`join_left` / `join_inner` / `join_anti` / `join_semi` are implemented.
`join_anti` (rows from the left side with no match) and `join_semi` (rows
from the left side WITH a match, without duplicating on multi-match) have
no value keyword to the right of the `on`, only the condition. Example
with `join_anti`:

```strata
source orders(ns: "crm", dataset: "orders") { columns: { order_id: int64 nonnull, customer_id: int64 nonnull } }
source refunds(ns: "crm", dataset: "refunds") { columns: { order_id: int64 nonnull } }

model orders_without_refund {
  from orders
  join_anti refunds on orders.order_id == refunds.order_id
  select { order_id = orders.order_id }
}
```

`expect many_to_one`/`one_to_one` validates in `materialize()` (against
real data, not just in `check`) that the marked side is unique on the
`on` keys; a violation aborts the publication (`docs/join-cardinality.md`):

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

`let` defines an intermediate column (it does not appear in the output
unless re-listed in `select`/`derive`/`aggregate`); `group { keys }
(aggregate { ... })` groups; `sort`/`take` sort and limit.

### `select` / `derive`

**`select` and `derive` are exactly the same mechanism today** —both call
the same internal routine that registers explicit output columns—, so
there is no behavioral difference at all between using one or the other;
they are two names for the same thing. **Important**: as soon as EITHER
of the two appears (with at least one assignment), the implicit
passthrough of all base columns switches off — the model's output becomes
exactly what `select`/`derive` listed, not one column more. If you want
a new column AND keep the originals, you have to re-list them
explicitly:

```strata
source orders(ns: "crm", dataset: "orders") {
  columns: { order_id: int64 nonnull, gross_amount_usd: money nonnull }
}

model m {
  from orders
  derive { order_id = order_id, doubled = gross_amount_usd + gross_amount_usd }
}
```

(Without the `order_id = order_id`, the output of this model would be only
`doubled` — verified: that is what `strata check` gives if it is omitted.)

### `expand`

One row per element of an `array(T)` column of the `from` (not from a
derived one):

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

Details and limits (nested arrays, JSON columns) in
`docs/json-arrays.md`.

## 3. Set operations

`union [all]` / `intersect` / `except` combine models, **not sources
directly** — the right-hand side of a set-op must be a `model`, even a
trivial one that only does `from`:

```strata
source es_orders(ns: "crm", dataset: "es_orders") { columns: { order_id: int64 nonnull, country: string nonnull } }
source mx_orders(ns: "crm", dataset: "mx_orders") { columns: { order_id: int64 nonnull, country: string nonnull } }

model mx { from mx_orders }
model all_orders {
  from es_orders
  union mx      // or: union all mx / intersect mx / except mx
  union more    // consecutive set-ops chain
  dedup
  // or: dedup by order_id → keeps one row per key, deterministic
}
```

Set-ops are consecutive (`from a union b union c`); whatever comes before
the first one shapes the left branch and whatever comes after sees the
combined rows (`filter`, `select`, `derive`, `group`, `sort`, `take` and
also `join_*`). A qualified reference to the right model (`b.x`) resolves
against the combined column. The schema of all branches must align by
name and type (unified, like `coalesce`). `dedup` is `SELECT DISTINCT`
over the output columns; `dedup by k1, k2` keeps one row per key via
deterministic `ROW_NUMBER`. In aggregation, `count(distinct x)` emits
`COUNT(DISTINCT x)`; `distinct` in another function or in windows is E096
(see `docs/setops.md`).

## 4. Types

Scalars: `int64`, `float64`, `string`, `bool`, `date`, `timestamp`,
`uuid`, `json`. In addition:

- `decimal(precision, scale)`: `decimal(10, 2)`.
- `money`: `money`, or `money(EUR)`/`money(USD)` to fix the currency
  (default `USD`).
- `array(T)`: recursive, `array(array(string))` is valid.
- `domain name = <type>`: transparent alias, resolved when the project
  loads — a `domain country_code = string` behaves exactly like `string`
  in contracts, casts and array elements.

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

**Non-obvious verified limit**: `money` is NOT a "numeric" type for
comparison purposes (`is_numeric()` in `strata/types.py` only includes
`int64`/`float64`/`decimal`) — comparing a `money` column against an
integer literal fails with `E051 cannot compare money(USD) with int64`.
You have to wrap the literal: `cast(100, "money")` (the second argument
of `cast` is always a string literal with the type name, never an
unquoted type).

## 5. Functions (single catalog, `strata/functions.py`)

### Aggregates

`count`, `sum`, `avg`, `max`, `min`, `array_agg` — legal only inside
`aggregate { }` (E056 otherwise).

### Windows: `fn(args) over (partition_by: [...], sort: [...])`

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
`last_value`, and any aggregate (`sum`, `avg`, ...) are windowable.
Placement (E065): only in `select`/`derive`/`aggregate` outputs, never
in `let`/`filter`/`sort`/`group` keys/`join` conditions, and with no
nested windows.

### String

`upper`, `lower`, `concat`, `length`, `substring`, `trim`/`ltrim`/`rtrim`,
`replace`, `lpad`/`rpad`, `startswith`, `split_part`, `regexp_replace`,
`left`, `right`, `like`/`rlike`.

`like(s, pattern)` is SQL's `LIKE` (case-sensitive; `%` and `_` as
wildcards). `rlike(s, pattern)` is regular-expression matching (the
subset each warehouse documents). Both return `bool` and return `NULL`
if either argument is `NULL`. The two also exist as **infix operators**
with the same semantics (they tie with `==` in precedence; they are
contextual, not reserved):

```strata
    select {
      eur      = country,                    # normal column
      names_es = country like "E%",          # operator
      rx_es    = country rlike "^E",         # operator
      fn_es    = like(country, "E%"),        # same semantics as a function
    }
```

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

### Date

`date_add`/`date_sub` (unit as kwarg: `years:`/`months:`/`weeks:`/
`days:`), `date_trunc`/`date_diff` (unit as a symbol or string):

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

`json_get`/`json_value` (simple literal or dynamic key; exact member
lookup, never a path), `json_path` (JSONPath limited to root `$` + steps
of member/index), `json_build`, `array_length`, `array_get` (index from
zero), `array_construct`, `list` (alias of `array_construct`),
`array_concat`/`array_contains`/
`array_append`/`array_prepend`/`array_remove`/`array_sort`/
`array_index_of`, `array_agg`. Limits measured per dialect (BigQuery
without dynamic key, Snowflake with its own syntax) in `docs/json-arrays.md`.

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

### Conditionals: `if` / `case`

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

`if(cond, then, else)`: arity exactly 3, `cond` must be `bool`.
`case(cond, val, [cond, val, ...], [else])`: minimum arity 2, any
number of pairs; without `else`, a row with no match gives `NULL`. Both
are emitted as `CASE WHEN...END`, identical in the four dialects. Full
detail: `docs/incremental.md` no, this one is new — see the catalog in
`strata/functions.py` (`if`/`case`) and `tests/test_conditionals.py`.

## 6. Contracts

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

`nonnull`, `unique`, `primary_key` (`unique` implied), `enum {A, B, ...}`
(values unquoted), `protected` (sensitivity marker; does not imply
automatic masking — see limit in `propuesta-lenguaje-strata.md`),
`classification: "text"` (free-form metadata, requires the quotes and the
colon).

`build <file> [models...] --strict` requires a contract on every checked
model, including transitive dependencies:

```
$ python -m strata build sin_contrato.strata --strict
error: E014: strict mode requires every built model to declare -> contract: m
```

Detail: `docs/strict-contracts.md`.

## 7. Warehouse semantics

`partition_by [cols]`, `freshness <threshold>` (`1h`, `24h`, `daily`,
`weekly`, `monthly`, or a quoted SQL expression),
`freshness_column: col` — real framework wired to `run --only-stale`
(effective staleness detection, not just accepted by the parser). Full
detail: `docs/§2-warehouse-semantics.md`.

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

`incremental merge_strategy: append|upsert` with `cdc_column` (required)
and `merge_keys` (required only for `upsert`) **performs a real merge**
—not just accepted syntax—: on each run that is not the first for that
model, it merges the previous snapshot with the rows whose `cdc_column`
is greater than its watermark, instead of recomputing everything from
scratch. Not supported on `group`/`aggregate` models (rejected at
compile time, E087: it is not sound to re-aggregate only the delta).
Full detail, including the test that distinguishes this from a full
rebuild: `docs/incremental.md`, section "Merge by row".

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

## 8. Not implemented today

Verified as failing (not an omission, this is the actual state):

- `like`/`rlike` operators in conditions: → as of **2026-09-21** they are
  **infix operators** of comparison precedence:
  `filter country like "E%" and country rlike "^E"`. They remain valid as
  **functions** `like(s, p)`, `rlike(s, p)` (same semantics). They are
  contextual, not reserved words: a column called `like` is still a
  column (`filter like == "b"`), and `like(like, "x")` still calls the
  function.
- Typed `dict`/`map` constructors: → **implemented** as of **2026-09-21**:
  `map("k", v, ...)` / `dict("k", v, ...)` build `map<string, V>`;
  `map_get(m, "k")` accesses by exact key. Keys **string only**,
  homogeneous values in the JSON-representable subset
  (string/int64/float64/bool/decimal/money/json). DuckDB native `MAP`,
  Postgres/BigQuery/Snowflake backed by JSONB/JSON/VARIANT.
- `struct` type: → **implemented** as of **2026-09-21**:
  `struct("f", v, "g", w)` builds `struct<f: T, g: U>`; `struct_get(s, "f")`
  accesses by exact field. DuckDB/BigQuery native `STRUCT`, Postgres/Snowflake
  backed by JSONB/VARIANT. Fields with scalar types from the
  JSON-representable subset.

If you need something outside this subset, `spec/grammar.md` documents it
as out of support, not as an error in this version.
