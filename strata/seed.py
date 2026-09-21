"""Demo fixtures: tables referenced by the example pipelines.

The prototype resolves `source` declarations to DuckDB tables named after the
declaration (the ns/dataset keys are metadata for the catalog/planning phase).
"""


def seed_sql() -> tuple:
    """DDL statements for the demo seed tables."""
    ddl = """
CREATE OR REPLACE TABLE orders (
  order_id BIGINT,
  customer_id BIGINT,
  country VARCHAR,
  gross_amount_usd DECIMAL(38,2),
  is_test BOOLEAN,
  order_day DATE
);
INSERT INTO orders VALUES
  (1, 1001, 'ES', 120.00, FALSE, DATE '2026-09-01'),
  (2, 1001, 'ES',  90.00, FALSE, DATE '2026-09-01'),
  (3, 1002, 'MX', 200.00, FALSE, DATE '2026-09-02'),
  (4, 1003, 'BR',  75.50, FALSE, DATE '2026-09-02'),
  (5, 1004, 'CO',  40.00, TRUE,  DATE '2026-09-03');

CREATE OR REPLACE TABLE refunds (
  order_id BIGINT,
  discount_usd DECIMAL(38,2),
  refunded_at TIMESTAMP
);
INSERT INTO refunds VALUES
  (2, 10.00, TIMESTAMP '2026-09-02 10:00:00'),
  (4,  5.50, TIMESTAMP '2026-09-03 09:30:00');
"""
    return (ddl,)