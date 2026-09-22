"""Testing utilities — internal fixture helpers, not a public runtime API.

Status: helper library for *authoring* tests/fixtures (create_test_source,
create_test_model, FreshnessTestHelper, etc.). Not wired to the executor
for end-to-end orchestration; the real freshness/staleness gate lives in
`strata.exec` (`stale_models`, `run --only-stale`). These helpers just
emit `.strata` text or create temp DuckDB tables.

See `plan-hito-2.md` §B: documented as "helper, not connected" — do not
present as a production testing framework.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import tempfile
import os


def create_test_source(
    name: str,
    columns: Dict[str, str],
    ns: str = "test_ns",
    dataset: str = "test_dataset",
) -> str:
    """Create a test source declaration."""
    cols = ", ".join(f"{col}: {typ}" for col, typ in columns.items())
    return f'source {name}(ns: "{ns}", dataset: "{dataset}") {{ columns: {{ {cols} }} }}'


def create_test_model(
    name: str,
    source: str,
    freshness: Optional[str] = None,
    freshness_column: Optional[str] = None,
    partition_by: Optional[List[str]] = None,
    incremental: bool = False,
    merge_strategy: Optional[str] = None,
    merge_keys: Optional[List[str]] = None,
    cdc_column: Optional[str] = None,
    staleness_ok: bool = False,
) -> str:
    """Create a test model declaration."""
    parts = [f"from {source}"]

    if partition_by:
        keys = ", ".join(partition_by)
        parts.append(f"partition_by [{keys}]")

    if freshness:
        parts.append(f"freshness {freshness}")

    if freshness_column:
        parts.append(f"freshness_column: {freshness_column}")

    if incremental:
        parts.append("incremental")
        if merge_strategy:
            parts.append(f"merge_strategy: {merge_strategy}")
        if merge_keys:
            keys = ", ".join(merge_keys)
            parts.append(f"merge_keys: [{keys}]")
        if cdc_column:
            parts.append(f"cdc_column: {cdc_column}")

    if staleness_ok:
        parts.append('staleness_ok: "true"')

    body = " ".join(parts)
    return f"model {name} {{ {body} }}"


def create_test_module(
    sources: Dict[str, Dict[str, str]],
    models: Dict[str, Dict[str, Any]],
    output_dir: Optional[str] = None,
) -> str:
    """Create a complete test module with sources and models.

    Args:
        sources: Dict of source_name -> {columns: {col: type}, ns: str, dataset: str}
        models: Dict of model_name -> {source: str, freshness: str, ...}
        output_dir: Directory to write the .strata file (if None, uses tempdir)

    Returns:
        Path to the created .strata file
    """
    parts = []

    # Add sources
    for src_name, src_config in sources.items():
        ns = src_config.get("ns", "test_ns")
        dataset = src_config.get("dataset", "test_dataset")
        columns = src_config.get("columns", {})
        parts.append(create_test_source(src_name, columns, ns, dataset))

    # Add models
    for model_name, model_config in models.items():
        source = model_config.get("source", list(sources.keys())[0])
        parts.append(create_test_model(
            name=model_name,
            source=source,
            freshness=model_config.get("freshness"),
            freshness_column=model_config.get("freshness_column"),
            partition_by=model_config.get("partition_by"),
            incremental=model_config.get("incremental", False),
            merge_strategy=model_config.get("merge_strategy"),
            merge_keys=model_config.get("merge_keys"),
            cdc_column=model_config.get("cdc_column"),
            staleness_ok=model_config.get("staleness_ok", False),
        ))

    content = "\n\n".join(parts) + "\n"

    if output_dir is None:
        output_dir = tempfile.mkdtemp()

    file_path = os.path.join(output_dir, "test_module.strata")
    with open(file_path, "w") as f:
        f.write(content)

    return file_path


def create_stale_data(
    con: Any,
    table_name: str,
    rows: List[Tuple],
    columns: List[str],
    hours_old: int = 0,
    timestamp_column: Optional[str] = None,
) -> None:
    """Create test data that appears stale.

    Args:
        con: DuckDB connection
        table_name: Name of the table to create
        rows: List of row tuples
        columns: List of column names
        hours_old: How many hours old the data should appear
        timestamp_column: Column to use for timestamp (if None, uses current time)
    """
    # Create the table
    cols = ", ".join(f"{col} VARCHAR" for col in columns)
    con.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({cols})")

    # Insert rows
    for row in rows:
        placeholders = ", ".join(["?" for _ in row])
        con.execute(f"INSERT INTO {table_name} VALUES ({placeholders})", row)


def create_fresh_data(
    con: Any,
    table_name: str,
    rows: List[Tuple],
    columns: List[str],
) -> None:
    """Create test data that appears fresh (current timestamp).

    Args:
        con: DuckDB connection
        table_name: Name of the table to create
        rows: List of row tuples
        columns: List of column names
    """
    create_stale_data(con, table_name, rows, columns, hours_old=0)


def simulate_source_change(
    con: Any,
    table_name: str,
    new_rows: List[Tuple],
    columns: List[str],
) -> None:
    """Simulate a source data change by inserting new rows.

    Args:
        con: DuckDB connection
        table_name: Name of the table
        new_rows: New rows to insert
        columns: List of column names
    """
    for row in new_rows:
        placeholders = ", ".join(["?" for _ in row])
        con.execute(f"INSERT INTO {table_name} VALUES ({placeholders})", row)


class FreshnessTestHelper:
    """Helper class for testing freshness and staleness detection."""

    def __init__(self, con: Any) -> None:
        self.con = con
        self.tables_created: List[str] = []

    def setup(self) -> None:
        """Set up test tables."""
        pass

    def teardown(self) -> None:
        """Clean up test tables."""
        for table in self.tables_created:
            try:
                self.con.execute(f"DROP TABLE IF EXISTS {table}")
            except Exception:
                pass
        self.tables_created.clear()

    def create_source_table(
        self,
        name: str,
        columns: Dict[str, str],
        rows: Optional[List[Tuple]] = None,
    ) -> None:
        """Create a source table for testing."""
        cols = ", ".join(f"{col} {typ}" for col, typ in columns.items())
        self.con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols})")
        self.tables_created.append(name)

        if rows:
            for row in rows:
                placeholders = ", ".join(["?" for _ in row])
                self.con.execute(f"INSERT INTO {name} VALUES ({placeholders})", row)

    def assert_model_is_stale(self, model_name: str, expected_stale: bool = True) -> None:
        """Assert that a model is stale or fresh."""
        # This would need to be implemented with actual staleness checking
        pass

    def assert_freshness_threshold(self, model_name: str, expected_hours: float) -> None:
        """Assert that a model has the expected freshness threshold."""
        # This would need to be implemented with actual threshold checking
        pass

    def get_table_row_count(self, table_name: str) -> int:
        """Get the row count of a table."""
        result = self.con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
        return result[0] if result else 0

    def get_table_columns(self, table_name: str) -> List[str]:
        """Get the column names of a table."""
        result = self.con.execute(f"DESCRIBE {table_name}").fetchall()
        return [row[0] for row in result]


def create_test_fixtures() -> Dict[str, Any]:
    """Create common test fixtures for freshness testing."""
    return {
        "sources": {
            "events": {
                "columns": {
                    "id": "INTEGER",
                    "event_type": "VARCHAR",
                    "user_id": "INTEGER",
                    "timestamp": "TIMESTAMP",
                },
                "ns": "analytics",
                "dataset": "events",
            },
            "users": {
                "columns": {
                    "id": "INTEGER",
                    "name": "VARCHAR",
                    "email": "VARCHAR",
                    "created_at": "TIMESTAMP",
                },
                "ns": "crm",
                "dataset": "users",
            },
        },
        "models": {
            "daily_events": {
                "source": "events",
                "freshness": "daily",
                "partition_by": ["timestamp"],
            },
            "hourly_events": {
                "source": "events",
                "freshness": "1h",
                "partition_by": ["timestamp"],
            },
            "user_events": {
                "source": "events",
                "freshness": "weekly",
            },
        },
    }
