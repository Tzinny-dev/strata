"""Inspect commands: graph, profile, dashboard, lineage"""

import json
import sys
import time
from typing import Any

from .. import exec as exec_mod
from ..analysis import Project, StrataError, TypedModel, blast_radius, build_down_edges
from ..utils import (
    check,
    get_dialect,
    load,
    open_warehouse,
)


def render_graph(tms: dict[str, TypedModel], names: list[str], fmt: str = "dot") -> str:
    """DAG of the typed module: inputs (sources) plus models, with edges for
    reads and model dependencies. Deterministic (sorted), machine-parsable
    DOT by default; `mermaid` emits a Mermaid flowchart; `text` prints edges.
    When a model subset is selected, upstream models and their sources are
    pulled in so the slice is still a connected DAG."""
    selected = set(names)
    if selected != set(tms):
        frontier = list(selected)
        while frontier:
            n = frontier.pop()
            for d in tms[n].deps:
                if d in tms and d not in selected:
                    selected.add(d)
                    frontier.append(d)
    models = sorted(selected)
    sources = sorted({inp.node for n in models
                      for inp in tms[n].plan.inputs if inp.is_source})
    deps = sorted({(d, n) for n in models for d in tms[n].deps
                   if d in tms and d in selected})
    reads = sorted({(s, n) for n in models for (s, _c) in tms[n].reads
                    if s not in tms})

    def q(name: str) -> str:
        return '"' + name.replace('"', '\\"') + '"'

    if fmt == "mermaid":
        out = ["flowchart LR"]
        for s in sources:
            out.append(f'  {q(s)}[{q(s)}]')
        for n in models:
            out.append(f'  {q(n)}(( {n} ))')
        for s, n in reads:
            out.append(f"  {q(s)} --> {q(n)}")
        for d, n in deps:
            out.append(f"  {q(d)} --> {q(n)}")
        return "\n".join(out)
    if fmt == "text":
        out = ["model graph (sources + models):"]
        if sources:
            out.append("  sources  " + ", ".join(sources))
        for d, n in deps:
            out.append(f"  {d} -> {n}")
        for s, n in reads:
            out.append(f"  source {s} -> {n}")
        return "\n".join(out)
    out = ["digraph strata {"]
    for s in sources:
        out.append(f'  {q(s)} [shape=box];')
    for n in models:
        out.append(f'  {q(n)} [shape=ellipse];')
    for s, n in reads:
        out.append(f"  {q(s)} -> {q(n)};")
    for d, n in deps:
        out.append(f"  {q(d)} -> {q(n)};")
    out.append("}")
    return "\n".join(out)


def _fail_loud_contracts(proj: Project, tms: dict[str, TypedModel],
                         changes: list[tuple[str, str]],
                         radius: list[tuple[str, str]]) -> list[tuple[str, str]] | None:
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
    print("\nE030: change to protected/consumed column(s) breaks consumer contract:")
    for n, c in breaking:
        print(f"  producer {n}.{c} (protected/contract-bound)")
    print(f"  -> {len(consumed)} consumer column(s) depend on it:")
    for n, c in consumed:
        print(f"    {n}.{c}")
    print("  producer PR must NOT ship this change (cross-team contract, E030-32)")
    return breaking


def cmd_lineage(args: Any) -> int:
    """`strata lineage <file> [<file2>]`: column-level dependency edges, or a semantic diff vs a second module."""
    base_path = args.file
    if getattr(args, "head2", None):
        return _semantic_diff(base_path, args.head2,
                              search_dir=getattr(args, "search_dir", None),
                              json_mode=getattr(args, "json", False))
    search_dirs = [args.search_dir] if getattr(args, 'search_dir', None) else None
    proj = load(base_path, search_dirs=search_dirs)
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
                   search_dir: str | None = None,
                   json_mode: bool = False) -> int:
    """`strata lineage-diff base.strata head.strata` -- column-level
    semantic diff between two module versions (spec/compiler-design.md §7
    `ref1..ref2`): added/removed/retyped/narrowed per column + downstream
    impact from the BASE lineage graph. Exit 1 + E030 when breaking."""
    from ..diff import diff_projects, impact_radius, render, to_json_dict

    def _load_checked(p: str) -> tuple[Project, str | None]:
        """Load and typecheck a module; returns (project, first-error-string-or-None)."""
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


def cmd_dashboard(args: Any) -> int:
    """`strata dashboard <file>`: one-screen supervision surface over a module."""
    from ..dashboard import build_dashboard, render

    search_dirs = [args.search_dir] if getattr(args, "search_dir", None) else None
    path = args.file
    proj = load(path, search_dirs=search_dirs)
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


