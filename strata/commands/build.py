"""Build commands: build, compile, plan"""

import sys
from typing import Any

from .. import exec as exec_mod
from ..utils import (
    check,
    get_dialect,
    load,
    render_build,
)


def cmd_build(args: Any) -> int:
    """`strata build <file>`: compile gate — prints typed contracts, fingerprints and lineage."""
    search_dirs = [args.search_dir] if getattr(args, 'search_dir', None) else None
    proj = load(args.file, search_dirs=search_dirs)
    tms = check(proj, args.model)
    names = args.model or list(tms)
    if getattr(args, "strict", False):
        # Strict contract mode (§1): a typed model without a declared
        # contract fails the build (E014) instead of passing silently —
        # verify_contract only checks models that declare one.
        bare = [n for n, tm in tms.items() if not tm.contract]
        if bare:
            print("error: E014: strict mode requires every built model to declare "
                  f"-> contract: {', '.join(sorted(bare))}", file=sys.stderr)
            return 2
    print(render_build(proj, tms, names))
    return 0


def cmd_compile(args: Any) -> int:
    """`strata compile <file> --dialect D`: emit dialect-constrained SQL for the selected models."""
    search_dirs = [args.search_dir] if getattr(args, 'search_dir', None) else None
    proj = load(args.file, search_dirs=search_dirs)
    tms = check(proj, args.model)
    dialect_name = getattr(args, "dialect", "duckdb")
    bad = exec_mod.check_physical_schema(dialect_name, tms)
    if bad:
        for name, issues in bad.items():
            for iss in issues:
                print(f"error: E070: {dialect_name}: model {name}: {iss}", file=sys.stderr)
        return 2
    try:
        dialect = get_dialect(dialect_name)
    except ValueError as ve:
        print(f"error: {ve}", file=sys.stderr)
        return 2
    names = args.model or (proj.model_names_for(None) or list(tms))
    from .. import sqlgen
    print(sqlgen.full_sql(tms, names, dialect=dialect))
    return 0


def cmd_plan(args: Any) -> int:
    """`strata plan <file>`: print the model graph with staleness status (or seed a baseline with --seed)."""
    search_dirs = [args.search_dir] if getattr(args, 'search_dir', None) else None
    proj = load(args.file, search_dirs=search_dirs)
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