"""Semantic diff between two versions of a Strata module.

spec/compiler-design.md §7: `strata lineage-diff ref1..ref2` -- column-level
diff -> PR comment. This is the Fase 4 "agent-native" artifact: supervision
happens over the semantic diff, never over reading the source
(propuesta-lenguaje-strata.md §1/§6).

Breaking taxonomy (what a consumer can hold onto):
  removed   - column gone                                -> breaking
  retyped   - output type changed (str(t) differs)       -> breaking
  narrowed  - nonnull -> nullable (NULLs now admitted)   -> breaking
  widened   - nullable -> nonnull (value superset)       -> non-breaking
  added     - new column                                 -> non-breaking
  contract  - protected/primary/unique/enum flag flip    -> informational
              (changes future blast radius, not today's consumers)
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple

from .analysis import TypedModel, blast_radius, source_decl_cols

BREAKING_KINDS = ("removed", "retyped", "narrowed")


@dataclass
class ColChange:
    model: str
    col: str
    kind: str            # added | removed | retyped | narrowed | widened | contract
    detail: str
    breaking: bool


@dataclass
class ModelChange:
    model: str
    kind: str            # added | removed | changed
    fingerprint_old: str = ""
    fingerprint_new: str = ""
    columns: List[ColChange] = field(default_factory=list)


def _contract_flags(c) -> str:
    bits = []
    if c.protected:
        bits.append("protected")
    if c.primary:
        bits.append("primary_key")
    if c.unique:
        bits.append("unique")
    if c.enum:
        bits.append("enum{" + ",".join(sorted(c.enum)) + "}")
    return ",".join(bits)


def _col_changes(name: str, b: dict, h: dict) -> List[ColChange]:
    """Shared column-level taxonomy over two {col: Col} schemas."""
    cols: List[ColChange] = []
    for cname in sorted(set(b) | set(h)):
        cb, ch = b.get(cname), h.get(cname)
        if cb is None:
            nn = "" if ch.nullable else " nonnull"
            cols.append(ColChange(name, cname, "added",
                                  f"new column {cname}: {ch.t}{nn}", False))
        elif ch is None:
            cols.append(ColChange(name, cname, "removed",
                                  f"dropped {cname}: {cb.t}", True))
        elif str(cb.t) != str(ch.t):
            cols.append(ColChange(name, cname, "retyped",
                                  f"{cname}: {cb.t} -> {ch.t}", True))
        else:
            if not cb.nullable and ch.nullable:
                cols.append(ColChange(name, cname, "narrowed",
                                      f"{cname}: nonnull -> nullable "
                                      "(NULLs now admitted)", True))
            elif cb.nullable and not ch.nullable:
                cols.append(ColChange(name, cname, "widened",
                                      f"{cname}: nullable -> nonnull", False))
            fb, fh = _contract_flags(cb), _contract_flags(ch)
            if fb != fh:
                cols.append(ColChange(name, cname, "contract",
                                      f"{cname}: [{fb or '-'}] -> [{fh or '-'}]",
                                      False))
    return cols


def diff_tms(base: Dict[str, TypedModel],
             head: Dict[str, TypedModel]) -> List[ModelChange]:
    """Column-level semantic diff between two checked module versions."""
    out: List[ModelChange] = []
    for name in sorted(set(base) | set(head)):
        b, h = base.get(name), head.get(name)
        if b is None:
            out.append(ModelChange(name, "added", fingerprint_new=h.fingerprint))
            continue
        if h is None:
            out.append(ModelChange(name, "removed", fingerprint_old=b.fingerprint))
            continue
        cols = _col_changes(name, b.schema, h.schema)
        if cols:
            out.append(ModelChange(name, "changed", b.fingerprint, h.fingerprint, cols))
    return out


def diff_projects(base_proj, head_proj) -> List[ModelChange]:
    """Full semantic diff: model outputs AND source (upstream catalog) schemas.
    Source schema changes are exactly the E030-32 story (types-and-contracts
    §5 phase B): a column that moved/narrowed upstream breaks every consumer.
    A model missing from `typed` because it failed to typecheck is reported as
    `compile-error` (fail-loud), never as `removed` (that would lie)."""
    changes = diff_tms(base_proj.typed, head_proj.typed)
    fixed: List[ModelChange] = []
    for mc in changes:
        if mc.kind == "removed" and mc.model in head_proj.models:
            mc.kind = "compile-error"
            mc.columns.append(ColChange(
                mc.model, "*", "compile-error",
                "model failed to typecheck in head (schema unknown)", True))
        elif mc.kind == "added" and mc.model in base_proj.models:
            mc.kind = "compile-error"
            mc.columns.append(ColChange(
                mc.model, "*", "compile-error",
                "model failed to typecheck in base (schema unknown)", True))
        fixed.append(mc)
    s_base = {n: {c.name: c for c in source_decl_cols(d)}
              for n, d in base_proj.sources.items()}
    s_head = {n: {c.name: c for c in source_decl_cols(d)}
              for n, d in head_proj.sources.items()}
    for name in sorted(set(s_base) | set(s_head)):
        cols = _col_changes(name, s_base.get(name, {}), s_head.get(name, {}))
        if cols:
            changes.append(ModelChange(name, "source-changed", columns=cols))
    return changes


def breaking_cols(changes: List[ModelChange]) -> List[Tuple[str, str]]:
    return [(c.model, c.col) for mc in changes for c in mc.columns if c.breaking]


def impact_radius(base: Dict[str, TypedModel],
                  changes: List[ModelChange]) -> List[Tuple[str, str]]:
    """Consumers affected by the breaking changes, via the BASE lineage graph
    (the question is who depended on the old version). With nullable
    propagation the diff flags every downstream column as breaking too, so we
    first reduce to the minimal roots -- breaking columns that are not
    themselves downstream of another breaking column -- and expand from those."""
    brk = breaking_cols(changes)
    if not brk:
        return []
    roots = []
    for b in brk:
        others = [x for x in brk if x != b]
        if b in blast_radius(base, others):
            continue  # downstream of another breaking change -> not a root
        roots.append(b)
    radius = blast_radius(base, roots)
    return [c for c in radius if c not in roots]


def render(changes: List[ModelChange],
           radius: List[Tuple[str, str]]) -> List[str]:
    lines: List[str] = []
    for mc in changes:
        if mc.kind == "added":
            lines.append(f"model {mc.model}  ADDED ({mc.fingerprint_new[:8]})")
        elif mc.kind == "removed":
            lines.append(f"model {mc.model}  REMOVED (was {mc.fingerprint_old[:8]})")
        elif mc.kind == "compile-error":
            lines.append(f"model {mc.model}  UNCHECKED (failed to typecheck)")
        elif mc.kind == "source-changed":
            lines.append(f"source {mc.model}  schema changed")
        else:
            lines.append(f"model {mc.model}  {mc.fingerprint_old[:8]} -> "
                         f"{mc.fingerprint_new[:8]}")
        for cc in mc.columns:
            line = f"  {cc.kind:<10} {cc.detail}"
            if cc.breaking:
                line += "  [BREAKING]"
            lines.append(line)
    if radius:
        lines.append("")
        lines.append("downstream impact (blast radius from base lineage):")
        for n, c in sorted(radius):
            lines.append(f"  {n}.{c}")
    return lines


def to_json_dict(base_path: str, head_path: str, changes, radius) -> dict:
    brk = [c for mc in changes for c in mc.columns if c.breaking]
    return {
        "base": base_path,
        "head": head_path,
        "models": [asdict(mc) for mc in changes],
        "breaking": [asdict(c) for c in brk],
        "radius": [list(r) for r in sorted(radius)],
    }
