"""Versioned metadata sidecars + `strata migrate` upgrade path (audit M31).

Covers the identity-preserving v0 -> v1 upgrade of the run history
(.strata-history.jsonl) and fingerprint manifest (.strata-manifest.json),
the lenient everyday readers (load_manifest back-compat), and the
strict migrate failures (E097 corrupt, E098 newer build)."""
import json

import pytest

from strata import exec as ex
from strata.analysis import StrataError


CURRENT = ex.METADATA_SCHEMA_VERSION


def _module(tmp_path) -> str:
    m = tmp_path / "m.strata"
    m.write_text('source s(ns: "t", dataset: "s") { columns: { id: int64 } }\n'
                 "model m { from s }\n")
    return str(m)


def _write_history(module: str, records: list[dict]) -> None:
    with open(ex.history_path(module), "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _legacy_record(rid: str, fp: str = "abc") -> dict:
    return {"run_id": rid, "at": "2026-10-01T00:00:00+00:00", "model": "m",
            "fingerprints": {"m": fp}, "applied": ["m"], "snapshots": {"m": "s_" + rid}}


class TestSchemaStamping:
    def test_record_run_stamps_schema_version(self, tmp_path):
        m = _module(tmp_path)
        entry = ex.record_run(m, {"model": "m", "applied": ["m"],
                                  "fingerprints": {"m": "abc"}})
        assert entry["schema_version"] == CURRENT
        assert ex.load_history(m)[0]["schema_version"] == CURRENT

    def test_run_id_is_stable_across_schema_stamp(self):
        base = {"model": "m", "applied": ["m"], "fingerprints": {"m": "abc"}}
        stamped = dict(base, schema_version=CURRENT)
        assert ex._run_id(base) == ex._run_id(stamped)


class TestManifestVersions:
    def test_save_manifest_writes_wrapper_and_load_manifest_normalizes(self, tmp_path):
        m = _module(tmp_path)
        ex.save_manifest(m, {"a": "ff", "b": "aa"})
        raw = json.loads(ex.manifest_path(m).read_text())
        assert raw["version"] == CURRENT
        assert raw["models"] == {"a": "ff", "b": "aa"}
        assert ex.load_manifest(m) == {"a": "ff", "b": "aa"}

    def test_load_manifest_accepts_legacy_flat_and_v1(self, tmp_path):
        m = _module(tmp_path)
        ex.manifest_path(m).write_text(json.dumps({"a": "ff"}))
        assert ex.load_manifest(m) == {"a": "ff"}
        ex.manifest_path(m).write_text(
            json.dumps({"version": CURRENT, "models": {"b": "aa"}}))
        assert ex.load_manifest(m) == {"b": "aa"}

    def test_load_manifest_degrades_on_corrupt(self, tmp_path):
        m = _module(tmp_path)
        ex.manifest_path(m).write_text("{not json")
        assert ex.load_manifest(m) == {}


class TestMigrateCommand:
    def test_upgrades_legacy_history_and_manifest_keeping_run_ids(self, tmp_path):
        m = _module(tmp_path)
        recs = [_legacy_record("aaaa1111aaaa", "abc"),
                _legacy_record("bbbb2222bbbb", "def")]
        _write_history(m, recs)
        ex.manifest_path(m).write_text(json.dumps({"m": "def"}))

        report = ex.migrate_metadata(m)
        assert report["history"] == {"records": 2, "schema_version": CURRENT,
                                     "migrated": 2}
        assert report["manifest"]["exists"] and report["manifest"]["migrated"]

        upgraded = ex.load_history(m)
        assert [r["run_id"] for r in upgraded] == [r["run_id"] for r in recs]
        first = json.loads(ex.history_path(m).read_text().splitlines()[0])
        assert first["schema_version"] == CURRENT and first["run_id"] == "aaaa1111aaaa"
        assert ex.load_manifest(m) == {"m": "def"}
        assert json.loads(ex.manifest_path(m).read_text())["version"] == CURRENT

    def test_noop_when_current_leaves_files_untouched(self, tmp_path):
        m = _module(tmp_path)
        hist_text = json.dumps(dict(_legacy_record("cafe00000001"),
                                    schema_version=CURRENT)) + "\n"
        ex.history_path(m).write_text(hist_text)
        ex.manifest_path(m).write_text(
            json.dumps({"version": CURRENT, "models": {"m": "def"}}))

        report = ex.migrate_metadata(m)
        assert report["history"]["migrated"] == 0
        assert not report["manifest"]["migrated"]
        assert ex.history_path(m).read_text() == hist_text

    def test_fresh_module_is_a_noop(self, tmp_path):
        m = _module(tmp_path)
        report = ex.migrate_metadata(m)
        assert report["history"] == {"records": 0, "schema_version": CURRENT,
                                     "migrated": 0}
        assert report["manifest"]["exists"] is False
        assert not ex.history_path(m).exists()
        assert not ex.manifest_path(m).exists()

    def test_fails_loud_on_corrupt_history_without_touching_it(self, tmp_path):
        m = _module(tmp_path)
        text = '{"run_id": "abc"}\nnot json\n'
        ex.history_path(m).write_text(text)
        with pytest.raises(StrataError) as cm:
            ex.migrate_metadata(m)
        assert cm.value.code == "E097"
        assert ex.history_path(m).read_text() == text

    def test_fails_loud_on_corrupt_manifest_without_touching_it(self, tmp_path):
        m = _module(tmp_path)
        _write_history(m, [_legacy_record("aaaa1111aaaa")])
        ex.manifest_path(m).write_text("{not json")
        with pytest.raises(StrataError) as cm:
            ex.migrate_metadata(m)
        assert cm.value.code == "E097"
        assert ex.manifest_path(m).read_text() == "{not json"

    def test_fails_loud_on_newer_history_schema(self, tmp_path):
        m = _module(tmp_path)
        text = json.dumps(dict(_legacy_record("aaaa1111aaaa"),
                               schema_version=CURRENT + 1)) + "\n"
        ex.history_path(m).write_text(text)
        with pytest.raises(StrataError) as cm:
            ex.migrate_metadata(m)
        assert cm.value.code == "E098"
        assert ex.history_path(m).read_text() == text

    def test_fails_loud_on_newer_manifest_schema(self, tmp_path):
        m = _module(tmp_path)
        _write_history(m, [_legacy_record("aaaa1111aaaa")])
        ex.manifest_path(m).write_text(
            json.dumps({"version": CURRENT + 1, "models": {}}))
        with pytest.raises(StrataError) as cm:
            ex.migrate_metadata(m)
        assert cm.value.code == "E098"


class TestMigrateCliAndDiffAlias:
    def test_migrate_cli(self, tmp_path, capsys):
        m = _module(tmp_path)
        _write_history(m, [_legacy_record("aaaa1111aaaa")])
        from strata.cli import main
        assert main(["migrate", m]) == 0
        out = capsys.readouterr().out
        assert "run history : 1 record(s) @ schema v1 (1 upgraded)" in out
        assert "manifest    : absent" in out

    def _write_modules(self, tmp_path, base_text, head_text):
        base = tmp_path / "base.strata"
        head = tmp_path / "head.strata"
        base.write_text(base_text)
        head.write_text(head_text)
        return str(base), str(head)

    def test_diff_alias_identical(self, tmp_path, capsys):
        text = 'source s(ns: "t", dataset: "s") { columns: { id: int64 } }\n' \
               "model m { from s }\n"
        base, head = self._write_modules(tmp_path, text, text)
        from strata.cli import main
        assert main(["diff", base, head]) == 0
        assert "semantic diff: identical" in capsys.readouterr().out

    def test_diff_alias_breaking_gate(self, tmp_path, capsys):
        base_text = 'source s(ns: "t", dataset: "s") ' \
                    "{ columns: { id: int64, v: int64 } }\nmodel m { from s }\n"
        head_text = 'source s(ns: "t", dataset: "s") ' \
                    "{ columns: { id: int64 } }\nmodel m { from s }\n"
        base, head = self._write_modules(tmp_path, base_text, head_text)
        from strata.cli import main
        assert main(["diff", base, head]) == 1
        assert "E030" in capsys.readouterr().out