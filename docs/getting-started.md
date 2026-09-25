# Getting started

All the code in this guide was actually run against the CLI (`strata
check`/`build`/`run`) before being written here; the numbers that appear
are the real output, not an invented example.

## Installation

The prototype lives in `prototype/` and uses its own `.venv` with `duckdb`
installed:

```
cd prototype
python3 -m venv .venv
.venv/bin/pip install duckdb
```

From here on, `strata` is `python -m strata` with that interpreter.

## A minimal pipeline

Strata declares **sources** (tables that already exist in the warehouse),
**models** (transformations) and, optionally, a **contract** that pins
the output schema of a model. Save this as `hello.strata`:

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
  select {
    order_id   = order_id,
    country    = upper(country),
    net_amount = gross_amount_usd,
  }
}
```

`ns`/`dataset` are catalog metadata; the actual SQL table that gets read is
the name of the declaration (`orders`), no matter what those keys say
(`strata/seed.py`, which loads this guide's data, documents it that way
explicitly).

### `strata check`: typecheck + contract, without touching any warehouse

```
$ python -m strata check hello.strata
  ok  paid_orders
    contract  order_id: int64 nonnull, country: string nonnull, net_amount: money(USD) nonnull
    pins      paid_orders.order_id:nonnull, paid_orders.country:nonnull, paid_orders.net_amount:nonnull
  check OK: 1 model(s) green, dialect duckdb, nothing materialized
```

### `strata build`: types, fingerprint and lineage

```
$ python -m strata build hello.strata
model paid_orders -> contract PaidOrders
  fingerprint  e7f6a391388296fc
  outputs      order_id: int64 nonnull, country: string nonnull, net_amount: money(USD) nonnull
  lineage order_id <- orders.order_id [passthrough]
  lineage country <- orders.country [derived]
  lineage net_amount <- orders.gross_amount_usd [passthrough]
  reads        [('orders', 'country'), ('orders', 'gross_amount_usd'), ('orders', 'is_test'), ('orders', 'order_id')]
```

### `strata run --seed`: actually run in DuckDB

`--seed` loads demo data (`strata/seed.py`: an `orders` table with 5 rows,
one of them marked `is_test: true`) into a new warehouse:

```
$ python -m strata run hello.strata --seed -o hello.duckdb
  materialized  v_paid_orders  (4 rows)
  ok  paid_orders.order_id: BIGINT (schema)
  ok  paid_orders.order_id: nonnull
  ok  paid_orders.country: VARCHAR (schema)
  ok  paid_orders.country: nonnull
  ok  paid_orders.net_amount: DECIMAL(38,2) (schema)
  ok  paid_orders.net_amount: nonnull

  preview paid_orders (v_paid_orders)
    order_id, country, net_amount
    1, ES, 120.00
    2, ES, 90.00
    3, MX, 200.00
```

Note the 4 rows, not 5: the `filter` discards the `is_test: true` row.
The result stays in `hello.duckdb` as a `v_paid_orders` view; a new
connection to that file confirms it:

```python
import duckdb
con = duckdb.connect("hello.duckdb")
con.execute("SELECT * FROM v_paid_orders ORDER BY order_id").fetchall()
# [(1, 'ES', Decimal('120.00')), (2, 'ES', Decimal('90.00')),
#  (3, 'MX', Decimal('200.00')), (4, 'BR', Decimal('75.50'))]
```

"Nothing gets published until the pin passes": if a row didn't satisfy
`nonnull` or a column's physical type didn't match the contract,
`run` would fail loud (`PinError`) and `v_paid_orders` would be untouched — see
`docs/strict-contracts.md`.

## Next steps

- `docs/syntax-reference.md`: each language construct implemented
  today, with its own verified example.
- `docs/tutorial.md`: a more complete pipeline built step by step
  (join, aggregation, conditionals, windows, declarative tests).
- `spec/grammar.md`: the formal grammar.
- remaining `docs/*.md`: deep dives into specific features (JSON/arrays,
  incremental, join cardinality, nested types, warehouse
  semantics, adapters).
