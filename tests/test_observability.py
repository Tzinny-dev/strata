"""Tests for observability module."""
import unittest
from strata.observability import (
    MetricsCollector,
    PrometheusExporter,
    StatsDExporter,
    JsonExporter,
    create_monitoring_config,
    dashboard_config,
)


class TestMetricsCollector(unittest.TestCase):
    def test_record_staleness_check(self):
        """Record staleness check metric."""
        collector = MetricsCollector()
        collector.record_staleness_check("model1", is_stale=True, duration_ms=100.5)
        metrics = collector.get_metrics()
        self.assertEqual(len(metrics), 2)
        self.assertEqual(metrics[0].name, "strata_staleness_check_duration_ms")
        self.assertEqual(metrics[0].value, 100.5)
        self.assertEqual(metrics[0].labels["model"], "model1")
        self.assertEqual(metrics[0].labels["is_stale"], "True")

    def test_record_materialization(self):
        """Record materialization metric."""
        collector = MetricsCollector()
        collector.record_materialization("model1", duration_ms=500.0, rows=1000)
        metrics = collector.get_metrics()
        self.assertEqual(len(metrics), 2)
        self.assertEqual(metrics[0].name, "strata_materialization_duration_ms")
        self.assertEqual(metrics[1].name, "strata_materialization_rows")
        self.assertEqual(metrics[1].value, 1000)

    def test_record_freshness_threshold(self):
        """Record freshness threshold metric."""
        collector = MetricsCollector()
        collector.record_freshness_threshold("model1", threshold_hours=24.0)
        metrics = collector.get_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].name, "strata_freshness_threshold_hours")
        self.assertEqual(metrics[0].value, 24.0)

    def test_record_error(self):
        """Record error metric."""
        collector = MetricsCollector()
        collector.record_error("model1", "pin_error")
        metrics = collector.get_metrics()
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].name, "strata_errors_total")
        self.assertEqual(metrics[0].labels["error_type"], "pin_error")

    def test_reset(self):
        """Reset metrics collector."""
        collector = MetricsCollector()
        collector.record_staleness_check("model1", is_stale=True, duration_ms=100.0)
        collector.reset()
        self.assertEqual(len(collector.get_metrics()), 0)


class TestExporters(unittest.TestCase):
    def test_prometheus_exporter(self):
        """Export metrics in Prometheus format."""
        collector = MetricsCollector()
        collector.record_staleness_check("model1", is_stale=True, duration_ms=100.0)
        exporter = PrometheusExporter(collector)
        output = exporter.export()
        self.assertIn("strata_staleness_check_duration_ms", output)
        self.assertIn('model="model1"', output)

    def test_statsd_exporter(self):
        """Export metrics in StatsD format."""
        collector = MetricsCollector()
        collector.record_staleness_check("model1", is_stale=True, duration_ms=100.0)
        exporter = StatsDExporter(collector)
        output = exporter.export()
        self.assertIn("strata.strata_staleness_check_duration_ms", output)
        self.assertIn("|g", output)

    def test_json_exporter(self):
        """Export metrics in JSON format."""
        collector = MetricsCollector()
        collector.record_staleness_check("model1", is_stale=True, duration_ms=100.0)
        exporter = JsonExporter(collector)
        output = exporter.export()
        self.assertIn("strata_staleness_check_duration_ms", output)
        self.assertIn("model1", output)


class TestConfig(unittest.TestCase):
    def test_create_monitoring_config(self):
        """Create monitoring configuration."""
        config = create_monitoring_config("pipeline.strata")
        self.assertEqual(config["module_path"], "pipeline.strata")
        self.assertIn("prometheus", config["exporters"])
        self.assertEqual(config["collection_interval_seconds"], 60)

    def test_dashboard_config(self):
        """Create dashboard configuration."""
        config = dashboard_config()
        self.assertEqual(config["dashboard"]["title"], "Strata Pipeline Metrics")
        self.assertEqual(len(config["dashboard"]["panels"]), 4)


if __name__ == "__main__":
    unittest.main()
