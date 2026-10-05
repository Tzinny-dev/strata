"""Execute commands: run, bench, replay, backfill, seed"""

import sys
from typing import Any

from .. import exec as exec_mod
from ..utils import (
    _valid_ident,
    check,
    get_dialect,
    load,
    open_warehouse,
)


def cmd_run(args: Any) -> int:
    """`strata run <file>`: materialize stale model views against a real warehouse (`-o`)."""
    search = [args.search_dir] if getattr(args, "search_dir", None) else []
    proj = load(args.file, search_dirs=search or None)
    tms = check(proj)
    try:
        dialect_obj = get_dialect(getattr(args, "dialect", "duckdb"))
    except ValueError as ve:
        print(str(ve), file=sys.stderr)
        return 4
    dialect_name = getattr(args, "dialect", "duckdb")
    # Fail-loud physical schema check: every declared type expressible.
    bad = exec_mod.check_physical_schema(dialect_name, tms)
    if bad:
        for name, issues in bad.items():
            for iss in issues:
                print(f"error: E070: {dialect_name}: model {name}: {iss}", file=sys.stderr)
        return 4
    dialect = dialect_obj
    pipeline = proj.pipeline_by_name(getattr(args, "pipeline", None))
    wanted = proj.model_names_for(pipeline, include_generated=True) if pipeline else None
    overrides = proj.pipeline_sources(pipeline.name if pipeline else None)
    if overrides:
        print(f"pipeline {pipeline.name!r} env={pipeline.env or '-'} "
              f"source overrides: {', '.join(f'{k}<-{v}' for k, v in sorted(overrides.items()))}")
    if not getattr(args, "output", None):
        print("warning: no -o given; this warehouse is in-memory and will "
              "not survive process exit — later replay/rollback/gc on this "
              "run will find nothing", file=sys.stderr)
    try:
        con = open_warehouse(getattr(args, "output", None))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if getattr(args, "iceberg_dir", None):
        if dialect_name != "duckdb":
            print(f"error: E100: --iceberg-dir requires duckdb SQL dialect, got "
                  f"{dialect_name!r} (Iceberg is a physical destination, not a "
                  f"SQL dialect)", file=sys.stderr)
            return 2
        from .. import iceberg as iceberg_mod
        try:
            iceberg_mod.ensure_iceberg(con)
        except iceberg_mod.IcebergUnavailable as e:
            print(f"error: E100: {e}", file=sys.stderr)
            return 2
    if args.seed:
        _run_seed(con, args.file)
    applied, pins, note = exec_mod.run(con, proj, tms, args.file,
                                       only_stale=args.only_stale,
                                       names=wanted,
                                       source_overrides=overrides or None,
                                       branch=getattr(args, "branch", "main"),
                                       stage_only=getattr(args, "stage_only", False),
                                       freshness_override=getattr(args, "freshness", None))
    if note:
        print(note)
    else:
        live = "v_" if not getattr(args, "stage_only", False) else f"stg_{getattr(args, 'branch', 'main')}__"
        for a in applied:
            n = con.execute(f"SELECT count(*) FROM {live}{a}").fetchone()[0]
            print(f"  materialized  {live}{a}  ({n} rows)")
    for p in pins:
        print(p)
    # demo preview
    if not args.only_stale:
        first = applied[0] if applied else (list(tms)[0] if tms else None)
        if first:
            view = ("v_" if not getattr(args, "stage_only", False)
                    else f"stg_{getattr(args, 'branch', 'main')}__") + first
            print("\n  preview " + first + f" ({view})" )
            cols = [d[0] for d in con.execute(f"SELECT * FROM {view} LIMIT 1").description]
            print("    " + ", ".join(cols))
            for row in con.execute(f"SELECT * FROM {view} LIMIT 3").fetchall():
                print("    " + ", ".join(str(v) for v in row))
    if getattr(args, "iceberg_dir", None):
        if applied:
            try:
                from .. import iceberg as iceberg_mod
                entry = exec_mod.load_history(args.file)[-1]
                rid = entry["run_id"]
                snapshots = dict(entry.get("snapshots") or {})
                manifest = iceberg_mod.export_run(
                    con, rid, snapshots, args.iceberg_dir)
                exported = manifest["runs"][rid]
                print(f"  iceberg: exported {len(exported)} table(s) to "
                      f"{args.iceberg_dir} (run {rid})")
            except iceberg_mod.IcebergExportError as e:
                print(f"error: E100: {e}", file=sys.stderr)
                if getattr(args, "output", None):
                    con.close()
                return 2
        else:
            print("  iceberg: nothing new to export (run applied no models)")
    if getattr(args, "gc", False) and not getattr(args, "stage_only", False):
        try:
            exec_mod.recover_metadata(con, args.file)
            gc = exec_mod.gc_snapshots(con, args.file, keep=args.gc_keep,
                                       keep_days=getattr(args, "gc_keep_days", None),
                                       apply=True)
        except exec_mod.PinError as e:
            print(f"error: E084: {e}", file=sys.stderr)
            if getattr(args, "output", None):
                con.close()
            return 1
        print(f"  gc: dropped {len(gc['drop_tables'])} snapshot table(s), "
              f"kept {len(gc['keep_tables'])}")
    if getattr(args, "output", None):
        con.close()
    # Export metrics if requested
    metrics_format = getattr(args, "metrics_format", None)
    if metrics_format:
        from ..observability import (
            MetricsCollector,
            PrometheusExporter,
            StatsDExporter,
            JsonExporter,
        )
        collector = exec_mod.get_metrics()
        if collector is not None:
            if metrics_format == "prometheus":
                exporter = PrometheusExporter(collector)
            elif metrics_format == "statsd":
                exporter = StatsDExporter(collector)
            elif metrics_format == "json":
                exporter = JsonExporter(collector)
            else:
                print(f"error: unknown metrics format {metrics_format}", file=sys.stderr)
                return 1
            print(exporter.export())
        else:
            print("(no metrics collected)")
    return 0


