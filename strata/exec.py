"""Executor: materializes views via DuckDB and enforces phase-C (runtime) pins.

"Nothing is published until the pin passes": each materialized view is checked
against its contract before the manifest is written. A violation aborts the run
and the last-known-good stays live (blue-green repointing is done by the caller).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from . import sqlgen
from .dialects import DUCKDB
from .analysis import Project, TypedModel, contract_field_col
from .types import StrataType, STRING


class PinError(Exception):
    pass


MANIFEST_SUFFIX = ".strata-manifest.json"


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
                dialect=DUCKDB):
    """Create/recreate views v_<name> in topological order; return pin report."""
    if names is None:
        names = list(tms)
    order = names
    if sort_by_deps:
        order = _dep_order(tms, names)
    pins: List[str] = []
    applied: List[str] = []
    for name in order:
        tm = tms[name]
        sql = sqlgen.full_sql(tms, [name], dialect=dialect)
        con.execute(sql)
        applied.append(name)
        runtime_pins(con, project, tm, f"v_{name}", pins)
    return applied, pins


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
        dialect=DUCKDB):
    if names is None:
        names = list(tms)
    if only_stale:
        stale = set(stale_models(tms, module_path))
        names = [n for n in names if n in stale] or []
        if not names:
            return [], [], "everything up to date (nothing to do)"
    applied, pins = materialize(con, project, tms, names, dialect=dialect)
    save_manifest(module_path, {n: tm.fingerprint for n, tm in tms.items()})
    return applied, pins, None