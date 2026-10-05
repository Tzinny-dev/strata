# Strata — Lifecycle Commands Reference

This document covers the CLI commands that operate on the **lifecycle** of a Strata project:
materialization, replay, rollback, garbage collection, inspection, and quality tools.

---

## Quick Reference

| Command | Purpose | DB Required |
|---------|---------|-------------|
| `run` | Materialize models | DuckDB/Postgres/BQ/SF |
| `replay` | List/verify/re-execute past runs | DuckDB (optional) |
| `backfill` | Corrected re-run from a past run | DuckDB/Postgres/BQ/SF |
| `branches` | List staging branches | DuckDB/Postgres |
| `rollback` | Repoint live views to a past run | DuckDB/Postgres/Iceberg |
| `gc` | Snapshot retention (report/drop) | DuckDB/Postgres/Iceberg |
| `catalog` | Inspect Iceberg catalog | Iceberg |
| `fmt` | Canonical formatter (AST → text) | No |
| `lint` | Static warnings | No |
| `profile` | Performance breakdown | Optional |
| `bench` | Golden-file regression suite | No |
| `dashboard` | One-screen supervision surface | Optional |
| `seed` | Load demo fixtures | DuckDB |
| `import-dbt` | Import dbt schema.yml → .strata | No |

---

## `strata run` — Materialize Models

```bash
strata run <file.strata> [model...] [options]
```

Materializes models into the target warehouse.

**Key options:**
- `--env <name>` — Pipeline environment (default: `prod`)
- `--dialect <duckdb|postgres|bigquery|snowflake>` — Target warehouse
- `-o, --output <file.duckdb>` — Persist warehouse to file
- `--seed` — Load built-in demo fixtures first
- `--only-stale` — Only rematerialize stale models (default: false)
- `--full-refresh` — Force full rebuild ignoring staleness
- `--dry-run` — Compile SQL without executing
- `--stage-only` — Build staged views without promoting to live
- `--branch <name>` — Staging branch name (default: `main`)

**Exit codes:** `0`=success, `1`=check failure, `2`=materialization error, `3`=pin failure, `4`=unsupported dialect

---

## `strata replay` — List / Verify / Re-execute Past Runs

```bash
strata replay <file.strata> [run_id] [options]
```

Inspects or re-executes a content-addressed run record.

**Modes:**
- **List** (no `run_id`): Shows last N runs with status
- **Verify** (`--verify <run_id>`): Checks run stable without re-execution
- **Re-execute** (`--execute <run_id>`): Re-runs the recorded plan

**Options:**
- `--last N` — Number of recent runs to list (default: 10)
- `--verify <run_id>` — Verify run's pins and sources match current state
- `--execute <run_id>` — Re-execute the run (uses recorded branch/overrides/models)
- `--seed` — With `--execute`: seed demo sources into fresh warehouse
- `-o, --output <file.duckdb>` — With `--execute`: persist warehouse
- `--search-dir <dir>` — Extra import resolution directory
- `--iceberg-dir <dir>` — With `--verify`: also verify in Iceberg catalog
- `--verify-reader {duckdb,pyiceberg}` — Reader for catalog verification

**Examples:**
```bash
# List recent runs
strata replay project.strata

# Verify a run is still valid
strata replay project.strata --verify abc123def456

# Re-execute a past run
strata replay project.strata --execute abc123def456 -o warehouse.duckdb
```

---

## `strata backfill` — Corrected Re-run from Past Run

```bash
strata backfill <file.strata> <run_id> [options]
```

Re-runs a past run with corrected source overrides (e.g., fixed upstream data).

**Required:**
- `run_id` — The past run to base the correction on
- `--reason <text>` — Why this correction exists (recorded in history)

