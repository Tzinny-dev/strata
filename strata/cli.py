"""strata -- command line interface.

One binary: build / plan / compile / run / lineage-diff / bench / grammar / dashboard / init / seed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import analysis
from . import sqlgen
from . import exec as exec_mod
from . import bench as bench_mod
from .dialects import get_dialect
from .lexer import LexError
from .parser import ParseError, parse_strata
from .analysis import StrataError, Checker, build_down_edges, blast_radius
from .diagnostic import format_diagnostic


def load(path: str, search_dirs=None):
    src = Path(path).read_text()
    module = parse_strata(src, path)
    proj = analysis.Project(module, search_dirs=search_dirs)
    return proj


def check(proj, model_names=None):
    ck = Checker(proj)
    tms = ck.check_all(model_names)
    return tms


def open_warehouse(output, read_only=False):
    """Open a warehouse connection for `-o`/`--output`.

    `postgres://...`/`postgresql://...` connects via psycopg2 (wrapped in
    dbcompat.PGConn, see strata/dbcompat.py); anything else is a DuckDB
    file path, or `:memory:` when `output` is falsy. `--dialect` (SQL
    emission) and `-o` (which engine to connect to) are independent and
    both explicit on purpose — no scheme-sniffing to infer one from the
    other. Raises RuntimeError with an actionable message if the needed
    driver isn't installed."""
    if output and output.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError(
                "postgres driver not available; pip install psycopg2-binary")
        from . import dbcompat
        return dbcompat.PGConn(psycopg2.connect(output))
    try:
        import duckdb
    except ImportError:
        raise RuntimeError(
            "duckdb not available; run with the venv interpreter "
            "(prototype/.venv/bin/python)")
    if read_only:
        return duckdb.connect(output, read_only=True)
    return duckdb.connect(output or ":memory:")


# ---------------------------------------------------------------- commands

def cmd_build(args):
    proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, 'search_dir', None) else None))
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


def render_build(proj, tms, names):
    """Deterministic build report: typed contracts + fingerprints + column
    lineage. Shared by `cmd_build` and the bench golden runner (Fase 4)."""
    out = []
    for name in names:
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
    return "\n".join(out).rstrip()


def cmd_compile(args):
    proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, 'search_dir', None) else None))
    tms = check(proj, args.model)
    dialect = getattr(args, "dialect", "duckdb")
    bad = exec_mod.check_physical_schema(dialect, tms)
    if bad:
        for name, issues in bad.items():
            for iss in issues:
                print(f"error: E070: {dialect}: model {name}: {iss}", file=sys.stderr)
        return 2
    try:
        dialect = get_dialect(dialect)
    except ValueError as ve:
        print(f"error: {ve}", file=sys.stderr)
        return 2
    names = args.model or (proj.model_names_for(None) or list(tms))
    print(sqlgen.full_sql(tms, names, dialect=dialect))
    return 0


def cmd_plan(args):
    proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, 'search_dir', None) else None))
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
    base_path = args.file
    if getattr(args, "head2", None):
        return _semantic_diff(base_path, args.head2,
                              search_dir=getattr(args, "search_dir", None),
                              json_mode=getattr(args, "json", False))
    proj = load(base_path, search_dirs=([args.search_dir] if getattr(args, 'search_dir', None) else None))
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


def _semantic_diff(base_path: str, head_path: str,
                   search_dir: str | None = None, json_mode: bool = False):
    """Fase 4: `strata lineage-diff base.strata head.strata` -- column-level
    semantic diff between two module versions (spec/compiler-design.md §7
    `ref1..ref2`): added/removed/retyped/narrowed per column + downstream
    impact from the BASE lineage graph. Exit 1 + E030 when breaking."""
    from .diff import diff_projects, impact_radius, render, to_json_dict

    def _load_checked(p):
        proj = load(p, search_dirs=([search_dir] if search_dir else None))
        diag = None
        try:
            check(proj)
        except StrataError as se:
            diag = f"{se.code}: {se}"
        return proj, diag

    base_proj, base_diag = _load_checked(base_path)
    head_proj, head_diag = _load_checked(head_path)
    changes = diff_projects(base_proj, head_proj)
    radius = impact_radius(base_proj.typed, changes)
    brk = [c for mc in changes for c in mc.columns if c.breaking]
    if json_mode:
        d = to_json_dict(base_path, head_path, changes, radius)
        d["compile_errors"] = [e for e in (base_diag, head_diag) if e]
        print(json.dumps(d, indent=2))
    else:
        if not changes and not (base_diag or head_diag):
            print("semantic diff: identical (no column-level changes)")
            return 0
        for line in render(changes, radius):
            print(line)
        for side, e in (("base", base_diag), ("head", head_diag)):
            if e:
                print(f"note: {side} module does not typecheck ({e}) -- "
                      "diff covers the models that compiled")
    if not json_mode and brk:
        print(f"\nE030: {len(brk)} breaking column change(s): "
              + ", ".join(f"{c.model}.{c.col} ({c.kind})" for c in brk))
        if radius:
            print(f"  -> {len(radius)} downstream consumer column(s) affected: "
                  + ", ".join(f"{n}.{c}" for n, c in sorted(radius)))
        print("  -> producer PR must NOT ship this change (cross-team contract)")
        # --json stdout stays a pure machine artifact: the breaking set is in
        # d["breaking"] and the exit code carries the verdict (1 = breaking).
    return 1 if brk else 0


