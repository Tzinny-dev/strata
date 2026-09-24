# -*- coding: utf-8 -*-
"""`strata import-dbt` — Fase 2 §11 adoption: deterministic adoption artifact
that must pass `build`, else fail-loud §4. Gate: a dbt model with `select *`
and no columns contract cannot be imported (E041 fail-loud) — the untyped
consumer would never be caught by the warehouse (spec §11)."""
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from strata import cli

FULL_SCHEMA = """\
version: 2

sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        description: "raw CRM orders"
        columns:
          - name: order_id
            data_type: bigint
            tests: [not_null]
          - name: customer_id
            data_type: bigint
            tests: [not_null]
          - name: gross_amount_usd
            data_type: numeric
          - name: order_day
            data_type: date
            tests: [not_null]
      - name: refunds
        description: "raw CRM refunds"
        columns:
          - name: order_id
            data_type: bigint
          - name: discount_usd
            data_type: numeric

models:
  - name: daily_orders
    description: "verified per-day revenue"
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
        tests: [not_null]
      - name: customer_id
        data_type: bigint
        tests: [not_null]
      - name: gross_amount_usd
        data_type: numeric
      - name: order_day
        data_type: date
        tests: [not_null]
"""

NO_CONTRACT_MODEL = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
models:
  - name: unverified_legacy
    description: "select * without columns"
"""


def _run_import(tmp: Path, schema: str, output: str = "imported.strata"):
    (tmp / "schema.yml").write_text(schema)
    err = io.StringIO()
    with redirect_stderr(err):
        code = cli.main(["import-dbt", str(tmp / "schema.yml"), "--output", str(tmp / output)])
    return code, err.getvalue()


class TestImportDbt(unittest.TestCase):
    def test_fail_loud_e041_when_model_has_no_columns(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_import(Path(d), NO_CONTRACT_MODEL)
            self.assertEqual(code, 1, "select * without a contract must fail-loud")
            self.assertIn("E041", err)
            self.assertIn("unverified_legacy", err)
            self.assertFalse((Path(d) / "imported.strata").exists(),
                             "no artifact may be emitted on a broken contract")

    def test_import_emits_artifact_that_builds_green(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_import(Path(d), FULL_SCHEMA)
            self.assertEqual(code, 0, f"import of a full contract must succeed: {err}")
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("source orders(ns: \"crm\", dataset: \"prod\")", art)
            self.assertIn("model daily_orders -> contract daily_ordersContract", art)
            tmp = Path(d) / "built.strata"
            tmp.write_text(art)
            herr = io.StringIO()
            with redirect_stderr(herr):
                bcode = cli.main(["build", str(tmp)])
            self.assertEqual(bcode, 0, f"imported artifact must pass build: {herr.getvalue()}")

    def test_import_is_deterministic_byte_identical(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            c1, _ = _run_import(Path(d), FULL_SCHEMA)
            c2, _ = _run_import(Path(d), FULL_SCHEMA, output="again.strata")
            self.assertEqual(c1, 0)
            self.assertEqual(c2, 0)
            self.assertEqual((Path(d) / "imported.strata").read_text(),
                             (Path(d) / "again.strata").read_text(),
                             "same schema.yml must produce byte-identical strata")

    def test_known_column_types_mapped_to_strata(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_import(Path(d), FULL_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("order_day: date nonnull,", art)
            self.assertIn("gross_amount_usd : money,", art)
            self.assertIn("gross_amount_usd = gross_amount_usd,", art)


PROJECT_SCHEMA = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
            tests: [not_null]
          - name: country
            data_type: string
            tests: [not_null]
          - name: gross
            data_type: numeric
          - name: order_day
            data_type: date
            tests: [not_null]
models:
  - name: int_clean
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
        tests: [not_null]
      - name: country
        data_type: string
        tests: [not_null]
      - name: gross
        data_type: numeric
      - name: order_day
        data_type: date
        tests: [not_null]
  - name: daily
    depends_on: [int_clean]
    columns:
      - name: country
        data_type: string
        tests: [not_null]
      - name: order_day
        data_type: date
        tests: [not_null]
      - name: total
        data_type: numeric
      - name: n
        data_type: bigint
"""