def cmd_bench(args: Any) -> int:
    """Golden-file harness over deterministic artifacts."""
    from .. import bench as bench_mod
    return bench_mod.run_cases(root=args.root, update=args.update)


def cmd_replay(args: Any) -> int:
    """`strata replay <file>`: verify (--verify), re-execute (--execute), or list recorded runs."""
    if getattr(args, "verify", None):
        try:
            e = exec_mod.verify_run(args.file, args.verify)
        except Exception as ex:
            print(f"error: E082: {ex}", file=sys.stderr)
            return 1
        print(f"verify OK: run {e['run_id']} stable ({len(e.get('fingerprints', {}))} model(s), no re-execution)")
        if getattr(args, "iceberg_dir", None):
            from .. import iceberg as iceberg_mod
            reader = getattr(args, "verify_reader", "duckdb")
            con = None
            try:
                if reader == "pyiceberg":
                    status = iceberg_mod.verify_catalog_run_pyiceberg(
                        args.iceberg_dir, e["run_id"])
                    label = "pyiceberg"
                else:
                    con = open_warehouse(None)
                    iceberg_mod.ensure_iceberg(con)
                    status = iceberg_mod.verify_catalog_run(
                        con, args.iceberg_dir, e["run_id"])
                    label = "duckdb iceberg_scan"
            except Exception as ex:
                print(f"error: E083: iceberg catalog verify failed for run "
                      f"{e['run_id']} via {reader}: {ex}", file=sys.stderr)
                return 1
            finally:
                if con is not None:
                    con.close()
            total = sum(status["rows"].values())
            print(f"  catalog OK via {label}: {len(status['models'])} table(s) "
                  f"in {args.iceberg_dir} readable ({total} rows)")
        return 0
    if getattr(args, "execute", False):
        if not args.run_id:
            print("error: E082: --execute needs a run_id", file=sys.stderr)
            return 1
        search_dirs = [args.search_dir] if getattr(args, "search_dir", None) else None
        proj = load(args.file, search_dirs=search_dirs)
        tms = check(proj)
        try:
            con = open_warehouse(getattr(args, "output", None))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if getattr(args, "seed", False):
            _run_seed(con, args.file)
        try:
            applied, pins, orig = exec_mod.execute_run(con, proj, tms, args.file, args.run_id)
        except exec_mod.PinError as pe:
            print(f"error: E082: {pe}", file=sys.stderr)
            return 1
        except Exception as se:
            print(f"error: {se}", file=sys.stderr)
            return 1
        if getattr(args, "output", None):
            con.close()
        print(f"replayed {orig['run_id']} -> new run recorded "
              f"(branch {orig.get('branch', 'main')}, {len(applied)} model(s) re-materialized)")
        for p in pins:
            print(p)
        return 0
    hist = exec_mod.load_history(args.file)
    if args.run_id:
        e = exec_mod.find_run(args.file, args.run_id)
        if e is None:
            print(f"error: E080: unknown run {args.run_id!r} ({len(hist)} in history)", file=sys.stderr)
            return 1
        print(f"run {e['run_id']} at {e.get('at', '?')}")
        print(f"  applied      {', '.join(e.get('applied', [])) or '-'}")
        print(f"  fingerprints {e.get('fingerprints', {})}")
        print(f"  pins         {len(e.get('pins', []))} pin report lines")
        if e.get("backfill_of"):
            print(f"  backfill_of  {e['backfill_of']}")
        if e.get("reason"):
            print(f"  reason       {e['reason']}")
        return 0
    if not hist:
        print("no runs recorded (run strata run first)")
        return 0
    for e in hist[-int(args.last):]:
        print(f"{e['run_id']}  {e.get('at', '?')}  applied={','.join(e.get('applied', [])) or '-'}")
    return 0


