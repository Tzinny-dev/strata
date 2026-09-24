"""Iceberg publication backend (propuesta-iceberg.md L1).

Iceberg is NOT a SQL dialect: the engine still compiles and executes DuckDB
SQL against its `con`, freezing results in run-addressed snapshot TABLES
(`snap_<run_id>_<model>`, see exec.publish_snapshots). This module is the
*physical destination change*: once a run's snapshots are committed, it
copies each one out to a real Apache Iceberg table (`COPY ... TO d
(FORMAT iceberg)`, written by the DuckDB iceberg extension) under a
lakehouse catalog dir, and records the `run_id -> {model: relative dir}`
mapping in a deterministic content-addressed manifest
(`<dir>/_strata_manifest.json`).

Fail-loud (§4 of the proposal): the extension is probed BEFORE the run runs
(no side-effects without it), every snapshot exists before export, and the
manifest is written last — a failed export leaves no manifest, so a partial
catalog is never mistaken for a published run.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

MANIFEST_NAME = "_strata_manifest.json"

class IcebergUnavailable(RuntimeError):
    """The DuckDB iceberg extension could not be loaded (fail-loud, §4)."""


class IcebergExportError(RuntimeError):
    """A committed snapshot could not be written as Iceberg (fail-loud)."""


def ensure_iceberg(con: Any) -> None:
    """Probe and load DuckDB's iceberg extension.

    Practical: `INSTALL` is idempotent and offline once cached (verified
    with DuckDB 1.5.5 on this repo's venv). Raises `IcebergUnavailable`
    (fail-loud, before any run side-effects) if the extension cannot load.
    """
    try:
        con.execute("INSTALL iceberg")
        con.execute("LOAD iceberg")
    except Exception as e:
        raise IcebergUnavailable(
            "DuckDB iceberg extension is not available; cannot materialize "
            f"to Iceberg in this environment ({e}). "
            "Install it via: INSTALL iceberg; LOAD iceberg"
        ) from None
    try:
        rows = con.execute(
            "SELECT installed, loaded FROM duckdb_extensions() "
            "WHERE extension_name='iceberg'").fetchone()
    except Exception:
        rows = None
    if not rows or not rows[0] or not rows[1]:
        raise IcebergUnavailable(
            "DuckDB iceberg extension did not stay installed+loaded after "
            "INSTALL/LOAD; cannot materialize to Iceberg in this environment")


def _safe_dirname(name: str) -> str:
    """Filesystem-safe directory name for a model under the catalog."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    return safe or "model"


def export_snapshot(con: Any, snapshot_table: str, model_name: str,
                    catalog_dir: Path) -> Path:
    """Copy `snapshot_table` to `catalog_dir/<model>` as a real Iceberg table.

    Returns the relative directory under `catalog_dir`. Materializes into the
    catalog dir only after the snapshot EXISTS (fail-loud): a missing table
    means we would invent data by exporting nothing.
    """
    exists = con.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = ?",
        [snapshot_table]).fetchone()
    if not exists or exists[0].lower() != "base table":
        raise IcebergExportError(
            f"snapshot table {snapshot_table!r} not found in warehouse; "
            "refusing to export nothing to Iceberg")
    rel = Path(_safe_dirname(model_name))
    target = catalog_dir / rel
    target.mkdir(parents=True, exist_ok=True)
    try:
        con.execute(
            f"COPY (SELECT * FROM {snapshot_table}) TO '{target}' (FORMAT iceberg)")
    except Exception as e:
        raise IcebergExportError(
            f"failed to write {model_name!r} as Iceberg table: {e}") from None
    return rel


def load_manifest(catalog_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the catalog manifest, or None when the catalog has none yet."""
    mf = catalog_dir / MANIFEST_NAME
    if not mf.exists():
        return None
    return json.loads(mf.read_text())


def write_manifest(catalog_dir: Path, run_id: str,
                   tables: Dict[str, Path]) -> Dict[str, Any]:
    """Merge `run_id -> {model: rel dir}` into the catalog manifest.

    Content-addressed and deterministic: same run gives byte-identical
    manifest content, so `replay`/rollback can diff against it. The manifest
    accumulates every exported run (append-only like the run history), and
    `default` points at the most recent one. Written atomically via os.replace
    so interrupted writes never leave a truncated catalog manifest.
    """
    catalog_dir.mkdir(parents=True, exist_ok=True)
    prev = load_manifest(catalog_dir) or {}
    runs = dict(prev.get("runs") or {})
    runs[run_id] = {model: str(rel) for model, rel in sorted(tables.items())}
    manifest = {"runs": runs, "default": run_id}
    mf = catalog_dir / MANIFEST_NAME
    tmp = catalog_dir / f".{MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, mf)
    return manifest


def export_run(con: Any, run_id: str, snapshots: Dict[str, str],
               catalog_dir: Path) -> Dict[str, Any]:
    """Export a committed run's snapshot tables to the Iceberg catalog.

    `snapshots` is {model name -> snapshot TABLE name} (the same mapping
    `publish_snapshots` records in the run entry). Every snapshot is
    exported first; only when all succeed is the manifest written — a
    catalog with no manifest is provably incomplete and fail-loud, never
    a half-published run.
    """
    tables: Dict[str, Path] = {}
    failed: List[str] = []
    for model, snap in sorted(snapshots.items()):
        try:
            tables[model] = export_snapshot(con, snap, model, catalog_dir)
        except IcebergExportError as e:
            failed.append(str(e))
    if failed:
        raise IcebergExportError(
            "iceberg export incomplete (no manifest written):\n  "
            + "\n  ".join(failed))
    return write_manifest(catalog_dir, run_id, tables)