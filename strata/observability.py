"""Observability: metrics collection for Strata pipelines.

Status: connected since 2026-09-20 — `strata.exec` imports
`MetricsCollector`/`PrometheusExporter` and records materialization
duration/row counts and staleness errors via `_run_locked`/`get_metrics()`.
Exporters (Prometheus/StatsD/JSON) are standalone formatters; wiring to a
real pushgateway/statsd endpoint is left to the caller. No external
dependency is required to import this module.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pathlib import Path
import json


@dataclass
class Metric:
    """A single metric data point."""
    name: str
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: Optional[float] = None
    metric_type: str = "gauge"  # gauge, counter, histogram


class MetricsCollector:
    """Collects and exports metrics for Strata pipelines."""

    def __init__(self, namespace: str = "strata") -> None:
        self.namespace = namespace
        self.metrics: List[Metric] = []
        self._start_time: Optional[float] = None

    def start_timer(self) -> None:
        """Start a timer for duration metrics."""
        self._start_time = time.time()

    def record_staleness_check(self, model: str, is_stale: bool, duration_ms: float) -> None:
        """Record a staleness check metric."""
        self.metrics.append(Metric(
            name=f"{self.namespace}_staleness_check_duration_ms",
            value=duration_ms,
            labels={"model": model, "is_stale": str(is_stale)},
            metric_type="histogram",
        ))
        self.metrics.append(Metric(
            name=f"{self.namespace}_models_stale_total",
            value=1 if is_stale else 0,
            labels={"model": model},
            metric_type="counter",
        ))

    def record_materialization(self, model: str, duration_ms: float, rows: int) -> None:
        """Record a materialization metric."""
        self.metrics.append(Metric(
            name=f"{self.namespace}_materialization_duration_ms",
            value=duration_ms,
            labels={"model": model},
            metric_type="histogram",
        ))
        self.metrics.append(Metric(
            name=f"{self.namespace}_materialization_rows",
            value=rows,
            labels={"model": model},
            metric_type="gauge",
        ))

    def record_freshness_threshold(self, model: str, threshold_hours: float) -> None:
        """Record a freshness threshold metric."""
        self.metrics.append(Metric(
            name=f"{self.namespace}_freshness_threshold_hours",
            value=threshold_hours,
            labels={"model": model},
            metric_type="gauge",
        ))

    def record_partition_count(self, model: str, count: int) -> None:
        """Record a partition count metric."""
        self.metrics.append(Metric(
            name=f"{self.namespace}_partition_count",
            value=count,
            labels={"model": model},
            metric_type="gauge",
        ))

    def record_error(self, model: str, error_type: str) -> None:
        """Record an error metric."""
        self.metrics.append(Metric(
            name=f"{self.namespace}_errors_total",
            value=1,
            labels={"model": model, "error_type": error_type},
            metric_type="counter",
        ))

    def get_metrics(self) -> List[Metric]:
        """Get all collected metrics."""
        return self.metrics

    def reset(self) -> None:
        """Reset all collected metrics."""
        self.metrics = []


class PrometheusExporter:
    """Export metrics in Prometheus format."""

    def __init__(self, collector: MetricsCollector) -> None:
        self.collector = collector

    def export(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = []
        for metric in self.collector.get_metrics():
            labels = ",".join(f'{k}="{v}"' for k, v in metric.labels.items())
            labels_str = f"{{{labels}}}" if labels else ""
            lines.append(f"{metric.name}{labels_str} {metric.value}")
        return "\n".join(lines)


class StatsDExporter:
    """Export metrics in StatsD format."""

    def __init__(self, collector: MetricsCollector, prefix: str = "strata") -> None:
        self.collector = collector
        self.prefix = prefix

    def export(self) -> str:
        """Export metrics in StatsD format."""
        lines = []
        for metric in self.collector.get_metrics():
            name = f"{self.prefix}.{metric.name}"
            lines.append(f"{name}:{metric.value}|g")
        return "\n".join(lines)


class JsonExporter:
    """Export metrics in JSON format."""

    def __init__(self, collector: MetricsCollector) -> None:
        self.collector = collector

    def export(self) -> str:
        """Export metrics in JSON format."""
        metrics_data = []
        for metric in self.collector.get_metrics():
            metrics_data.append({
                "name": metric.name,
                "value": metric.value,
                "labels": metric.labels,
                "type": metric.metric_type,
            })
        return json.dumps(metrics_data, indent=2)


def create_monitoring_config(
    module_path: str,
    exporters: Optional[List[str]] = None,
    pushgateway_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a monitoring configuration for a Strata pipeline."""
    if exporters is None:
        exporters = ["prometheus"]

    return {
        "module_path": module_path,
        "exporters": exporters,
        "pushgateway_url": pushgateway_url,
        "metrics_collector": MetricsCollector(),
        "collection_interval_seconds": 60,
        "labels": {
            "environment": "production",
            "service": "strata",
        },
    }


def dashboard_config() -> Dict[str, Any]:
    """Create a Grafana dashboard configuration for Strata metrics."""
    return {
        "dashboard": {
            "title": "Strata Pipeline Metrics",
            "panels": [
                {
                    "title": "Staleness Checks",
                    "type": "graph",
                    "targets": [
                        {"expr": "rate(strata_staleness_check_duration_ms[5m])"},
                    ],
                },
                {
                    "title": "Materialization Duration",
                    "type": "graph",
                    "targets": [
                        {"expr": "rate(strata_materialization_duration_ms[5m])"},
                    ],
                },
                {
                    "title": "Freshness Thresholds",
                    "type": "singlestat",
                    "targets": [
                        {"expr": "strata_freshness_threshold_hours"},
                    ],
                },
                {
                    "title": "Stale Models",
                    "type": "singlestat",
                    "targets": [
                        {"expr": "sum(strata_models_stale_total)"},
                    ],
                },
            ],
        },
    }
