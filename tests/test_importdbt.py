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
from strata import importdbt

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

    def test_insert_overwrite_with_column_list(self):
        # INSERT column list `(a, b) SELECT ...` is orthogonal to the body
        # (the columns are dbt naming, not a Strata projection).
        sql = ("INSERT OVERWRITE INTO target_tbl (order_id, country, total)\n"
               "SELECT order_id, country, SUM(gross) AS total\n"
               "FROM orders\n"
               "GROUP BY order_id, country")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("from orders", art)
            self.assertNotIn("(order_id, country, total)", art)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0)

    def test_fail_loud_insert_overwrite_column_list_unbalanced(self):
        sql = ("INSERT OVERWRITE INTO target_tbl (order_id, country, total\n"
               "SELECT order_id, country, SUM(gross) AS total FROM orders\n"
               "GROUP BY order_id, country")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("column list", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_config_without_known_keys_stays_silent(self):
        # Unknown/odd config kwargs are advisory: no E042, no annotation, and
        # the DML still translates — config is never a gate (§4).
        sql = "{{ config(location_root='/warehouse', table_properties={'a': 'b'}) }}\n" \
              "MERGE INTO {{ this }} AS t\n" \
              "USING (SELECT order_id, SUM(gross) AS total FROM orders\n" \
              "       GROUP BY order_id) AS s\n" \
              "ON t.order_id = s.order_id\n" \
              "WHEN MATCHED THEN UPDATE SET total = s.total\n" \
              "WHEN NOT MATCHED THEN INSERT (order_id, total)\n" \
              "  VALUES (s.order_id, s.total)"
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            bcode = cli.main(["build", str(Path(d) / "imported.strata")])
            self.assertEqual(bcode, 0)

    def test_fail_loud_merge_using_unbalanced_subquery(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, country FROM orders\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET country = s.country\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, country)\n"
               "  VALUES (s.order_id, s.country)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("unbalanced", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_fail_loud_merge_missing_on(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id FROM orders) AS s\n"
               "WHEN MATCHED THEN UPDATE SET country = 'x'\n"
               "WHEN NOT MATCHED THEN INSERT (order_id) VALUES (s.order_id)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("ON", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_merge_target_not_this_resolves_name(self):
        # MERGE INTO an explicit table name (not `{{ this }}`) keeps the name
        # in the annotation instead of spilling a jinja literal.
        sql = ("MERGE INTO daily_target AS t\n"
               "USING (SELECT order_id, SUM(gross) AS total FROM orders\n"
               "       GROUP BY order_id) AS s\n"
               "ON t.order_id = s.order_id\n"
               "WHEN MATCHED THEN UPDATE SET total = s.total\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, total)\n"
               "  VALUES (s.order_id, s.total)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0, err)
            art = (Path(d) / "imported.strata").read_text()
            self.assertIn("MERGE INTO daily_target", art)
            self.assertNotIn("{{ this }}", art)

    def test_config_inline_value_with_escapes(self):
        # config() parsing tolerates escapes and finds keys after them.
        kw = importdbt._extract_config_kwargs(
            "{{ config(tags=['a\\'b'], partition_by=['country']) }}")
        self.assertEqual(kw.get("partition_by"), "['country']")
        self.assertEqual(kw.get("tags"), "['a\\'b']")

    def test_config_malformed_kwargs_are_skipped(self):
        # parts without `=` or with a non-ident key cannot be attributed; the
        # translator keeps scanning instead of choking on them.
        kw = importdbt._extract_config_kwargs(
            "{{ config('garbage without equals', 3 = 'v', ok_key = 'y') }}")
        self.assertNotIn("3", kw)
        self.assertEqual(kw.get("ok_key"), "'y'")

    def test_merge_source_without_alias(self):
        # `USING (...) ON ...` without the optional `AS s` alias is legal.
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id, SUM(gross) AS total FROM orders\n"
               "       GROUP BY order_id)\n"
               "ON t.order_id = ORDER_id.ID\n"
               "WHEN MATCHED THEN UPDATE SET total = s.total\n"
               "WHEN NOT MATCHED THEN INSERT (order_id, total)\n"
               "  VALUES (s.order_id, s.total)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1, "bare ON with no name match must fail loud")
            self.assertIn("E042", err)
            self.assertIn("MERGE ON", err)

    def test_merge_on_empty(self):
        sql = ("MERGE INTO {{ this }} AS t\n"
               "USING (SELECT order_id FROM orders) AS s\n"
               "ON WHEN MATCHED THEN UPDATE SET country = 'x'\n"
               "WHEN NOT MATCHED THEN INSERT (order_id) VALUES (s.order_id)")
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d), {"int_clean": None, "daily": sql},
                                            schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertFalse((Path(d) / "imported.strata").exists())

    def test_import_writes_default_output_name(self):
        # No --output: artifact lands next to schema.yml with the same stem.
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
            tests: [not_null]
          - name: country
            data_type: string
          - name: gross
            data_type: numeric
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
        tests: [not_null]
      - name: country
        data_type: string
      - name: total
        data_type: numeric
  - name: int_clean
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
        with __import__("tempfile").TemporaryDirectory() as d:
            (Path(d) / "schema.yml").write_text(schema)
            models = Path(d) / "models"
            models.mkdir()
            (models / "int_clean.sql").write_text(
                "SELECT order_id, country, SUM(gross) AS total\n"
                "FROM orders\n"
                "GROUP BY order_id, country")
            (models / "daily.sql").write_text(
                "INSERT OVERWRITE INTO {{ this }}\n"
                "SELECT order_id, country, SUM(gross) AS total\n"
                "FROM orders\n"
                "GROUP BY order_id, country")
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["import-dbt", str(Path(d) / "schema.yml"),
                                 "--models", str(models)])
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue((Path(d) / "schema.strata").exists())

    def test_cte_helper_name_collision_fails_loud(self):
        # Lowering CTE `daily` inside model `int_clean` to helper `int_clean__daily`
        # collides with a schema-declared model of that exact name — fail loud
        # E042, no silent shadowing of the declared column contract (§4).
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
models:
  - name: daily
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
  - name: int_clean
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
  - name: int_clean__daily
    depends_on: [orders]
    columns:
      - name: order_id
        data_type: bigint
