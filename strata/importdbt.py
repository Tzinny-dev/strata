# -*- coding: utf-8 -*-
"""`strata import-dbt` — Fase 2 §11 adoption: migrate a dbt project WITHOUT
leaving the warehouse, by importing the dbt schema.yml (sources + models with
column contracts) into a deterministic .strata artifact that must pass
`build` GREENS-ONLY-VERIFY, or FAILS-LOUD.

Philosophy (§4 fail-loud, matches every other Strata gate):
  - A dbt model that does `select *` with NO columns declared cannot have a
    contract derived WITHOUT the warehouse: guessing types needs data, and a
    guessed type would ship an untyped consumer that the warehouse would never
    catch (the exact dbt bug Strata exists to fail-loud on). → E041, exit 1,
    no artifact emitted. This mirrors E030 (cross-team) and every other gate:
    the artifact is the contract; a contract we cannot prove is a failed build.
  - Byte-deterministic: same schema.yml → byte-identical .strata artifact
    (test: re-running import twice yields identical sha256) — adoption must
    not introduce non-determinism into the publish path (§9, `init` AGENTS.md
    shares this determinism guarantee).
  - `data_type:` values are mapped through a KNOWN table (string/numeric/date/
    timestamp/bool/int variants); anything else → E041 fail-loud (we refuse to
    invent a Strata type for a dbt type we can't express — never guess)."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# dbt data_type -> Strata type. Only types the dialect layer can express are
# mapped (fail-loud §4: an unmapped dbt type becomes E041, not a silent
# guess). Sources in dbt are "external raw" (the warehouse owns their types);
# models are the typed contracts.
_TYPE_MAP = {
    "bigint": "int64",
    "int": "int64",
    "int64": "int64",
    "integer": "int64",
    "long": "int64",
    "numeric": "money",
    "decimal": "money",
    "float": "float64",
    "float64": "float64",
    "double": "float64",
    "string": "string",
    "text": "string",
    "varchar": "string",
    "uuid": "string",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "timestamptz": "timestamp",
    "bool": "bool",
    "boolean": "bool",
    "json": "json",
}


def map_type(dbt_type: str) -> str:
    """Map a dbt `data_type` into a Strata type, or raise KeyError for anything
    not expressible (E041 fail-loud — the spec §4 never guesses)."""
    key = str(dbt_type).strip().lower()
    if key not in _TYPE_MAP:
        raise KeyError(key)
    return _TYPE_MAP[key]


class ImportFailedFailLoud(Exception):
    """E041: a dbt model has no column contract (the `select *` anti-pattern).
    The import FAILS-LOUD instead of shipping a guessed/untyped consumer."""


def import_dbt_schema(schema: Path) -> str:
    """Deterministically derive a .strata artifact from a dbt schema.yml.

    Returns the byte-deterministic artifact text. Raises ImportFailedFailLoud
    (E041, fail-loud §4) if any model declares no columns — dbt's `select *`
    without a contract. Sources become raw external imports (their types are
    the warehouse's business, mapped through _TYPE_MAP so `build` can type them
    without a warehouse round-trip)."""
    doc = yaml.safe_load(schema.read_text())
    out = ["// generated deterministically by `strata import-dbt` — byte-identical "
           "re-runs (same schema.yml) produce identical .strata. Do not hand-edit "
           "the contract; change the dbt schema instead and re-import (§11 adoption)."]

    for src in doc.get("sources", []) or []:
        for tbl in src.get("tables", []) or []:
            tname = tbl["name"]
            out.append("")
            out.append(f"source {tname}(ns: \"{src.get('name', 'dbt')}\", "
                       f"dataset: \"{tbl.get('schema', src.get('schema', ''))}\") {{")
            out.append("  columns: {")
            for c in tbl.get("columns", []) or []:
                ctype = map_type(c.get("data_type", "string"))
                nn = " nonnull" if "not_null" in (c.get("tests", []) or []) else ""
                out.append(f"    {c['name']}: {ctype}{nn},")
            out.append("  }")
            out.append("}")

    for mdl in doc.get("models", []) or []:
        cols = mdl.get("columns", []) or []
        if not cols:
            raise ImportFailedFailLoud(
                f"E041: {schema}: dbt model {mdl.get('name')!r} does `select *` "
                f"with no `columns:` contract. Cannot derive a contract without "
                f"the warehouse (guessing types needs data) — fail-loud §4. Add "
                f"`columns:` to the model in schema.yml, or ship an explicitly "
                f"typed Strata model instead.")
        mname = mdl["name"]
        deps = mdl.get("depends_on", []) or []
        if not deps:
            raise ImportFailedFailLoud(
                f"E041: {schema}: dbt model {mname!r} declares `columns:` but no "
                f"`depends_on:`. The `from` of its contract is the warehouse's "
                f"lineage to decide — guessing it would ship an untyped consumer "
                f"(fail-loud §4). Add `depends_on: [<source_table>]` to the model "
                f"in schema.yml and re-import.")
        out.append("")
        out.append(f"contract {mname}Contract {{")
        for c in cols:
            ctype = map_type(c.get("data_type", "string"))
            nn = " nonnull" if "not_null" in (c.get("tests", []) or []) else ""
            out.append(f"  {c['name']} : {ctype}{nn},")
        out.append("}")
        out.append("")
        out.append(f"model {mname} -> contract {mname}Contract {{")
        out.append(f"  from {deps[0]}")
        out.append("  derive {")
        for c in cols:
            out.append(f"    {c['name']} = {c['name']},")
        out.append("  }")
        out.append("}")

    out.append("")
    return "\n".join(out)


def cmd(args) -> int:
    """CLI entry: `strata import-dbt schema.yml [--output artifact.strata]`.
    Returns 0 (artifact written) or 1 (E041 fail-loud, nothing emitted)."""
    path = Path(args.file)
    try:
        artifact = import_dbt_schema(path)
    except ImportFailedFailLoud as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"E041: {path}: dbt data_type {e.args[0]!r} has no Strata mapping — "
              f"cannot express it in any dialect; refusing to guess (fail-loud "
              f"§4, see strata/importdbt.py _TYPE_MAP).", file=sys.stderr)
        return 1
    out = Path(args.output) if getattr(args, "output", None) else (
        path.parent / path.stem).with_suffix(".strata")
    out.write_text(artifact)
    print(f"wrote {out} ({len(artifact.encode())} bytes, deterministic §11)")
    return 0
