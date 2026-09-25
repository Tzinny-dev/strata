# Incrementality and backfills

No new syntax for the author: incrementality is decided by the engine, over
versioned layers, never with an `INSERT` that mutates what was published
(proposal §43 and §179). This delivery closes §4 with two pieces:

1. `run --only-stale` rebuilds the minimal set by code **and** by data
   (previously, any data change rebuilt everything).
2. `strata backfill` re-runs with corrected data under an explicit reason,
   audited in the history next to the corrected run.

## Staleness per source

Each run freezes `source_fingerprints` (schema hash + rows per source).
Under `--only-stale`, the current run compares source by source against the
latest run with snapshots of the branch:

- Only the models downstream of the changed sources are rebuilt,
  plus those whose code changed (transitive via fingerprints, as before).
- Intact branches keep their snapshots untouched.
- No changes: `everything up to date (nothing to do)`, nothing written.
- The rollback/different-warehouse patch is kept: if the live view does
  not expose the latest run's snapshot, the model is rebuilt.

The rebuilt set includes the stale's dependencies (the
materializer always builds transitive dependencies: reading a stale live
view would be worse than recomputing; if the content matches, the existing
snapshot is reused by identity, never overwritten). Changing an
override to a table with identical content does not rebuild: hashes are
resolved through the overrides, so content rules, not names.

## Backfill

```
strata backfill module.strata <run_id> --source orders=orders_fixed \
    --reason "corrected upstream extract" -o warehouse.duckdb
```

- It starts from the context of the given run (models, branch, overrides) and
  corrects it: `--models m1,m2` trims the selection, the repeatable
  `--source s=t` points sources at corrected tables (it wins over the run's
  override).
- `--reason` is mandatory: without a reason there is no backfill, just a
  run.
- It records `backfill_of` + `reason` in the history entry; `strata
  replay <id>` shows them. The selection is rebuilt as minimal (stale),
  so correcting a source only touches its downstream.
- Errors E085: unknown run, unknown model, malformed `--source`
  or with an unknown source, and any materialization `PinError`
  (including cardinality or contract violations).

A backfill does not rewrite history: the corrected run stays intact for
`rollback`/`replay`/`verify`, and the new run has its own content id
(same data + same selection = same id, idempotent).

## Row-level merge (`incremental merge_strategy`)

Distinct from the previous point (which decides which **models** to
rebuild): `merge_strategy: append`/`upsert` decides which **rows**, within a
model, enter the new snapshot without rereading everything already
processed.

```strata
model m {
  from orders
  incremental
  merge_strategy: upsert      -- or "append"; "replace" (default) is full rebuild
  merge_keys: [id]            -- only for upsert
  cdc_column: updated_at      -- required for append/upsert
}
```

On every run that is not the first for that model, `exec.materialize`
locates the previous snapshot (`snap_<run_id>_<model>` behind the current
`v_<model>` view) and computes the delta as the rows of the full
recomputation with `cdc_column > MAX(cdc_column)` from that snapshot:

- `append`: `previous_snapshot UNION ALL delta`. No dedup: a row with the
  same `merge_keys` as a previous one stays duplicated on purpose.
- `upsert`: rows from the previous snapshot whose `merge_keys` do **not**
  appear in the delta, plus the full delta (antijoin + union, never `NOT IN`
  to avoid `NULL` semantics). A delta row replaces any previous row with the
  same keys.

Constraints checked at compile time (not at execution):
unknown `merge_strategy` (E086), `upsert`/`append` on a model with
`group`/`aggregate` (E087 — re-aggregating only the delta without rereading
everything already merged is not sound, so it is rejected instead of
silently producing an incorrect aggregate), missing `cdc_column` as an actual
output column (E088), and `upsert` without valid `merge_keys` (E089).
`replace` (or no `merge_strategy`) has no extra
requirements: it remains the full rebuild as always.

**Source pushdown (closed 2026-09-19)**: when `cdc_column` is
resolvable within the scope of the base subquery itself —a direct
passthrough of the source, or a `let` already computed there— and the model
has no `join`/set-op/`expand`, the filter `cdc_column > watermark` is added
to `plan.preds` **before** compiling the SQL (the same mechanism `filter`
already uses), so the base subquery's own `WHERE` trims what
is read, not a later filter over the full recomputation:
`_pushdown_base_expr` in `strata/exec.py` decides eligibility;
`sqlgen._lit()` gained support for `datetime.date`/
`datetime.datetime` literals (the watermark is read with `MAX(cdc_column)`
against the previous snapshot, not parsed from Strata text) to be able to embed the
value. Verified by inspecting the generated SQL, not just the result:
the `WHERE` appears inside the `base` CTE, and `__full` (the wrapper from
the previous path) does not appear at all in this case.

Outside that case —`cdc_column` depends on a join, a set-op, an
`expand`, or only exists as an expression of the outer `select`/`derive`—
the behavior is exactly as before: everything is recomputed and
filtered afterwards (`__full`/`__delta`), correct but without the
scan savings. It never fails because of this; it is an opportunistic
optimization, not a requirement. The first execution of a model (no previous
snapshot) is always a full recomputation, whatever `merge_strategy` is.

Tests: `tests/test_incremental.py` (validation) and
`tests/test_incremental_merge.py` (real execution on DuckDB): the test
that distinguishes this from a full rebuild (mutating an already-merged row
without touching `cdc_column` must not show up in the next run), plus
`TestIncrementalPushdown` (the `WHERE` falls inside the base CTE for
passthrough/`let`; a `join_left` falls back to the previous path and is
still correct; a `date`-typed `cdc_column` also does pushdown) and
`TestDateTimeLiterals` (`_lit()` with `datetime.date`/`datetime.datetime`).

## Limits

- Minimum effective granularity of staleness per model (`run --only-stale`):
  the full **model**, unless it declares `incremental merge_strategy:
  append|upsert` (see above), in which case the model itself decides per row
  via `cdc_column`.
- `check`/`build`/`plan` do not execute: data staleness only decides in
  `run --only-stale` and `backfill`. `test` and `replay --execute`
  materialize in full (verification, not savings).
- Runs without snapshots in the history (e.g. `run --stage-only`)
  do not serve as a basis for staleness: the first run with snapshots
  rebuilds everything.

Errors: E085 backfill (see above), E086-E089 `merge_strategy` (see above);
the rest of the run vocabulary does not change. Pending: push the
`cdc_column` filter down to the sources (today it only trims the already
recomputed result), exempt the cardinality test when uniqueness is already
pinned, and GC by age.
