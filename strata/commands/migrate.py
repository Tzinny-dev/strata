"""`strata migrate <file>`: validate and upgrade a module's metadata
sidecars (run history + fingerprint manifest) to the current schema version.

Strict counterpart to the lenient everyday readers (load_history/
load_manifest): corrupt metadata fails loud (E097), files claimed by a
newer build fail loud (E098), and legacy v0 files are upgraded in place
without changing any run_id. See exec.migrate_metadata."""

from __future__ import annotations

from typing import Any

from .. import exec as exec_mod


def cmd_migrate(args: Any) -> int:
    """`strata migrate <file>`: validate metadata sidecars, upgrade legacy v0 files."""
    report = exec_mod.migrate_metadata(args.file)
    mh = report["history"]
    mm = report["manifest"]
    print(f"module {report['module']}")
    print(f"  run history : {mh['records']} record(s) @ schema v{mh['schema_version']}"
          f" ({mh['migrated']} upgraded)")
    if mm["exists"]:
        state = "upgraded" if mm["migrated"] else "current"
        print(f"  manifest    : schema v{mm['schema_version']} ({state})")
    else:
        print("  manifest    : absent (no fingerprints published yet)")
    return 0