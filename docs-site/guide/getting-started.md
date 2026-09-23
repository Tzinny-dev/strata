# Getting started

Todo el código de esta guía se corrió de verdad contra el CLI (`strata
check`/`build`/`run`) antes de escribirse aquí; los números que aparecen
son la salida real, no un ejemplo inventado.

## Instalación

El prototipo vive en `prototype/` y usa un `.venv` propio con `duckdb`
instalado:

```
cd prototype
python3 -m venv .venv
.venv/bin/pip install duckdb
```

A partir de aquí, `strata` es `python -m strata` con ese intérprete.

## Un pipeline mínimo

Strata declara **fuentes** (tablas que ya existen en el warehouse),
**modelos** (transformaciones) y, opcionalmente, un **contrato** que fija
el esquema de salida de un modelo. Guarda esto como `hello.strata`:

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

`ns`/`dataset` son metadatos de catálogo; la tabla SQL real que se lee es
el nombre de la declaración (`orders`), sin importar lo que digan esas
claves (`strata/seed.py`, que carga los datos de esta guía, lo documenta
así explícitamente).

### `strata check`: typecheck + contrato, sin tocar ningún warehouse

```
$ python -m strata check hello.strata
  ok  paid_orders
    contract  order_id: int64 nonnull, country: string nonnull, net_amount: money(USD) nonnull
    pins      paid_orders.order_id:nonnull, paid_orders.country:nonnull, paid_orders.net_amount:nonnull
  check OK: 1 model(s) green, dialect duckdb, nothing materialized
```

### `strata build`: tipos, fingerprint y lineage

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

### `strata run --seed`: ejecutar de verdad en DuckDB

`--seed` carga datos de demostración (`strata/seed.py`: una tabla `orders`
con 5 filas, una de ellas marcada `is_test: true`) en un warehouse nuevo:

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

Nota las 4 filas, no 5: la fila `is_test: true` la descarta el `filter`.
El resultado queda en `hello.duckdb` como una vista `v_paid_orders`; una
conexión nueva a ese archivo lo confirma:

```python
import duckdb
con = duckdb.connect("hello.duckdb")
con.execute("SELECT * FROM v_paid_orders ORDER BY order_id").fetchall()
# [(1, 'ES', Decimal('120.00')), (2, 'ES', Decimal('90.00')),
#  (3, 'MX', Decimal('200.00')), (4, 'BR', Decimal('75.50'))]
```

"Nada se publica hasta que el pin pasa": si una fila no cumpliera
`nonnull` o el tipo físico de una columna no coincidiera con el contrato,
`run` fallaría fuerte (`PinError`) y `v_paid_orders` no se tocaría — ver
`docs/strict-contracts.md`.

## Siguientes pasos

- `docs/syntax-reference.md`: cada construcción del lenguaje implementada
  hoy, con su propio ejemplo verificado.
- `docs/tutorial.md`: un pipeline más completo construido paso a paso
  (join, agregación, condicionales, ventanas, tests declarativos).
- `spec/grammar.md`: la gramática formal.
- `docs/*.md` restantes: profundizan en features específicas (JSON/arrays,
  incremental, cardinalidad de joins, tipos anidados, semántica de
  warehouse, adaptadores).
