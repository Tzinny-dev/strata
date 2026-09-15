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


if __name__ == "__main__":
    unittest.main()
