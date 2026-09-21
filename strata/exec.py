"""Executor: transactional staging + atomic swap via DuckDB (Fase 3).

"Nothing is published until the pin passes": each materialized view is checked
against its contract before the manifest is written. A violation aborts the run
and the last-known-good stays live (blue-green repointing is done by the caller).
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import ast
from . import sqlgen
from . import dbcompat
from .dialects import DUCKDB, get_dialect, physical_type
from .analysis import Project, TypedModel, StrataError, contract_field_col
from .types import StrataType, STRING
from .observability import MetricsCollector, PrometheusExporter


class PinError(Exception):
    pass


# Module-level metrics collector (initialized on first run).
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics() -> Optional[MetricsCollector]:
    """Return the metrics collector from the last run, or None."""
    return _metrics_collector


def parse_freshness_threshold(freshness: str) -> Optional[datetime.timedelta]:
    """Parse freshness spec to timedelta threshold.

    Supported formats:
    - 'incremental': None (handled separately, not time-based)
    - '1h', '24h': hours
    - '1d', '7d': days
    - 'daily': 24 hours
    - 'weekly': 7 days
    - 'monthly': 30 days
    - Custom expressions (e.g., "now() - interval '1 day'"): returns CUSTOM_MARKER
    """
    if freshness == "incremental":
        return None  # Not time-based, handled by source staleness

    # Named periods
    named_periods = {
        "daily": datetime.timedelta(hours=24),
        "weekly": datetime.timedelta(days=7),
        "monthly": datetime.timedelta(days=30),
    }
    if freshness in named_periods:
        return named_periods[freshness]

    # Numeric + unit: 1h, 24h, 1d, 7d
    m = re.match(r"^(\d+)([hdw])$", freshness)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            return datetime.timedelta(hours=num)
        elif unit == "d":
            return datetime.timedelta(days=num)
        elif unit == "w":
            return datetime.timedelta(weeks=num)

    # Custom expression (contains SQL-like syntax)
    if "(" in freshness or "interval" in freshness.lower():
        return CUSTOM_FRESHNESS_MARKER  # Needs warehouse evaluation

    return None  # Unknown format, skip time-based staleness


# Sentinel value indicating a custom freshness expression that needs
# to be evaluated by the warehouse (not a simple timedelta).
CUSTOM_FRESHNESS_MARKER = datetime.timedelta(days=-1)


MANIFEST_SUFFIX = ".strata-manifest.json"
HISTORY_SUFFIX = ".strata-history.jsonl"
LOCK_SUFFIX = ".strata-lock"
ID_SUFFIX = ".strata-id"


def history_path(module_path: str) -> Path:
    """Path to the module's run-history sidecar (`.strata-history.jsonl`)."""
    p = Path(module_path)
    return p.parent / (p.stem + HISTORY_SUFFIX)


def _lock_path(module_path: str) -> Path:
    """Path to the module's writer-lock sidecar (`.strata-lock`)."""
    p = Path(module_path)
    return p.parent / (p.stem + LOCK_SUFFIX)


def _id_path(module_path: str) -> Path:
    """Path to the module's stable-identity sidecar (`.strata-id`)."""
    p = Path(module_path)
    return p.parent / (p.stem + ID_SUFFIX)


def _module_id(module_path: str) -> str:
    """Stable identity for this module's warehouse-side bookkeeping
    (strata_commits.module_path), independent of its absolute path — so
    moving the module together with its sidecars (history/manifest/lock/
    id) to a new location, e.g. a CI checkout at a fresh path every run,
    keeps pending-commit recovery working. Generated once and persisted
    alongside the other sidecars.

    Known, accepted gap: pending_commit_runs/recover_metadata can be
    called outside _module_lock (e.g. gc_plan called directly, not through
    gc_snapshots), so the very first creation of this file for a brand new
    module has a narrow race if two processes touch it at once. Not worth
    forcing a read-only function like gc_plan to take an exclusive lock to
    close a window this narrow."""
    ip = _id_path(module_path)
    if ip.exists():
        return ip.read_text().strip()
    token = uuid.uuid4().hex
    _atomic_write(ip, token)
    return token