INT_CLEAN_SQL = """\
{{
  config(materialized='table')
}}
SELECT
  order_id,
  country,
  gross,
  order_day
FROM {{ source('crm', 'orders') }}
WHERE order_id > 0
"""

DAILY_SQL = """\
SELECT
  country,
  order_day,
  SUM(gross) AS total,
  COUNT(*)    AS n
FROM {{ ref('int_clean') }}
WHERE country IS NOT NULL
  AND order_day IS NOT NULL
GROUP BY country, order_day
"""


def _run_project_import(tmp: Path, sql_map, schema=PROJECT_SCHEMA,
                        output="imported.strata"):
    """sql_map: name -> .sql text; a None value omits that model's .sql file."""
    (tmp / "schema.yml").write_text(schema)
    models = tmp / "models"
    models.mkdir(exist_ok=True)
    defaults = {"int_clean": INT_CLEAN_SQL, "daily": DAILY_SQL}
    for model_dir in ("int_clean", "daily"):
        text = sql_map.get(model_dir) if model_dir in sql_map else defaults[model_dir]
        if text is not None:
            (models / f"{model_dir}.sql").write_text(text)
    err = io.StringIO()
    with redirect_stderr(err):
        code = cli.main(["import-dbt", str(tmp / "schema.yml"),
                         "--models", str(models), "--output", str(tmp / output)])
    return code, err.getvalue()


CTE_SCHEMA = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
            tests: [not_null]
          - name: country
            data_type: string
          - name: gross
            data_type: numeric
          - name: order_day
            data_type: date
            tests: [not_null]
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: order_day
        data_type: date
        tests: [not_null]
      - name: total
        data_type: numeric
"""


class TestImportDbtTransform(unittest.TestCase):
    def test_transform_imports_and_builds_green(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {})
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("filter order_id > 0", art)
            self.assertIn("filter not (country == null) and not (order_day == null)", art)
            self.assertIn("group { country, order_day } (", art)
            self.assertIn("aggregate { total = sum(gross), n = count(*) }", art)
            tmp = Path(d) / "built.strata"
            tmp.write_text(art)
            herr = io.StringIO()
            with redirect_stderr(herr):
                bcode = cli.main(["build", str(tmp)])
            self.assertEqual(bcode, 0, f"translated artifact must pass build: {herr.getvalue()}")

    def test_transform_is_deterministic_byte_identical(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            c1, _ = _run_project_import(Path(d), {}, output="a.strata")
            c2, _ = _run_project_import(Path(d), {}, output="b.strata")
            self.assertEqual((c1, c2), (0, 0))
            self.assertEqual((Path(d) / "a.strata").read_text(),
                             (Path(d) / "b.strata").read_text())

    def test_transform_join_succeeds(self):
        # Use a schema where the model's contract matches the SELECT
        schema = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
          - name: country
            data_type: string
          - name: gross
            data_type: numeric
      - name: refunds
        columns:
          - name: order_id
            data_type: bigint
          - name: gross
            data_type: numeric
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: country
        data_type: string
      - name: total
        data_type: numeric
"""
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "int_clean": None,
                "daily": "SELECT a.country, SUM(b.gross) AS total\n"
                         "FROM orders a JOIN refunds b ON a.order_id = b.order_id\n"
                         "GROUP BY a.country"}, schema=schema)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("join_inner", art)
            self.assertIn("orders.order_id == refunds.order_id", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0, err)

    def test_transform_case_succeeds(self):
        schema = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: country
            data_type: string
          - name: order_id
            data_type: bigint
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: region
        data_type: string
      - name: order_id
        data_type: bigint
