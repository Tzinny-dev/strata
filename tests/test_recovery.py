"""Failure injection for the DuckDB publication outbox (single writer)."""
import json
from unittest.mock import patch

import duckdb
import pytest

from strata import exec as ex
from strata.analysis import Checker, Project
from strata.parser import parse_strata


@pytest.fixture
def pipeline(tmp_path):
    module = tmp_path / "p.strata"
    module.write_text('source s(ns: "test", dataset: "s") { columns: { id: int64 } }\n'
                      'model m { from s }\n')
    project = Project(parse_strata(module.read_text(), str(module)))
    tms = Checker(project).check_all()
    warehouse = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute("CREATE TABLE s(id BIGINT)")
    con.execute("INSERT INTO s VALUES (1)")
    yield con, project, tms, str(module), warehouse
    con.close()


@pytest.mark.parametrize("writer", ["record_run", "save_manifest"])
def test_recovers_after_commit_and_reopen(pipeline, writer):
    con, project, tms, module, warehouse = pipeline
    with patch.object(ex, writer, side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            ex.run(con, project, tms, module)
    assert con.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    event, raw, exported = con.execute(
        "SELECT event_id, entry, exported FROM strata_commits").fetchone()
    assert not exported
    payload = json.loads(raw)
    assert payload["fingerprints"] and payload["input_snapshots"]
    con.close()
    with duckdb.connect(str(warehouse)) as reopened:
        # No sources or re-execution needed to repair metadata.
        reopened.execute("DROP TABLE s")
        assert ex.recover_metadata(reopened, module) == [event]
        assert ex.recover_metadata(reopened, module) == []
        assert reopened.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    history = ex.load_history(module)
    assert len(history) == 1
    assert history[0]["commit_id"] == event
    assert history[0]["snapshots"] == payload["snapshots"]
    assert ex.load_manifest(module) == payload["fingerprints"]


def test_failure_before_commit_rolls_back_registry_and_snapshots(pipeline):
    con, project, tms, module, _ = pipeline
    ex.run(con, project, tms, module)
    history = ex.history_path(module).read_bytes()
    tables = con.execute("SHOW TABLES").fetchall()
    con.execute("UPDATE s SET id=2")
    real_record = ex._record_commit

    def fail_after_insert(*args):
        real_record(*args)
        raise OSError("before commit")

    with patch.object(ex, "_record_commit", side_effect=fail_after_insert):
        with pytest.raises(OSError, match="before commit"):
            ex.run(con, project, tms, module)
    assert con.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    assert con.execute("SHOW TABLES").fetchall() == tables
    assert con.execute("SELECT count(*) FROM strata_commits").fetchone() == (1,)
    assert ex.history_path(module).read_bytes() == history
    assert ex.recover_metadata(con, module) == []


def test_next_run_recovers_before_stale_check(pipeline):
    con, project, tms, module, _ = pipeline
    with patch.object(ex, "record_run", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            ex.run(con, project, tms, module)
    applied, _, note = ex.run(con, project, tms, module, only_stale=True)
    assert applied == [] and note
    assert len(ex.load_history(module)) == 1
    assert con.execute("SELECT count(*) FROM strata_commits").fetchone() == (1,)


def test_replay_recovers_missing_history_before_lookup(pipeline):
    con, project, tms, module, _ = pipeline
    with patch.object(ex, "record_run", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            ex.run(con, project, tms, module)
    raw = con.execute("SELECT entry FROM strata_commits").fetchone()[0]
    rid = json.loads(raw)["run_id"]
    con.execute("UPDATE s SET id=99")
    ex.execute_run(con, project, tms, module, rid)
    assert con.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    assert len(ex.load_history(module)) == 2
    assert ex.recover_metadata(con, module) == []


def test_recovery_preserves_intentional_rollback(pipeline):
    con, project, tms, module, _ = pipeline
    ex.run(con, project, tms, module)
    first = ex.load_history(module)[0]
    con.execute("UPDATE s SET id=2")
    with patch.object(ex, "save_manifest", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            ex.run(con, project, tms, module)
    ex.rollback_to_run(con, first)
    # Different marker detects even a same-code fingerprint overwrite.
    ex.save_manifest(module, {"m": "intentional-rollback"})
    assert len(ex.recover_metadata(con, module)) == 1
    assert con.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    assert ex.load_manifest(module) == {"m": "intentional-rollback"}
    assert len(ex.load_history(module)) == 2


def test_rollback_failure_keeps_views_and_manifest(pipeline):
    con, project, tms, module, _ = pipeline
    ex.run(con, project, tms, module)
    first = ex.load_history(module)[0]
    con.execute("UPDATE s SET id=2")
    ex.run(con, project, tms, module)
    with patch.object(ex, "save_manifest", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            ex.rollback_to_run(con, first, module_path=module)
    # Manifest write happens after commit -> views already repointed; the
    # journaled event is recovered without touching views again.
    assert con.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    pending = con.execute(
        "SELECT count(*) FROM strata_commits WHERE NOT exported").fetchone()
    assert pending == (1,)
    assert len(ex.recover_metadata(con, module)) == 1
    assert ex.load_manifest(module) == first["fingerprints"]
    assert ex.recover_metadata(con, module) == []


def test_rollback_recovery_survives_interrupted_export_and_reopen(pipeline):
    con, project, tms, module, warehouse = pipeline
    ex.run(con, project, tms, module)
    first = ex.load_history(module)[0]
    con.execute("UPDATE s SET id=2")
    ex.run(con, project, tms, module)
    # Interrupt export AFTER the transaction has committed. Closing and
    # reopening verifies durability; this is not a simulated SIGKILL.
    with patch.object(ex, "recover_metadata", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            ex.rollback_to_run(con, first, module_path=module)
    con.close()
    with duckdb.connect(str(warehouse)) as reopened:
        assert ex.recover_metadata(reopened, module) != []
        assert ex.load_manifest(module) == first["fingerprints"]


def test_rollback_without_module_path_skips_journal(pipeline):
    con, project, tms, module, _ = pipeline
    ex.run(con, project, tms, module)
    first = ex.load_history(module)[0]
    con.execute("UPDATE s SET id=2")
    ex.run(con, project, tms, module)
    ex.rollback_to_run(con, first)  # legacy signature: no journaling
    assert con.execute("SELECT * FROM v_m").fetchall() == [(1,)]
    assert ex.recover_metadata(con, module) == []


def test_rollback_before_commit_preserves_publication(pipeline):
    con, project, tms, module, _ = pipeline
    ex.run(con, project, tms, module)
    first = ex.load_history(module)[0]
    con.execute("UPDATE s SET id=2")
    ex.run(con, project, tms, module)
    ex.save_manifest(module, {"m": "second-publication"})
    before = ex.manifest_path(module).read_bytes()
    real_record = ex._record_commit

    def fail_after_insert(*args):
        real_record(*args)
        raise OSError("before commit")

    with patch.object(ex, "_record_commit", side_effect=fail_after_insert):
        with pytest.raises(OSError, match="before commit"):
            ex.rollback_to_run(con, first, module_path=module)
    assert con.execute("SELECT * FROM v_m").fetchall() == [(2,)]
    assert ex.manifest_path(module).read_bytes() == before
    assert con.execute("SELECT count(*) FROM strata_commits").fetchone() == (2,)
    assert ex.recover_metadata(con, module) == []


def test_cli_missing_snapshot_preserves_manifest(pipeline):
    from strata.cli import main

    con, project, tms, module, warehouse = pipeline
    ex.run(con, project, tms, module)
    first = ex.load_history(module)[0]
    con.execute("UPDATE s SET id=2")
    ex.run(con, project, tms, module)
    con.execute(f"DROP TABLE {first['snapshots']['m']}")
    ex.save_manifest(module, {"m": "second-publication"})
    before = ex.manifest_path(module).read_bytes()
    assert main(["rollback", module, first["run_id"], "-o", str(warehouse)]) == 1
    assert ex.manifest_path(module).read_bytes() == before
    assert con.execute("SELECT * FROM v_m").fetchall() == [(2,)]


def test_recovery_is_scoped_to_module(pipeline, tmp_path):
    con, project, tms, module, _ = pipeline
    with patch.object(ex, "record_run", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            ex.run(con, project, tms, module)
    other = str(tmp_path / "other.strata")
    assert ex.recover_metadata(con, other) == []
    assert not ex.history_path(other).exists()
    assert not ex.manifest_path(other).exists()
    assert con.execute("SELECT exported FROM strata_commits").fetchone() == (False,)


def test_atomic_replace_failure_keeps_previous_file(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text("old")
    with patch.object(ex.os, "replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError):
            ex._atomic_write(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]