def _topo_order(tms: dict[str, TypedModel],
                names: list[str]) -> list[str]:
    """Deterministic topological order of a model subset (Kahn). Used by
    `strata profile --run` to materialize one model at a time while its
    upstreams are already live (v_*)."""
    wanted = set(names)
    remaining = set(wanted)
    order: list[str] = []
    while remaining:
        ready = sorted(n for n in remaining
                       if all(d not in wanted or d in order for d in tms[n].deps))
        if not ready:
            break  # cycle: let materialize fail loud later
        order.extend(ready)
        remaining -= set(ready)
    for n in sorted(names):
        if n not in order:
            order.append(n)
    return order


def render_profile(proj: Project, tms: dict[str, TypedModel], path: str,
                   dialect: Any, parse_ms: float, check_ms: float,
                   emit_ms: dict[str, float],
                   runs: list[tuple[str, float, int]] | None = None) -> str:
    """Deterministic (sorted) profile report: compile phases plus, when a run
    happened, per-model materialization time and row counts."""
    out = [f"profile: {path}   dialect {dialect.name}   "
           f"models {len(tms)}   sources {len(proj.sources)}",
           f"  parse       {parse_ms:7.2f} ms",
           f"  check       {check_ms:7.2f} ms"]
    for name in sorted(emit_ms):
        out.append(f"  emit        {emit_ms[name]:7.2f} ms   {name}")
    total = parse_ms + check_ms + sum(emit_ms.values())
    out.append(f"  total comp. {total:7.2f} ms")
    if runs:
        out.append("  run (materialize + pin + promote):")
        for name, ms, rows in runs:
            out.append(f"    {name:20s} {ms:7.2f} ms   {rows:6d} rows")
        out.append(f"    {'total':20s} {sum(ms for _, ms, _ in runs):7.2f} ms")
    return "\n".join(out)


def cmd_graph(args: Any) -> int:
    """`strata graph <file> [--format dot|mermaid|text]`: DAG of the typed module."""
    search_dirs = [args.search_dir] if getattr(args, "search_dir", None) else None
    proj = load(args.file, search_dirs=search_dirs)
    tms = check(proj)
    print(render_graph(tms, args.model or list(tms), fmt=args.format))
    return 0


def cmd_profile(args: Any) -> int:
    """`strata profile <file> [--dialect D] [--run] [--model M ...]`: performance breakdown."""
    path = args.file
    src = path.read_text() if hasattr(path, 'read_text') else open(path).read()
    from .. import analysis
    from ..parser import parse_strata

    t0 = time.time()
    module = parse_strata(src, path)
    parse_ms = (time.time() - t0) * 1000.0
    proj = analysis.Project(module,
                            search_dirs=([args.search_dir] if getattr(args, "search_dir", None) else None))
    t0 = time.time()
    tms = check(proj)
    check_ms = (time.time() - t0) * 1000.0
    try:
        dialect = get_dialect(getattr(args, "dialect", "duckdb"))
    except ValueError as ve:
        print(str(ve), file=sys.stderr)
        return 4
    names = args.model or list(tms)
    emit_ms: dict[str, float] = {}
    for name in _topo_order(tms, names):
        t0 = time.time()
        from .. import sqlgen
        sqlgen.model_sql(tms[name], dialect=dialect)
        emit_ms[name] = (time.time() - t0) * 1000.0
    runs = None
    if args.run:
        try:
            con = open_warehouse(getattr(args, "output", None))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if getattr(args, "seed", False):
            _run_seed(con, args.file)
        runs = []
        for name in _topo_order(tms, names):
            t0 = time.time()
            exec_mod.materialize(con, proj, tms, names=[name], dialect=dialect,
                                 branch="main", manage_transaction=True)
            ms = (time.time() - t0) * 1000.0
            rows = con.execute(
                f"SELECT count(*) FROM {exec_mod.promoted_name(name)}").fetchone()[0]
            runs.append((name, ms, rows))
        if getattr(args, "output", None):
            con.close()
    print(render_profile(proj, tms, path, dialect, parse_ms, check_ms, emit_ms,
                         runs=runs))
    return 0


def _run_seed(con: Any, path: str) -> None:
    """Seed a warehouse with the built-in demo sources (from strata.seed)."""
    from ..seed import seed_sql  # lazy: seed imports live alongside examples
    con.execute(seed_sql()[0])