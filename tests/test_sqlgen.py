import glob
import os
import unittest
from pathlib import Path

from strata import analysis
from strata.analysis import Checker
from strata.parser import parse_strata
from strata import sqlgen
from strata.dialects import DUCKDB, BIGQUERY, SNOWFLAKE
from strata.dialects import get_dialect
from strata.analysis import StrataError

EX = Path(__file__).parent.parent / "examples"


def build(name):
    text = (EX / name).read_text()
    proj = analysis.Project(parse_strata(text, name))
    Checker(proj).check_all()
    return proj


class TestSQLGenDaily(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = build("daily_orders.strata")
        cls.sql = sqlgen.model_sql(cls.proj.typed["daily_orders"])

    def test_join_and_group(self):
        self.assertIn("LEFT JOIN refunds", self.sql)
        self.assertIn("GROUP BY", self.sql)

    def test_aggregates(self):
        self.assertIn("SUM(", self.sql)
        self.assertIn("COUNT(", self.sql)
        self.assertIn("COALESCE(", self.sql)

    def test_key_columns(self):
        self.assertIn("order_day", self.sql)
        self.assertIn("country", self.sql)

    def test_sorts(self):
        self.assertIn("ORDER BY", self.sql)


class TestSQLGenMultinacional(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = build("multinacional.strata")

    def test_generated_models_are_nodes(self):
        names = ["m_ES_orders", "m_MX_orders", "m_CO_orders", "m_BR_orders"]
        for n in names:
            self.assertIn(n, self.proj.typed)
            self.assertTrue(self.proj.models[n].generated)

    def test_generated_sql_filters(self):
        tm = self.proj.typed["m_ES_orders"]
        sql = sqlgen.model_sql(tm)
        self.assertIn("'ES'", sql)

    def test_contract_enforced_on_generated(self):
        tm = self.proj.typed["m_MX_orders"]
        self.assertEqual(tm.contract, "OrderMetrics")
        self.assertIn("country", tm.schema)


class TestFullSQLExecutableSyntax(unittest.TestCase):
    def test_all_models_sql(self):
        proj = build("daily_orders.strata")
        sql = sqlgen.full_sql(proj.typed, list(proj.typed))
        self.assertTrue(sql.strip().startswith("CREATE OR REPLACE VIEW"))

    def test_no_duplicate_column_alias(self):
        # let `country = upper(...)` shadows orders.country: the base projection
        # must not also emit t0.country, or DuckDB group-by disambiguates wrong.
        src = (EX / "daily_orders.strata").read_text().replace(
            "upper(orders.country)", "lower(orders.country)")
        proj = analysis.Project(parse_strata(src, "shadow.strata"))
        Checker(proj).check_all()
        sql = sqlgen.model_sql(proj.typed["daily_orders"])
        self.assertIn("LOWER(t0.country) AS country", sql)
        self.assertNotIn("t0.country AS country", sql)


class TestPostgresDialect(unittest.TestCase):
    """Fase 2 closure: the postgres adapter (spec listed it from day one).
    Type map + quoting + fail-loud ANTI JOIN pinned as unit tests; the emitted
    SQL is additionally validated against a real postgres server when one is
    installed (test_postgres_live_e2e, skipped otherwise)."""

    @classmethod
    def setUpClass(cls):
        cls.pg = get_dialect("postgres")

    def test_registered_and_type_map(self):
        self.assertEqual(self.pg.name, "postgres")
        self.assertEqual(self.pg.sql_type("string"), "TEXT")
        self.assertEqual(self.pg.sql_type("int64"), "BIGINT")
        self.assertEqual(self.pg.sql_type("float64"), "DOUBLE PRECISION")
        self.assertEqual(self.pg.sql_type("json"), "JSONB")
        self.assertEqual(self.pg.cast_target("money"), "NUMERIC(38,2)")
        self.assertEqual(self.pg.cast_target("decimal(10,4)"), "NUMERIC(10,4)")

    def test_quoting_escapes_embedded_quotes(self):
        self.assertEqual(self.pg.ident('weird"name'), '"weird""name"')

    def test_daily_orders_sql(self):
        proj = build("daily_orders.strata")
        sql = sqlgen.model_sql(proj.typed["daily_orders"], dialect=self.pg)
        self.assertIn("UPPER(t0.country) AS country", sql)
        self.assertIn("FROM orders t0", sql)  # bare identifiers: pg-safe
        self.assertIn("COALESCE(", sql)
        self.assertIn("GROUP BY", sql)

    def test_cast_to_json_targets_jsonb(self):
        self.assertIn("JSONB", self.pg.cast_target("json"))


class TestDialectFailLoud(unittest.TestCase):
    """The dialect adapter's fail-loud guarantee must hold at the codegen
    boundary, not just on the capability flag: compiling a plan whose ANTI JOIN
    the pinned warehouse cannot express must raise, never emit wrong SQL."""

    @classmethod
    def setUpClass(cls):
        src = (EX / "daily_orders.strata").read_text().replace(
            "join_left refunds on orders.order_id == refunds.order_id",
            "join_anti refunds on orders.order_id == refunds.order_id")
        proj = analysis.Project(parse_strata(src, "daily_anti_orders.strata"))
        Checker(proj).check_all()
        cls.anti = proj.typed["daily_orders"]

    def test_duckdb_expresses_anti(self):
        sql = sqlgen.model_sql(self.anti, dialect=DUCKDB)
        self.assertIn("ANTI JOIN", sql)

    def test_bigquery_anti_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            sqlgen.model_sql(self.anti, dialect=BIGQUERY)

    def test_snowflake_anti_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            sqlgen.model_sql(self.anti, dialect=SNOWFLAKE)


class TestPostgresLiveE2E(unittest.TestCase):
    """Same seed fixture, same emitted model SQL, REAL postgres server:
    v_daily_orders must yield the exact 3-row result duckdb produces.
    Skips when no postgres server installation is found (client-only or
    CI without the server). Port raised above 55321 to dodge stray servers."""

    @classmethod
    def setUpClass(cls):
        cls.pgbin = cls._find_pgbin()

    @staticmethod
    def _find_pgbin():
        import shutil
        for cand in (shutil.which("initdb"),
                     *[str(p) for p in sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))]):
            if cand and Path(cand).exists():
                return Path(cand).resolve().parent
        return None

    def test_psql_roundtrip_matches_duckdb_rows(self):
        if not self.pgbin:
            self.skipTest("no postgres server installation found")
        import subprocess
        import tempfile
        pgbin = self.pgbin
        with tempfile.TemporaryDirectory() as td:
            data, sock = Path(td) / "pg", Path(td) / "sock"
            port, db = 55432, "strata_pg_e2e"
            sock.mkdir()
            env = dict(os.environ)
            # pg_ctl spawns the postmaster, which INHERITS our stdio pipes and
            # keeps them open forever -> subprocess.run(deadline) hangs waiting
            # for EOF. Detach the server: DEVNULL streams + own session.
            run = lambda *a: subprocess.run(*a, check=True, env=env,
                                            stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            run([str(pgbin / "initdb"), "-U", "strata", "-A", "trust",
                 "-E", "UTF8", "--no-locale", str(data)])
            subprocess.run([str(pgbin / "pg_ctl"), "-D", str(data), "-w", "-s",
                            "-o", f"-p {port} -k {sock} -c listen_addresses=",
                            "start"], check=True, env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                run([str(pgbin / "createdb"), "-h", str(sock), "-p", str(port),
                     "-U", "strata", db])
                psql = lambda sql: subprocess.run(
                    [str(pgbin / "psql"), "-h", str(sock), "-p", str(port),
                     "-U", "strata", "-d", db, "-v", "ON_ERROR_STOP=1", "-qAt",
                     "-c", sql], check=True, capture_output=True, text=True,
                    env=env).stdout
                psql(
                    "CREATE TABLE orders (order_id BIGINT, customer_id BIGINT, "
                    "country TEXT, gross_amount_usd NUMERIC(38,2), is_test BOOLEAN, "
                    "order_day DATE);\n"
                    "CREATE TABLE refunds (order_id BIGINT, discount_usd NUMERIC(38,2), "
                    "refunded_at TIMESTAMP);\n"
                    "INSERT INTO orders VALUES (1,1001,'ES',120.00,FALSE,DATE '2026-09-01'),"
                    "(2,1001,'ES',90.00,FALSE,DATE '2026-09-01'),"
                    "(3,1002,'MX',200.00,FALSE,DATE '2026-09-02'),"
                    "(4,1003,'BR',75.50,FALSE,DATE '2026-09-02'),"
                    "(5,1004,'CO',40.00,TRUE,DATE '2026-09-03');\n"
                    "INSERT INTO refunds VALUES (2,10.00,TIMESTAMP '2026-09-02 10:00:00'),"
                    "(4,5.50,TIMESTAMP '2026-09-03 09:30:00');\n")
                proj = build("daily_orders.strata")
                pg = get_dialect("postgres")
                psql(sqlgen.full_sql(proj.typed, list(proj.typed), dialect=pg))
                rows = psql("SELECT country, order_day, order_id, gross_amount, "
                            "net_amount FROM v_daily_orders ORDER BY order_day, country")
                self.assertEqual([r.split("|") for r in rows.strip().splitlines()],
                                 [["ES", "2026-09-01", "2", "210.00", "200.00"],
                                  ["BR", "2026-09-02", "1", "75.50", "70.00"],
                                  ["MX", "2026-09-02", "1", "200.00", "200.00"]])
            finally:
                subprocess.run([str(pgbin / "pg_ctl"), "-D", str(data), "-m",
                                "immediate", "stop"], capture_output=True, env=env)


if __name__ == "__main__":
    unittest.main()