def cmd_run(args):
    search = ([args.search_dir] if getattr(args, "search_dir", None) else [])
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
    return 0


def cmd_bench(args):
    """Fase 4: golden-file harness over deterministic artifacts (build, SQL
    per dialect, semantic diff). `--update` re-blesses after an INTENTIONAL
    compiler change; a mismatch without it is exit 1."""
    return bench_mod.run_cases(root=args.root, update=args.update)


def cmd_grammar(args):
    """Fase 4: emit the Strata grammar as llama.cpp GBNF for constrained decoding.

    The grammar is grammar-as-code (strata/grammar.py) kept in lockstep with the
    lexer by tests/test_grammar.py; this command validates it fail-loud (exit 2)
    and then prints the GBNF text ready to feed a constrained sampler. --doc
    annotates every rule with its spec sentence for human supervision. --check
    runs the independent consumer engine (strata/gbnf.py) over a program,
    proving the emitted grammar covers it (exit 0) or not (exit 2).
    """
    from . import grammar
    problems = grammar.validate()
    if problems:
        for pr in problems:
            print(f"error: grammar inconsistency: {pr}", file=sys.stderr)
        return 2
    if args.check:
        from . import gbnf, lexer
        g = gbnf.GbnfGrammar.from_text(grammar.emit_gbnf())
        toks = lexer.Lexer(Path(args.check).read_text()).tokenize()
        if gbnf.accepts_program(toks, g):
            print(f"{args.check}: accepted by the GBNF grammar (independent consumer)")
            return 0
        print(f"{args.check}: REJECTED by the GBNF grammar", file=sys.stderr)
        return 2
    if not args.doc:
        sys.stdout.write(grammar.emit_gbnf())
        return 0
    lines = ["# Strata GBNF grammar (spec/grammar.md, parser-authoritative)", ""]
    for name, (doc, _alts) in grammar.RULES.items():
        lines.append(f"# {name.replace('-', '_')}: {doc}")
    lines.append("")
    sys.stdout.write("\n".join(lines))
    sys.stdout.write(grammar.emit_gbnf())
    return 0


def cmd_dashboard(args):
    """Fase 4: one-screen supervision surface over a module (spec tooling row
    `strata dashboard`): typed models + contracts + fingerprints, DAG edges,
    staleness vs manifest, content-addressed run history and the protected-
    consumer blast surface. Render-only: no DB, deterministic, sorted. On a
    module that does not typecheck it still renders what compiled (fail-loud
    exit 1 with the diagnostic, same philosophy as the semantic diff)."""
    from .dashboard import build_dashboard, render

    path = args.file
    proj = load(path, search_dirs=([args.search_dir] if getattr(args, "search_dir", None) else None))
    diag = None
    try:
        tms = check(proj)
    except StrataError as se:
        diag = f"{se.code}: {se}"
        tms = proj.typed  # partial: whatever compiled before the error
    d = build_dashboard(proj, tms, path,
                        history=exec_mod.load_history(path),
                        manifest=exec_mod.load_manifest(path))
    if diag:
        d["compile_error"] = diag
    if getattr(args, "json", False):
        print(json.dumps(d, indent=2, sort_keys=True))
    else:
        print(render(d))
        if diag:
            print(f"note: module does not typecheck ({diag}) -- "
                  "dashboard covers the models that compiled")
    return 1 if diag else 0


