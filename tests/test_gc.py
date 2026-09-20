"""Entrega C: snapshot retention / garbage collection (`strata gc`).

Protected: the last --keep runs, any run with snapshots recorded within
--keep-days (independent of --keep: a run needs to satisfy only one),
the run live in the views, and any publication whose metadata export is
pending. Report-only by default; --apply drops all-or-nothing. History is
never pruned, so a collected run's rollback/replay fails loud instead of
reading the wrong data. `strata run --gc` runs this automatically
(same policy as `strata gc --apply`) right after a successful publish.
"""
import datetime
import json
from unittest.mock import patch

import duckdb
import pytest

from strata import exec as ex
from strata.analysis import Checker, Project
from strata.cli import main
from strata.parser import parse_strata


@pytest.fixture
def warehouse(tmp_path):
    module = tmp_path / "p.strata"
    module.write_text('source s(ns: "test", dataset: "s") { columns: { id: int64 } }\n'
                      'model m { from s }\n')
    project = Project(parse_strata(module.read_text(), str(module)))
    tms = Checker(project).check_all()
    db = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE s(id BIGINT)")
    con.execute("INSERT INTO s VALUES (0)")
    yield con, project, tms, str(module), db
    con.close()


def three_runs(con, project, tms, module):
    """Distinct data per run -> distinct run ids (thus distinct snapshots)."""
    rids = []
    for value in (1, 2, 3):
        con.execute(f"UPDATE s SET id={value}")
        ex.run(con, project, tms, module)
        rids.append(ex.load_history(module)[-1]["run_id"])
    return rids


def tables_of(con, rid):
    return {t for t, r in ex.run_tables(con).items() if r == rid}


def backdate(module, run_id, days_ago):
    """Rewrite a history entry's `at` timestamp directly (bypassing the
    public API on purpose: this is test setup simulating an old run, not a
    thing `strata` itself ever does)."""
    hp = ex.history_path(module)
    at = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(days=days_ago)).isoformat()
    out = []
    for line in hp.read_text().splitlines():
        e = json.loads(line)
        if e.get("run_id") == run_id:
            e["at"] = at
        out.append(json.dumps(e, sort_keys=True))
    hp.write_text("\n".join(out) + "\n")


