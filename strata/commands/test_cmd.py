"""Test commands: test, lsp"""

import sys
from typing import Any

from .. import exec as exec_mod
from ..utils import check, get_dialect, load, open_warehouse


def cmd_test(args: Any) -> int:
    """`strata test <file>`: run declarative data tests against a module's models."""
    search_dirs = [args.search_dir] if getattr(args, 'search_dir', None) else None
    proj = load(args.file, search_dirs=search_dirs)
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
    from ..analysis import Checker
    ck = Checker(proj)
    try:
        ck.check_tests()
    except Exception as se:
        print(f"error: {se}", file=sys.stderr)
        return 1
    try:
        con = open_warehouse(getattr(args, "output", None))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if getattr(args, "seed", False):
        _run_seed(con, args.file)
    model_arg = getattr(args, "model", None)
    tested_models: list[str] | None = [str(model_arg)] if model_arg else None
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


def cmd_lsp(args: Any) -> int:
    """`strata lsp`: run the LSP server on stdio (for VS Code etc.)."""
    from .. import lsp as lsp_mod
    lsp_mod.run()
    return 0


def _run_seed(con: Any, path: str) -> None:
    """Seed a warehouse with the built-in demo sources (from strata.seed)."""
    from ..seed import seed_sql  # lazy: seed imports live alongside examples
    con.execute(seed_sql()[0])