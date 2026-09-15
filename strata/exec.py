"""Executor: transactional staging + atomic swap via DuckDB (Fase 3).

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


BRANCH_SEP = "__"
PROMOTED_PREFIX = "v_"
STAGED_PREFIX = "stg_"


def staged_name(name: str, branch: str = "main") -> str:
    """Staging view per model+branch: stg_<branch>__<model>."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in branch)
    return f"{STAGED_PREFIX}{safe}{BRANCH_SEP}{name}"


def promoted_name(name: str) -> str:
    return f"{PROMOTED_PREFIX}{name}"


def list_branches(con) -> List[str]:
    try:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()
    except Exception:
        return ["main"]
    branches = set()
    for (tn,) in rows:
        if tn.startswith(STAGED_PREFIX) and BRANCH_SEP in tn:
            rest = tn[len(STAGED_PREFIX):]
            branches.add(rest.split(BRANCH_SEP, 1)[0])
    return sorted(branches) or ["main"]


def materialize(con, project: Project, tms: Dict[str, TypedModel],
                names: Optional[List[str]] = None, sort_by_deps=True,
                dialect=DUCKDB, source_overrides: Optional[Dict[str, Dict[str, str]]] = None,
                branch: str = "main", stage_only: bool = False):
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
    staged_prefix = STAGED_PREFIX + branch + BRANCH_SEP
    for name in order:
        tm = tms[name]
        # Layered read: live v_* for upstreams already promoted, staged for
        # upstreams built in THIS run (never a half-promoted mix).
        sql_live = sqlgen.full_sql(tms, [name], dialect=dialect,
                                   view_prefix=staged_prefix,
                                   upstream_prefix=PROMOTED_PREFIX)
        sql_staged = sqlgen.full_sql(tms, [name], dialect=dialect,
                                     view_prefix=staged_prefix,
                                     upstream_prefix=staged_prefix)
        sql = sql_live
        for d in tm.deps:
            if d in order:
                sql = sql_staged
                break
        if source_overrides:
            sql, _ = _apply_source_overrides(sql, project, source_overrides)
        for stmt in sql.split(";\n"):
            if stmt.strip():
                con.execute(stmt)
        applied.append(name)
        runtime_pins(con, project, tm, staged_name(name, branch), pins)
    if not stage_only:
        swap_branch(con, order, branch)
    return applied, pins


def swap_branch(con, names: List[str], branch: str = "main") -> List[str]:
    """Atomic promote: staged stg_<branch>__<m> -> live v_<m>.

    Last-known-good stays queryable until every staged view exists; the swap
    itself is one transaction (CREATE OR REPLACE VIEW per model). Returns
    the promoted view names.
    """
    done: List[str] = []
    con.execute("BEGIN TRANSACTION")
    try:
        for name in names:
            stg = staged_name(name, branch)
            live = promoted_name(name)
            con.execute(f"CREATE OR REPLACE VIEW {live} AS SELECT * FROM {stg}")
            done.append(live)
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return done


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
        dialect=DUCKDB, source_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        branch: str = "main", stage_only: bool = False):
    if names is None:
        names = list(tms)
    if only_stale:
        stale = set(stale_models(tms, module_path))
        names = [n for n in names if n in stale] or []
        if not names:
            return [], [], "everything up to date (nothing to do)"
    applied, pins = materialize(con, project, tms, names, dialect=dialect,
                                source_overrides=source_overrides, branch=branch,
                                stage_only=stage_only)
    fps = {n: tm.fingerprint for n, tm in tms.items()}
    save_manifest(module_path, fps)
    record_run(module_path, {
        "fingerprints": fps,
        "applied": applied,
        "pins": pins,
        "names": names,
        "dialect": getattr(dialect, "name", str(dialect)),
        "source_overrides": source_overrides or {},
        "branch": branch,
    })
    return applied, pins, None


def verify_run(module_path: str, run_id: str) -> dict:
    """Replay WITHOUT re-execution: the record is valid iff every pinned
    fingerprint still matches a freshly typechecked model (content-addressed
    stability proof). Raises PinError otherwise."""
    e = find_run(module_path, run_id)
    if e is None:
        raise PinError(f"unknown run {run_id!r} (see strata replay)")
    from .parser import parse_strata as _parse
    from .analysis import Checker as _Checker, Project as _Project
    src = Path(module_path).read_text()
    proj = _Project(_parse(src, module_path))
    _Checker(proj).check_all()
    fps = e.get("fingerprints", {})
    for name, fp in fps.items():
        tm = proj.typed.get(name)
        if tm is None:
            raise PinError(f"verify FAILED: model {name!r} gone since run {e['run_id']}")
        if tm.fingerprint != fp:
            raise PinError(f"verify FAILED: {name} changed since run {e['run_id']} "
                           f"({fp[:8]} -> {tm.fingerprint[:8]})")
    return e