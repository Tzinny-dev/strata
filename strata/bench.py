"""Fase 4: bench harness -- golden files as supervision + regression artifacts.

`strata bench` runs every case in bench/manifest.json, materializing the
DETERMINISTIC artifacts of each module (typed build report, per-dialect SQL,
semantic diff payload) and comparing them byte-for-byte against golden files
in bench/golden/. Supervision story (propuesta §6): an LLM-generated module
is reviewed through these artifacts, never by reading source; here they also
pin the current compiler behavior as regression tests.

Exit codes: 0 all green; 1 mismatch/drift (run `strata bench --update` to
re-bless goldens after an INTENTIONAL compiler change); 2 harness error.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .analysis import Checker, StrataError
from .dialects import get_dialect
from .sqlgen import full_sql
from .diff import diff_projects, impact_radius, to_json_dict

BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"
DIALECTS = ("duckdb", "postgres", "snowflake", "bigquery")


def _run_module(root: Path, case: dict) -> list[str]:
    from .cli import load, check, render_build  # lazy: cli imports this module
    mod = root / case["module"]
    proj = load(str(mod))
    tms = check(proj)
    names = proj.model_names_for(None)
    out = [f"# build: {case['module']}", render_build(proj, tms, names), ""]
    for d in DIALECTS:
        dialect = get_dialect(d)
        out.append(f"# sql: {d}")
        out.append(full_sql(tms, names, dialect=dialect))
        out.append("")
    return out


def _run_diff(root: Path, case: dict) -> list[str]:
    from .cli import load, check  # lazy: cli imports this module
    def _load(p):
        proj = load(str(root / p))
        diag = None
        try:
            check(proj)
        except StrataError as se:
            diag = f"{se.code}: {se}"
        return proj, diag

    base_proj, base_diag = _load(case["base"])
    head_proj, head_diag = _load(case["head"])
    changes = diff_projects(base_proj, head_proj)
    radius = impact_radius(base_proj.typed, changes)
    d = to_json_dict(case["base"], case["head"], changes, radius)
    d["compile_errors"] = [e for e in (base_diag, head_diag) if e]
    return [json.dumps(d, indent=2, sort_keys=True)]


def run_cases(root: Path | None = None, update: bool = False) -> int:
    root = Path(root) if root else Path.cwd()
    manifest = json.loads((BENCH_DIR / "manifest.json").read_text())
    fails: list[str] = []
    for case in manifest["cases"]:
        safe = case["name"].replace("/", "__")
        gdir = BENCH_DIR / "golden"
        gdir.mkdir(parents=True, exist_ok=True)
        golden = gdir / f"{safe}.golden"
        try:
            if case["type"] == "module":
                lines = _run_module(root, case)
            elif case["type"] == "diff":
                lines = _run_diff(root, case)
            else:
                print(f"error: unknown case type {case['type']!r}", file=sys.stderr)
                return 2
        except StrataError as se:
            lines = [f"# compile error (fail-loud)\n{se.code}: {se}"]
        got = "\n".join(lines) + "\n"
        if update:
            golden.write_text(got)
            print(f"  blessed   {os.path.relpath(golden, root)}")
            continue
        if not golden.exists():
            fails.append(f"{case['name']}: MISSING golden {golden.name} "
                         "(run `strata bench --update`)")
            continue
        want = golden.read_text()
        if got == want:
            print(f"  ok        {case['name']}")
        else:
            fails.append(f"{case['name']}: golden MISMATCH "
                         f"({os.path.relpath(golden, root)})")
    if update:
        print(f"bench: goldens updated under {BENCH_DIR / 'golden'}")
        return 0
    if fails:
        for f in fails:
            print(f"  FAIL      {f}")
        print(f"bench: {len(fails)} mismatch(es) -- artifacts drifted from "
              "goldens (or goldens missing)")
        return 1
    print("bench: all green")
    return 0
