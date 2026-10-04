"""Quality commands: fmt, lint, check"""

import sys
from typing import Any

from .. import exec as exec_mod
from ..utils import check, get_dialect, load


def cmd_fmt(args: Any) -> int:
    """`strata fmt <file>`: canonical formatting — prints (default), writes in place, or --check."""
    from ..fmt import format_module
    search_dirs = [args.search_dir] if getattr(args, "search_dir", None) else None
    proj = load(args.file, search_dirs=search_dirs)
    text = format_module(proj.module)
    if getattr(args, "check", False):
        from pathlib import Path
        if Path(args.file).read_text() != text:
            print(f"{args.file}: not formatted (run strata fmt --write)")
            return 1
        print(f"{args.file}: formatted")
        return 0
    if getattr(args, "write", False):
        from pathlib import Path
        Path(args.file).write_text(text)
        print(f"formatted {args.file}")
        return 0
    print(text, end="")
    return 0


def cmd_lint(args: Any) -> int:
    """`strata lint <file>`: static warnings over the typed module; --strict turns warnings into a failure."""
    from ..lint import lint
    search_dirs = [args.search_dir] if getattr(args, "search_dir", None) else None
    proj = load(args.file, search_dirs=search_dirs)
    tms = check(proj)
    warns = lint(proj, tms)
    for w in warns:
        print(f"  {w}")
    if warns:
        print(f"\nlint: {len(warns)} warning(s)")
        return 2 if getattr(args, "strict", False) else 0
    print("lint: clean")
    return 0


def cmd_check(args: Any) -> int:
    """`strata check <file> [--dialect D]`: autonomous CI guard, sibling of plan/lineage-diff."""
    search_dirs = [args.search_dir] if getattr(args, 'search_dir', None) else None
    proj = load(args.file, search_dirs=search_dirs)
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
            from .. import sqlgen
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