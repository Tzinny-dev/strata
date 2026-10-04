"""Project commands: init, import-dbt"""

import sys
from pathlib import Path
from typing import Any


def cmd_init(args: Any) -> int:
    """`strata init <dir>`: scaffold a project — writing a deterministic AGENTS.md for Strata."""
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


def cmd_import_dbt(args: Any) -> int:
    """Import a dbt schema.yml into a deterministic .strata artifact."""
    from strata.importdbt import (
        ImportFailedFailLoud,
        TransformFailLoud,
        import_dbt_project,
        import_dbt_schema,
    )
    path = Path(args.file)
    model_dir = getattr(args, "models", None)
    try:
        if model_dir:
            artifact = import_dbt_project(path, Path(model_dir))
        else:
            artifact = import_dbt_schema(path)
    except TransformFailLoud as e:
        print(str(e), file=sys.stderr)
        return 1
    except ImportFailedFailLoud as e:
        msg = str(e)
        print(msg if msg.startswith("E041:") else f"E041: {msg}", file=sys.stderr)
        return 1
    out = Path(getattr(args, 'output', None) or (path.parent / path.stem).with_suffix('.strata'))
    out.write_text(artifact)
    print(f"wrote {out}")
    return 0