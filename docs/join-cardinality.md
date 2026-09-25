# Join cardinality

A join can be annotated with the cardinality it promises (`expect many_to_one`
or `expect one_to_one`). The shape is validated at compile time (E079) and the
data verifies it when materializing: count groups of duplicate keys in the
upstream tables, and abort like a pin if any appear.

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

## Semantics

- `many_to_one`: each left row matches at most one right row. It is verified
  by checking that the right keys are unique; the duplicates on the left are
  "the manys" and are allowed.
- `one_to_one`: additionally, the left keys are unique.
- Only `join_left` and `join_inner`: `anti`/`semi` never multiply rows and
  the annotation there is E079. The expectation limits the *multiplicity* of
  the matches, not the preservation (that is done by the join type: an
  `inner` can still drop rows).
- With no annotation there is no check: existing joins compile and run
  exactly as before.

## Which keys count

The `on` condition must be an AND of `==` between flat base columns (or a
column and a literal). Conjunctions that only filter left rows are safely
ignored (they remove matches, they don't create them); any reference to the
right table outside of a clean equi-key is E079, as are non-equational
conditions (`>`, `or`, calls over right columns), same-side comparisons, and
missing keys. Keys can be composite (`on a.x == b.x and a.y == b.y` groups
by both).

Keys from a left-side expression (`upper(email)`, a `let`) count for
`many_to_one` — the uniqueness of the right still bounds the matches — but
`one_to_one` requires at least one pair relating a flat left column to a flat
right one. What cannot be proven by counting keys gets rewritten upstream,
out loud.

## Runtime verification

When materializing, for each annotated join a query runs against the same
tables the model sees (staged/live views depending on the run, with
`source_overrides` applied):

```sql
SELECT COUNT(*) FROM (
  SELECT 1 FROM <table> WHERE <k> IS NOT NULL [AND ...]
  GROUP BY <keys> HAVING COUNT(*) > 1
) t
```

Zero means pass; anything else aborts with `PinError` (`join cardinality
FAILED [model join table]: expected many_to_one but ... duplicate key
groups (...)`) and leaves the last good data alive, like pins. All-NULL keys
are excluded: in an equi-join they never match and cannot explode rows. The
success is reported in the pins report (`ok m left customers:
many_to_one (...)`).

The check runs in `materialize` (covering `run`, `test` and `replay`
if they re-execute); `check`/`build` only validate the shape (E079). Real
execution only in DuckDB, like the rest of `exec`.

## Dialects and limits

- The syntax adds no keywords: `expect` already existed and
  `many_to_one`/`one_to_one` are contextual identifiers (a typo is
  `ParseError`). The GBNF grammar accepts them and the sampler stays green.
- There is no `many_to_many`/`one_to_many`: they would describe fanout, not
  prevent it.
- A declared `unique`/`primary_key` does not exempt the test: the flags are
  declarations and the data can violate them (that is why pins re-check
  `unique` at runtime); the annotation always runs its count.
- Cost: one aggregation per checked side and per materialization over the
  full upstream table.

Errors: E079 shape/placement (anti/semi, non-equational condition, exotic
right references, no keys, `one_to_one` without a left-right pair),
`ParseError` on the cardinality word, `PinError` on the data violation.
Pending: exempt the test when uniqueness is already pinned in the same run,
and bounded sampling for huge dimensions.
