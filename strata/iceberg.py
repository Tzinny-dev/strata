"""Iceberg physical catalog (propuesta-iceberg.md L1+L2).

Iceberg is NOT a SQL dialect: the engine still compiles and executes DuckDB
SQL against its `con`, freezing results in run-addressed snapshot TABLES
(`snap_<run_id>_<model>`, see exec.publish_snapshots). This module is the
*physical destination change*: once a run's snapshots are committed, it
copies each one out to a real Apache Iceberg table (`COPY ... TO d
(FORMAT iceberg)`, written by the DuckDB iceberg extension) under a lakehouse
catalog dir.

Catalog layout (immutable per run, mirroring snapshot tables):

    <catalog>/
      _strata_manifest.json   {runs: {<run_id>: {model: rel_dir}}, default: <run_id>}
      runs/<run_id>/<model>/  real Iceberg table (data + metadata)

`default` is the *live* run (what consumers read); rollback repoints it
without destroying physical data, gc retires the dirs of runs outside
retention. The manifest accumulates every exported run just like the engine's
append-only history, is byte-deterministic for an identical run, and is
written atomically via os.replace.

Fail-loud (§4 of the proposal): the extension is probed BEFORE the run runs
(no side-effects without it), every snapshot exists before export, the
manifest is written last — a failed export leaves no manifest, so a partial
catalog is never mistaken for a published run — and rollback/verify refuse a
run that is not physically in the catalog.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST_NAME = "_strata_manifest.json"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


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


def run_rel(run_id: str, model_name: str) -> Path:
    """Relative catalog path of one run's Iceberg table for `model_name`."""
    if not _RUN_ID_RE.match(run_id):
        raise IcebergExportError(f"unsafe run id for catalog path: {run_id!r}")
    return Path("runs") / run_id / _safe_dirname(model_name)