def cmd_test(args):
    """strata test <file> [--model NAME] [--dialect D] [--output FILE] [--seed]
    Compile the module, then execute any declarative `test` blocks against the
    compiled views in a DuckDB warehouse (in-memory unless --output persists).

    Exit 0 on all-green; exit 1 on any failing test (message to stderr). A
    module with no `test` declarations is a no-op (exit 0) so the command is
    safe to gate CI on unconditionally."""
    proj = load(args.file,
                search_dirs=([args.search_dir] if getattr(args, 'search_dir', None) else None))
    tms = check(proj)
    try:
        dialect_obj = get_dialect(getattr(args, "dialect", "duckdb"))
    except ValueError as ve:
        print(str(ve), file=sys.stderr)
        return 4
    dialect_name = getattr(args, "dialect", "duckdb")
    # Fail-loud physical schema check before test materialization.
    bad = exec_mod.check_physical_schema(dialect_name, tms)
    if bad:
        for name, issues in bad.items():
            for iss in issues:
                print(f"error: E070: {dialect_name}: model {name}: {iss}", file=sys.stderr)
        return 4
    dialect = dialect_obj
    ck = Checker(proj)
    try:
        ck.check_tests()
    except StrataError as se:
        print(f"error: {se.code}: {se}", file=sys.stderr)
        return 1
    try:
        con = open_warehouse(getattr(args, "output", None))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if getattr(args, "seed", False):
        _run_seed(con, args.file)
    tested_models = [getattr(args, "model", None)] if getattr(args, "model", None) else None
    exec_mod.materialize(con, proj, tms, names=tested_models, dialect=dialect,
                         source_overrides=None, stage_only=False, branch="main")
    try:
        results = exec_mod.run_tests(con, proj, tms, tested_models, dialect, "main")
    except exec_mod.StrataTestError as te:
        print(f"test FAILED: {te}", file=sys.stderr)
        con.close()
        return 1
    for r in results:
        print(r)
    if getattr(args, "output", None):
        con.close()
    return 0


def cmd_check(args):
    """§16: strata check <file> [--dialect D] -- autonomous CI guard, sibling
    of plan/lineage-diff. Runs the compiled-model gates (E0xx fail-loud) plus
    a fail-loud dialect probe, prints the typed contracts and pins each model
    declares, and exits 0 only when everything is green. It NEVER materializes:
    the guard a PR runs in CI without side effects."""
    proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, 'search_dir', None) else None))
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
    for name in sorted(tms):
        tm = tms[name]
        cols = ", ".join(c.describe() for c in tm.schema.values()) or "(no columns)"
        # Phase-C pins declared by this model's contract (what `run` enforces
        # via exec.runtime_pins without materializing here).
        pins: list[str] = []
        if tm.contract:
            cd = proj.contracts.get(tm.contract)
            if cd is None:
                print(f"error: E061: unknown contract {tm.contract!r}", file=sys.stderr)
                return 1
            for f in cd.fields:
                bits: list[str] = []
                if f.nonnull:
                    bits.append("nonnull")
                if f.enum:
                    bits.append("enum{" + ",".join(f.enum) + "}")
                if f.primary:
                    bits.append("primary_key")
                elif f.unique:
                    bits.append("unique")
                pins.append(f"{name}.{f.name}:{'+'.join(bits) if bits else 'type'}")
        # Fail-loud dialect probe: same translator `run`/`compile` use, but
        # without touching any warehouse (CI guard has no side effects).
        try:
            sqlgen.model_sql(tm, dialect=dialect)
        except RuntimeError as re:
            print(f"error: E070: dialect {dialect.name!r} cannot express model {name!r}: {re}",
                  file=sys.stderr)
            return 4
        print(f"  ok  {name}")
        print(f"    contract  {cols}")
        print(f"    pins      {', '.join(pins) if pins else '(none)'}")
    print(f"  check OK: {len(tms)} model(s) green, dialect {dialect.name}, "
          "nothing materialized")
    return 0


def cmd_seed(args):
    """§15: `strata seed <file>` is the autonomous sibling of `run --seed`.

    It only loads the demo source fixtures (orders with its 5 rows, refunds
    with its 2 rows) into a duckdb warehouse and prints what was seeded --
    it does NOT run the DAG (that stays with `strata run`). Honors -o to
    persist to a .duckdb file, else in-memory (§14 :memory: byte-certain)."""
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
               "- `strata compile <file> --dialect postgres|bigquery|snowflake`  dialect-constrained SQL\n"
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


