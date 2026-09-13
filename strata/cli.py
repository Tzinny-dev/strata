"""strata -- command line interface.

One binary: build / plan / compile / run / lineage-diff / plan / init / seed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analysis
from . import sqlgen
from . import exec as exec_mod
from .dialects import get_dialect
from .lexer import LexError
from .parser import ParseError, parse_strata
from .analysis import StrataError, Checker, build_down_edges, blast_radius


def load(path: str):
    src = Path(path).read_text()
    module = parse_strata(src, path)
    proj = analysis.Project(module)
    return proj


def check(proj, model_names=None):
    ck = Checker(proj)
    tms = ck.check_all(model_names)
    return tms


# ---------------------------------------------------------------- commands

def cmd_build(args):
    proj = load(args.file)
    tms = check(proj, args.model)
    out = []
    for name in (args.model or list(tms)):
        tm = tms[name]
        cols = ", ".join(c.describe() for c in tm.schema.values())
        out.append(f"model {name}" + (f" -> contract {tm.contract}" if tm.contract else ""))
        out.append(f"  fingerprint  {tm.fingerprint}")
        out.append(f"  outputs      {cols}")
        for cname, origins in tm.lineage.items():
            o = ", ".join(f"{o.node}.{o.col} [{o.kind}]" for o in origins)
            out.append(f"  lineage {cname} <- {o}")
        out.append(f"  reads        {sorted(tm.reads)}")
        out.append("")
    print("\n".join(out).rstrip())
    return 0


def cmd_compile(args):
    proj = load(args.file)
    tms = check(proj, args.model)
    names = args.model or (proj.model_names_for(None) or list(tms))
    print(sqlgen.full_sql(tms, names, dialect=get_dialect(args.dialect)))
    return 0


def cmd_plan(args):
    proj = load(args.file)
    tms = check(proj)
    stale = exec_mod.stale_models(tms, args.file)
    print("model graph (topological):")
    for name, tm in tms.items():
        st = "STALE" if name in stale else "ok   "
        deps = ", ".join(tm.deps) if tm.deps else "-"
        print(f"  [{st}] {name}   deps: {deps}")
    if stale:
        print(f"\n{len(stale)} stale model(s): {', '.join(stale)}")
    else:
        print("\neverything up to date (no re-computation needed)")
    return 0


def cmd_lineage(args):
    proj = load(args.file)
    tms = check(proj)
    down = build_down_edges(tms)
    print("lineage (column-level dependency edges):")
    for key in sorted(down):
        targets = ", ".join(f"{m}.{c}" for m, c in down[key])
        print(f"  {key[0]}.{key[1]} -> {targets}")
    if args.change:
        changes = []
        for spec in args.change.split(","):
            node, _, col = spec.strip().partition(":")
            changes.append((node, col))
        radius = blast_radius(tms, changes)
        radius = [c for c in radius if c not in changes]
        print("\nblast radius (what breaks if source protected/changed):")
        for node, col in sorted(radius):
            print(f"  {node}.{col}")
    return 0


def cmd_run(args):
    proj = load(args.file)
    tms = check(proj)
    try:
        import duckdb
    except ImportError as ie:
        print("duckdb not available; run with the venv interpreter "
              "(/tmp/opencode/strata-venv/bin/python)", file=sys.stderr)
        return 2
    con = duckdb.connect()
    if args.seed:
        _run_seed(con, args.file)
    applied, pins, note = exec_mod.run(con, proj, tms, args.file,
                                       only_stale=args.only_stale)
    if note:
        print(note)
    else:
        for a in applied:
            n = con.execute(f"SELECT count(*) FROM v_{a}").fetchone()[0]
            print(f"  materialized  v_{a}  ({n} rows)")
    for p in pins:
        print(p)
    # demo preview
    if not args.only_stale:
        first = applied[0] if applied else (list(tms)[0] if tms else None)
        if first:
            print("\n  preview " + first)
            cols = [d[0] for d in con.execute(f"SELECT * FROM v_{first} LIMIT 1").description]
            print("    " + ", ".join(cols))
            for row in con.execute(f"SELECT * FROM v_{first} LIMIT 3").fetchall():
                print("    " + ", ".join(str(v) for v in row))
    return 0


def _run_seed(con, path: str):
    from .seed import seed_sql  # lazy: seed imports live alongside examples
    con.execute(seed_sql()[0])


def cmd_init(args):
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    name = "--codex" if args.codex else ("--claude" if args.claude else None)
    content = ("# AGENTS.md for Strata projects\n\n"
               "## Commands\n"
               "- `strata build <file>`  typecheck + contracts + lineage (no DB needed)\n"
               "- `strata compile <file> --emit-sql`  emit DuckDB SQL\n"
               "- `strata plan <file>`   show stale set (fingerprints)\n"
               "- `strata run <file> --seed`  materialize views (requires venv duckdb)\n"
               "- `strata lineage-diff <file> --change crm.orders:order_id`  blast radius\n")
    if name == "--codex":
        content = "Prohibitions/extensions for Codex:\n\n" + content
    elif name == "--claude":
        content = "Claude Code .claude settings:\n\n" + content
    (target / "AGENTS.md").write_text(content)
    print(f"wrote {target / 'AGENTS.md'}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="strata", description="declarative, versioned data transformations")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="typecheck + contracts + lineage (no DB)")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("compile", help="emit SQL")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--dialect", default="duckdb",
                   help="target warehouse: duckdb | bigquery | snowflake")
    p.set_defaults(fn=cmd_compile)

    p = sub.add_parser("plan", help="compute stale set")
    p.add_argument("file")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("lineage-diff", help="print lineage + blast radius")
    p.add_argument("file")
    p.add_argument("--change", help="source col change to simulate, e.g. crm.orders:order_id")
    p.set_defaults(fn=cmd_lineage)

    p = sub.add_parser("run", help="materialize views (duckdb required)")
    p.add_argument("file")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--only-stale", action="store_true")
    p.add_argument("--dialect", default="duckdb",
                   help="target warehouse: duckdb | bigquery | snowflake")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("init", help="write AGENTS.md")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--claude", action="store_true")
    p.add_argument("target", nargs="?", default=".")
    p.set_defaults(fn=cmd_init)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ParseError, LexError, StrataError) as e:
        print(f"error: {getattr(e, 'code', 'E000')}: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())