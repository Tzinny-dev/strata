"""Fase 4: `strata dashboard` -- one-screen supervision surface over a typed
module. Pure render layer: every fact it shows already exists in the compiler
(typed models, contracts, fingerprints, column lineage, manifest staleness,
content-addressed run history). Deterministic and sorted, so the same module
state always renders byte-identically; `--json` exposes the same dict for
agent consumption (spec/compiler-design.md tooling row `strata dashboard`,
and the supervision story of propuesta §6: humans/agents supervise through
artifacts, not by reading source)."""

from __future__ import annotations

from typing import Dict, List

from .analysis import TypedModel, build_down_edges


def build_dashboard(proj, tms: Dict[str, TypedModel], path: str,
                    history: List[dict] | None = None,
                    manifest: Dict[str, str] | None = None) -> dict:
    """Collect the dashboard facts. `history` is the content-addressed run
    log (exec.load_history), `manifest` the last-applied fingerprints
    (exec.load_manifest); both optional so the dashboard degrades cleanly on
    a module that was never materialized."""
    manifest = manifest if manifest is not None else {}
    down = build_down_edges(tms)

    models = []
    edges: List[List[str]] = []
    for name in sorted(tms):
        tm = tms[name]
        cols = list(tm.schema.values())
        # `protected` is authoritative in the contract decl: verify_contract
        # checks type/nullability/enum but does not stamp flags onto the
        # inferred output cols, so read them from the contract (plus any the
        # model body inherited from a protected source column).
        cd = proj.contracts.get(tm.contract) if tm.contract else None
        protected = sorted({f.name for f in (cd.fields if cd else []) if f.protected}
                           | {c.name for c in cols if c.protected})
        src_reads = sorted({node for (node, _c) in tm.reads if node in proj.sources})
        model_deps = sorted({d for d in tm.deps if d in tms and d != name})
        models.append({
            "name": name,
            "contract": tm.contract or "",
            "fingerprint": tm.fingerprint,
            "columns": len(cols),
            "protected": protected,
            "reads_sources": src_reads,
            "deps": model_deps,
            "stale": manifest.get(name) != tm.fingerprint,
        })
        for dep in model_deps:
            edges.append([dep, name])

    # Cross-team blast surface: protected producer columns that downstream
    # models consume (the reverse-edge set Down(m,c) of types-and-contracts §5).
    protected_consumed = []
    for name in sorted(tms):
        tm = tms[name]
        cd = proj.contracts.get(tm.contract) if tm.contract else None
        prot = ({f.name for f in (cd.fields if cd else []) if f.protected}
                | {c.name for c in tm.schema.values() if c.protected})
        for cname in sorted(prot):
            cons = sorted(f"{m}.{col}" for (m, col) in down.get((name, cname), []))
            if cons:
                protected_consumed.append({"col": f"{name}.{cname}", "consumers": cons})

    stale = sorted(m["name"] for m in models if m["stale"])
    runs = []
    for e in (history or [])[-3:]:
        runs.append({
            "run_id": e.get("run_id", ""),
            "branch": e.get("branch", "main"),
            "at": e.get("at", ""),
            "applied": len(e.get("applied", e.get("names", []))),
            "dialect": e.get("dialect", "duckdb"),
        })

    sources = sorted(proj.sources)
    return {
        "module": path,
        "sources": sources,
        "models": models,
        "edges": sorted(edges),
        "protected_consumed": protected_consumed,
        "stale": stale,
        "runs": runs,
        "health": {
            "n_models": len(models),
            "n_sources": len(sources),
            "n_edges": len(edges),
            "n_stale": len(stale),
            "n_protected_consumed": len(protected_consumed),
        },
    }


def render(d: dict) -> str:
    h = d["health"]
    out = [f"DASHBOARD {d['module']}",
           f"models {h['n_models']}  sources {h['n_sources']}  edges {h['n_edges']}"
           f"  stale {h['n_stale']}  protected-consumed {h['n_protected_consumed']}"]
    if d.get("compile_error"):
        out.append(f"typecheck FAILED ({d['compile_error']}) -- surface covers what compiled")
    out.append("")
    out.append("models:")
    for m in d["models"]:
        label = f"contract {m['contract']}" if m["contract"] else "(no contract)"
        flag = "  STALE" if m["stale"] else ""
        out.append(f"  {m['name']}  {label}  fp {m['fingerprint'][:8]}{flag}")
        out.append(f"    columns {m['columns']}  protected [{', '.join(m['protected']) or '-'}]"
                   f"  reads [{', '.join(m['reads_sources']) or '-'}]"
                   f"  deps [{', '.join(m['deps']) or '-'}]")
    out.append("")
    out.append("lineage (model edges):")
    out.extend(f"  {a} -> {b}" for a, b in d["edges"] or [["(none)", ""]][:1] if b)
    if not d["edges"]:
        out.append("  (none)")
    out.append("")
    out.append("protected columns consumed downstream (blast surface):")
    if d["protected_consumed"]:
        for pc in d["protected_consumed"]:
            out.append(f"  {pc['col']} <- {', '.join(pc['consumers'])}")
    else:
        out.append("  (no protected column has consumers)")
    out.append("")
    out.append("stale vs manifest: " + (", ".join(d["stale"]) if d["stale"] else "none"))
    out.append("")
    out.append("runs (content-addressed history, last 3):")
    if d["runs"]:
        for r in d["runs"]:
            out.append(f"  {r['run_id']}  branch {r['branch']}  applied {r['applied']}"
                       f"  dialect {r['dialect']}")
    else:
        out.append("  (none recorded)")
    return "\n".join(out)