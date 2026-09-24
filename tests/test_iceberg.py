"""Iceberg publication backend (propuesta-iceberg.md L1).

The engine still computes in DuckDB and freezes run-addressed snapshot
TABLES; `strata/iceberg.py` copies those committed snapshots out to real
Iceberg tables under a lakehouse catalog dir and records a deterministic
manifest. Fail-loud: missing extension -> no side effects; missing snapshot
-> nothing exported; partial export -> no manifest written.
"""
from pathlib import Path

import duckdb
import pytest

from strata import analysis, exec as exec_mod, parser
from strata import iceberg as iceberg_mod

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

model m1 -> contract C0 {
  owner: "x"
  from orders
  select {
    order_id = orders.order_id,
    ctry     = concat(orders.country, "!"),
  }
}
"""


def _proj(tmp_path: Path, text: str = BASE):
    f = tmp_path / "p.strata"
    f.write_text(text)
    proj = analysis.Project(parser.parse_strata(text, str(f)))
    tms = analysis.Checker(proj).check_all()
    return proj, tms, str(f)


def _con_and_seed():
    con = duckdb.connect(":memory:")
    con.execute("INSTALL iceberg")
    con.execute("LOAD iceberg")
    con.execute("CREATE TABLE orders(order_id BIGINT, country VARCHAR)")
    con.execute("INSERT INTO orders VALUES (1, 'es'), (2, 'us'), (3, 'de')")
    return con


def _run_full(tmp_path: Path, catalog: Path, text: str = BASE,
              con=None):
    """Full engine run + iceberg export; returns (history entry, manifest)."""
    con = con or _con_and_seed()
    proj, tms, path = _proj(tmp_path, text)
    applied, pins, note = exec_mod.run(con, proj, tms, path)
    entry = exec_mod.load_history(path)[-1]
    manifest = iceberg_mod.export_run(con, entry["run_id"],
                                      entry["snapshots"], catalog)
    return entry, manifest, con


def test_export_writes_readable_iceberg_table(tmp_path):
    catalog = tmp_path / "lakehouse"
    entry, manifest, con = _run_full(tmp_path, catalog)
    rel = manifest["runs"][entry["run_id"]]["m0"]
    tbl_dir = catalog / rel
    assert (tbl_dir / "metadata").exists()
    # Independent DuckDB connection reads the published Iceberg table back.
    other = duckdb.connect(":memory:")
    other.execute("INSTALL iceberg")
    other.execute("LOAD iceberg")
    rows = other.execute(
        f"SELECT * FROM iceberg_scan('{tbl_dir}') ORDER BY order_id").fetchall()
    assert rows == [(1, 'ES'), (2, 'US'), (3, 'DE')]


def test_manifest_is_deterministic_and_accumulates(tmp_path):
    catalog = tmp_path / "lakehouse"
    entry1, manifest, con = _run_full(tmp_path, catalog)
    path_m1 = manifest["runs"][entry1["run_id"]]
    assert set(path_m1) == {"m0", "m1"}  # both exported this run
    # Same catalog, same content, second run: manifest merges (never clobbers).
    _, manifest2, _ = _run_full(tmp_path, catalog)
    assert set(manifest2["runs"]) == {entry1["run_id"]}
    assert manifest2["default"] == entry1["run_id"]
    assert manifest2["runs"][entry1["run_id"]] == path_m1


def test_fail_loud_when_extension_unavailable(tmp_path):
    catalog = tmp_path / "lakehouse"

    def _no_iceberg_exec(sql: str, *a, **k):
        raise duckdb.Error("binder error: extension 'iceberg' not found")
    con = duckdb.connect(":memory:")

    class _Broken:
        def execute(self, sql, *a, **k):
            return _no_iceberg_exec(sql, *a, **k)
    with pytest.raises(iceberg_mod.IcebergUnavailable):
        iceberg_mod.ensure_iceberg(_Broken())
    # No side effects: no catalog touched.
    assert not catalog.exists()


def test_fail_loud_missing_snapshot_exports_nothing(tmp_path):
    catalog = tmp_path / "lakehouse"
    con = _con_and_seed()
    with pytest.raises(iceberg_mod.IcebergExportError):
        iceberg_mod.export_snapshot(con, "snap_000000000000_x", "m0",
                                    "000000000000", catalog)
    assert not catalog.exists()


def test_fail_loud_partial_export_no_manifest(tmp_path):
    catalog = tmp_path / "lakehouse"
    con = _con_and_seed()
    con.execute("CREATE TABLE snap_aaaa11111111_ok AS SELECT 1 AS x")
    with pytest.raises(iceberg_mod.IcebergExportError):
        iceberg_mod.export_run(
            con, "aaaa11111111",
            {"ok": "snap_aaaa11111111_ok", "missing": "snap_bbbb22222222_nope"},
            catalog)
    # Without a manifest the catalog is provably incomplete.
    assert not (catalog / iceberg_mod.MANIFEST_NAME).exists()


# ------------------------------------------------------------------ L2 ops

def test_catalog_verify_reads_exported_run_without_execution(tmp_path):
    catalog = tmp_path / "lakehouse"
    entry, manifest, con = _run_full(tmp_path, catalog)
    # Fresh connection, no re-execution: just read the physical tables back.
    other = duckdb.connect(":memory:")
    other.execute("INSTALL iceberg")
    other.execute("LOAD iceberg")
    status = iceberg_mod.verify_catalog_run(other, catalog, entry["run_id"])
    assert set(status["models"]) == {"m0", "m1"}
    assert status["rows"]["m0"] == 3
    assert status["rows"]["m1"] == 3


# --------------------------------------------------------------- L3 reader

def test_pyiceberg_reader_independent_of_duckdb(tmp_path):
    catalog = tmp_path / "lakehouse"
    entry, _manifest, con = _run_full(tmp_path, catalog)
    static = pytest.importorskip("pyiceberg.table").StaticTable
    status = iceberg_mod.verify_catalog_run_pyiceberg(
        catalog, entry["run_id"], engine_cls=static)
    assert set(status["models"]) == {"m0", "m1"}
    assert status["rows"]["m0"] == 3
    assert status["rows"]["m1"] == 3


def test_pyiceberg_reader_fails_loud_when_unavailable(tmp_path):
    catalog = tmp_path / "lakehouse"
    entry, _manifest, con = _run_full(tmp_path, catalog)

    class _NoPyIceberg:
        pass

    with pytest.raises(iceberg_mod.IcebergExportError):
        iceberg_mod.verify_catalog_run_pyiceberg(
            catalog, entry["run_id"], engine_cls=_NoPyIceberg)


def test_pyiceberg_ensure_fails_loud_when_not_installed(monkeypatch):
    real_import = __import__
    def _no_pyiceberg(name, *a, **k):
        if name == "pyiceberg" or name.startswith("pyiceberg."):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", _no_pyiceberg)
    with pytest.raises(iceberg_mod.IcebergUnavailable):
        iceberg_mod.ensure_pyiceberg()


def test_catalog_verify_fails_loud_after_gc_collects_run(tmp_path):
    catalog = tmp_path / "lakehouse"
    con = _con_and_seed()
    proj, tms, path = _proj(tmp_path)
    exec_mod.run(con, proj, tms, path)
    e1 = exec_mod.load_history(path)[-1]
    iceberg_mod.export_run(con, e1["run_id"], e1["snapshots"], catalog)
    changed = BASE.replace('ctry     = concat(orders.country, "!")',
                           'ctry     = concat(orders.country, "!!")')
    proj2, tms2, path = _proj(tmp_path, changed)
    exec_mod.run(con, proj2, tms2, path)
    e2 = exec_mod.load_history(path)[-1]
    iceberg_mod.export_run(con, e2["run_id"], e2["snapshots"], catalog)
    assert e1["run_id"] != e2["run_id"]
    # keep=1: the default (newest) is kept+protected; old run is retirable only
    # with --apply. Verify the LIVE run still reads fine afterward.
    plan = iceberg_mod.gc_catalog(catalog, keep=1, apply=True)
    assert e1["run_id"] in plan["drop_runs"]
    assert e2["run_id"] in plan["keep_runs"]
    other = duckdb.connect(":memory:")
    other.execute("INSTALL iceberg")
    other.execute("LOAD iceberg")
    status = iceberg_mod.verify_catalog_run(other, catalog, e2["run_id"])
    assert status["rows"]["m0"] == 3
    # The collected run must fail loud — its physical tables are gone.
    with pytest.raises(iceberg_mod.IcebergExportError):
        iceberg_mod.verify_catalog_run(other, catalog, e1["run_id"])


def test_rollback_repoints_default_and_is_immutable(tmp_path):
    catalog = tmp_path / "lakehouse"
    con = _con_and_seed()
    # Run 1 with both models minted; then a *changed* run exports a second ID.
    proj, tms, path = _proj(tmp_path)
    applied, _pins, _ = exec_mod.run(con, proj, tms, path)
    entry1 = exec_mod.load_history(path)[-1]
    iceberg_mod.export_run(con, entry1["run_id"], entry1["snapshots"], catalog)
    rid1 = entry1["run_id"]
    # Change the module: m1 gets different content, m0 stays identical.
    changed = BASE.replace('ctry     = concat(orders.country, "!")',
                           'ctry     = concat(orders.country, "!!")')
    proj2, tms2, path = _proj(tmp_path, changed)
    applied2, _pins2, _ = exec_mod.run(con, proj2, tms2, path)
    entry2 = exec_mod.load_history(path)[-1]
    iceberg_mod.export_run(con, entry2["run_id"], entry2["snapshots"], catalog)
    rid2 = entry2["run_id"]
    assert rid1 != rid2
    assert iceberg_mod.load_manifest(catalog)["default"] == rid2

    # Rollback to run 1: default repoints, physical tables untouched.
    mf = iceberg_mod.rollback_run(catalog, rid1)
    assert mf["default"] == rid1
    assert rid2 in mf["runs"]  # nothing deleted
    assert (catalog / "runs" / rid2 / "m1").exists()


def test_rollback_fails_loud_when_run_not_in_catalog(tmp_path):
    catalog = tmp_path / "lakehouse"
    _entry, _manifest, con = _run_full(tmp_path, catalog)
    with pytest.raises(iceberg_mod.IcebergExportError):
        iceberg_mod.rollback_run(catalog, "deadbeef0000")


def test_gc_keeps_default_and_retires_others(tmp_path):
    catalog = tmp_path / "lakehouse"
    entry, manifest, con = _run_full(tmp_path, catalog)
    rid = manifest["default"]
    assert rid == entry["run_id"]
    plan = iceberg_mod.gc_catalog(catalog, keep=0, apply=False)
    assert plan["keep_runs"] == [rid]  # default always protected
    assert plan["drop_runs"] == []
    # Without --apply nothing is deleted.
    assert (catalog / "runs" / rid).exists()
    assert (catalog / iceberg_mod.MANIFEST_NAME).exists()


# ------------------------------------------------------------------ catalog

def test_cmd_catalog_lists_runs_and_marks_default(tmp_path, capsys, monkeypatch):
    from strata.cli import main
    catalog = tmp_path / "lakehouse"
    entry, manifest, con = _run_full(tmp_path, catalog)
    monkeypatch.setattr("sys.argv", ["strata", "catalog", str(catalog)])
    assert main() == 0
    out = capsys.readouterr().out
    assert entry["run_id"] in out
    assert "m0" in out and "m1" in out
    assert f"{entry['run_id']} *" in out  # live run marker


def test_cmd_catalog_run_shows_row_counts(tmp_path, capsys, monkeypatch):
    from strata.cli import main
    catalog = tmp_path / "lakehouse"
    entry, manifest, con = _run_full(tmp_path, catalog)
    monkeypatch.setattr("sys.argv", ["strata", "catalog", str(catalog),
                                     "--run", entry["run_id"]])
    assert main() == 0
    out = capsys.readouterr().out
    assert f"{entry['run_id']} (duckdb)" in out
    assert "m0" in out and "3 rows" in out


def test_cmd_catalog_run_via_pyiceberg(tmp_path, capsys, monkeypatch):
    px = pytest.importorskip("pyiceberg.table")
    from strata.cli import main
    catalog = tmp_path / "lakehouse"
    entry, manifest, con = _run_full(tmp_path, catalog)
    monkeypatch.setattr("sys.argv", ["strata", "catalog", str(catalog),
                                     "--run", entry["run_id"],
                                     "--verify-reader", "pyiceberg"])
    assert main() == 0
    out = capsys.readouterr().out
    assert f"{entry['run_id']} (pyiceberg)" in out
    assert "3 rows" in out


def test_cmd_catalog_fails_loud_when_run_absent(tmp_path, capsys, monkeypatch):
    from strata.cli import main
    catalog = tmp_path / "lakehouse"
    _entry, _manifest, con = _run_full(tmp_path, catalog)
    monkeypatch.setattr("sys.argv", ["strata", "catalog", str(catalog),
                                     "--run", "deadbeef0000"])
    assert main() == 1
    err = capsys.readouterr().err
    assert "not in catalog" in err


def test_cmd_catalog_fails_loud_without_manifest(tmp_path, capsys, monkeypatch):
    from strata.cli import main
    catalog = tmp_path / "lakehouse"
    monkeypatch.setattr("sys.argv", ["strata", "catalog", str(catalog)])
    assert main() == 1
    err = capsys.readouterr().err
    assert "has no manifest" in err