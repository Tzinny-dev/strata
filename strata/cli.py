"""strata -- command line interface.

One binary: build / plan / graph / profile / compile / run / lineage-diff / diff / migrate / bench / grammar / dashboard / init / seed / lsp.
"""
from __future__ import annotations

import argparse
import sys

# Import command modules
from .commands import (
    build,
    execute,
    grammar,
    inspect,
    migrate,
    project,
    quality,
    test_cmd,
    warehouse,
)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the argument parser, dispatch the subcommand, return its exit code."""
    ap = argparse.ArgumentParser(prog="strata", description="declarative, versioned data transformations")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Build commands
    p = sub.add_parser("build", help="typecheck + contracts + lineage (no DB)")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.add_argument("--strict", action="store_true",
                   help="fail (E014) unless every built model declares -> contract")
    p.set_defaults(fn=build.cmd_build)

    p = sub.add_parser("compile", help="emit SQL")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--dialect", default="duckdb",
                   help="target warehouse: duckdb | postgres | bigquery | snowflake")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=build.cmd_compile)

    p = sub.add_parser("plan", help="compute stale set")
    p.add_argument("file")
    p.add_argument("--seed", action="store_true",
                   help="pin current fingerprints as content-addressed baseline "
                        "(self-pinning: identical re-runs see nothing stale)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=build.cmd_plan)

    # Inspect commands
    p = sub.add_parser("graph", help="emit the module DAG (DOT default)")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--format", choices=["dot", "mermaid", "text"], default="dot",
                   help="dot (graphviz) | mermaid flowchart | text edges")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=inspect.cmd_graph)

    p = sub.add_parser("profile", help="performance breakdown (parse, check, emit; --run adds materialization)")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--run", action="store_true",
                   help="also materialize each model against a warehouse and report "
                        "per-model timing + rows")
    p.add_argument("--seed", action="store_true", help="load the built-in demo source fixtures first")
    p.add_argument("-o", "--output", default=None,
                   help="persist the warehouse at this path (default: in-memory)")
    p.add_argument("--dialect", default="duckdb",
                   help="target warehouse: duckdb | postgres | bigquery | snowflake")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=inspect.cmd_profile)

    p = sub.add_parser("dashboard",
                       help="one-screen supervision surface: models, contracts, DAG, staleness, runs, blast surface")
    p.add_argument("file")
    p.add_argument("--json", action="store_true",
                   help="machine-readable dashboard (agent supervision artifact)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=inspect.cmd_dashboard)

    p = sub.add_parser("lineage-diff", help="print lineage + blast radius")
    p.add_argument("file")
    p.add_argument("head2", nargs="?", default=None,
                   help="optional second module: column-level semantic diff file..head2")
    p.add_argument("--change", help="source col change to simulate, e.g. crm.orders:order_id")
    p.add_argument("--json", action="store_true",
                   help="machine-readable diff (agent supervision artifact)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=inspect.cmd_lineage)

    p = sub.add_parser("diff",
                       help="column-level semantic diff between two module versions "
                            "(breaking taxonomy, E030 gate; alias of lineage-diff base head)")
    p.add_argument("file")
    p.add_argument("head2", help="second module version to diff against")
    p.add_argument("--json", action="store_true",
                   help="machine-readable diff (agent supervision artifact)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=inspect.cmd_diff)

    # Execute commands
    p = sub.add_parser("run", help="materialize views (duckdb required)")
    p.add_argument("file")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--only-stale", action="store_true")
    p.add_argument("--freshness", default=None,
                   help="override freshness threshold for all models (e.g., 1h, daily, 7d)")
    p.add_argument("--branch", default="main", help="staging branch (stg_<branch>__*, promoted to v_* on swap)")
    p.add_argument("--stage-only", action="store_true", help="build + pin staged views without promoting (blue-green hold)")
    p.add_argument("--pipeline", default=None,
                   help="pipeline to materialize (default: first pipeline; "
                        "its sources: overrides select the env tables)")
    p.add_argument("--search-dir", default=None,
                   help="extra dir resolving `import a.b` -> a/b.strata")
    p.add_argument("--dialect", default="duckdb",
                   help="target warehouse: duckdb | postgres | bigquery | snowflake")
    p.add_argument("--iceberg-dir", default=None,
                   help="also publish this run's snapshots as real Iceberg "
                        "tables under this lakehouse catalog dir (duckdb dialect "
                        "required; writes <dir>/_strata_manifest.json)")
    p.add_argument("--output", "-o",
                   help="persist the warehouse to this .duckdb file "
                        "(default: in-memory, discarded on exit)")
    p.add_argument("--gc", action="store_true",
                   help="drop retired snapshot tables after a successful run "
                        "(same policy as `strata gc --apply`, run automatically)")
    p.add_argument("--gc-keep", type=int, default=2,
                   help="with --gc: most recent runs to retain (default: 2)")
    p.add_argument("--gc-keep-days", type=float, default=None,
                   help="with --gc: also retain runs newer than this many days")
    p.add_argument("--metrics-format", default=None,
                   choices=["prometheus", "statsd", "json"],
                   help="export metrics after run in specified format (prometheus|statsd|json)")
    p.add_argument("--metrics-file", default=None,
                   help="write metrics to file instead of stdout")
    p.set_defaults(fn=execute.cmd_run)

    p = sub.add_parser("bench", help="golden-file artifacts: supervision + regression")
    p.add_argument("--update", action="store_true",
                   help="re-bless golden files after an intentional compiler change")
    p.add_argument("--root", default=".",
                   help="project root that case module paths resolve against")
    p.set_defaults(fn=execute.cmd_bench)

    p = sub.add_parser("replay", help="list/inspect/verify content-addressed run records")
    p.add_argument("file")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--last", default="10")
    p.add_argument("--verify", default=None, help="verify run stable without re-execution")
    p.add_argument("--execute", action="store_true",
                   help="re-execute the run from its record (branch/overrides/model set from the record)")
    p.add_argument("--seed", action="store_true", help="with --execute: seed demo sources into a fresh warehouse")
    p.add_argument("-o", "--output", default=None, help="with --execute: persist the warehouse to this .duckdb file")
    p.add_argument("--search-dir", default=None, help="with --execute: extra dir resolving import a.b")
    p.add_argument("--iceberg-dir", default=None,
                   help="with --verify: also verify the run's tables in this Iceberg "
                        "catalog (read-only, no re-execution)")
    p.add_argument("--verify-reader", choices=("duckdb", "pyiceberg"),
                   default="duckdb",
                   help="reader engine for the catalog check: duckdb iceberg_scan "
                        "(default) or pyiceberg (independent of the writer; "
                        "'pyiceberg[pyarrow]' must be installed)")
    p.set_defaults(fn=execute.cmd_replay)

    p = sub.add_parser("backfill", help="corrected rerun journaled against a past run (duckdb required)")
    p.add_argument("file")
    p.add_argument("run_id", help="past run to base the correction on")
    p.add_argument("--models", default=None,
                   help="comma-separated model subset (default: the run's model set)")
    p.add_argument("--source", action="append", default=[],
                   help="corrected source table as src=table (repeatable; wins over the run's overrides)")
    p.add_argument("--reason", required=True,
                   help="why this correction exists (recorded in history)")
    p.add_argument("--branch", default=None, help="staging branch (default: the run's branch)")
    p.add_argument("--stage-only", action="store_true",
                   help="build + pin staged views without promoting (blue-green hold)")
    p.add_argument("--search-dir", default=None)
    p.add_argument("--output", "-o", help="persist the warehouse to this .duckdb file")
    p.set_defaults(fn=execute.cmd_backfill)

    p = sub.add_parser("seed", help="load demo source fixtures into a warehouse (duckdb required)")
    p.add_argument("file")
    p.add_argument("--output", "-o",
                   help="persist the seeded warehouse to this .duckdb file "
                        "(default: in-memory, discarded on exit)")
    p.set_defaults(fn=execute.cmd_seed)

    # Quality commands
    p = sub.add_parser("fmt", help="canonical formatter (AST -> text, idempotent)")
    p.add_argument("file")
    p.add_argument("--write", action="store_true", help="rewrite file in place")
    p.add_argument("--check", action="store_true", help="exit 1 if not formatted (CI)")
    p.add_argument("--search-dir", default=None)
    p.set_defaults(fn=quality.cmd_fmt)

    p = sub.add_parser("lint", help="static warnings (no DB)")
    p.add_argument("file")
    p.add_argument("--strict", action="store_true", help="exit 2 on warnings")
    p.add_argument("--search-dir", default=None)
    p.set_defaults(fn=quality.cmd_lint)

    p = sub.add_parser("check", help="validate a .strata artifact without materializing (CI guard)")
    p.add_argument("file")
    p.add_argument("--dialect", default="duckdb",
                   help="dialect probe target (default: duckdb)")
    p.set_defaults(fn=quality.cmd_check)

    # Project commands
    p = sub.add_parser("init", help="write AGENTS.md")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--claude", action="store_true")
    p.add_argument("target", nargs="?", default=".")
    p.set_defaults(fn=project.cmd_init)

    p = sub.add_parser("import-dbt", help="import a dbt schema.yml (and model .sql transforms with --models) into a .strata artifact")
    p.add_argument("file", metavar="schema.yml", help="dbt schema.yml (sources + models with columns)")
    p.add_argument("--models", metavar="DIR", default=None,
                   help="dbt models/ dir: translate each *.sql into the Strata model "
                        "body (select/where/group-by/join/case subset, plus WITH CTEs; "
                        "out-of-subset SQL fails loud E042)")
    p.add_argument("--output", help="output .strata path (default: alongside the schema.yml)")
    p.set_defaults(fn=project.cmd_import_dbt)

    # Warehouse commands
    p = sub.add_parser("branches", help="list staging branches in a warehouse (staged/live views per branch)")
    p.add_argument("-o", "--output", default=None, help="warehouse .duckdb file (default: describe :memory: as empty)")
    p.set_defaults(fn=warehouse.cmd_branches)

    p = sub.add_parser("rollback", help="repoint manifest (and live views with -o) to a recorded run")
    p.add_argument("file")
    p.add_argument("run_id")
    p.add_argument("-o", "--output", default=None, help="warehouse file whose live v_* views to repoint")
    p.add_argument("--iceberg-dir", default=None,
                   help="repoint this Iceberg catalog's live run instead of "
                        "warehouse views (fail-loud if the run isn't in the catalog)")
    p.add_argument("--branch", default=None, help="staging branch to repoint from (default: run's recorded branch)")
    p.set_defaults(fn=warehouse.cmd_rollback)

    p = sub.add_parser("gc", help="snapshot retention: report (default) or drop (--apply) old run snapshots")
    p.add_argument("file")
    p.add_argument("-o", "--output", default=None, help="warehouse .duckdb file holding the snapshots")
    p.add_argument("--iceberg-dir", default=None,
                   help="gc an Iceberg catalog instead of a warehouse: drop retired "
                        "runs/<run_id> dirs (default run and last --keep always kept)")
    p.add_argument("--keep", type=int, default=2,
                   help="most recent runs to retain for rollback/replay (default: 2)")
    p.add_argument("--keep-days", type=float, default=None,
                   help="also retain any run with snapshots recorded within "
                        "this many days, regardless of --keep (default: off, "
                        "count-only)")
    p.add_argument("--apply", action="store_true", help="drop the reported tables (default: report only)")
    p.add_argument("--json", action="store_true", help="machine-readable plan (agent supervision artifact)")
    p.set_defaults(fn=warehouse.cmd_gc)

    p = sub.add_parser("catalog", help="inspect an Iceberg catalog: runs, models, live run (read-only)")
    p.add_argument("catalog", help="Iceberg catalog directory (the --iceberg-dir of a run)")
    p.add_argument("--run", default=None, help="show this run's tables with row counts (no re-execution)")
    p.add_argument("--verify-reader", choices=("duckdb", "pyiceberg"),
                   default="duckdb",
                   help="reader for --run row counts: duckdb iceberg_scan (default) "
                        "or pyiceberg (independent of the writer)")
    p.add_argument("--json", action="store_true", help="machine-readable runs/default")
    p.set_defaults(fn=warehouse.cmd_catalog)

    # Test commands
    p = sub.add_parser("test", help="run declarative data tests against a module's models")
    p.add_argument("file")
    p.add_argument("--model", default=None, help="only run tests for this model (default: all tested models)")
    p.add_argument("--dialect", default="duckdb", help="warehouse for evaluation: duckdb | postgres | bigquery | snowflake")
    p.add_argument("--output", "-o", help="persist warehouse to .duckdb (default: in-memory, discarded on exit)")
    p.add_argument("--seed", action="store_true", help="load demo source fixtures before running tests")
    p.add_argument("--fixtures", default=None,
                   help="load a raw SQL fixtures file (statements separated by ';') "
                        "into the warehouse before staging")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b -> a/b.strata")
    p.set_defaults(fn=test_cmd.cmd_test)

    p = sub.add_parser("lsp", help="Language Server Protocol (stdio JSON-RPC)")
    p.set_defaults(fn=test_cmd.cmd_lsp)

    # Metadata commands
    p = sub.add_parser("migrate",
                       help="validate and upgrade the module's run history and "
                            "fingerprint manifest to the current schema version")
    p.add_argument("file")
    p.set_defaults(fn=migrate.cmd_migrate)

    # Grammar command
    p = sub.add_parser("grammar", help="emit the Strata grammar as llama.cpp GBNF (constrained decoding)")
    p.add_argument("--doc", action="store_true",
                   help="prefix each rule with its spec sentence as a comment")
    p.add_argument("--check", metavar="FILE", default=None,
                   help="verify a .strata program is accepted by the emitted GBNF "
                        "(exit 0 accepted, 2 rejected) — independent consumer engine")
    p.set_defaults(fn=grammar.cmd_grammar)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (Exception, SystemExit) as e:
        # Only catch our known errors, let others propagate
        if isinstance(e, SystemExit):
            return e.code if isinstance(e.code, int) else 1
        from strata.analysis import StrataError
        from strata.diagnostic import format_diagnostic
        from strata.lexer import LexError
        from strata.parser import ParseError
        if isinstance(e, (ParseError, LexError, StrataError)):
            print(format_diagnostic(e), file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())


# Backward compatibility: export command functions directly from strata.cli
# Tests and external code may import: from strata.cli import cmd_build, cmd_run, etc.
from .commands.build import cmd_build, cmd_compile, cmd_plan
from .commands.inspect import (
    cmd_graph, cmd_profile, cmd_dashboard, cmd_lineage, cmd_diff,
    _run_seed, _fail_loud_contracts, _semantic_diff,
    render_graph, _topo_order, render_profile,
)
from .commands.execute import cmd_run, cmd_bench, cmd_replay, cmd_backfill, cmd_seed
from .commands.quality import cmd_fmt, cmd_lint, cmd_check
from .commands.project import cmd_init, cmd_import_dbt
from .commands.warehouse import cmd_branches, cmd_rollback, cmd_gc, cmd_catalog
from .commands.test_cmd import cmd_test, cmd_lsp
from .commands.grammar import cmd_grammar
from .commands.migrate import cmd_migrate
from .utils import (
    _mask_dsn, _is_dsn, _valid_ident, load, check, open_warehouse,
    get_dialect, render_build,
)