@contextlib.contextmanager
def _module_lock(module_path: str):
    """Mutual exclusion across PROCESSES for anything that reads or writes
    this module's history/manifest: without it, `record_run`'s read-modify-
    write (read the whole history, append a line, overwrite) loses another
    writer's entry outright if it races with a concurrent one — DuckDB
    hides this by accident (opening the same .duckdb file twice normally
    just fails), Postgres does not.

    Coarse-grained on purpose: held for the WHOLE operation (run/replay/gc/
    rollback), not just the metadata write. The goal is one writer in
    flight per module, not fine-grained per-row locking — and holding it
    end to end is also what closes gc_snapshots' own plan-then-apply
    window for free (a concurrent run() blocks on this same lock instead
    of publishing a run gc's plan never saw).

    Discipline: only the four top-level entry points (run, execute_run,
    gc_snapshots, rollback_to_run) acquire this. record_run/save_manifest/
    recover_metadata never do — they always run already inside one of
    those, and flock is not reentrant across separate os.open() calls in
    the same process, so a nested acquire would deadlock the process
    against itself. POSIX advisory lock: released automatically if the
    holding process dies, so a crash never leaves an orphaned lock; not
    supported on Windows, consistent with the rest of this prototype."""
    fd = os.open(str(_lock_path(module_path)), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _run_id(entry: dict) -> str:
    """Content-addressed run identity. Excludes 'run_id' itself and the
    derived/phase fields (snapshot names are derived from the id; the
    pending/complete phase is not part of identity)."""
    payload = json.dumps(
        {k: v for k, v in entry.items()
         if k not in ("run_id", "snapshots", "input_snapshots", "status")},
        sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _atomic_write(path: Path, text: str):
    """Replace a metadata file only after its complete contents are durable.

    Single-writer prototype; concurrent filesystem writers are not supported.
    """
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record_run(module_path: str, entry: dict, run_id: str = None) -> dict:
    """Append a content-addressed run record; return it (with run_id)."""
    hp = history_path(module_path)
    entry = dict(entry)
    entry["run_id"] = run_id or _run_id(entry)
    entry["at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _atomic_write(hp, hp.read_text() + json.dumps(entry, sort_keys=True) + "\n"
                  if hp.exists() else json.dumps(entry, sort_keys=True) + "\n")
    return entry


def load_history(module_path: str, lenient: bool = False) -> list:
    """Reads the append-only history newest-last.

    Strictness matters here: a malformed line means unreadable records after
    it, which would silently hide runs from `replay`/`rollback`. Fail loud
    (RuntimeError) by default; lenient=True skips bad lines (recovery only)."""
    hp = history_path(module_path)
    if not hp.exists():
        return []
    out = []
    for lineno, line in enumerate(hp.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception as exc:
            if lenient:
                continue
            raise RuntimeError(
                f"unreadable run history at {hp}: line {lineno}: {exc}") from exc
    return out


def find_run(module_path: str, run_id: str) -> Optional[dict]:
    """Resolve a (possibly prefix) run_id to its history entry, or None.

    Prefers the latest non-pending phase: a pending record was superseded by
    its completion (or abandoned by recovery)."""
    matches = [e for e in load_history(module_path)
               if e.get("run_id", "").startswith(run_id)]
    # Prefer the latest non-pending phase: a pending record was superseded by
    # its completion (or abandoned by recovery).
    for e in reversed(matches):
        if e.get("status") != "pending":
            return e
    return matches[-1] if matches else None


def manifest_path(module_path: str) -> Path:
    """Path to the module's manifest sidecar (`.strata-manifest.json`)."""
    p = Path(module_path)
    return p.parent / (p.stem + MANIFEST_SUFFIX)


def load_manifest(path: str) -> Dict[str, str]:
    """Load the module's manifest as {model_name: fingerprint}; {} if absent/corrupt."""
    mp = manifest_path(path)
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            return {}
    return {}


def save_manifest(path: str, fingerprints: Dict[str, str]):
    """Atomically persist the module's {model_name: fingerprint} manifest."""
    _atomic_write(manifest_path(path), json.dumps(fingerprints, indent=2, sort_keys=True))


def stale_models(tms: Dict[str, TypedModel], path: str) -> List[str]:
    """Models whose fingerprint differs from the persisted manifest (code-changed)."""
    manifest = load_manifest(path)
    return [n for n, tm in tms.items() if manifest.get(n) != tm.fingerprint]


def _contract_decl(project: Project, tm: TypedModel):
    """Resolve a model's contract declaration, or None; raises PinError if missing."""
    if not tm.contract:
        return None
    cd = project.contracts.get(tm.contract)
    if cd is None:
        raise PinError(f"contract {tm.contract!r} not found")
    return cd


def physical_schema(con, view: str) -> dict:
    """Actual physical schema of a staged view/table: {column: storage type}.

    Phase-C input: the compiled plan is trusted, the warehouse is not. An
    upstream table that drifted behind the declared source schema must be
    caught here, before anything is published. Dialect-aware via
    dbcompat.physical_schema (DuckDB and Postgres report/spell types
    differently — see that module for what was actually measured)."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", view):
        raise PinError(f"physical schema check FAILED: unsafe view name {view!r}")
    out = dbcompat.physical_schema(con, view)
    if not out:
        raise PinError(
            f"physical schema check FAILED: view {view!r} has no columns (does it exist?)")
    return out


def _physical_types(con, t: StrataType) -> set:
    """Acceptable warehouse storage types for a declared Strata type,
    dialect-aware via the connection in use. See dbcompat.physical_types."""
    return dbcompat.physical_types(con, t)


def check_join_cardinality(con, project: Project, tm: TypedModel, order,
                           branch: str, source_overrides, pins: List[str]):
    """Enforce `expect many_to_one|one_to_one` join annotations at
    materialize time: count duplicate equi-join key groups on the upstream
    tables (right side always; left side too for one_to_one). A violation
    aborts the run like a pin failure, leaving last-known-good live."""
    if not any(j.expect for j in tm.plan.joins):
        return
    # Same upstream resolution as the model's own SQL: staged views for
    # upstreams built in this run, live views otherwise.
    staged = any(d in order for d in tm.deps)
    staged_prefix = STAGED_PREFIX + branch + BRANCH_SEP
    prefix = staged_prefix if staged else PROMOTED_PREFIX
    for j in tm.plan.joins:
        if not j.expect:
            continue
        sides = [(j.index, j.right_keys)]
        if j.expect == "one_to_one":
            sides.append((0, j.left_keys))
        for idx, keys in sides:
            inp = tm.plan.inputs[idx]
            table = f"{prefix}{inp.node}" if not inp.is_source else inp.node
            sql = sqlgen.join_check_sql(table, keys)
            if source_overrides:
                sql, _ = _apply_source_overrides(sql, project, source_overrides)
            n = con.execute(sql).fetchone()[0]
            if n:
                raise PinError(
                    f"join cardinality FAILED [{tm.name} {j.kind} {j.alias}]: "
                    f"expected {j.expect} but {table} has {n} duplicate "
                    f"key groups ({', '.join(keys)})")
            pins.append(f"  ok  {tm.name} {j.kind} {j.alias}: {j.expect} "
                        f"({table} unique on {', '.join(keys)})")


def runtime_pins(con, project: Project, tm: TypedModel, view: str, report: List[str]):
    """Phase-C runtime pins: verify the materialized view's physical schema
    against the model's declared contract. Raises PinError on any mismatch
    and appends one `ok` line per field to `report`."""
    if not tm.contract:
        return
    cd = _contract_decl(project, tm)
    physical = physical_schema(con, view)
    for f in cd.fields:
        exp = contract_field_col(f, project.domain_types)

        def bad(why):
            raise PinError(f"phase-C pin FAILED [{tm.name}.{f.name}] {why}")

        if f.name not in physical:
            bad("column missing from materialized schema")
        allowed = _physical_types(con, exp.t)
        if not allowed:
            bad(f"no physical type check defined for contract type {exp.t}")
        if physical[f.name] not in allowed:
            bad(f"physical type {physical[f.name]!r} incompatible with contract {exp.t}")
        report.append(f"  ok  {tm.name}.{f.name}: {physical[f.name]} (schema)")

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

    # §2 warehouse semantics: freshness validation
    if tm.plan.freshness and tm.plan.partition_by:
        for p_expr in tm.plan.partition_by:
            if hasattr(p_expr, 'name'):
                col_name = p_expr.name
                if col_name not in physical:
                    raise PinError(
                        f"phase-C pin FAILED [{tm.name}] freshness "
                        f"partition_by column {col_name!r} not in materialized schema")
                report.append(f"  ok  {tm.name}.{col_name}: freshness partition_by present")
            elif hasattr(p_expr, 'items'):
                for item in p_expr.items:
                    if hasattr(item, 'name'):
                        if item.name not in physical:
                            raise PinError(
                                f"phase-C pin FAILED [{tm.name}] freshness "
                                f"partition_by column {item.name!r} not in materialized schema")
                        report.append(f"  ok  {tm.name}.{item.name}: freshness partition_by present")


def check_physical_schema(dialect: str, tms: Dict[str, TypedModel]) -> Dict[str, List[str]]:
    """Validate that every declared column type is expressible in dialect.

    No execution: reads `TypedModel.schema` and the `Dialect` type map.
    Returns {model: [unsupported_type, ...]} (empty = all green).
    Raises ValueError if the dialect itself is unknown.
    """
    d = get_dialect(dialect)
    out: Dict[str, List[str]] = {}
    for name, tm in tms.items():
        bad: List[str] = []
        for col_name, col in tm.schema.items():
            try:
                physical_type(d, col.t)
            except ValueError as e:
                bad.append(f"{col_name}: {e}")
        if bad:
            out[name] = bad
    return out


BRANCH_SEP = "__"
PROMOTED_PREFIX = "v_"
STAGED_PREFIX = "stg_"


def staged_name(name: str, branch: str = "main") -> str:
    """Staging view per model+branch: stg_<branch>__<model>."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in branch)
    return f"{STAGED_PREFIX}{safe}{BRANCH_SEP}{name}"


def promoted_name(name: str) -> str:
    """Live view name for a model: `v_<name>`."""
    return f"{PROMOTED_PREFIX}{name}"


def list_branches(con) -> List[str]:
    """Branches present in the warehouse (staged view prefixes), or `["main"]`."""
    try:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = ?", [dbcompat.db_schema(con)]).fetchall()
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
                branch: str = "main", stage_only: bool = False,
                run_id: Optional[str] = None, manage_transaction: bool = True):
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
            default = src  # SQL codegen emits the logical source name.
            if default == target:
                continue
            if default not in read_sql and target not in read_sql:
                raise PinError(f"source override {src!r} matches no compiled table "
                               f"(default {default!r}, target {target!r}) — dangling override")
    staged_prefix = STAGED_PREFIX + branch + BRANCH_SEP
    for name in order:
        tm = tms[name]
        plan = tm.plan
        is_merge = plan.incremental and plan.merge_strategy in ("append", "upsert")
        prev_snap = _current_snapshot_table(con, name) if is_merge else None
        # Pushdown eligibility: cdc_column must be resolvable inside the
        # base subquery's own scope (see _pushdown_base_expr), and no
        # join/set-op/expand may change what row each base row represents.
        # Outside that, correctness already holds via the post-filter path
        # below — this is strictly an opportunistic performance narrowing.
        pushdown_pred = None
        if prev_snap is not None and not plan.joins and plan.set_op is None \
                and plan.expand is None:
            base_expr = _pushdown_base_expr(plan, plan.cdc_column)
            if base_expr is not None:
                watermark = con.execute(
                    f"SELECT MAX({plan.cdc_column}) FROM {prev_snap}").fetchone()[0]
                if watermark is not None:
                    pushdown_pred = ast.BinOp(op=">", left=base_expr,
                                              right=ast.Literal(value=watermark))
                    plan.preds.append(pushdown_pred)
        try:
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
            prefix = f"CREATE OR REPLACE VIEW {staged_name(name, branch)} AS\n"
            if pushdown_pred is not None and sql.startswith(prefix):
                # cdc_column is already filtered inside `sql` itself (the
                # base subquery's own WHERE): this compiled query IS the
                # delta, so it never executes the pre-pushdown full scan.
                _apply_incremental_merge(con, tm, staged_name(name, branch),
                                         sql[len(prefix):], delta_is_prefiltered=True)
            else:
                for stmt in sql.split(";\n"):
                    if stmt.strip():
                        con.execute(stmt)
                if is_merge and prev_snap is not None and sql.startswith(prefix):
                    _apply_incremental_merge(con, tm, staged_name(name, branch),
                                             sql[len(prefix):])
        finally:
            if pushdown_pred is not None:
                plan.preds.remove(pushdown_pred)
        applied.append(name)
        runtime_pins(con, project, tm, staged_name(name, branch), pins)
        check_join_cardinality(con, project, tm, order, branch, source_overrides, pins)
    if not stage_only:
        # Publish into run-addressed snapshot TABLES (data frozen at this
        # run's moment) when the run identity is known; legacy staged-view
        # swap otherwise (standalone `strata test` path).
        if run_id is not None:
            def validate_snapshots():
                """Validate every published snapshot's pins and declarative tests."""
                for name in order:
                    runtime_pins(con, project, tms[name], snapshot_name(run_id, name), [])
                run_tests(con, project, tms, order, dialect, branch)
            publish_snapshots(con, order, run_id, branch, validate=validate_snapshots,
                              manage_transaction=manage_transaction)
        else:
            swap_branch(con, order, branch,
                       validate=lambda: run_tests(con, project, tms, names, dialect, branch))
    return applied, pins


def swap_branch(con, names: List[str], branch: str = "main", validate=None) -> List[str]:
    """Atomic promote: staged stg_<branch>__<m> -> live v_<m>.

    Last-known-good stays queryable until every staged view exists; the swap
    itself is one transaction (CREATE OR REPLACE VIEW per model). `validate`
    (if given) runs AFTER the repoint but before COMMIT, same contract as
    `publish_snapshots`' own `validate`: a failing declarative test rolls
    back the repoint too, instead of leaving already-live views that never
    passed their tests (this legacy path used to run tests only after the
    swap had already committed — no way back if one failed). Returns the
    promoted view names.
    """
    done: List[str] = []
    con.execute("BEGIN TRANSACTION")
    try:
        for name in names:
            stg = staged_name(name, branch)
            live = promoted_name(name)
            con.execute(f"CREATE OR REPLACE VIEW {live} AS SELECT * FROM {stg}")
            done.append(live)
        if validate is not None:
            validate()
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return done


SNAP_PREFIX = "snap_"


def snapshot_name(run_id: str, name: str) -> str:
    """Run-addressed snapshot TABLE: snap_<run_id>_<model>."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    return f"{SNAP_PREFIX}{run_id}_{safe}"


def _current_snapshot_table(con, name: str) -> Optional[str]:
    """The snap_<run_id>_<model> table the live view v_<model> currently
    reads from, or None if the model was never published (first/bootstrap
    run). `publish_snapshots` only ever writes `CREATE OR REPLACE VIEW
    {live} AS SELECT * FROM {snap}` (same pattern already trusted by
    `protected_runs`/`gc_plan`), so matching that exact shape in the live
    catalog SQL is as reliable as tracking it separately would be."""
    live = promoted_name(name)
    defn = dbcompat.live_view_defs(con).get(live)
    if not defn:
        return None
    m = re.search(re.escape(SNAP_PREFIX) + r"[0-9a-f]{12}_\w+", defn)
    return m.group(0) if m else None


def _apply_incremental_merge(con, tm: TypedModel, staged: str, select_sql: str,
                             delta_is_prefiltered: bool = False) -> None:
    """Rewrite the just-recomputed staged view for an incremental
    append/upsert model so it publishes prev_snapshot merged with only the
    cdc_column-new slice, instead of the full recompute.

    `delta_is_prefiltered=False` (the general case: cdc_column depends on a
    join/set-op/expand, or isn't resolvable in the base subquery's scope —
    see `_pushdown_base_expr`): `select_sql` is the full, unfiltered
    recompute; this wraps it and filters by cdc_column here, after the
    fact. `delta_is_prefiltered=True` (the common case: cdc_column is a
    plain passthrough or a `let` already computed in the base subquery):
    the caller already compiled `select_sql` with the cdc_column watermark
    pushed into the base subquery's own WHERE (materialize()'s
    `plan.preds`), so `select_sql` already IS the delta — the source scan
    itself is smaller, not just the row count kept afterward.

    First run for this model (no live snapshot yet): a no-op, the full
    recompute already staged by the caller is the correct (and only
    possible) result. `merge_strategy: replace` is handled by never calling
    this at all (plain full rebuild, unchanged behavior)."""
    plan = tm.plan
    if not plan.incremental or plan.merge_strategy not in ("append", "upsert"):
        return
    prev_snap = _current_snapshot_table(con, tm.name)
    if prev_snap is None:
        return
    if delta_is_prefiltered:
        delta_cte = f"WITH __delta AS (\n{select_sql}\n)\n"
    else:
        cdc = plan.cdc_column
        watermark = f"(SELECT MAX({cdc}) FROM {prev_snap})"
        delta_filter = f"{watermark} IS NULL OR {cdc} > {watermark}"
        delta_cte = (f"WITH __full AS (\n{select_sql}\n),\n"
                     f"__delta AS (\n  SELECT * FROM __full WHERE {delta_filter}\n)\n")
    if plan.merge_strategy == "append":
        merge_body = f"SELECT * FROM {prev_snap}\nUNION ALL\nSELECT * FROM __delta"
    else:  # upsert: cdc-new rows replace any prior row sharing the same merge_keys
        keys = [k.name for k in plan.merge_keys]
        on = " AND ".join(f"__prev.{k} = __delta.{k}" for k in keys)
        merge_body = (
            f"SELECT __prev.* FROM {prev_snap} AS __prev\n"
            f"WHERE NOT EXISTS (SELECT 1 FROM __delta WHERE {on})\n"
            f"UNION ALL\nSELECT * FROM __delta"
        )
    con.execute(f"CREATE OR REPLACE VIEW {staged} AS\n{delta_cte}{merge_body}\n")


def _pushdown_base_expr(plan, name: str) -> Optional[ast.Node]:
    """The expression `name` already has in the base subquery's own scope
    (`sqlgen._base_select`) — a plain passthrough of the source's own
    column, or a `let` already computed there — or None if `name` is not
    resolvable at that level (only exists as a select/derive expression in
    the outer query, or depends on a join/set-op/expand). Reusing the
    expression `_base_select` already knows how to project is what makes
    pushing a `cdc_column > watermark` predicate into `plan.preds` safe:
    it is exactly the same shape `filter` already relies on."""
    for bc in plan.base_cols:
        if bc.name == name and bc.expr is not None:
            return bc.expr
    if name in plan.inputs[0].cols:
        return ast.ColumnRef(name=name)
    return None


def publish_snapshots(con, names: List[str], run_id: str,
                      branch: str = "main", validate=None,
                      manage_transaction: bool = True) -> List[str]:
    """Freeze a run's staged results into run-addressed TABLES and repoint the
    live views at them, in ONE transaction: either the whole run publishes or
    the last-known-good stays untouched. Unlike the staging views (which read
    through to mutable sources), a snapshot never changes after it is taken —
    that is what rollback restores."""
    if not re.match(r"^[0-9a-f]{12}$", run_id):
        raise PinError(f"unsafe run id for snapshot naming: {run_id!r}")
    done: List[str] = []
    if manage_transaction:
        con.execute("BEGIN TRANSACTION")
    try:
        for name in names:
            snap = snapshot_name(run_id, name)
            staged = staged_name(name, branch)
            exists = con.execute(
                "SELECT table_type FROM information_schema.tables "
                "WHERE table_schema = ? AND table_name=?",
                [dbcompat.db_schema(con), snap]).fetchone()
            if exists:
                if exists[0] != "BASE TABLE":
                    raise PinError(f"snapshot {snap!r} is not a table")
                if physical_schema(con, snap) != physical_schema(con, staged):
                    raise PinError(f"snapshot identity conflict: {snap}")
                different = con.execute(
                    f"SELECT EXISTS ((SELECT * FROM {snap} EXCEPT ALL SELECT * FROM {staged}) "
                    f"UNION ALL (SELECT * FROM {staged} EXCEPT ALL SELECT * FROM {snap}))"
                ).fetchone()[0]
                if different:
                    raise PinError(f"snapshot identity conflict: {snap}")
            else:
                con.execute(f"CREATE TABLE {snap} AS SELECT * FROM {staged}")
            con.execute(f"CREATE OR REPLACE VIEW {promoted_name(name)} AS "
                        f"SELECT * FROM {snap}")
            done.append(snap)
        if validate is not None:
            validate()
        if manage_transaction:
            con.execute("COMMIT")
    except Exception:
        if manage_transaction:
            con.execute("ROLLBACK")
        raise
    return done


def rollback_to_run(con, e: dict, module_path: Optional[str] = None) -> List[str]:
    """Public entry point: holds the module lock for the whole operation
    (see _module_lock) when `module_path` is given — no history file to
    protect without one."""
    lock = _module_lock(module_path) if module_path is not None else contextlib.nullcontext()
    with lock:
        return _rollback_to_run_locked(con, e, module_path)


def _rollback_to_run_locked(con, e: dict, module_path: Optional[str] = None) -> List[str]:
    """Repoint live views to the snapshot tables recorded by a past run.

    Snapshots are materialized tables, so rollback never re-executes and is
    immune to source changes since the run. Fail-loud if any snapshot table
    was dropped (rollback must never invent data)."""
    snaps = e.get("snapshots") or {}
    if not snaps:
        raise PinError(
            f"run {e.get('run_id', '?')!r} has no recorded snapshots "
            "(pre-snapshot history: use staged-view rollback)")
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ?", [dbcompat.db_schema(con)]).fetchall()}
    missing = sorted(t for t in snaps.values() if t not in have)
    if missing:
        raise PinError(
            f"snapshot table(s) missing: {', '.join(missing)} — rollback "
            "cannot repoint to data that does not exist (fail-loud)")
    done: List[str] = []
    con.execute("BEGIN TRANSACTION")
    try:
        for name, snap in sorted(snaps.items()):
            con.execute(f"CREATE OR REPLACE VIEW {promoted_name(name)} AS "
                        f"SELECT * FROM {snap}")
            done.append(promoted_name(name))
        if module_path is not None:
            event = dict(e, operation="rollback", rollback_of=e["run_id"])
            _record_commit(con, module_path, event, e["run_id"])
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    if module_path is not None:
        recover_metadata(con, module_path)
    return done


def source_fingerprints(con, project: Project, source_overrides=None) -> Dict[str, str]:
    """Hash schemas and sorted JSON rows of the effective source relations.

    Prototype implementation: a full scan/sort, including duplicate rows.
    Resolution matches SQL codegen (source name unless explicitly overridden).
    A missing source is an error, never a reusable 'missing' fingerprint.
    """
    out: Dict[str, str] = {}
    overrides = source_overrides or {}
    for name in sorted(project.sources):
        kv = overrides.get(name, {})
        table = kv.get("dataset") or kv.get("table") or name
        quoted = '"' + table.replace('"', '""') + '"'
        # DB-API `.description` (not DuckDB's `DESCRIBE`, Postgres has no
        # such statement) — works identically on both connection types and
        # is only ever hashed, never compared across dialects.
        desc = con.execute(f"SELECT * FROM {quoted} LIMIT 0").description
        schema = [(d[0], d[1]) for d in desc]
        digest = hashlib.sha256(json.dumps(schema, default=str).encode())
        # Cast to TEXT before ORDER BY: Postgres's plain `json` type (unlike
        # `jsonb`) has no ordering operator, so ordering the JSON value
        # itself fails there — measured against a real Postgres 16.
        rows = con.execute(f"SELECT to_json(t)::TEXT FROM {quoted} t ORDER BY 1")
        while batch := rows.fetchmany(1024):
            for (row,) in batch:
                data = row.encode()
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
        out[name] = digest.hexdigest()
    return out



_ROW_COUNT_OPS = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
}


def run_tests(con, project: Project, tms: Dict[str, TypedModel],
              names: Optional[List[str]] = None, dialect=DUCKDB,
              branch: str = "main") -> List[str]:
    """Run declarative tests against promoted live views, after the swap.

    Each ``test <model> { ... }`` block is evaluated against ``v_<model>``.
    Returns list of result lines (one per expect clause); raises
    ``StrataTestError`` on first failure.  Used by ``run`` (post-swap) and
    ``strata test`` (standalone).
    """
    if not project.tests:
        return []
    results: List[str] = []
    tested_models = set(names) if names else {
        td.model for tds in project.tests.values() for td in tds
    }
    for tds in project.tests.values():
        for td in tds:
            if td.model not in tested_models:
                continue
            tm = tms.get(td.model)
            if tm is None:
                raise StrataError(
                    f"test references model not in compiled set: {td.model!r}",
                    "E080",
                )
            view = promoted_name(td.model)
            for check in td.checks:
                if check.kind == "row_count":
                    got = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
                    expected = int(check.value)
                    if not _ROW_COUNT_OPS[check.op](got, expected):
                        raise StrataTestError(
                            f"test {td.model}: row_count {got} {check.op} "
                            f"{expected} is false"
                        )
                    results.append(
                        f"  ok  {td.model}: row_count {check.op} {expected} "
                        f"(got {got})"
                    )
                else:
                    col = tm.schema[check.col]
                    # SELECT count(*) WHERE NOT (col op lit) — nonzero means violation
                    sql = (
                        f"SELECT count(*) FROM {view} "
                        f"WHERE NOT ({check.col} {check.op} "
                        f"{sqlgen._lit(check.value)})"
                    )
                    n_bad = con.execute(sql).fetchone()[0]
                    total = con.execute(
                        f"SELECT count(*) FROM {view}"
                    ).fetchone()[0]
                    if n_bad:
                        raise StrataTestError(
                            f"test {td.model}: {check.col} {check.op} "
                            f"{check.value!r} violated for {n_bad}/{total} rows"
                        )
                    results.append(
                        f"  ok  {td.model}: {check.col} {check.op} "
                        f"{check.value!r}"
                    )
    return results


class StrataTestError(StrataError):
    """Raised when a declarative test assertion fails."""
    pass


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
        default = src  # Match gen_base_subquery, not source metadata.
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
    """Topologically sort `names` so every dependency precedes its dependents."""
    done: set = set()
    out: List[str] = []

    def visit(n):
        """Depth-first helper: append n after its (in-set) dependencies."""
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


COMMIT_REGISTRY = "strata_commits"


def _record_commit(con, module_path, entry, rid):
    """Transactional outbox: one event per publication, not per content id."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {COMMIT_REGISTRY} ("
                "event_id VARCHAR PRIMARY KEY, module_path VARCHAR, "
                # entry is a VARCHAR holding json.dumps() output, not a
                # native JSON/JSONB column: psycopg2 auto-decodes json/jsonb
                # columns into dicts on fetch, which would make the
                # json.loads(raw) call sites below double-decode on
                # Postgres. DuckDB has no such auto-decoding, so this keeps
                # both dialects on the identical (str in, str out) contract.
                "committed_at TIMESTAMP, entry VARCHAR, exported BOOLEAN)")
    event = uuid.uuid4().hex
    payload = dict(entry, run_id=rid, commit_id=event)
    con.execute(f"INSERT INTO {COMMIT_REGISTRY} VALUES (?, ?, now(), ?, false)",
                [event, _module_id(module_path), json.dumps(payload)])