def test_gc_plan_protects_recent_and_live_runs(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    plan = ex.gc_plan(con, module, keep=2)
    assert plan["keep_runs"] == sorted({r2, r3})
    assert plan["retired_runs"] == [r1]
    assert set(plan["drop_tables"]) == tables_of(con, r1)
    assert any(t.startswith("snap_") for t in plan["drop_tables"])
    assert any(t.startswith("input_") for t in plan["drop_tables"])
    assert not set(plan["drop_tables"]) & set(plan["keep_tables"])


def test_gc_is_report_only_by_default(warehouse):
    con, project, tms, module, _ = warehouse
    three_runs(con, project, tms, module)
    before = ex.run_tables(con)
    plan = ex.gc_snapshots(con, module, keep=2)
    assert plan["applied"] is False
    assert plan["drop_tables"]
    assert ex.run_tables(con) == before


def test_gc_apply_drops_only_the_retired_run(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    plan = ex.gc_snapshots(con, module, keep=2, apply=True)
    assert plan["applied"] is True
    assert set(ex.run_tables(con).values()) == {r2, r3}
    assert con.execute("SELECT * FROM v_m").fetchall() == [(3,)]
    # History stays append-only: the collected run is still on record.
    assert {e["run_id"] for e in ex.load_history(module)} == {r1, r2, r3}


def test_gc_never_drops_a_live_snapshot_even_with_keep_zero(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    plan = ex.gc_snapshots(con, module, keep=0, apply=True)
    assert plan["applied"] is True
    assert set(ex.run_tables(con).values()) == {r3}
    assert con.execute("SELECT * FROM v_m").fetchall() == [(3,)]
    assert r3 in plan["keep_runs"]


def test_rollback_and_replay_to_a_collected_run_fail_loud(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    ex.gc_snapshots(con, module, keep=2, apply=True)
    assert set(ex.run_tables(con).values()) == {r2, r3}
    with pytest.raises(ex.PinError, match="missing"):
        ex.rollback_to_run(con, ex.find_run(module, r1))
    with pytest.raises(ex.PinError, match="missing"):
        ex.execute_run(con, project, tms, module, r1)
    # The retained run still rolls back for real.
    ex.rollback_to_run(con, ex.find_run(module, r2))
    assert con.execute("SELECT * FROM v_m").fetchall() == [(2,)]


def test_gc_protects_a_pending_metadata_export(warehouse):
    con, project, tms, module, _ = warehouse
    with patch.object(ex, "record_run", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            ex.run(con, project, tms, module)
    assert ex.load_history(module) == []
    plan = ex.gc_plan(con, module, keep=0)
    # The only evidence of that publication is its snapshot + outbox event.
    assert plan["drop_tables"] == []
    assert set(plan["keep_runs"]) == set(ex.run_tables(con).values())


def test_gc_refuses_a_corrupt_pending_entry(warehouse):
    con, project, tms, module, _ = warehouse
    with patch.object(ex, "save_manifest", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            ex.run(con, project, tms, module)  # export left pending
    # DuckDB enforces the JSON column, so the reachable corruption is valid
    # JSON that lost the run_id key: refuse rather than guess.
    con.execute("UPDATE strata_commits SET entry = '{\"a\": 1}'::JSON")
    with pytest.raises(ex.PinError, match="corrupt"):
        ex.gc_plan(con, module, keep=0)


def test_gc_rejects_negative_keep(warehouse):
    con, project, tms, module, _ = warehouse
    with pytest.raises(ex.PinError, match="keep"):
        ex.gc_plan(con, module, keep=-1)


def test_cli_gc_reports_then_applies(warehouse, capsys):
    con, project, tms, module, db = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    assert main(["gc", module, "-o", str(db)]) == 0
    out = capsys.readouterr().out
    assert "would drop" in out and r1 in out
    assert set(ex.run_tables(con).values()) == {r1, r2, r3}  # still report-only
    assert main(["gc", module, "-o", str(db), "--apply"]) == 0
    assert "dropped" in capsys.readouterr().out
    assert set(ex.run_tables(con).values()) == {r2, r3}
    assert main(["gc", module, "-o", str(db), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["applied"] is False and plan["drop_tables"] == []


def test_cli_gc_requires_output_and_existing_warehouse(warehouse, capsys):
    con, project, tms, module, db = warehouse
    assert main(["gc", module]) == 1
    assert "E084" in capsys.readouterr().err
    assert main(["gc", module, "-o", str(db.parent / "nope.duckdb")]) == 1
    assert "E083" in capsys.readouterr().err
    assert main(["gc", module, "-o", str(db), "--keep", "-1"]) == 1
    assert "keep" in capsys.readouterr().err


def test_keep_days_protects_a_run_keep_alone_would_drop(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    # keep=0 alone would drop everything (test_gc_never_drops_a_live_snapshot
    # already covers the live-view floor keeping r3); keep_days=1 protects r1
    # and r2 too, since they were "recorded" today, on top of that floor.
    plan = ex.gc_plan(con, module, keep=0, keep_days=1)
    assert plan["drop_tables"] == []
    assert set(plan["keep_runs"]) == {r1, r2, r3}


def test_keep_days_does_not_protect_backdated_runs(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    backdate(module, r1, days_ago=10)
    backdate(module, r2, days_ago=10)
    plan = ex.gc_plan(con, module, keep=0, keep_days=1)
    assert r1 not in plan["keep_runs"]
    assert r2 not in plan["keep_runs"]
    assert r3 in plan["keep_runs"]  # still live
    assert set(plan["drop_tables"]) == tables_of(con, r1) | tables_of(con, r2)


def test_keep_days_none_matches_prior_behavior(warehouse):
    con, project, tms, module, _ = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    with_none = ex.gc_plan(con, module, keep=2, keep_days=None)
    without_arg = ex.gc_plan(con, module, keep=2)
    assert with_none == without_arg


def test_gc_rejects_negative_keep_days(warehouse):
    con, project, tms, module, _ = warehouse
    with pytest.raises(ex.PinError, match="keep_days"):
        ex.gc_plan(con, module, keep=2, keep_days=-1)


def test_cli_gc_keep_days(warehouse, capsys):
    con, project, tms, module, db = warehouse
    r1, r2, r3 = three_runs(con, project, tms, module)
    backdate(module, r1, days_ago=10)
    assert main(["gc", module, "-o", str(db), "--keep", "0",
                "--keep-days", "1", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert r1 not in plan["keep_runs"]
    assert {r2, r3} <= set(plan["keep_runs"])


def test_cli_run_with_gc_flag_drops_retired_snapshots_automatically(tmp_path, capsys):
    module = tmp_path / "p.strata"
    module.write_text('source s(ns: "test", dataset: "s") { columns: { id: int64 } }\n'
                      'model m { from s }\n')
    db = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE s(id BIGINT)")
    con.execute("INSERT INTO s VALUES (0)")
    con.close()

    for value in (1, 2, 3):
        con = duckdb.connect(str(db))
        con.execute(f"UPDATE s SET id={value}")
        con.close()
        assert main(["run", str(module), "-o", str(db)]) == 0

    con = duckdb.connect(str(db))
    assert len(set(ex.run_tables(con).values())) == 3
    con.close()

    assert main(["run", str(module), "-o", str(db), "--gc", "--gc-keep", "0"]) == 0
    out = capsys.readouterr().out
    assert "gc: dropped" in out
    con = duckdb.connect(str(db))
    # keep=0 protects only the run live in the views (this run's own).
    assert len(set(ex.run_tables(con).values())) == 1
    assert con.execute("SELECT * FROM v_m").fetchall() == [(3,)]
    con.close()


