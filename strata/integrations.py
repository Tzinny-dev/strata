"""Orchestrator integrations: Airflow, Prefect, Dagster.

Provides hooks and operators to integrate Strata pipelines with
workflow orchestrators for production deployments.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from pathlib import Path


def airflow_dag_factory(
    module_path: str,
    dag_id: Optional[str] = None,
    schedule: str = "@daily",
    catchup: bool = False,
    tags: Optional[List[str]] = None,
) -> str:
    """Generate an Airflow DAG Python file for a Strata module.

    Returns the DAG file content as a string.
    """
    if dag_id is None:
        dag_id = Path(module_path).stem.replace("-", "_").replace(".", "_")

    tags_str = repr(tags or ["strata"])
    schedule_str = repr(schedule)

    return f'''"""
Auto-generated Airflow DAG for Strata module: {module_path}
"""
from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator

default_args = {{
    "owner": "strata",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}}


def run_strata_only_stale(module_path: str):
    """Run Strata pipeline, materializing only stale models."""
    import subprocess
    result = subprocess.run(
        ["strata", "run", module_path, "--only-stale"],
        capture_output=True, text=True, check=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)


def run_strata_all(module_path: str):
    """Run Strata pipeline, materializing all models."""
    import subprocess
    result = subprocess.run(
        ["strata", "run", module_path],
        capture_output=True, text=True, check=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)


def check_strata(module_path: str):
    """Run Strata check (CI guard)."""
    import subprocess
    result = subprocess.run(
        ["strata", "check", module_path],
        capture_output=True, text=True, check=True,
    )
    print(result.stdout)


with DAG(
    dag_id={dag_id!r},
    default_args=default_args,
    description="Strata pipeline: {module_path}",
    schedule_interval={schedule_str},
    start_date=datetime(2024, 1, 1),
    catchup={catchup},
    tags={tags_str},
) as dag:

    check = PythonOperator(
        task_id="strata_check",
        python_callable=check_strata,
        op_kwargs={{"module_path": {module_path!r}}},
    )

    materialize = PythonOperator(
        task_id="strata_materialize",
        python_callable=run_strata_only_stale,
        op_kwargs={{"module_path": {module_path!r}}},
    )

    check >> materialize
'''


def prefect_flow_factory(
    module_path: str,
    flow_name: Optional[str] = None,
    schedule: Optional[str] = None,
) -> str:
    """Generate a Prefect flow Python file for a Strata module.

    Returns the flow file content as a string.
    """
    if flow_name is None:
        flow_name = f"strata-{Path(module_path).stem}"

    schedule_decorator = ""
    if schedule:
        schedule_decorator = f'''
@flow(schedule={schedule!r})
'''
    else:
        schedule_decorator = "\n@flow\n"

    return f'''"""
Auto-generated Prefect flow for Strata module: {module_path}
"""
from prefect import flow, task
import subprocess


@task(retries=3, retry_delay_seconds=60)
def check_strata(module_path: str):
    """Run Strata check (CI guard)."""
    result = subprocess.run(
        ["strata", "check", module_path],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


@task(retries=2, retry_delay_seconds=30)
def materialize_stale(module_path: str):
    """Run Strata pipeline, materializing only stale models."""
    result = subprocess.run(
        ["strata", "run", module_path, "--only-stale"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


@task(retries=2, retry_delay_seconds=30)
def materialize_all(module_path: str):
    """Run Strata pipeline, materializing all models."""
    result = subprocess.run(
        ["strata", "run", module_path],
        capture_output=True, text=True, check=True,
    )
    return result.stdout
{schedule_decorator}
def {flow_name.replace("-", "_")}(module_path: str = {module_path!r}):
    """Main Strata pipeline flow."""
    check_result = check_strata(module_path)
    materialize_result = materialize_stale(module_path)
    return materialize_result


if __name__ == "__main__":
    {flow_name.replace("-", "_")}()
'''


def freshness_check_hook(
    module_path: str,
    webhook_url: Optional[str] = None,
    slack_channel: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a freshness check hook for monitoring.

    Returns a configuration dict that can be used by monitoring tools.
    """
    return {
        "module_path": module_path,
        "check_type": "freshness",
        "webhook_url": webhook_url,
        "slack_channel": slack_channel,
        "command": ["strata", "run", module_path, "--only-stale"],
        "alert_on_stale": True,
        "alert_on_error": True,
    }


def partition_dependency_resolver(
    module_path: str,
    partition_column: str = "ds",
) -> Dict[str, Any]:
    """Create a partition dependency resolver for orchestrators.

    Returns configuration for partition-based scheduling.
    """
    return {
        "module_path": module_path,
        "partition_column": partition_column,
        "dependency_type": "partition",
        "partition_format": "{{ ds }}",
        "airflow_params": {
            "ds": "{{ ds }}",
            "execution_date": "{{ execution_date }}",
        },
    }


def metrics_exporter(
    module_path: str,
    metrics_format: str = "prometheus",
) -> Dict[str, Any]:
    """Create a metrics exporter configuration for Strata pipelines.

    Returns configuration for exporting pipeline metrics.
    """
    return {
        "module_path": module_path,
        "metrics_format": metrics_format,
        "metrics": [
            "strata_staleness_check_duration_seconds",
            "strata_models_stale_total",
            "strata_materialization_duration_seconds",
            "strata_freshness_threshold_seconds",
            "strata_partition_count",
        ],
        "labels": ["model", "dialect", "branch"],
    }