def recover_metadata(con, module_path: str) -> list:
    """Export committed but unacknowledged events. Single writer required.

    A file replacement can succeed before acknowledgement fails: commit_id
    deduplicates that retry. Corrupt legacy history fails loudly. No registry
    is created by a read; legacy warehouses remain untouched.
    """
    exists = con.execute("SELECT count(*) FROM information_schema.tables "
                         "WHERE table_schema = ? AND table_name=?",
                         [dbcompat.db_schema(con), COMMIT_REGISTRY]).fetchone()[0]
    if not exists:
        return []
    pending = con.execute(
        f"SELECT event_id, entry FROM {COMMIT_REGISTRY} "
        "WHERE module_path=? AND NOT exported ORDER BY committed_at, event_id",
        [_module_id(module_path)]).fetchall()
    history = load_history(module_path)
    known = {e.get("commit_id") for e in history}
    recovered = []
    for event, raw in pending:
        entry = json.loads(raw)
        # Rollbacks reference existing runs; do not invent a new execution in
        # history. Their full manifest is already in the transactional outbox.
        if entry.get("operation") != "rollback" and event not in known:
            record_run(module_path, entry, run_id=entry["run_id"])
            known.add(event)
        # Do not undo an intentional rollback or another publication. Only
        # export the manifest if the recorded snapshots are still live.
        live = dbcompat.live_view_defs(con)
        if all(snap in (live.get(promoted_name(n)) or "")
               for n, snap in entry["snapshots"].items()):
            save_manifest(module_path, entry["fingerprints"])
        con.execute(f"UPDATE {COMMIT_REGISTRY} SET exported=true WHERE event_id=?", [event])
        recovered.append(event)
    return recovered