def export_snapshot(con: Any, snapshot_table: str, model_name: str,
                    run_id: str, catalog_dir: Path) -> Path:
    """Copy `snapshot_table` to `catalog/runs/<run_id>/<model>` as Iceberg.

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
    rel = run_rel(run_id, model_name)
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


def _atomic_write_manifest(catalog_dir: Path, runs: Dict[str, Any],
                           default: str) -> Dict[str, Any]:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"runs": runs, "default": default}
    mf = catalog_dir / MANIFEST_NAME
    tmp = catalog_dir / f".{MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.replace(tmp, mf)
    return manifest


def write_manifest(catalog_dir: Path, run_id: str,
                   tables: Dict[str, Path]) -> Dict[str, Any]:
    """Merge `run_id -> {model: rel dir}` into the catalog manifest.

    Content-addressed and deterministic: same run gives byte-identical
    manifest content, so `replay`/rollback can diff against it. The manifest
    accumulates every exported run (append-only like the run history), and
    `default` points at the most recent one.
    """
    prev = load_manifest(catalog_dir) or {}
    runs = dict(prev.get("runs") or {})
    runs[run_id] = {model: str(rel) for model, rel in sorted(tables.items())}
    return _atomic_write_manifest(catalog_dir, runs, run_id)


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
            tables[model] = export_snapshot(con, snap, model, run_id, catalog_dir)
        except IcebergExportError as e:
            failed.append(str(e))
    if failed:
        raise IcebergExportError(
            "iceberg export incomplete (no manifest written):\n  "
            + "\n  ".join(failed))
    return write_manifest(catalog_dir, run_id, tables)


def _require_run(catalog_dir: Path, run_id: str) -> Dict[str, Any]:
    """The manifest entry for `run_id` or a fail-loud error."""
    mf = load_manifest(catalog_dir)
    if mf is None:
        raise IcebergExportError(
            f"catalog {catalog_dir} has no manifest: no run is published there")
    if run_id not in mf.get("runs", {}):
        raise IcebergExportError(
            f"run {run_id!r} is not in catalog {catalog_dir} "
            "(see ~/. …; a run only rolls back/verifies against the catalog "
            "that actually exported it)")
    return mf


def verify_catalog_run(con: Any, catalog_dir: Path, run_id: str) -> Dict[str, Any]:
    """Verify a published run WITHOUT re-execution: every model's Iceberg
    table must be physically present and readable through iceberg_scan.

    Returns {run_id, models, rows} when green; raises IcebergExportError with
    the first broken table otherwise. Content-addressed stability of the
    module side stays in `exec.verify_run` (CLI chains both).
    """
    mf = _require_run(catalog_dir, run_id)
    out = {"run_id": run_id, "models": {}, "rows": {}}
    for model, rel in sorted(mf["runs"][run_id].items()):
        tbl = catalog_dir / rel
        if not (tbl / "metadata").exists():
            raise IcebergExportError(
                f"run {run_id!r}: model {model!r} Iceberg table {tbl} missing "
                "metadata (catalog is incomplete)")
        try:
            rows = con.execute(
                f"SELECT count(*) FROM iceberg_scan('{tbl}')").fetchone()[0]
        except Exception as e:
            raise IcebergExportError(
                f"run {run_id!r}: model {model!r} unreadable via iceberg_scan "
                f"({tbl}): {e}") from None
        out["models"][model] = str(rel)
        out["rows"][model] = rows
    return out


def rollback_run(catalog_dir: Path, run_id: str) -> Dict[str, Any]:
    """Repoint the catalog's live run (`default`) to a previously exported run.

    Physical data is immutable; rollback only repoints the manifest, so it
    never re-executes and is immune to source changes since the run. Fail-loud
    if the run isn't in the catalog or its Iceberg metadata is gone (rollback
    must never invent data).
    """
    mf = _require_run(catalog_dir, run_id)
    for model, rel in mf["runs"][run_id].items():
        if not (catalog_dir / rel / "metadata").exists():
            raise IcebergExportError(
                f"run {run_id!r}: model {model!r} Iceberg table {rel} has no "
                "metadata (was it collected by gc?) — rollback refuses")
    return _atomic_write_manifest(catalog_dir, mf["runs"], run_id)


def gc_catalog(catalog_dir: Path, keep: int = 2, keep_days: Optional[float] = None,
               apply: bool = False) -> Dict[str, Any]:
    """Snapshot retention over the Iceberg catalog.

    Protected: the last `keep` runs, the `default` (live) run, and runs
    younger than `keep_days`. History is never pruned, so a collected run's
    rollback/verify fails loud instead of reading wrong data (the physical
    dir is gone, which _require_run catches). Returns the plan regardless of
    `apply`; `apply=True` drops the retired dirs and rewrites the manifest.
    """
    mf = load_manifest(catalog_dir)
    if mf is None:
        raise IcebergExportError(f"catalog {catalog_dir} has no manifest (nothing to gc)")
    runs = mf["runs"]
    keep_runs = []
    drop_runs = []
    protected = set()
    default = mf.get("default")
    if default and default in runs:
        protected.add(default)
    # Recency is tracked by the run dirs' mtime (manifest is written with
    # sort_keys, so dict insertion order is NOT preserved on disk).
    def _mtime(rid: str) -> float:
        d = catalog_dir / "runs" / rid
        return d.stat().st_mtime_ns if d.exists() else -1.0
    newest = sorted(runs, key=_mtime, reverse=True)[:keep] if keep > 0 else []
    protected.update(newest)
    now = None
    if keep_days is not None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(days=keep_days)
        for rid, _entry in runs.items():
            d = catalog_dir / "runs" / rid
            if d.exists():
                import datetime as _dt
                mtime = _dt.datetime.fromtimestamp(d.stat().st_mtime,
                                                   datetime.timezone.utc)
                if mtime >= cutoff:
                    protected.add(rid)
    for rid in runs:
        if rid in protected:
            keep_runs.append(rid)
        else:
            drop_runs.append(rid)
    drop_dirs = [str(catalog_dir / "runs" / rid) for rid in drop_runs]
    plan = {
        "keep_runs": keep_runs,
        "drop_runs": drop_runs,
        "drop_dirs": drop_dirs,
        "applied": False,
    }
    if apply and drop_runs:
        for rid in drop_runs:
            shutil.rmtree(catalog_dir / "runs" / rid, ignore_errors=True)
            runs.pop(rid, None)
        _atomic_write_manifest(catalog_dir, runs, default)
        plan["applied"] = True
    return plan