def cmd_branches(args):
    """Branch inventory of a persisted warehouse: staged (stg_<branch>__*) and
    live (v_*) views per branch. The rollback/promotion surface at a glance."""
    if not getattr(args, "output", None):
        print("(no warehouse: pass -o FILE.duckdb; an in-memory warehouse is always empty)")
        return 0
    is_dsn = args.output.startswith(("postgres://", "postgresql://"))
    if not is_dsn and not Path(args.output).exists():
        print(f"error: E083: warehouse {args.output!r} not found", file=sys.stderr)
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


def cmd_fmt(args):
    from .fmt import format_module
    proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, "search_dir", None) else None))
    text = format_module(proj.module)
    if getattr(args, "check", False):
        if Path(args.file).read_text() != text:
            print(f"{args.file}: not formatted (run strata fmt --write)")
            return 1
        print(f"{args.file}: formatted")
        return 0
    if getattr(args, "write", False):
        Path(args.file).write_text(text)
        print(f"formatted {args.file}")
        return 0
    print(text, end="")
    return 0


def cmd_lint(args):
    from .lint import lint
    proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, "search_dir", None) else None))
    tms = check(proj)
    warns = lint(proj, tms)
    for w in warns:
        print(f"  {w}")
    if warns:
        print(f"\nlint: {len(warns)} warning(s)")
        return 2 if getattr(args, "strict", False) else 0
    print("lint: clean")
    return 0


def cmd_replay(args):
    if getattr(args, "verify", None):
        try:
            e = exec_mod.verify_run(args.file, args.verify)
        except Exception as ex:
            print(f"error: E082: {ex}", file=sys.stderr)
            return 1
        print(f"verify OK: run {e['run_id']} stable ({len(e.get('fingerprints', {}))} model(s), no re-execution)")
        return 0
    if getattr(args, "execute", False):
        if not args.run_id:
            print("error: E082: --execute needs a run_id", file=sys.stderr)
            return 1
        proj = load(args.file, search_dirs=([args.search_dir] if getattr(args, "search_dir", None) else None))
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
        except StrataError as se:
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


def cmd_backfill(args):
    rec = exec_mod.find_run(args.file, args.run_id)
    if rec is None:
        print(f"error: E085: unknown run {args.run_id!r} (see strata replay)", file=sys.stderr)
        return 1
    search = ([args.search_dir] if getattr(args, "search_dir", None) else [])
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
        if src not in proj.sources:
            print(f"error: E085: unknown source {src!r}", file=sys.stderr)
            return 1
        overrides[src] = {"dataset": table.strip()}
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


