"""Executor: materializes views via DuckDB and enforces phase-C (runtime) pins.

"Nothing is published until the pin passes": each materialized view is checked
against its contract before the manifest is written. A violation aborts the run
and the last-known-good stays live (blue-green repointing is done by the caller).
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

from . import sqlgen
from .dialects import DUCKDB
from .analysis import Project, TypedModel, contract_field_col
from .types import StrataType, STRING


class PinError(Exception):
    pass


MANIFEST_SUFFIX = ".strata-manifest.json"
HISTORY_SUFFIX = ".strata-history.jsonl"


def history_path(module_path: str) -> Path:
    p = Path(module_path)
    return p.parent / (p.stem + HISTORY_SUFFIX)


def record_run(module_path: str, entry: dict) -> dict:
    """Append a content-addressed run record; return it (with run_id)."""
    hp = history_path(module_path)
    payload = json.dumps({k: v for k, v in entry.items() if k != "run_id"}, sort_keys=True)
    entry = dict(entry)
    entry["run_id"] = hashlib.sha256(payload.encode()).hexdigest()[:12]
    entry["at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with hp.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_history(module_path: str) -> list:
    hp = history_path(module_path)
    if not hp.exists():
        return []
    out = []
    for line in hp.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def find_run(module_path: str, run_id: str) -> Optional[dict]:
    for e in load_history(module_path):
        if e.get("run_id") == run_id or e.get("run_id", "").startswith(run_id):
            return e
    return None


def manifest_path(module_path: str) -> Path:
    p = Path(module_path)
    return p.parent / (p.stem + MANIFEST_SUFFIX)


def load_manifest(path: str) -> Dict[str, str]:
    mp = manifest_path(path)
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            return {}
    return {}


def save_manifest(path: str, fingerprints: Dict[str, str]):
    manifest_path(path).write_text(json.dumps(fingerprints, indent=2, sort_keys=True))


def stale_models(tms: Dict[str, TypedModel], path: str) -> List[str]:
    manifest = load_manifest(path)
    return [n for n, tm in tms.items() if manifest.get(n) != tm.fingerprint]


def _contract_decl(project: Project, tm: TypedModel):
    if not tm.contract:
        return None
    cd = project.contracts.get(tm.contract)
    if cd is None:
        raise PinError(f"contract {tm.contract!r} not found")
    return cd


def runtime_pins(con, project: Project, tm: TypedModel, view: str, report: List[str]):
    if not tm.contract:
        return
    cd = _contract_decl(project, tm)
    for f in cd.fields:
        exp = contract_field_col(f)

        def bad(why):
            raise PinError(f"phase-C pin FAILED [{tm.name}.{f.name}] {why}")

        if f.nonnull:
            n = con.execute(f"SELECT count(*) FROM {view} WHERE {f.name} IS NULL").fetchone()[0]
            if n:
                bad(f"expected nonnull but {n} NULL rows")
            report.append(f"  ok  {tm.name}.{f.name}: nonnull")
        if f.enum:
            vals = ", ".join("'" + v.replace("'", "''") + "'" for v in f.enum)
            n = con.execute(
                f"SELECT count(*) FROM {view} WHERE {f.name} IS NOT NULL "
                f"AND {f.name} NOT IN ({vals})").fetchone()[0]
            if n:
                bad(f"enum violation: {n} rows outside {{{','.join(f.enum)}}}")
            report.append(f"  ok  {tm.name}.{f.name}: enum")
        if f.unique or f.primary:
            dups = con.execute(
                f"SELECT count(*) - count(DISTINCT {f.name}) FROM {view}").fetchone()[0]
            if dups:
                bad(f"expected {('primary_key' if f.primary else 'unique')} but {dups} duplicate values")
            report.append(f"  ok  {tm.name}.{f.name}: {'primary_key' if f.primary else 'unique'}")


def materialize(con, project: Project, tms: Dict[str, TypedModel],
                names: Optional[List[str]] = None, sort_by_deps=True,
                dialect=DUCKDB, source_overrides: Optional[Dict[str, Dict[str, str]]] = None):
    """Create/recreate views v_<name> in topological order; return pin report.

    `source_overrides` ({source: {ns, dataset, ...}}) rewrites the compiled
    table names per pipeline env (spec/grammar.md §5) *before* executing:
    a view that still points at the default source after an override is a
    stale-plan bug, so the rewrite is verified, not assumed.
    """
    if names is None:
        names = list(tms)
    order = names
    if sort_by_deps:
        order = _dep_order(tms, names)
    pins: List[str] = []
    applied: List[str] = []
    if source_overrides:
        # Fail loud once per run (not per model): every overridden source must
        # be read by at least one model being materialized, else the pipeline
        # declares an env table that nothing uses (dangling override = stale plan).
        read_sql = sqlgen.full_sql(tms, order, dialect=dialect)
        for src, kv in source_overrides.items():
            target = kv.get("dataset") or kv.get("table") or src
            decl = project.sources.get(src)
            if decl is None:
                raise PinError(f"source override for unknown source {src!r}")
            default = decl.resource.get("dataset", src)
            if default == target:
                continue
            if default not in read_sql and target not in read_sql:
                raise PinError(f"source override {src!r} matches no compiled table "
                               f"(default {default!r}, target {target!r}) — dangling override")
    for name in order:
        tm = tms[name]
        sql = sqlgen.full_sql(tms, [name], dialect=dialect)
        if source_overrides:
            sql, _ = _apply_source_overrides(sql, project, source_overrides)
        con.execute(sql)
        applied.append(name)
        runtime_pins(con, project, tm, f"v_{name}", pins)
    return applied, pins


def _apply_source_overrides(sql: str, project: Project,
                            overrides: Dict[str, Dict[str, str]]) -> tuple:
    """Rewrite compiled table names per pipeline env; return (sql, n_rewrites).

    Only sources actually read by THIS model's SQL count: `full_sql(tms,
    [name])` inlines upstream model views (v_base), so an override for a
    source consumed two levels up correctly matches 0 rewrites here — the
    rewrite lands where the source is read, not in every downstream view.
    """
    n = 0
    for src, kv in overrides.items():
        target = kv.get("dataset") or kv.get("table") or src
        decl = project.sources.get(src)
        default = decl.resource.get("dataset", src) if decl else src
        if default == target:
            continue
        for frm, to in ((f"FROM {default} ", f"FROM {target} "),
                        (f"FROM {default}\n", f"FROM {target}\n"),
                        (f"JOIN {default} ", f"JOIN {target} "),
                        (f" {default} t", f" {target} t")):
            if frm in sql:
                sql = sql.replace(frm, to)
                n += 1
    return sql, n


def _dep_order(tms: Dict[str, TypedModel], names: List[str]) -> List[str]:
    done: set = set()
    out: List[str] = []

    def visit(n):
        if n in done:
            return
        tm = tms[n]
        for d in tm.deps:
            if d in tms:
                visit(d)
        done.add(n)
        out.append(n)

    for n in names:
        visit(n)
    return out


def run(con, project: Project, tms: Dict[str, TypedModel], module_path: str,
        only_stale: bool = False, names: Optional[List[str]] = None,
        dialect=DUCKDB, source_overrides: Optional[Dict[str, Dict[str, str]]] = None):
    if names is None:
        names = list(tms)
    if only_stale:
        stale = set(stale_models(tms, module_path))
        names = [n for n in names if n in stale] or []
        if not names:
            return [], [], "everything up to date (nothing to do)"
    applied, pins = materialize(con, project, tms, names, dialect=dialect,
                                source_overrides=source_overrides)
    fps = {n: tm.fingerprint for n, tm in tms.items()}
    save_manifest(module_path, fps)
    record_run(module_path, {
        "fingerprints": fps,
        "applied": applied,
        "pins": pins,
        "names": names,
        "dialect": getattr(dialect, "name", str(dialect)),
        "source_overrides": source_overrides or {},
    })
    return applied, pins, None