INPUT_PREFIX = "input_"
_RUN_TABLE_RE = re.compile(r"^(?:snap|input)_([0-9a-f]{12})_")


def run_tables(con) -> Dict[str, str]:
    """{snapshot table -> run id} over the engine's default-schema base tables."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ? AND table_type='BASE TABLE'",
        [dbcompat.db_schema(con)]).fetchall()
    out: Dict[str, str] = {}
    for (tn,) in rows:
        m = _RUN_TABLE_RE.match(tn)
        if m:
            out[tn] = m.group(1)
    return out


def pending_commit_runs(con, module_path: str) -> set:
    """Runs whose metadata export is still outstanding (never collectable)."""
    exists = con.execute("SELECT count(*) FROM information_schema.tables "
                         "WHERE table_schema = ? AND table_name=?",
                         [dbcompat.db_schema(con), COMMIT_REGISTRY]).fetchone()[0]
    if not exists:
        return set()
    rows = con.execute(f"SELECT entry FROM {COMMIT_REGISTRY} "
                       "WHERE module_path=? AND NOT exported",
                       [_module_id(module_path)]).fetchall()
    out = set()
    for (raw,) in rows:
        try:
            out.add(json.loads(raw)["run_id"])
        except Exception:
            raise PinError("corrupt pending commit entry (refusing to collect)")
    return out


def _recent_by_age(history: list, keep_days: Optional[float]) -> set:
    """Run ids with snapshots recorded within the last `keep_days` days.

    `entry["at"]` is the UTC ISO timestamp `record_run` stamps every history
    entry with; a run without one (only possible on a hand-edited or
    pre-timestamp history file) is never protected by age, only by `keep`
    or liveness — never silently kept forever by a missing/malformed field.
    """
    if keep_days is None:
        return set()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=keep_days)
    out = set()
    for e in history:
        if not e.get("snapshots") or not e.get("at"):
            continue
        try:
            at = datetime.datetime.fromisoformat(e["at"])
        except ValueError:
            continue
        if at >= cutoff:
            out.add(e["run_id"])
    return out


def protected_runs(con, module_path: str, keep: int,
                   keep_days: Optional[float] = None) -> set:
    """Run ids a GC must preserve: recent (by count and/or by age), live, or
    metadata-pending. `keep` and `keep_days` are independent floors — a run
    needs to satisfy only one to be protected, never both."""
    history = load_history(module_path)
    runs = [e["run_id"] for e in history if e.get("snapshots")]
    protected = set(runs[-keep:]) if keep > 0 else set()
    protected |= _recent_by_age(history, keep_days)
    live = list(dbcompat.live_view_defs(con).values())
    for tn, rid in run_tables(con).items():
        if any(tn in sql for sql in live):
            protected.add(rid)
    return protected | pending_commit_runs(con, module_path)


def gc_plan(con, module_path: str, keep: int = 2,
           keep_days: Optional[float] = None) -> dict:
    """Compute snapshot garbage WITHOUT changing the warehouse.

    Protected: the last `keep` runs with snapshots, every run with snapshots
    recorded within the last `keep_days` days (if given), every run
    referenced by a live view, and every publication whose metadata export
    is still pending. History is never pruned — a collected run stays on
    record, so rollback or replay against it fails loud instead of silently
    reading wrong data.
    """
    if keep < 0:
        raise PinError("keep must be >= 0")
    if keep_days is not None and keep_days < 0:
        raise PinError("keep_days must be >= 0")
    protected = protected_runs(con, module_path, keep, keep_days)
    live = list(dbcompat.live_view_defs(con).values())
    drop, keep_tables = [], []
    for tn, rid in sorted(run_tables(con).items()):
        if rid in protected or any(tn in sql for sql in live):
            keep_tables.append(tn)
        else:
            drop.append(tn)
    history = load_history(module_path)
    retired = sorted({e["run_id"] for e in history
                      if e.get("snapshots") and e["run_id"] not in protected
                      and all(s in drop for s in e["snapshots"].values())})
    return {"keep_runs": sorted(protected), "drop_tables": drop,
            "keep_tables": keep_tables, "retired_runs": retired,
            "keep_days": keep_days, "applied": False}


def gc_snapshots(con, module_path: str, keep: int = 2,
                 keep_days: Optional[float] = None,
                 apply: bool = False) -> dict:
    """Public entry point: holds the module lock for the whole operation
    (see _module_lock) — including the report-only path, so a report
    reflects a consistent snapshot instead of racing a concurrent run(),
    and so a concurrent run() can never publish a new run in the window
    between this plan and its apply."""
    with _module_lock(module_path):
        return _gc_snapshots_locked(con, module_path, keep, keep_days, apply)


def _gc_snapshots_locked(con, module_path: str, keep: int = 2,
                         keep_days: Optional[float] = None,
                         apply: bool = False) -> dict:
    """Report (default) or drop unreferenced snapshot tables, all-or-nothing."""
    plan = gc_plan(con, module_path, keep, keep_days)
    if not apply or not plan["drop_tables"]:
        return plan
    live = list(dbcompat.live_view_defs(con).values())
    con.execute("BEGIN TRANSACTION")
    try:
        for tn in plan["drop_tables"]:
            if any(tn in sql for sql in live):
                raise PinError(f"refusing to drop {tn!r}: referenced by a live view")
            con.execute(f"DROP TABLE {tn}")
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    plan["applied"] = True
    return plan


def _frozen_materialize(con, project, tms, entry, source_overrides=None,
                        expected_sources=None, module_path=None):
    """Capture inputs and publish outputs in the same DuckDB transaction.

    A complete publication event is committed with the snapshots. The caller
    exports filesystem metadata afterwards through recover_metadata.
    """
    con.execute("BEGIN TRANSACTION")
    try:
        if entry["dialect"] not in ("duckdb", "postgres"):
            raise PinError(
                f"frozen execution currently supports only DuckDB and "
                f"Postgres, not {entry['dialect']!r}")
        entry["source_fingerprints"] = source_fingerprints(con, project, source_overrides)
        if expected_sources is not None and entry["source_fingerprints"] != expected_sources:
            raise PinError("input snapshot content changed since recorded run")
        entry["snapshot_format"] = 2
        rid = _run_id(entry)
        inputs = {}
        for name in sorted(project.sources):
            # Separate namespace from output snapshots; hash avoids sanitized
            # source-name collisions.
            suffix = hashlib.sha256(name.encode()).hexdigest()[:16]
            snap = f"{INPUT_PREFIX}{rid}_{suffix}"
            kv = (source_overrides or {}).get(name, {})
            table = kv.get("dataset") or kv.get("table") or name
            quoted = '"' + table.replace('"', '""') + '"'
            con.execute(f"CREATE TABLE IF NOT EXISTS {snap} AS SELECT * FROM {quoted}")
            inputs[name] = snap
        frozen = {name: {"dataset": snap} for name, snap in inputs.items()}
        if source_fingerprints(con, project, frozen) != entry["source_fingerprints"]:
            raise PinError("input snapshot identity conflict")
        order = _dep_order(tms, entry["names"])
        used = {inp.node for n in order for inp in tms[n].plan.inputs if inp.is_source}
        applied, pins = materialize(
            con, project, tms, entry["names"],
            source_overrides={n: v for n, v in frozen.items() if n in used},
            branch=entry["branch"], run_id=rid, manage_transaction=False)
        entry["input_snapshots"] = inputs
        entry["snapshots"] = {n: snapshot_name(rid, n) for n in applied}
        entry["physical_schemas"] = {n: physical_schema(con, snap)
                                     for n, snap in entry["snapshots"].items()}
        entry["applied"] = applied
        entry["pins"] = pins
        if module_path is not None:
            _record_commit(con, module_path, entry, rid)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return applied, pins, rid


def _downstream_models(tms, seeds):
    """Models transitively downstream of the seed nodes (sources or models),
    following plan inputs and lineage origins (covers from/join/set-op
    uniformly, even for models with empty lineage)."""
    children = {}
    for m, tm in tms.items():
        for inp in tm.plan.inputs:
            children.setdefault(inp.node, set()).add(m)
        for origins in tm.lineage.values():
            for o in origins:
                children.setdefault(o.node, set()).add(m)
    seen, stack, out = set(), list(seeds), set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for ch in children.get(cur, ()):
            if ch not in seen:
                out.add(ch)
                stack.append(ch)
    return out


def run(con, project: Project, tms: Dict[str, TypedModel], module_path: str,
        only_stale: bool = False, names: Optional[List[str]] = None,
        dialect=DUCKDB, source_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        branch: str = "main", stage_only: bool = False,
        reason: Optional[str] = None, backfill_of: Optional[str] = None,
        freshness_override: Optional[str] = None):
    """Public entry point: holds the module lock for the whole operation
    (see _module_lock) around the actual implementation below."""
    with _module_lock(module_path):
        return _run_locked(con, project, tms, module_path, only_stale=only_stale,
                           names=names, dialect=dialect, source_overrides=source_overrides,
                           branch=branch, stage_only=stage_only, reason=reason,
                           backfill_of=backfill_of, freshness_override=freshness_override)


def _run_locked(con, project: Project, tms: Dict[str, TypedModel], module_path: str,
                only_stale: bool = False, names: Optional[List[str]] = None,
                dialect=DUCKDB, source_overrides: Optional[Dict[str, Dict[str, str]]] = None,
                branch: str = "main", stage_only: bool = False,
                reason: Optional[str] = None, backfill_of: Optional[str] = None,
                freshness_override: Optional[str] = None):
    """Body of `run()` while holding the module writer lock: computes staleness,
    materializes the selected models, records the run, and collects metrics."""
    global _metrics_collector
    _metrics_collector = MetricsCollector()
    start = datetime.datetime.now()
    if names is None:
        names = list(tms)
    for src in source_overrides or {}:
        if src not in project.sources:
            raise PinError(f"source override for unknown source {src!r}")
    if not stage_only:
        # Export any publication whose metadata write crashed last time
        # BEFORE reading history for staleness decisions.
        recover_metadata(con, module_path)
    source_fps = source_fingerprints(con, project, source_overrides)
    if only_stale:
        history = load_history(module_path)
        previous = next((e for e in reversed(history)
                         if e.get("snapshots") and e.get("branch") == branch), None)
        if previous is None:
            stale = set(tms)
        else:
            # Per-source staleness: only models downstream of changed sources
            # rebuild (plus code-changed models, transitive via fingerprints).
            # Override changes surface as hash changes since fingerprints
            # resolve through the overrides, so identical content still skips.
            prev_fps = previous.get("source_fingerprints", {})
            changed = {s for s, h in source_fps.items() if prev_fps.get(s) != h}
            changed |= {s for s in prev_fps if s not in source_fps}
            stale = set(stale_models(tms, module_path)) | _downstream_models(tms, changed)
        # §2 freshness-based staleness: check if data is older than any threshold
        if previous and "committed_at" in previous:
            prev_time = datetime.datetime.fromisoformat(previous["committed_at"])
            now = datetime.datetime.now()
            for name in names:
                tm = tms.get(name)
                if tm and tm.plan.freshness:
                    # Use freshness_override if specified, otherwise use model's freshness
                    freshness_specs = [freshness_override] if freshness_override else tm.plan.freshness
                    # Check each freshness threshold
                    for freshness_spec in freshness_specs:
                        threshold = parse_freshness_threshold(freshness_spec)
                        if threshold is not None:
                            # Handle custom freshness expressions
                            if threshold == CUSTOM_FRESHNESS_MARKER:
                                try:
                                    # Evaluate the custom expression against the warehouse
                                    view_name = promoted_name(name)
                                    result = con.execute(
                                        f"SELECT {freshness_spec} FROM {view_name} LIMIT 1"
                                    ).fetchone()
                                    if result and result[0] is not None:
                                        # Result should be a datetime or interval
                                        custom_threshold = result[0]
                                        if isinstance(custom_threshold, datetime.timedelta):
                                            threshold = custom_threshold
                                        elif isinstance(custom_threshold, datetime.datetime):
                                            # If result is a datetime, use it as the cutoff
                                            age = now - custom_threshold
                                            if age > datetime.timedelta(0):
                                                stale.add(name)
                                                break
                                        else:
                                            # Unknown type, skip this check
                                            continue
                                    else:
                                        # Expression returned NULL, skip this check
                                        continue
                                except Exception:
                                    # If we can't evaluate the expression, skip it
                                    continue
                            # If freshness_column is specified, check max value of that column
                            if tm.plan.freshness_column:
                                try:
                                    view_name = promoted_name(name)
                                    max_val = con.execute(
                                        f"SELECT MAX({tm.plan.freshness_column}) FROM {view_name}"
                                    ).fetchone()[0]
                                    if max_val is not None:
                                        # Convert to datetime if it's a string
                                        if isinstance(max_val, str):
                                            max_val = datetime.datetime.fromisoformat(max_val)
                                        # Check if the data is older than threshold
                                        if isinstance(max_val, datetime.datetime):
                                            age = now - max_val
                                            if age > threshold:
                                                stale.add(name)
                                                break  # No need to check other thresholds
                                        else:
                                            # Not a datetime column, use time-based staleness
                                            age = now - prev_time
                                            if age > threshold:
                                                stale.add(name)
                                                break
                                    else:
                                        # No data, mark as stale
                                        stale.add(name)
                                        break
                                except Exception:
                                    # If we can't check the column, fall back to time-based
                                    age = now - prev_time
                                    if age > threshold:
                                        stale.add(name)
                                        break
                            else:
                                # No freshness_column, use time-based staleness
                                age = now - prev_time
                                if age > threshold:
                                    stale.add(name)
                                    break  # No need to check other thresholds
        # Cascade staleness to downstream models
        # If a model is stale, all models that depend on it should also be stale
        freshness_stale = set(stale)  # Models stale due to freshness
        if freshness_stale:
            stale |= _downstream_models(tms, freshness_stale)
        # A rollback (or a different warehouse) may not expose the last run.
        live = dbcompat.live_view_defs(con)
        if previous:
            for name in names:
                snap = previous.get("snapshots", {}).get(name)
                if not snap or snap not in live.get(promoted_name(name), ""):
                    stale.add(name)
        # Exclude models with staleness_ok attribute from stale set
        for name in list(stale):
            tm = tms.get(name)
            if tm and tm.attrs.get("staleness_ok"):
                stale.discard(name)
        names = [n for n in names if n in stale]
        if not names:
            elapsed = (datetime.datetime.now() - start).total_seconds() * 1000
            try:
                n_rows = sum(
                    con.execute(f"SELECT COUNT(*) FROM {('v_' if not stage_only else 'stg_main__')}{n}").fetchone()[0]
                    for n in []
                )
            except Exception:
                n_rows = 0
            get_metrics().record_materialization(", ".join(names) if names else "unknown", elapsed, n_rows)
            return [], [], "everything up to date (nothing to do)"
    # Identity includes data; snapshots are never overwritten on a repeated id.
    entry = {
        "fingerprints": {n: tm.fingerprint for n, tm in tms.items()},
        "names": names,
        "dialect": getattr(dialect, "name", str(dialect)),
        "source_overrides": source_overrides or {},
        "branch": branch,
        "source_fingerprints": source_fps,
    }
    elapsed = (datetime.datetime.now() - start).total_seconds() * 1000
    n_rows = 0
    if not names:
        try:
            n_rows = sum(
                con.execute(f"SELECT COUNT(*) FROM {('v_' if not stage_only else 'stg_main__')}{n}").fetchone()[0]
                for n in []
            )
        except Exception:
            n_rows = 0
        get_metrics().record_materialization(", ".join(names) if names else "unknown", elapsed, n_rows)
        return [], [], "everything up to date (nothing to do)"
    if reason is not None:
        entry["reason"] = reason
    if backfill_of is not None:
        entry["backfill_of"] = backfill_of
    if stage_only:
        rid = _run_id(entry)
        applied, pins = materialize(con, project, tms, names, dialect=dialect,
                                    source_overrides=source_overrides, branch=branch,
                                    stage_only=True)
    else:
        applied, pins, rid = _frozen_materialize(con, project, tms, entry,
                                                 source_overrides,
                                                 module_path=module_path)
    entry["applied"] = applied
    entry["pins"] = pins
    get_metrics().record_materialization(
        ", ".join(names) if names else "unknown",
        elapsed,
        n_rows,
    )
    if not applied:
        get_metrics().record_error("run", "no_models_applied")
    if stage_only:
        save_manifest(module_path, entry["fingerprints"])
        record_run(module_path, entry, run_id=rid)
    else:
        recover_metadata(con, module_path)
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


def execute_run(con, project: Project, tms: Dict[str, TypedModel], module_path: str,
                run_id: str, dialect=DUCKDB):
    """Public entry point: holds the module lock for the whole operation
    (see _module_lock) around the actual implementation below."""
    with _module_lock(module_path):
        return _execute_run_locked(con, project, tms, module_path, run_id, dialect=dialect)


def _execute_run_locked(con, project: Project, tms: Dict[str, TypedModel], module_path: str,
                        run_id: str, dialect=DUCKDB):
    """Replay WITH re-execution (spec §5): re-materialize a recorded run from
    its content-addressed record. The record must verify first (same gate as
    `verify_run`) — a drifted module never re-executes (fail-loud). Branch,
    source_overrides and model set come from the RECORD, not from flags, so
    the replay reproduces the recorded environment exactly. The new run is
    appended with `replay_of` for lineage (history stays append-only)."""
    recover_metadata(con, module_path)  # restore history before looking up the run
    e = verify_run(module_path, run_id)
    branch = e.get("branch", "main")
    overrides = e.get("source_overrides") or None
    inputs = e.get("input_snapshots")
    if inputs is None:
        raise PinError("run has no frozen inputs; historical replay is unavailable")
    if set(inputs) != set(project.sources):
        raise PinError("incomplete input snapshot inventory")
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ? AND table_type='BASE TABLE'",
        [dbcompat.db_schema(con)]).fetchall()}
    if any(snap not in have for snap in inputs.values()):
        raise PinError("input snapshot table(s) missing")
    frozen = {name: {"dataset": snap} for name, snap in inputs.items()}
    names = e.get("names") or list(tms)
    entry = {
        "fingerprints": {n: tm.fingerprint for n, tm in tms.items()},
        "names": names,
        "dialect": e.get("dialect", getattr(dialect, "name", str(dialect))),
        "source_overrides": overrides or {},
        "branch": branch,
        "replay_of": e["run_id"],
    }
    applied, pins, rid = _frozen_materialize(
        con, project, tms, entry, frozen,
        expected_sources=e.get("source_fingerprints"), module_path=module_path)
    recover_metadata(con, module_path)
    return applied, pins, e


def warehouse_branches(con) -> dict:
    """Branch inventory of a warehouse: {branch: {staged: [views], live: [views]}}."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ?", [dbcompat.db_schema(con)]).fetchall()
    have = {r[0] for r in rows}
    branches: dict = {}
    for tn in sorted(have):
        if tn.startswith(STAGED_PREFIX) and BRANCH_SEP in tn:
            rest = tn[len(STAGED_PREFIX):]
            b, model = rest.split(BRANCH_SEP, 1)
            branches.setdefault(b, {"staged": [], "live": []})["staged"].append(model)
        elif tn.startswith(PROMOTED_PREFIX):
            branches.setdefault("main", {"staged": [], "live": []})["live"].append(
                tn[len(PROMOTED_PREFIX):])
        else:
            branches.setdefault("main", {"staged": [], "live": []})
    for b in branches:
        branches[b]["staged"].sort()
        branches[b]["live"].sort()
    return branches