def cmd_rollback(args):
    e = exec_mod.find_run(args.file, args.run_id)
    if e is None:
        print(f"error: E081: unknown run {args.run_id!r} (see strata replay)", file=sys.stderr)
        return 1
    fps = e.get("fingerprints", {})
    if getattr(args, "output", None):
        is_dsn = args.output.startswith(("postgres://", "postgresql://"))
        if not is_dsn and not Path(args.output).exists():
            print(f"error: E083: warehouse {args.output!r} not found "
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


def cmd_gc(args):
    """Snapshot retention: report (default) or drop (--apply) old run
    snapshots. Protected: the last --keep runs, the run live in the views and
    any publication whose metadata export is pending. History is never pruned,
    so a collected run's rollback/replay fails loud instead of reading wrong
    data."""
    if not getattr(args, "output", None):
        print("error: E084: --output <warehouse.duckdb> is required "
              "(snapshots live in the warehouse)", file=sys.stderr)
        return 1
    is_dsn = args.output.startswith(("postgres://", "postgresql://"))
    if not is_dsn and not Path(args.output).exists():
        print(f"error: E083: warehouse {args.output!r} not found", file=sys.stderr)
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="strata", description="declarative, versioned data transformations")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="typecheck + contracts + lineage (no DB)")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.add_argument("--strict", action="store_true",
                   help="fail (E014) unless every built model declares -> contract")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("compile", help="emit SQL")
    p.add_argument("file")
    p.add_argument("model", nargs="*")
    p.add_argument("--dialect", default="duckdb",
                   help="target warehouse: duckdb | postgres | bigquery | snowflake")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=cmd_compile)

    p = sub.add_parser("plan", help="compute stale set")
    p.add_argument("file")
    p.add_argument("--seed", action="store_true",
                   help="pin current fingerprints as content-addressed baseline "
                        "(self-pinning: identical re-runs see nothing stale)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("bench", help="golden-file artifacts: supervision + regression")
    p.add_argument("--update", action="store_true",
                   help="re-bless golden files after an intentional compiler change")
    p.add_argument("--root", default=".",
                   help="project root that case module paths resolve against")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("grammar", help="emit the Strata grammar as llama.cpp GBNF (constrained decoding)")
    p.add_argument("--doc", action="store_true",
                   help="prefix each rule with its spec sentence as a comment")
    p.add_argument("--check", metavar="FILE", default=None,
                   help="verify a .strata program is accepted by the emitted GBNF "
                        "(exit 0 accepted, 2 rejected) — independent consumer engine")
    p.set_defaults(fn=cmd_grammar)

    p = sub.add_parser("dashboard",
                       help="one-screen supervision surface: models, contracts, DAG, staleness, runs, blast surface")
    p.add_argument("file")
    p.add_argument("--json", action="store_true",
                   help="machine-readable dashboard (agent supervision artifact)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("lineage-diff", help="print lineage + blast radius")
    p.add_argument("file")
    p.add_argument("head2", nargs="?", default=None,
                   help="optional second module: column-level semantic diff file..head2")
    p.add_argument("--change", help="source col change to simulate, e.g. crm.orders:order_id")
    p.add_argument("--json", action="store_true",
                   help="machine-readable diff (agent supervision artifact)")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b")
    p.set_defaults(fn=cmd_lineage)

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
    p = sub.add_parser("seed", help="load demo source fixtures into a warehouse (duckdb required)")
    p.add_argument("file")
    p.add_argument("--output", "-o",
                   help="persist the seeded warehouse to this .duckdb file "
                        "(default: in-memory, discarded on exit)")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("fmt", help="canonical formatter (AST -> text, idempotent)")
    p.add_argument("file")
    p.add_argument("--write", action="store_true", help="rewrite file in place")
    p.add_argument("--check", action="store_true", help="exit 1 if not formatted (CI)")
    p.add_argument("--search-dir", default=None)
    p.set_defaults(fn=cmd_fmt)

    p = sub.add_parser("lint", help="static warnings (no DB)")
    p.add_argument("file")
    p.add_argument("--strict", action="store_true", help="exit 2 on warnings")
    p.add_argument("--search-dir", default=None)
    p.set_defaults(fn=cmd_lint)

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
    p.set_defaults(fn=cmd_replay)

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
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("branches", help="list staging branches in a warehouse (staged/live views per branch)")
    p.add_argument("-o", "--output", default=None, help="warehouse .duckdb file (default: describe :memory: as empty)")
    p.set_defaults(fn=cmd_branches)

    p = sub.add_parser("rollback", help="repoint manifest (and live views with -o) to a recorded run")
    p.add_argument("file")
    p.add_argument("run_id")
    p.add_argument("-o", "--output", default=None, help="warehouse file whose live v_* views to repoint")
    p.add_argument("--branch", default=None, help="staging branch to repoint from (default: run's recorded branch)")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("gc", help="snapshot retention: report (default) or drop (--apply) old run snapshots")
    p.add_argument("file")
    p.add_argument("-o", "--output", default=None, help="warehouse .duckdb file holding the snapshots")
    p.add_argument("--keep", type=int, default=2,
                   help="most recent runs to retain for rollback/replay (default: 2)")
    p.add_argument("--keep-days", type=float, default=None,
                   help="also retain any run with snapshots recorded within "
                        "this many days, regardless of --keep (default: off, "
                        "count-only)")
    p.add_argument("--apply", action="store_true", help="drop the reported tables (default: report only)")
    p.add_argument("--json", action="store_true", help="machine-readable plan (agent supervision artifact)")
    p.set_defaults(fn=cmd_gc)

    p = sub.add_parser("test", help="run declarative data tests against a module's models")
    p.add_argument("file")
    p.add_argument("--model", default=None, help="only run tests for this model (default: all tested models)")
    p.add_argument("--dialect", default="duckdb", help="warehouse for evaluation: duckdb | postgres | bigquery | snowflake")
    p.add_argument("--output", "-o", help="persist warehouse to .duckdb (default: in-memory, discarded on exit)")
    p.add_argument("--seed", action="store_true", help="load demo source fixtures before running tests")
    p.add_argument("--search-dir", default=None, help="extra dir resolving import a.b -> a/b.strata")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("check", help="validate a .strata artifact without materializing (CI guard)")
    p.add_argument("file")
    p.add_argument("--dialect", default="duckdb",
                   help="dialect probe target (default: duckdb)")
    p.set_defaults(fn=cmd_check)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ParseError, LexError, StrataError) as e:
        print(format_diagnostic(e), file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())