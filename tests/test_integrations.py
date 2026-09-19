"""Tests for orchestrator integrations."""
import unittest
from strata.integrations import (
    airflow_dag_factory,
    prefect_flow_factory,
    freshness_check_hook,
    partition_dependency_resolver,
    metrics_exporter,
)


class TestAirflowIntegration(unittest.TestCase):
    def test_dag_factory_basic(self):
        """Generate basic Airflow DAG."""
        dag = airflow_dag_factory("pipeline.strata")
        self.assertIn("dag_id", dag)
        self.assertIn("strata_check", dag)
        self.assertIn("strata_materialize", dag)
        self.assertIn("pipeline.strata", dag)

    def test_dag_factory_custom_id(self):
        """Generate Airflow DAG with custom ID."""
        dag = airflow_dag_factory("pipeline.strata", dag_id="my_custom_dag")
        self.assertIn("my_custom_dag", dag)

    def test_dag_factory_schedule(self):
        """Generate Airflow DAG with custom schedule."""
        dag = airflow_dag_factory("pipeline.strata", schedule="@hourly")
        self.assertIn("@hourly", dag)


class TestPrefectIntegration(unittest.TestCase):
    def test_flow_factory_basic(self):
        """Generate basic Prefect flow."""
        flow = prefect_flow_factory("pipeline.strata")
        self.assertIn("@flow", flow)
        self.assertIn("check_strata", flow)
        self.assertIn("materialize_stale", flow)

    def test_flow_factory_custom_name(self):
        """Generate Prefect flow with custom name."""
        flow = prefect_flow_factory("pipeline.strata", flow_name="my-flow")
        self.assertIn("my_flow", flow)


class TestHooks(unittest.TestCase):
    def test_freshness_check_hook(self):
        """Create freshness check hook."""
        hook = freshness_check_hook("pipeline.strata")
        self.assertEqual(hook["module_path"], "pipeline.strata")
        self.assertEqual(hook["check_type"], "freshness")
        self.assertTrue(hook["alert_on_stale"])

    def test_partition_dependency_resolver(self):
        """Create partition dependency resolver."""
        resolver = partition_dependency_resolver("pipeline.strata", "date_col")
        self.assertEqual(resolver["partition_column"], "date_col")
        self.assertEqual(resolver["dependency_type"], "partition")

    def test_metrics_exporter(self):
        """Create metrics exporter."""
        exporter = metrics_exporter("pipeline.strata")
        self.assertEqual(exporter["module_path"], "pipeline.strata")
        self.assertIn("strata_staleness_check_duration_seconds", exporter["metrics"])


if __name__ == "__main__":
    unittest.main()