def cmd_backfill(args: Any) -> int:
    """`strata backfill <file> --run <id>`: re-run a past run's model set against today's sources."""
    rec = exec_mod.find_run(args.file, args.run_id)
    if rec is None:
        print(f"error: E085: unknown run {args.run_id!r} (see strata replay)", file=sys.stderr)
        return 1
    search = [args.search_dir] if getattr(args, "search_dir", None) else []
    proj = load(args.file, search_dirs=search or None)
    tms = check(proj)
    if getattr(args, "models", None):
        names = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        names = list(rec.get("names", [])) or list(tms)
    unknown = [m for m in names if m not in tms]
    if unknown:
        print(f"error: E085: unknown model(s) {', '.join(unknown)}", file=sys.stderr)
        return 1
    overrides = dict(rec.get("source_overrides", {}))
    for spec in getattr(args, "source", None) or []:
        if "=" not in spec:
            print(f"error: E085: --source expects src=table, got {spec!r}", file=sys.stderr)
            return 1
        src, table = spec.split("=", 1)
        table = table.strip()
        if src not in proj.sources:
            print(f"error: E085: unknown source {src!r}", file=sys.stderr)
            return 1
        if not _valid_ident(table):
            print(f"error: E085: source override table {table!r} is not a valid identifier "
                  f"(must match ^[A-Za-z_][A-Za-z0-9_]*$)", file=sys.stderr)
            return 1
        overrides[src] = {"dataset": table}
    branch = getattr(args, "branch", None) or rec.get("branch", "main")
    if not getattr(args, "output", None):
        print("warning: no -o given; this warehouse is in-memory and will "
              "not survive process exit — later replay/rollback/gc on this "
              "run will find nothing", file=sys.stderr)
    try:
        con = open_warehouse(getattr(args, "output", None))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        applied, pins, note = exec_mod.run(
            con, proj, tms, args.file, only_stale=True, names=names,
            source_overrides=overrides or None, branch=branch,
            stage_only=getattr(args, "stage_only", False),
            reason=args.reason, backfill_of=rec.get("run_id"))
    except exec_mod.PinError as pe:
        print(f"error: E085: {pe}", file=sys.stderr)
        return 1
    if getattr(args, "output", None):
        con.close()
    if note:
        print(note)
        return 0
    print(f"backfilled {rec.get('run_id')} -> corrected run "
          f"({len(applied)} model(s), reason: {args.reason})")
    for p in pins:
        print(p)
    return 0


def cmd_seed(args: Any) -> int:
    """`strata seed <file>`: load demo source fixtures into a warehouse."""
    proj = load(args.file)
    tms = check(proj)
    try:
        con = open_warehouse(getattr(args, "output", None))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _run_seed(con, args.file)
    for tname, in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name").fetchall():
        rows = con.execute(f"SELECT count(*) FROM {tname}").fetchone()[0]
        print(f"  seeded  {tname}  ({rows} rows)")
    if getattr(args, "output", None):
        con.close()
    return 0


def _run_seed(con: Any, path: str) -> None:
    """Seed a warehouse with the built-in demo sources (from strata.seed)."""
    from ..seed import seed_sql  # lazy: seed imports live alongside examples
    con.execute(seed_sql()[0])