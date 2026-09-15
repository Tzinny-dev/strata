"""Strata linter: static warnings over the typed graph (no DB needed)."""
from __future__ import annotations
from typing import Dict, List
from .analysis import Project, TypedModel

def lint(proj: Project, tms: Dict[str, TypedModel]) -> List[str]:
    warns: List[str] = []
    for name, tm in tms.items():
        if not tm.contract:
            warns.append(f"W001 {name}: no contract (strict mode requires -> contract)")
        for cname, col in tm.schema.items():
            if col.classification and "protected" not in str(col.classification):
                pass
            if col.classification and not col.protected:
                warns.append(f"W004 {name}.{cname}: classification {col.classification!r} without protected/mask")
        decl = proj.models.get(name)
        if decl is not None and not decl.attrs.get("owner"):
            warns.append(f"W002 {name}: no owner (add owner: \"...\")")
        # SELECT * smell: model with no explicit projection after group
        if tm.plan is not None and not tm.plan.outputs:
            warns.append(f"W003 {name}: no explicit outputs (implicit SELECT *)")
    # unused models: never read as input nor in any pipeline
    used = set()
    for tm in tms.values():
        used.update(tm.deps)
    for p in proj.pipelines:
        try:
            used.update(proj.model_names_for(p, include_generated=True))
        except Exception:
            pass
    for name in tms:
        if name not in used and len(tms) > 1:
            warns.append(f"W005 {name}: unreachable (not read by any model/pipeline)")
    # duplicate fingerprints: two models with identical code+upstreams
    seen: dict = {}
    for name, tm in tms.items():
        if tm.fingerprint in seen:
            warns.append(f"W006 {name}: identical fingerprint to {seen[tm.fingerprint]} (duplicate?)")
        else:
            seen[tm.fingerprint] = name
    return sorted(warns)