"""
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "int_clean": None,
                "daily": "SELECT CASE WHEN country = 'ES' THEN 'Spain' WHEN country = 'MX' THEN 'Mexico' ELSE 'Other' END AS region, order_id FROM orders"}, schema=schema)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn('case(country == "ES"', art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0, err)

    def test_transform_join_with_case_succeeds(self):
        schema = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
          - name: country
            data_type: string
          - name: gross
            data_type: numeric
      - name: refunds
        columns:
          - name: order_id
            data_type: bigint
          - name: gross
            data_type: numeric
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: region
        data_type: string
      - name: total
        data_type: numeric
"""
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "int_clean": None,
                "daily": "SELECT CASE WHEN a.country = 'ES' THEN 'EU' ELSE 'Other' END AS region, SUM(b.gross) AS total\n"
                         "FROM orders a LEFT JOIN refunds b ON a.order_id = b.order_id\n"
                         "GROUP BY region"}, schema=schema)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("join_left", art)
            self.assertIn("case(", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0, err)

    def test_transform_fail_loud_join_using_still_fails(self):
        schema = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
          - name: country
            data_type: string
          - name: gross
            data_type: numeric
      - name: refunds
        columns:
          - name: order_id
            data_type: bigint
          - name: gross
            data_type: numeric
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: country
        data_type: string
      - name: total
        data_type: numeric
"""
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "int_clean": None,
                "daily": "SELECT a.country, SUM(b.gross) AS g\n"
                         "FROM orders a JOIN refunds b USING (order_id)\n"
                         "GROUP BY a.country"}, schema=schema)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            # USING is out of subset — error mentions USING or ON
            self.assertTrue("USING" in err or "ON" in err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_transform_fail_loud_select_star(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": "SELECT * FROM orders"})
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("SELECT *", err)

    def test_transform_fail_loud_macro(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": '{{ dbt_utils.date_spine(datepart="day") }}'})
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("macro", err)

    def test_transform_fail_loud_order_by(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": "SELECT country FROM orders ORDER BY country"})
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("ORDER", err)

    def test_transform_fail_loud_unknown_ref(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": "SELECT order_id FROM {{ ref('no_such_model') }}"})
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("no_such_model", err)

    def test_transform_fail_loud_aggregate_without_alias(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": "SELECT country, SUM(gross) FROM orders GROUP BY country"})
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("alias", err)

    def test_transform_fail_loud_sql_without_schema_contract(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            (Path(d) / "schema.yml").write_text(PROJECT_SCHEMA)
            models = Path(d) / "models"
            models.mkdir()
            (models / "int_clean.sql").write_text(INT_CLEAN_SQL)
            (models / "daily.sql").write_text(DAILY_SQL)
            (models / "lonely.sql").write_text("SELECT country FROM orders")
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["import-dbt", str(Path(d) / "schema.yml"),
                                 "--models", str(models),
                                 "--output", str(Path(d) / "imported.strata")])
            self.assertEqual(code, 1)
            self.assertIn("E042", err.getvalue())
            self.assertIn("lonely", err.getvalue())
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_transform_model_without_sql_needs_depends_on(self):
        schema = PROJECT_SCHEMA.replace("    depends_on: [int_clean]\n", "")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"daily": None},
                                            schema=schema)
            self.assertEqual(code, 1)
            self.assertIn("E041", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_transform_with_cte_succeeds(self):
        # Chained CTEs lower to contract-less helper models daily__es/daily__by_day
        # that the main query reads from; the artifact must pass build.
        cte_sql = ("WITH es AS (\n"
                   "  SELECT order_id, gross, order_day\n"
                   "  FROM {{ source('crm', 'orders') }}\n"
                   "  WHERE country = 'ES'\n"
                   "),\n"
                   "by_day AS (\n"
                   "  SELECT order_day, SUM(gross) AS total\n"
                   "  FROM es\n"
                   "  GROUP BY order_day\n"
                   ")\n"
                   "SELECT order_day, total FROM by_day")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": cte_sql},
                                            schema=CTE_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("model daily__es {", art)
            self.assertIn("from orders", art)
            self.assertIn("filter country == \"ES\"", art)
            self.assertIn("model daily__by_day {", art)
            self.assertIn("from daily__es", art)
            self.assertIn("aggregate { total = sum(gross) }", art)
            self.assertIn("from daily__by_day", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0, err)

    def test_transform_with_cte_is_deterministic_byte_identical(self):
        cte_sql = ("WITH es AS (SELECT gross, order_day FROM orders WHERE country = 'ES'),\n"
                   "by_day AS (SELECT order_day, SUM(gross) AS total FROM es GROUP BY order_day)\n"
                   "SELECT order_day, total FROM by_day")
        with __import__("tempfile").TemporaryDirectory() as d:
            c1, _ = _run_project_import(Path(d), {"int_clean": None, "daily": cte_sql},
                                        schema=CTE_SCHEMA, output="a.strata")
            c2, _ = _run_project_import(Path(d), {"int_clean": None, "daily": cte_sql},
                                        schema=CTE_SCHEMA, output="b.strata")
            self.assertEqual((c1, c2), (0, 0))
            self.assertEqual((Path(d) / "a.strata").read_text(),
                             (Path(d) / "b.strata").read_text())

    def test_transform_fail_loud_with_recursive(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": "WITH RECURSIVE t AS (SELECT order_day FROM orders) SELECT order_day FROM t"},
                schema=CTE_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("RECURSIVE", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_transform_fail_loud_with_column_list(self):
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": "WITH t(a, b) AS (SELECT order_day, gross FROM orders) SELECT a FROM t"},
                schema=CTE_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("column", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_transform_fail_loud_with_forward_reference(self):
        # A CTE may only read sources/models/earlier CTEs; a forward ref (b defined
        # after a reads it) would silently change semantics — fail-loud E042.
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {
                "daily": "WITH a AS (SELECT order_day FROM b), "
                         "b AS (SELECT order_day FROM orders) SELECT order_day FROM a"},
                schema=CTE_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertFalse((Path(d) / "imported.strata").exists())


ICEBERG_SCHEMA = """\
version: 2
sources:
  - name: crm
    schema: prod
    tables:
      - name: orders
        columns:
          - name: order_id
            data_type: bigint
            tests: [not_null]
          - name: country
            data_type: string
          - name: gross
            data_type: numeric
