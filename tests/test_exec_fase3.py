"""Fase 3: transactional staging + atomic swap (spec/compiler-design.md §8).

Every view materialization lands in a per-branch staging namespace
(stg_<branch>__*) and only the final swap is visible to consumers; no
staged view is ever queryable through the public name. Iceberg-parity is
DuckDB-parity here: CREATE OR REPLACE ... AS SELECT is the atomic metadata
repoint, staged views stay as rollback targets, and replay --verify checks
content-addressed stability WITHOUT re-execution.
"""
from pathlib import Path

import duckdb
import pytest

from strata import analysis, exec as exec_mod, parser


BASE = """
source orders(ns: "crm", dataset: "prod_orders") {
  columns: {
    order_id: int64 nonnull,
    country:  string nonnull,
  }
}

contract C0 {
  order_id: int64 nonnull
  ctry:     string nonnull
}

model m0 -> contract C0 {
  owner: "x"
  from orders
  select {
    order_id = orders.order_id,
    ctry     = upper(orders.country),
  }
}
"""


def _proj(tmp_path: Path, text: str = BASE):
    f = tmp_path / "p.strata"
    f.write_text(text)
    proj = analysis.Project(parser.parse_strata(text, str(f)))
    tms = analysis.Checker(proj).check_all()
    return proj, tms, str(f)


def test_stage_only_never_promotes(tmp_path):
    proj, tms, path = _proj(tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    applied, _ = exec_mod.materialize(con, proj, tms, branch="feat", stage_only=True)
    assert applied == ["m0"]
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert "stg_feat__m0" in have
    assert "v_m0" not in have  # stage-only must not touch the public name
    con.execute("SELECT count(*) FROM stg_feat__m0").fetchone()  # queryable via staged name


def test_swap_promotes_all_or_nothing(tmp_path):
    proj, tms, path = _proj(tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    exec_mod.materialize(con, proj, tms, branch="main", stage_only=True)
    have0 = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert "v_m0" not in have0
    # the public name only becomes queryable through the atomic swap
    exec_mod.swap_branch(con, ["m0"], "main")
    assert con.execute("SELECT count(*) FROM v_m0").fetchone()[0] == 0
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert {"v_m0", "stg_main__m0"} <= have


def test_legacy_swap_rolls_back_on_failing_test(tmp_path):
    """The legacy staged-view path (no run_id, e.g. standalone `strata
    test`) used to commit the branch swap BEFORE running declarative
    tests: a failing test left already-live views with no way back.
    swap_branch's `validate` (run inside its transaction, same contract
    publish_snapshots already had) closes that: a failing test rolls back
    the repoint, v_m0 never goes live."""
    text = BASE + "\ntest m0 { expect row_count == 999 }\n"
    proj, tms, path = _proj(tmp_path, text)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    con.execute("INSERT INTO orders VALUES (1, 'es')")
    with pytest.raises(exec_mod.StrataTestError):
        exec_mod.materialize(con, proj, tms, branch="main")
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert "v_m0" not in have


def test_layered_reads_in_one_run(tmp_path):
    # m1 reads m0: within a single staged run m0 must be read from the staging
    # view (never the public v_m0, which may still be last-known-good).
    text = BASE + """
model m1 -> contract C0 {
  owner: "x"
  from m0
  select {
    order_id = m0.order_id,
    ctry     = upper(m0.ctry),
  }
}
"""
    proj, tms, path = _proj(tmp_path, text)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    applied, _ = exec_mod.materialize(con, proj, tms, branch="feat", stage_only=True)
    assert applied == ["m0", "m1"]
    have = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert "stg_feat__m1" in have
    # rows flow through the layered staged reads
    con.execute("INSERT INTO orders VALUES (1, 'ES'), (2, 'MX')")
    exec_mod.materialize(con, proj, tms, branch="feat", stage_only=True)
    assert con.execute("SELECT count(*) FROM stg_feat__m1").fetchone()[0] == 2


def test_record_run_branch_and_verify(tmp_path):
    proj, tms, path = _proj(tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    exec_mod.run(con, proj, tms, path, branch="feat")
    hist = exec_mod.load_history(path)
    assert hist[-1]["branch"] == "feat"
    rid = hist[-1]["run_id"]
    e = exec_mod.verify_run(path, rid)  # no re-execution, fingerprints still match
    assert e["run_id"] == rid
    # semantic mutation of the model body -> verify must fail loud (PinError)
    f = Path(path)
    f.write_text(BASE.replace("upper(orders.country)", "lower(orders.country)"))
    with pytest.raises(exec_mod.PinError):
        exec_mod.verify_run(path, rid)


def test_rollback_manifest_and_branch_recorded(tmp_path):
    proj, tms, path = _proj(tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    exec_mod.run(con, proj, tms, path, branch="b2")
    hist = exec_mod.load_history(path)
    assert hist[-1]["branch"] == "b2"
    fps = hist[-1]["fingerprints"]
    assert set(fps) == {"m0"}
    # staged views survive the swap -> rollback has a data target; repoint is idempotent
    con2 = duckdb.connect(":memory:")
    con2.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    exec_mod.materialize(con2, proj, tms, branch="b2", stage_only=True)
    exec_mod.swap_branch(con2, ["m0"], "b2")
    exec_mod.swap_branch(con2, ["m0"], "b2")  # idempotent repoint
    assert con2.execute("SELECT count(*) FROM v_m0").fetchone()[0] == 0


def test_execute_run_replays_from_record(tmp_path):
    proj, tms, path = _proj(tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    con.execute("INSERT INTO orders VALUES (1, 'ES'), (2, 'MX')")
    exec_mod.run(con, proj, tms, path, branch="feat")
    rid = exec_mod.load_history(path)[-1]["run_id"]
    applied, pins, orig = exec_mod.execute_run(con, proj, tms, path, rid)
    assert applied == ["m0"]
    assert con.execute("SELECT count(*) FROM v_m0").fetchone()[0] == 2
    hist = exec_mod.load_history(path)
    assert len(hist) == 2
    assert hist[-1]["replay_of"] == rid          # append-only lineage
    assert hist[-1]["branch"] == "feat"          # environment from the RECORD
    # drifted module never re-executes (fail-loud)
    f = Path(path)
    f.write_text(BASE.replace("upper(orders.country)", "lower(orders.country)"))
    with pytest.raises(exec_mod.PinError):
        exec_mod.execute_run(duckdb.connect(":memory:"), proj, tms, path, rid)


def test_execute_run_rejects_unknown_run(tmp_path):
    proj, tms, path = _proj(tmp_path)
    with pytest.raises(exec_mod.PinError):
        exec_mod.execute_run(duckdb.connect(":memory:"), proj, tms, path, "deadbeef0000")


def test_warehouse_branches_inventory(tmp_path):
    proj, tms, path = _proj(tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    con.execute("CREATE TABLE refunds(x BIGINT)")  # un-namespaced table lands on main
    exec_mod.materialize(con, proj, tms, branch="feat", stage_only=True)
    exec_mod.materialize(con, proj, tms, branch="b2", stage_only=True)
    exec_mod.swap_branch(con, ["m0"], "feat")
    inv = exec_mod.warehouse_branches(con)
    assert inv["feat"]["staged"] == ["m0"]
    assert inv["b2"]["staged"] == ["m0"] and inv["b2"].get("live", []) == []
    # the live v_m0 view belongs to the promoted-name namespace, reported on main
    assert inv["main"]["live"] == ["m0"]