**Options:**
- `--models <csv>` — Subset of models to re-run (default: run's model set)
- `--source src=table` — Corrected source table (repeatable; wins over run's overrides)
- `--branch <name>` — Staging branch (default: run's branch)
- `--stage-only` — Build + pin staged views without promoting
- `-o, --output <file.duckdb>` — Persist warehouse
- `--search-dir <dir>` — Extra import resolution directory

**Example:**
```bash
# Re-run with corrected source data
strata backfill project.strata abc123def456 \
  --reason "Fix duplicate orders from upstream bug" \
  --source orders=orders_corrected \
  -o warehouse.duckdb
```

---

## `strata branches` — List Staging Branches

```bash
strata branches [options]
```

Lists all staging branches in a warehouse (staged/live views per branch).

**Options:**
- `-o, --output <file.duckdb>` — Warehouse file (default: in-memory)

**Example:**
```bash
strata branches -o warehouse.duckdb
# Output:
# Branch: main
#   Staged: v_main__daily_orders, v_main__monthly_revenue
#   Live:   v_main__daily_orders, v_main__monthly_revenue
# Branch: feature_xyz
#   Staged: v_feature_xyz__daily_orders
#   Live:   (none)
```

---

## `strata rollback` — Repoint to Past Run

```bash
strata rollback <file.strata> <run_id> [options]
```

Re-points live views (or Iceberg catalog) to a recorded run's snapshots.

**Options:**
- `-o, --output <file.duckdb>` — Warehouse whose live views to repoint
- `--iceberg-dir <dir>` — Repoint Iceberg catalog instead of warehouse views
- `--branch <name>` — Staging branch to repoint from (default: run's recorded branch)

**Fail-loud:** If any snapshot table is missing, fails with `E083` (cannot invent data).

**Example:**
```bash
# Repoint warehouse views to past run
strata rollback project.strata abc123def456 -o warehouse.duckdb

# Repoint Iceberg catalog
strata rollback project.strata abc123def456 --iceberg-dir /catalog
```

---

## `strata gc` — Snapshot Garbage Collection

```bash
strata gc <file.strata> [options]
```

Reports or drops old run snapshots based on retention policy.

**Options:**
- `-o, --output <file.duckdb>` — Warehouse holding snapshots
- `--iceberg-dir <dir>` — GC Iceberg catalog instead of warehouse
- `--keep N` — Most recent runs to retain (default: 2)
- `--keep-days N` — Also retain runs within N days (default: off)
- `--apply` — Actually drop tables (default: report only)
- `--json` — Machine-readable plan

**Retention rules:**
- Always keeps the `default` run (latest promoted)
- Always keeps the latest `--keep` runs per branch
- `--keep-days` adds time-based retention on top

**Examples:**
```bash
# Report what would be dropped
strata gc project.strata -o warehouse.duckdb

# Apply GC keeping 5 runs + last 30 days
strata gc project.strata -o warehouse.duckdb --keep 5 --keep-days 30 --apply

# GC Iceberg catalog
strata gc project.strata --iceberg-dir /catalog --keep 3 --apply
```

---

## `strata catalog` — Inspect Iceberg Catalog

```bash
strata catalog <catalog_dir> [options]
```

Inspects an Iceberg catalog (the `--iceberg-dir` of a run).

**Options:**
- `--run <run_id>` — Show tables with row counts for this run
- `--verify-reader {duckdb,pyiceberg}` — Reader for `--run` (default: duckdb)
- `--json` — Machine-readable runs/default listing

**Examples:**
```bash
# List all runs in catalog
strata catalog /catalog

# Show tables for a specific run
strata catalog /catalog --run abc123def456

# Machine-readable output
strata catalog /catalog --json
```

---

## `strata fmt` — Canonical Formatter

```bash
strata fmt <file.strata> [options]
```

Idempotent formatter: parses → emits canonical text. Same AST = same output.

**Options:**
- `--write` — Rewrite file in place
- `--check` — Exit 1 if not formatted (CI guard)
- `--search-dir <dir>` — Extra import resolution directory

**Examples:**
```bash
# Check formatting (CI)
strata fmt project.strata --check

# Rewrite in place
strata fmt project.strata --write
```

---

## `strata lint` — Static Warnings

```bash
strata lint <file.strata> [options]
```

Static analysis without a database connection.

**Options:**
- `--strict` — Exit 2 on warnings (CI gate)
- `--search-dir <dir>` — Extra import resolution directory

**Checks performed:**
- Unused model outputs
- Missing contracts on public models
- Deprecated syntax
- Naming convention violations
- Dead code (unreachable statements)

**Exit codes:** `0`=clean, `1`=errors, `2`=warnings (with `--strict`)

---

## `strata profile` — Performance Breakdown

```bash
strata profile <file.strata> [model...] [options]
```

Breaks down parse/check/emit time; optionally materializes models.

**Options:**
- `--run` — Also materialize each model and report timing + rows
- `--seed` — Load built-in demo fixtures first
- `-o, --output <file.duckdb>` — Persist warehouse
- `--dialect <duckdb|postgres|bigquery|snowflake>` — Target warehouse
- `--search-dir <dir>` — Extra import resolution directory

**Example output:**
```
Phase          Time (ms)   Rows
parse               12         -
check               45         -
emit (duckdb)       8         -
materialize m1      120      10k
materialize m2      85       5k
TOTAL               270      15k
```

---

## `strata bench` — Golden-File Regression Suite

```bash
strata bench [options]
```

Runs the golden-file test suite: compiles each case module and compares output to committed goldens.

**Options:**
- `--update` — Re-bless golden files after intentional compiler change
- `--root <dir>` — Project root for case module path resolution

**Structure:**
```
bench/
  cases/           # .strata input modules
  golden/          # Expected SQL output (committed)
  artifacts/       # Actual output (git-ignored)
```

**Exit codes:** `0`=all match, `1`=mismatch

---

## `strata dashboard` — Supervision Surface

```bash
strata dashboard <file.strata> [options]
```

One-screen supervision: models, contracts, DAG, staleness, runs, blast radius.

**Options:**
- `--json` — Machine-readable output (agent supervision artifact)
- `--search-dir <dir>` — Extra import resolution directory

**Example JSON output:**
```json
{
  "models": 12,
  "contracts": 8,
  "stale": ["daily_orders", "monthly_revenue"],
  "runs": 47,
  "last_run": "abc123def456 (2026-10-05T14:30:00Z)"
}
```

---

## `strata seed` — Load Demo Fixtures

```bash
strata seed <file.strata> [options]
```

Loads built-in demo source fixtures into a warehouse for quick testing.

**Options:**
- `-o, --output <file.duckdb>` — Persist seeded warehouse (default: in-memory)

---

## `strata import-dbt` — Import dbt Project

```bash
strata import-dbt <schema.yml> [options]
```

Translates dbt `schema.yml` (sources + models with columns) into a `.strata` artifact.

**Options:**
- `--models <dir>` — dbt `models/` dir: translate each `*.sql` into Strata model body
  - Supported subset: `select`/`where`/`group by`/`join`/`case`/`with` (CTEs)
  - Out-of-subset SQL fails loud `E042`
- `--output <file.strata>` — Output path (default: alongside schema.yml)

**Example:**
```bash
strata import-dbt dbt_project/schema.yml --models dbt_project/models -o project.strata
```

---

## Common Patterns

### CI Pipeline
```yaml
- name: Check formatting
  run: strata fmt project.strata --check

- name: Lint
  run: strata lint project.strata --strict

- name: Check (no DB)
  run: strata check project.strata --dialect duckdb

- name: Materialize
  run: strata run project.strata --dialect duckdb -o warehouse.duckdb
```

### Replay/Rollback Workflow
```bash
# 1. List recent runs
strata replay project.strata

# 2. Verify a run is still valid
strata replay project.strata --verify abc123def456

# 3. If source data was fixed, backfill
strata backfill project.strata abc123def456 \
  --reason "Fixed upstream deduplication" \
  --source orders=orders_fixed

# 4. Or rollback to a known-good run
strata rollback project.strata abc123def456 -o warehouse.duckdb
```

### GC Policy
```bash
# Weekly cron: keep last 7 runs + 30 days
strata gc project.strata -o warehouse.duckdb --keep 7 --keep-days 30 --apply
```

---

## Exit Codes Summary

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Check/validation failure |
| `2` | Materialization/execution error |
| `3` | Pin/contract failure |
| `4` | Unsupported dialect/feature |
| `5` | CLI usage error |

---

## See Also

- `strata check` — Validate artifact without materializing
- `strata graph` — Emit module DAG (DOT/Mermaid/Text)
- `strata lineage-diff` — Lineage + blast radius
- `strata test` — Run declarative data tests
- `strata plan` — Compute stale model set
- `strata compile` — Emit SQL without executing