models:
  - name: daily
    description: "dbt-iceberg incremental/overwrite model"
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
        tests: [not_null]
      - name: country
        data_type: string
      - name: total
        data_type: numeric
"""


class TestImportDbtIceberg(unittest.TestCase):
    """L4: dbt-iceberg SQL constructs translate to plain Strata (`propuesta-iceberg`
    §4.2 philosophy: FULL_RECOMPUTE; the DML is the model, not a mutation).

    `INSERT OVERWRITE INTO <t> <select>` and `MERGE INTO <t> USING (<select>) ON
    <equi-keys> WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...`
    lower to the select plus a `dedup by <keys>` line (the USING-select is the
    deterministic form of the upsert). The materialization/partition/ttl config
    kwargs become `// dbt-iceberg ...` annotation notes only. Anything else in the
    DML mutation set (DELETE, non-parenthesized USING, missing WHEN arms, non-equi
    JOIN keys) fails loud E042 — no silent semantic drift.

    Regression the class guards: the dispatcher must run BEFORE jinja stripping
    (`{{ this }}` is a legal DML target but never legal inside a SELECT body), and
    the artifact must build green (the `dedup by` line is real Strata, not a hint).
    """

    def test_insert_overwrite_succeeds_and_builds(self):
        sql = ("{{ config(materialized='table') }}\n"
               "INSERT OVERWRITE INTO {{ this }}\n"
               "SELECT order_id, country, SUM(gross) AS total\n"
               "FROM orders\n"
               "GROUP BY order_id, country")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("from orders", art)
            self.assertIn("aggregate { total = sum(gross) }", art)
            self.assertIn("dbt-iceberg INSERT OVERWRITE INTO daily", art)
            self.assertNotIn("dedup by", art)
            self.assertNotIn("{{ this }}", art)
            self.assertNotIn("INSERT OVERWRITE INTO {{ this }}", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0, "the lower-to-select artifact must build")

    def test_merge_succeeds_with_dedup_and_annotations(self):
        sql = ("{{\n"
               "  config(materialized='incremental', unique_key='order_id',\n"
               "         partition_by=['country'], ttl_days=30)\n"
               "}}\n"
               "MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country, SUM(gross) AS total\n"
               "       FROM orders\n"
               "       GROUP BY order_id, country) AS s\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET\n"
               "  country = s.country, total = s.total\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, country, total)\n"
               "  VALUES (s.order_id, s.country, s.total)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("dedup by order_id", art)
            self.assertIn("aggregate { total = sum(gross) }", art)
            self.assertIn("dbt-iceberg materialized='incremental'", art)
            self.assertIn("dbt-iceberg unique_key='order_id'", art)
            self.assertIn("dbt-iceberg partition_by=['country']", art)
            self.assertIn("dbt-iceberg ttl_days=30", art)
            self.assertNotIn("{{ this }}", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0, "dedup by order_id must be real Strata")

    def test_merge_composite_key_one_line_deduplicates(self):
        sql = ("MERGE INTO {{ this }} "
               "USING (SELECT order_id, country, gross FROM orders) AS s "
               "ON t.order_id = s.order_id AND t.country = s.country "
               "WHEN MATCHED THEN UPDATE SET country = s.country "
               "WHEN NOT MATCHED THEN INSERT (order_id, country, gross) "
               "VALUES (s.order_id, s.country, s.gross)")
        # one-line form; the annotation + the fact the dispatcher still wins.
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("dedup by order_id, country", art)

    def test_merge_multi_key_extracts_all_dedup_keys(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country, SUM(gross) AS total\n"
               "       FROM orders GROUP BY order_id, country) AS s\n"
               "ON t.order_id = s.order_id AND t.country = s.country\n"
               "WHEN MATCHED THEN UPDATE SET total = s.total\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, country, total)\n"
               "  VALUES (s.order_id, s.country, s.total)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("dedup by order_id, country", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0)

    def test_merge_is_deterministic_byte_identical(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country, SUM(gross) AS total\n"
               "       FROM orders GROUP BY order_id, country) AS s\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET total = s.total\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, country, total)\n"
               "  VALUES (s.order_id, s.country, s.total)")
        with __import__("tempfile").TemporaryDirectory() as d:
            c1, _ = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                        schema=ICEBERG_SCHEMA, output="a.strata")
            c2, _ = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                        schema=ICEBERG_SCHEMA, output="b.strata")
            self.assertEqual((c1, c2), (0, 0))
            self.assertEqual((Path(d) / "a.strata").read_text(),
                             (Path(d) / "b.strata").read_text())

    def test_fail_loud_merge_delete(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country FROM orders) AS s\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN DELETE")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("DELETE", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_fail_loud_merge_using_not_parenthesized(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING orders AS s\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET country = s.country\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, country)\n"
               "  VALUES (s.order_id, s.country)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("USING", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_fail_loud_merge_missing_when_arm(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country FROM orders) AS s\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET country = s.country")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("WHEN NOT MATCHED", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_fail_loud_merge_on_non_equi_join(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country FROM orders) AS s\n"
               "ON t.order_id > s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET country = s.country\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, country)\n"
               "  VALUES (s.order_id, s.country)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("equi", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_fail_loud_this_outside_dml_still_rejected(self):
        # `{{ this }}` is legal only as a DML target; inside a plain SELECT it
        # remains an unsupported jinja form — the dispatcher wins, then the
        # strip still fails on the body.
        sql = ("SELECT order_id FROM orders "
               "WHERE country = {{ this }}")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertFalse((Path(d) / "imported.strata").exists())


if __name__ == "__main__":
    unittest.main()
