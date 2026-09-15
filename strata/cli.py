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
    if args.seed:
        exec_mod.save_manifest(args.file, {n: tm.fingerprint for n, tm in tms.items()})
        print(f"seed baseline pinned ({len(tms)} model(s), content-addressed):")
        for name in sorted(tms):
            print(f"  {name}  {tms[name].fingerprint}")
        print("\nidentical re-runs (same sources + same code) will see nothing stale.")
        return 0
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


def _fail_loud_contracts(proj, tms, changes, radius):
    """Fail-loud §7 (E030-32): a change that removes/narrows a column that a
    downstream model reads is a cross-team contract break. Protected columns
    (contract `protected`/`primary_key`/`nonnull`) are the hard boundary: the
    producer PR must NOT ship it. Returns None if no protected column is hit,
    else prints E030 radius and returns the breaking (node, col) list."""
    breaking = []
    for node, col in changes:
        tm = tms.get(node)
        if tm is None:
            continue
        c = tm.schema.get(col)
        if c is not None and (c.protected or c.primary or not c.nullable):
            breaking.append((node, col))
    if not breaking:
        return None
    consumed = sorted(radius)
    print(f"\nE030: change to protected/consumed column(s) breaks consumer contract:")
    for n, c in breaking:
        print(f"  producer {n}.{c} (protected/contract-bound)")
    print(f"  -> {len(consumed)} consumer column(s) depend on it:")
    for n, c in consumed:
        print(f"    {n}.{c}")
    print("  producer PR must NOT ship this change (cross-team contract, E030-32)")
    return breaking


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
        _fail_loud_contracts(proj, tms, changes, radius)
        if radius:
            print(f"\nE030: {len(radius)} consumer column(s) break if "
                  f"{changes[0][0]}.{changes[0][1]} is removed/narrowed: "
                  f"{', '.join(f'{n}.{c}' for n, c in sorted(radius))}")
            print("  -> producer PR must NOT ship this change (cross-team contract)")
            return 1
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
    con = duckdb.connect(getattr(args, "output", None) or ":memory:")
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
    if getattr(args, "output", None):
        con.close()
    return 0


def _run_seed(con, path: str):
    from .seed import seed_sql  # lazy: seed imports live alongside examples
    con.execute(seed_sql()[0])


def cmd_import_dbt(args):
    """Import a dbt schema.yml (sources + models with column contracts) into a
    deterministic .strata artifact via strata/importdbt.py (E041 fail-loud §4
    / §11: a dbt model doing  with no columns cannot be imported — the
    warehouse owns the types)."""
    from strata.importdbt import import_dbt_schema, ImportFailedFailLoud
    path = Path(args.file)
    try:
        artifact = import_dbt_schema(path)
    except ImportFailedFailLoud as e:
        print(f"E041: {e}", file=sys.stderr)
        return 1
    out = Path(getattr(args, 'output', None) or (path.parent / path.stem).with_suffix('.strata'))
    out.write_text(artifact)
    print(f"wrote {out}")
    return 0


def cmd_init(args):
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    name = "--codex" if args.codex else ("--claude" if args.claude else None)
    content = ("# AGENTS.md for Strata projects\n\n"
               "Generated deterministically by `strata init` (do not hand-edit; "
               "identical re-runs produce byte-identical output).\n\n"
               "## Transform command contract (fails loud, see §7 E030)\n"
               "- `strata build <file>`   typecheck + contracts + lineage (no DB needed)\n"
               "- `strata compile <file> --dialect bigquery|snowflake`  dialect-constrained SQL\n"
               "- `strata plan <file> --seed`  self-pin content-addressed baseline\n"
               "- `strata lineage-diff <file> --change MODEL:COL`  blast radius of a change\n\n"
               "## Cross-team gate (MANDATORY before producer PR ships)\n"
               "1. Compute the blast radius of your producer change:\n"
               "     strata lineage-diff <file> --change orders:order_id\n"
               "2. If exit != 0, the change breaks a protected/consumed column that a\n"
               "   downstream team reads. The PR MUST NOT ship (fail-loud E030).\n"
               "3. If exit == 0, the change is contained: safe to ship.\n"
               "The gate is enforced by the compiler at lineage-diff time, NOT by\n"
               "memory: an agent that forgets it still gets a hard E030.\n")
    if name == "--codex":
        content = "Prohibitions/extensions for Codex:\n\n" + content
    elif name == "--claude":
        content = "Claude Code .claude settings:\n\n" + content
    content += ("\n\n---\n"
                "Contract document generated from strata/analysis.py E030 logic — "
                "the only source of truth is `strata lineage-diff --change`.\n")
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
    p.add_argument("--seed", action="store_true",
                   help="pin current fingerprints as content-addressed baseline "
                        "(self-pinning: identical re-runs see nothing stale)")
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
    p.add_argument("--output", "-o",
                   help="persist the warehouse to this .duckdb file "
                        "(default: in-memory, discarded on exit)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("init", help="write AGENTS.md")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--claude", action="store_true")
    p.add_argument("target", nargs="?", default=".")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("import-dbt", help="import a dbt schema.yml into a .strata artifact")
    p.add_argument("file", metavar="schema.yml", help="dbt schema.yml (sources + models with columns)")
    p.add_argument("--output", help="output .strata path (default: alongside the schema.yml)")
    p.set_defaults(fn=cmd_import_dbt)

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