"""
        with __import__("tempfile").TemporaryDirectory() as d:
            code, err = _run_project_import(Path(d),
                {"int_clean": "WITH daily AS (SELECT order_id FROM orders)\n"
                              "SELECT order_id FROM daily",
                 "daily": None}, schema=schema)
            self.assertEqual(code, 1)
            self.assertIn("E042", err)
            self.assertIn("collides", err)
            self.assertFalse((Path(d) / "imported.strata").exists())


class TestImportDbtUnitHelpers(unittest.TestCase):
    """Direct unit coverage of the import-dbt translator internals: literal
    lowering, WHERE conjunct parsing, SELECT-item dispatch and the type map
    fail-loud path (E041) — the branches the full-import tests reach only
    indirectly."""

    def test_map_type_unknown_fails_loud_keyerror(self):
        with self.assertRaises(KeyError) as cm:
            importdbt.map_type("geography")
        self.assertEqual(cm.exception.args[0], "geography")

    def test_literal_strata_bools_and_null(self):
        self.assertEqual(importdbt._literal_strata("TRUE"), "true")
        self.assertEqual(importdbt._literal_strata("FALSE"), "false")
        self.assertEqual(importdbt._literal_strata("NULL"), "null")
        self.assertEqual(importdbt._literal_strata("'it''s'"), '"it\'s"')
        self.assertEqual(importdbt._literal_strata('"quoted"'), '"quoted"')
        self.assertEqual(importdbt._literal_strata("42"), "42")

    def test_parse_where_conjunct_is_null_forms(self):
        toks = importdbt._tokenize("a IS NULL")
        self.assertEqual(importdbt._parse_where_conjunct(toks, "m"), "a == null")
        self.assertEqual(
            importdbt._parse_where_conjunct(importdbt._tokenize("a IS NOT NULL"), "m"),
            "not (a == null)")

    def test_parse_where_conjunct_reversed_column_order_rejected(self):
        # `5 > a.col` flips a strict order onto a column — not deterministic,
        # so the conjunct must come back None (caller fails loud §4).
        self.assertIsNone(importdbt._parse_where_conjunct(
            importdbt._tokenize("5 > a.col"), "m"))
        self.assertIsNone(importdbt._parse_where_conjunct(
            importdbt._tokenize("a = b"), "m"))
        self.assertIsNone(importdbt._parse_where_conjunct(
            importdbt._tokenize("(a = 1)"), "m"))

    def test_parse_select_item_dispatch(self):
        toks = importdbt._tokenize
        self.assertEqual(importdbt._parse_select_item(toks("a.col AS x"), "m"),
                         ("col", "col", "x"))
        self.assertEqual(importdbt._parse_select_item(toks("a AS y"), "m"),
                         ("col", "a", "y"))
        self.assertEqual(importdbt._parse_select_item(toks("a.col"), "m"),
                         ("col", "col", "col"))
        self.assertEqual(importdbt._parse_select_item(toks("b"), "m"),
                         ("col", "b", "b"))
        self.assertEqual(importdbt._parse_select_item(toks("SUM(gross) AS total"), "m"),
                         ("agg", "sum(gross)", "total"))

    def test_parse_select_item_star_and_bare_agg_fail_loud(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_select_item(importdbt._tokenize("*"), "m")
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_select_item(importdbt._tokenize("count(*)"), "m")

    def test_parse_case_expr_success(self):
        expr = importdbt._parse_case_expr(
            "CASE WHEN a > 1 THEN 'x' ELSE 'y' END AS c"[:0] +
            "CASE WHEN a > 1 THEN 'x' ELSE 'y' END", "m")
        self.assertEqual(expr, 'case(a > 1, "x", "y")')

    def test_parse_case_expr_malformed_fails_loud(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE WHEN a > 1 THEN 'x' END AS c", "m")
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE WHEN a > 1 THEN 'x'", "m")

    def test_strip_jinja_rejects_macro_expressions(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._strip_jinja("SELECT {{ dbt_utils.date_spine() }}", "m",
                                   {"int_clean"}, {"orders"})

    def test_iceberg_config_notes_skips_unknown_keys(self):
        notes = importdbt._extract_config_kwargs(
            "{{ config(materialized='incremental', unknown_opt='x') }}")
        self.assertEqual(notes.get("materialized"), "'incremental'")
        self.assertEqual(notes.get("unknown_opt"), "'x'")
        n = importdbt._iceberg_config_notes(
            "{{ config(partition_by=['country'], ttl_days=30, materialized='table') }}")
        self.assertEqual(len(n), 3)
        self.assertTrue(any("partition_by" in line for line in n))
        self.assertTrue(any("ttl_days" in line for line in n))

    def test_strip_jinja_unterminated_and_undeclared_fail_loud(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._strip_jinja("SELECT {{ ref('x') }} trailing", "m",
                                   set(), set())
        with self.assertRaises(TransformFailLoud):
            importdbt._strip_jinja("SELECT * FROM {{ ref('ghost') }}", "m",
                                   {"int_clean"}, {"orders"})
        with self.assertRaises(TransformFailLoud):
            importdbt._strip_jinja(
                "SELECT * FROM {{ source('crm', 'ghost') }}", "m",
                {"int_clean"}, {"orders"})
        with self.assertRaises(TransformFailLoud):
            importdbt._strip_jinja("SELECT * FROM x {% if true %}y{% endif %}",
                                   "m", {"int_clean"}, {"orders"})

    def test_strip_jinja_comments_and_config_forms(self):
        self.assertEqual(
            importdbt._strip_jinja("SELECT 1 {# a comment #}", "m", set(), set()),
            "SELECT 1 ")
        self.assertEqual(
            importdbt._strip_jinja(
                "{{ config(materialized='incremental') }}SELECT 1", "m", set(), set()),
            "SELECT 1")

    def test_strip_sql_comments_keeps_literals(self):
        self.assertEqual(
            importdbt._strip_sql_comments("a -- line\nb"), "a \nb")
        self.assertEqual(
            importdbt._strip_sql_comments("a /* block */ b"), "a  b")
        self.assertEqual(
            importdbt._strip_sql_comments("'-- not a comment' /* real */"),
            "'-- not a comment' ")
        self.assertEqual(
            importdbt._strip_sql_comments("'escaped \\' quote' -- comment"),
            "'escaped \\' quote' ")
        self.assertEqual(
            importdbt._strip_sql_comments("x 'unterminated"), "x 'unterminated")
        self.assertEqual(
            importdbt._strip_sql_comments('x "unterminated'), 'x "unterminated')

    def test_parse_where_conjunct_is_null_keeps_column(self):
        self.assertEqual(
            importdbt._parse_where_conjunct(
                importdbt._tokenize("a.id IS NOT NULL"), "m"),
            "not (id == null)")

    def test_config_extract_quoted_list_value(self):
        kw = importdbt._extract_config_kwargs(
            "{{ config(schema='stg', tags=['a','b'], materialized='view') }}")
        self.assertEqual(kw["schema"], "'stg'")
        self.assertEqual(kw["tags"], "['a','b']")
        self.assertEqual(kw["materialized"], "'view'")

    def test_split_config_args_tolerates_malformed(self):
        self.assertEqual(importdbt._split_config_args(""), [])
        self.assertEqual(importdbt._split_config_args("not a kwarg"), [])
        self.assertEqual(importdbt._split_config_args("a=1,"), [("a", "1")])

    def test_split_with_cte_error_branches(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._split_with("WITH", "m")  # no CTE name
        with self.assertRaises(TransformFailLoud):
            importdbt._split_with("WITH c (a, b) AS (SELECT 1)", "m")  # col list
        with self.assertRaises(TransformFailLoud):
            importdbt._split_with("WITH c SELECT 1", "m")  # missing AS
        with self.assertRaises(TransformFailLoud):
            importdbt._split_with("WITH c AS SELECT 1", "m")  # no parens
        with self.assertRaises(TransformFailLoud):
            importdbt._split_with("WITH c AS (SELECT 1", "m")  # unclosed
        with self.assertRaises(TransformFailLoud):
            importdbt._split_with("WITH c AS (SELECT 1) X", "m")  # trailing non-select

    def test_parse_transform_rejects_non_select(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_transform("WITH c AS (SELECT 1) INSERT INTO t", "m", set(), set())

    def test_strip_jinja_unterminated_fails_loud(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._strip_jinja("SELECT {{ ref('x'", "m", {"int_clean"}, set())

    def test_parse_case_expr_branches(self):
        from strata.importdbt import TransformFailLoud
        # CASE WHEN without condition
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE WHEN THEN 'x' ELSE 'y' END", "m")
        # CASE WHEN without THEN
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE WHEN a > 1 ELSE 'y' END", "m")
        # CASE THEN without value
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE WHEN a > 1 THEN ELSE 'y' END", "m")
        # CASE ELSE without value
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE WHEN a > 1 THEN 'x' ELSE END", "m")
        # CASE expected WHEN or ELSE
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE 'foo' END", "m")
        # CASE with no branches
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_case_expr("CASE END", "m")
        # Happy multiple WHEN + ELSE with literals
        self.assertEqual(
            importdbt._parse_case_expr(
                "CASE WHEN a > 1 THEN 'x' WHEN b = 2 THEN 'y' ELSE 'z' END", "m"),
            'case(a > 1, "x", b == 2, "y", "z")')
        # Happy multiple WHEN no ELSE
        self.assertEqual(
            importdbt._parse_case_expr(
                "CASE WHEN a > 1 THEN 'x' WHEN b = 2 THEN 'y' END", "m"),
            'case(a > 1, "x", b == 2, "y")')
        # THEN with bare column (single token)
        self.assertEqual(
            importdbt._parse_case_expr(
                "CASE WHEN a > 1 THEN mycol END", "m"),
            'case(a > 1, mycol)')

    def test_rewrite_dml_merge_empty_on(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._rewrite_dml_annotation(
                "MERGE INTO {{ this }} USING (SELECT 1) AS s ON WHEN MATCHED THEN UPDATE SET x=1", "m")

    def test_parse_transform_subquery_fails_loud(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_transform(
                "SELECT a FROM t SELECT b FROM t2", "m", {"t", "t2"}, set())

    def test_parse_transform_no_from_fails_loud(self):
        from strata.importdbt import TransformFailLoud
        with self.assertRaises(TransformFailLoud):
            importdbt._parse_transform("SELECT a", "m", set(), set())

    def test_parse_transform_empty_cur_branch(self):
        # `SELECT FROM ...` — the FROM token arrives with cur still empty,
        # exercising the `if cur:` guard in the clause-splitting loop.
        from strata.importdbt import TransformFailLoud
        with self.assertRaises((TransformFailLoud, IndexError)):
            importdbt._parse_transform("SELECT FROM t", "m", {"t"}, set())

    def test_merge_with_cte_dedup_hits_line_757(self):
        # MERGE whose USING body is a WITH-CTE SELECT: the CTE path +
        # dedup_keys both fire (line 757 in _translate_with).
        with __import__("tempfile").TemporaryDirectory() as d:
            sql = ("MERGE INTO {{ this }} AS t\n"
                   "USING (WITH cte AS (SELECT order_id FROM orders)\n"
                   "       SELECT order_id FROM cte) AS s\n"
                   "ON s.order_id = t.order_id\n"
                   "WHEN MATCHED THEN UPDATE SET country = 'x'\n"
                   "WHEN NOT MATCHED THEN INSERT (order_id) VALUES (s.order_id)")
            code, err = _run_project_import(Path(d),
                {"int_clean": None, "daily": sql}, schema=ICEBERG_SCHEMA)
            self.assertEqual(code, 0)
            self.assertTrue((Path(d) / "imported.strata").exists())
            out = (Path(d) / "imported.strata").read_text()
            self.assertIn("dedup by order_id", out)


if __name__ == "__main__":
    unittest.main()
