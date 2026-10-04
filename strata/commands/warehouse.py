"""Warehouse commands: branches, rollback, gc, catalog"""

import json
import sys
from pathlib import Path
from typing import Any

from .. import exec as exec_mod
from ..utils import _is_dsn, _mask_dsn, open_warehouse


def cmd_branches(args: Any) -> int:
    """Branch inventory of a persisted warehouse: staged (stg_<branch>__*) and live (v_*) views per branch."""
    if not getattr(args, "output", None):
        print("(no warehouse: pass -o FILE.duckdb; an in-memory warehouse is always empty)")
        return 0
    if not _is_dsn(args.output) and not Path(args.output).exists():
        print(f"error: E083: warehouse {_mask_dsn(args.output)!r} not found", file=sys.stderr)
        return 1
    try:
        con = open_warehouse(args.output, read_only=True)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    branches = exec_mod.warehouse_branches(con)
    con.close()
    if not branches:
        print("(no branches: warehouse has no staged/live views)")
        return 0
    for b in sorted(branches):
        inv = branches[b]
        print(f"branch {b}")
        print(f"  staged  {', '.join(inv['staged']) or '-'}")
        print(f"  live    {', '.join(inv['live']) or '-'}")
    return 0


def cmd_rollback(args: Any) -> int:
    """`strata rollback <file> --run <id>`: repoint live views to a previous run's snapshots."""
    if getattr(args, "iceberg_dir", None):
        e = exec_mod.find_run(args.file, args.run_id)
        if e is None:
            print(f"error: E081: unknown run {args.run_id!r} (see strata replay)", file=sys.stderr)
            return 1
        from .. import iceberg as iceberg_mod
        try:
            mf = iceberg_mod.rollback_run(Path(args.iceberg_dir), e["run_id"])
        except iceberg_mod.IcebergExportError as ice:
            print(f"error: E083: {ice}", file=sys.stderr)
            return 1
        print(f"catalog {args.iceberg_dir}: default run repointed -> {e['run_id']} "
              f"({len(mf['runs'][e['run_id']])} table(s))")
        return 0
    e = exec_mod.find_run(args.file, args.run_id)
    if e is None:
        print(f"error: E081: unknown run {args.run_id!r} (see strata replay)", file=sys.stderr)
        return 1
    fps = e.get("fingerprints", {})
    if getattr(args, "output", None):
        if not _is_dsn(args.output) and not Path(args.output).exists():
            print(f"error: E083: warehouse {_mask_dsn(args.output)!r} not found "
                  "(nothing to repoint)", file=sys.stderr)
            return 1
        try:
            con = open_warehouse(args.output)
        except RuntimeError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
        if e.get("snapshots"):
            # Snapshot-addressed rollback: repoint live views to the frozen
            # tables recorded by that run (immune to later source changes).
            # The manifest repoints only AFTER the swap commits; the rollback
            # event itself is journaled in the same transaction, so a crash
            # between swap and file write is repaired by recover_metadata.
            try:
                exec_mod.rollback_to_run(con, e, module_path=args.file)
            except Exception as pe:  # PinError, OSError, or a driver-specific DB error
                print(f"error: E083: {pe}", file=sys.stderr)
                return 1
            finally:
                con.close()
            print(f"repointed live views v_* <- snapshots of run {e['run_id']} "
                  f"({len(e['snapshots'])} view(s))")
        else:
            # Pre-snapshot history: legacy staged-view repoint.
            branch = getattr(args, "branch", None) or e.get("branch", "main")
            names = list(fps)
            have = {r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
            missing = [n for n in names if exec_mod.staged_name(n, branch) not in have]
            if missing:
                con.close()
                print(f"error: E083: staged view(s) missing for branch {branch!r}: "
                      f"{', '.join(missing)} — rollback cannot repoint to data that "
                      "does not exist (fail-loud, no silent replay)", file=sys.stderr)
                return 1
            exec_mod.swap_branch(con, names, branch)
            con.close()
            exec_mod.save_manifest(args.file, fps)
            print(f"repointed live views v_* <- stg_{branch}__* ({len(names)} view(s))")
    else:
        exec_mod.save_manifest(args.file, fps)
        print(f"rolled back manifest to run {e['run_id']} ({len(fps)} model(s) pinned)")
        print("next strata run --only-stale will rebuild what diverged since")
    return 0


def cmd_gc(args: Any) -> int:
    """Snapshot retention: report (default) or drop (--apply) old run snapshots."""
    if not getattr(args, "output", None) and not getattr(args, "iceberg_dir", None):
        print("error: E084: --output <warehouse.duckdb> (or --iceberg-dir <catalog>) "
              "is required (snapshots live in the warehouse/catalog)", file=sys.stderr)
        return 1
    if getattr(args, "iceberg_dir", None):
        from .. import iceberg as iceberg_mod
        try:
            plan = iceberg_mod.gc_catalog(
                Path(args.iceberg_dir), keep=args.keep,
                keep_days=getattr(args, "keep_days", None), apply=args.apply)
        except iceberg_mod.IcebergExportError as ice:
            print(f"error: E084: {ice}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        print(f"catalog retained runs: {', '.join(plan['keep_runs']) or '-'}")
        verb = "dropped" if plan["applied"] else "would drop"
        print(f"{verb} {len(plan['drop_dirs'])} Iceberg run dir(s)")
        for d in plan["drop_dirs"]:
            print(f"  - {d}")
        if plan["drop_runs"] and not plan["applied"]:
            print("re-run with --apply to drop them")
        return 0
    if not getattr(args, "output", None):
        print("error: E084: --output <warehouse.duckdb> is required "
              "(snapshots live in the warehouse)", file=sys.stderr)
        return 1
    if not _is_dsn(args.output) and not Path(args.output).exists():
        print(f"error: E083: warehouse {_mask_dsn(args.output)!r} not found", file=sys.stderr)
        return 1
    try:
        con = open_warehouse(args.output)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        exec_mod.recover_metadata(con, args.file)  # export pending events first
        plan = exec_mod.gc_snapshots(con, args.file, keep=args.keep,
                                     keep_days=getattr(args, "keep_days", None),
                                     apply=args.apply)
    except Exception as e:  # PinError, or a driver-specific DB error
        print(f"error: E084: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    print(f"retained runs: {', '.join(plan['keep_runs']) or '-'}")
    verb = "dropped" if plan["applied"] else "would drop"
    print(f"{verb} {len(plan['drop_tables'])} snapshot table(s)")
    for tn in plan["drop_tables"]:
        print(f"  - {tn}")
    if plan["retired_runs"]:
        print(f"runs losing rollback/replay: {', '.join(plan['retired_runs'])}")
    if plan["drop_tables"] and not plan["applied"]:
        print("re-run with --apply to drop them")
    return 0


def cmd_catalog(args: Any) -> int:
    """Inspect an Iceberg catalog: runs, their models, and the live (`default`) run."""
    from .. import iceberg as iceberg_mod
    catalog = Path(args.catalog)
    try:
        mf = iceberg_mod.load_manifest(catalog)
    except Exception as e:
        print(f"error: E084: cannot read catalog {catalog}: {e}", file=sys.stderr)
        return 1
    if mf is None:
        print(f"error: E084: catalog {catalog} has no manifest "
              "(run strata run --iceberg-dir first)", file=sys.stderr)
        return 1
    runs = mf["runs"]
    default = mf.get("default")
    if args.run:
        if args.run not in runs:
            print(f"error: E081: run {args.run!r} is not in catalog {catalog} "
                  "(see the runs listed below)", file=sys.stderr)
            return 1
        status = None
        reader = getattr(args, "verify_reader", "duckdb") or "duckdb"
        if reader == "pyiceberg":
            try:
                status = iceberg_mod.verify_catalog_run_pyiceberg(catalog, args.run)
            except (iceberg_mod.IcebergUnavailable,
                    iceberg_mod.IcebergExportError) as e:
                print(f"error: E083: {e}", file=sys.stderr)
                return 1
        else:
            con = None
            try:
                con = open_warehouse(None)
                iceberg_mod.ensure_iceberg(con)
                status = iceberg_mod.verify_catalog_run(con, catalog, args.run)
            except (iceberg_mod.IcebergUnavailable,
                    iceberg_mod.IcebergExportError) as e:
                print(f"error: E083: {e}", file=sys.stderr)
                return 1
            finally:
                if con is not None:
                    con.close()
        print(f"catalog {catalog}: run {args.run} ({reader})")
        for model in sorted(status["models"]):
            print(f"  {model:<24} {status['rows'][model]} rows")
        return 0
    if args.json:
        print(json.dumps({"default": default, "runs": runs}, indent=2, sort_keys=True))
        return 0
    live = " (live)" if default in runs else ""
    print(f"catalog {catalog}: {len(runs)} run(s){live}")
    for rid in sorted(runs):
        mark = " *" if rid == default else ""
        print(f"  {rid}{mark}: {', '.join(sorted(runs[rid]))}")
    if default and default not in runs:
        print(f"  default {default} is gone (collected by gc)")
    return 0