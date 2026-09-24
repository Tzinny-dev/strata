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
        iceberg_mod.export_snapshot(con, "snap_000000000000_x", "m0", catalog)
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