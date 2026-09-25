# Changelog — strata-lang

Format: `0.1.x` pre-1.0, `feat`/`fix`/`chore` per commit. Tag `v0.1.0`/`v0.1.1` on `origin`.

## 0.1.1 — 2026-09-22

**PyPI `strata-lang 0.1.1` (`sdist 215K + wheel 149K`, `twine PASSED`)**

- `pyproject.toml:8` `license {text}+classifier` → `license="MIT"` + `license-files=["LICENSE*"]` (setuptools≥77, removes deprecation `2027-02-18` that broke `build` in `f838337`).
- `strata/__init__.py:3` `0.1.0→0.1.1` sync.
- `.github/workflows/publish.yml:1` `on: tag v*` → `build sdist+wheel` → `pypa/gh-action-pypi-publish` OIDC trusted publishing (`environment: pypi`) + `gh-release` with `dist/*` + SLSA attestations. Requires *Trusted publisher* on PyPI (`Tzinny-dev/strata` / `publish.yml` / `pypi`).
- Verified `strata_lang-0.1.1.tar.gz` + `whl` on PyPI, `gh release v0.1.1` with both assets.

## 0.1.0 — 2026-09-22

**First release `strata-lang 0.1.0` (PyPI + GH Release + Docker)**

- **Packaging:** `pyproject.toml:1` `name strata-lang` (`strata` already taken 2013 by `clastic`), `console_scripts strata=cli:main` `pyproject.toml:50`, `extras postgres/bigquery/snowflake/airflow/prefect/portalocker`, `README.md:19` `pip install strata-lang` + `uv tool install strata-lang`, `.gitignore:26` `dist/build/*.egg-info`, `LICENSE MIT`, wheel `149K` `twine PASSED`.
- **CI:** `.github/workflows/ci.yml:1` matrix `3.10-3.12` + `postgresql-16` (`pg_harness` ephemeral), `pip install -e .[dev]` + `pytest --cov-fail-under=80` (`537 passed 81%`).
- **Warehouse honesty:** `docs/warehouse-adapters.md:11` 4-col table (adapter ABC vs engine `cli.open_warehouse` `dbcompat.PGConn`), `adapters.py:103` honest `E095` (postgres without `Warehouse` ABC yet, `bigquery/snowflake` sql-emit only), `README.md:57` aligned.
- **Stubs:** `integrations.py:1` scaffolding only, `testing.py:1` fixture helpers, `observability.py:1` connected (`exec.py:27` `MetricsCollector`).
- **Fix 3.10:** `tests/test_warehouse_partition_freshness.py:44` `f"..., {", ".join` → `f"..., {', '.join` (PEP 701, `e8dec1d`, `ci 3.10` `SyntaxError`).
- **Docker:** `Dockerfile:1` `python:3.12-slim` `pip install strata-lang[postgres]==0.1.0`, `.dockerignore`, `.github/workflows/docker.yml:1` `on tag v*` `buildx` + `GHCR` `strata:0.1.0/latest`.

## Binary standalone — Phase 0-4 (2026-09-22)

Documented `docs/binary-standalone.md:1`.

- **Phase 0 prep** `5572426`: `exec.py:11` `fcntl` → `try/except ImportError` + `portalocker` fallback + single warning (`plan-hito-2.md:159` Windows), `bench.py:26` `_bench_dir()` `sys.frozen/_MEIPASS`, `pyproject.toml:32` extra `portalocker`.
- **Phase 1 POC** `0cc832c`: `strata_entry.py:1` absolute entry + `strata.spec:4` onefile `hiddenimports duckdb/psycopg2/yaml/strata.*`, `datas examples+bench`, `upx True` → `dist/strata 34M` (`_duckdb.so 58M` → `34M` compressed), smoke `help/build/compile postgres/run --seed 3 rows/bench all green`.
- **Phase 2 CI** `f88769d`: `.github/workflows/binary.yml:1` matrix `ubuntu/macos/windows` `setup-python 3.12` `pyinstaller strata.spec`, smoke 4 commands, `upload-artifact` per OS, `release` merges `ELF/Mach-O/.exe` → `strata-linux/macos/windows-amd64`.
- **Phase 3 hardening** `6dedf62`: `strata.spec:4` adds `LICENSE+README` to the bundle, `binary.yml:34` `upx-ucl` on ubuntu + `sha256sum` + gate `>120M` warn, `codesign --sign -` ad-hoc macOS, `attest-build-provenance` SLSA.
- **Phase 4 distribution** `3374913`: `install.sh:1` `curl|bash` detects OS/arch, pull `GH Releases v$VERSION/strata-*`, `homebrew/strata-lang.rb:1` tap formula (`version 0.1.1` `REPLACE_SHA256`), `README.md:72` Standalone binary section, attest already in Phase 3.

## 0.1.2 — 2026-09-23

**Binary standalone debt closed Phase 0-4 → production**

- `docs/binary-standalone.md:1` rewritten: from "requirements analysis / do not build" to implemented state `34M` + remaining debt documented (real signing, auto SHA, parity, compat, arm64).
- `binary.yml:185` release now patches `homebrew/strata-lang.rb:7` `version` + `REPLACE_*SHA256` and commits `[skip ci]` to `main`.
- `binary.yml:103` gate `binary vs pip parity` (`build` diff + `bench` + `test --seed`) only on `ubuntu-latest`.
- `binary.yml:140` + `strata.spec:33` conditional UPX + job `compat` `ubuntu:20.04` via Docker (glibc 2.31).
- `install.sh:33` arm64 detects native `strata-*-arm64` or falls back to `amd64` with a Rosetta/qemu warning; `binary.yml:build` arm64 matrix commented out, ready (`macos-14`/`ubuntu-24.04-arm`).
- `binary.yml:88` prod signing documented `codesign + notarytool` / `signtool` with `secrets.APPLE_CERT/WINDOWS_CERT`.

## 0.1.3 — 2026-09-23

**Fix binary compat + release collision**

- `binary.yml:11` `ubuntu-latest→ubuntu-22.04` (GLIBC 2.38 on 24.04 breaks `ubuntu:20.04` 2.31 — probe `Failed to load libpython3.12.so.1.0`), `compat` `22.04 OK` + `20.04 probe warn` `continue-on-error`.
- `binary.yml:185` `download-artifact` `merge-multiple true→pattern strata-* false` (collision `strata` linux/macos overwrote one — that's why `v0.1.2` was missing `macos` and `b66bd0a` ended up with `sha ""`), `b66bd0a` hotfix `sha 32a9...` + manual upload `macos-amd64` (arm64) to `v0.1.2`.
- `homebrew/strata-lang.rb:8` `b66bd0a` fixed, `install.sh:33` + `binary.yml:30c321d/4774bf3` already in `main`.

## 0.1.4 — 2026-09-23

**VS Code extension + sync**

- `vscode/package.json:2` `strata-lang→strata-tzinny` `displayName Strata→Strata — Tzinny` (Marketplace was blocking `strata-lang` by `prabathkumar` and `Strata` by `StrataTeam`), `vscode 0.1.4` published `Tzinny-dev.strata-tzinny` (`vsce 4.0` `Node 24` segfault → web Upload `vsix 6.07K`).
- `strata/cli.py:1315` `cmd_lsp` + `cli.py:1328` parser `lsp` (already documented `cli.py:50` but not wired) + `strata.spec:14` `hiddenimport strata.lsp`.
- Sync `pyproject.toml:7` `strata-lang 0.1.3→0.1.4` (`strata/__init__.py:3`), `README.md:81` `0.1.1→0.1.4`, `Docker 0.1.0→0.1.4`, `guide/binary-standalone.md:3` `0.1.1→0.1.4`.
- `vscode/README.md` `0.1.3→0.1.4` + `strata-tzinny-0.1.4.vsix` + `Tzinny-dev.strata-tzinny`.

## 0.1.5 — 2026-09-23

**B1 real BigQuery/Snowflake adapters**

- `strata/dbcompat.py:70` `BigQueryConn`/`SnowflakeConn` (chaining `execute→fetchall` like `PGConn`, `?` inlining for BQ, `%s` for SF, `db_schema`/`live_view_defs`/`physical_schema`/`physical_types` dispatch for the 4 dialects).
- `strata/adapters.py:113` `BigQueryWarehouse`/`SnowflakeWarehouse` (Wraps `BigQueryConn`/`SnowflakeConn`, `materialize` `CREATE OR REPLACE TABLE`, `get_adapter("bigquery"/"snowflake")` no longer `E095` when the driver is installed).
- `strata/cli.py:42` `open_warehouse` now `bigquery://project/dataset?location=US` → `BigQueryConn` and `snowflake://user:pass@account/db/schema?warehouse=WH&role=ROLE` → `SnowflakeConn`.
- `strata.spec:14` `hiddenimports` `google.cloud.bigquery`/`snowflake.connector`.
- Tests `tests/test_adapters.py:60` `+5` mocks (BigQuery/Snowflake `get_adapter` + `open_warehouse` URL), `521 passed` (was `516`).

## Unreleased — next 0.1.6

- Pending product: `Iceberg` [`propuesta-iceberg.md`](../../propuesta-iceberg.md) (L1–L4 ✅, REST catalog no-go documented §10.5), `WASM` discarded.

- **`strata run --iceberg-dir <catalog>` → real Iceberg publication (L1)** (`strata/cli.py:358` `cmd_run`, new `strata/iceberg.py`): after a run that freezes snapshot tables (`snap_<run_id>_<model>`), each one is copied to `<catalog>/runs/<run_id>/<model>` as a real Apache Iceberg table via `COPY ... (FORMAT iceberg)` (DuckDB extension, not a SQL dialect) and registered in `<catalog>/_strata_manifest.json` (`run_id → {model: dir}`, byte-for-byte deterministic, cumulative, `default` = latest). Fail-loud: no iceberg extension → `E100` before running (no side effects); missing snapshot / incomplete export → the manifest is not written (all-or-nothing). `--dialect` ≠ duckdb + `--iceberg-dir` → `E100`. Verified: external `iceberg_scan` reads the published table in a separate connection; `534 passed` (was `529`).

- **Operational Iceberg catalog L2: replay/rollback/gc** (`strata/iceberg.py` `verify_catalog_run`/`rollback_run`/`gc_catalog`, `strata/cli.py`): `strata replay <file> --verify <run> --iceberg-dir` additionally validates that the published tables are readable (`iceberg_scan`) and their count matches the snapshots (`E082` prior verify, `E083` unreadable catalog); `strata rollback <file> --run <id> --iceberg-dir` repoints the manifest `default` without deleting anything physical (immutable, `E081` run absent, `E083` not published); `strata gc <file> --iceberg-dir [--keep N] [--keep-days D] [--apply]` lists/protects `default`+latest and deletes old dirs (`E084`). Recency by mtime of run dirs (the manifest uses `sort_keys`) — `539 passed` (was `534`).

- **Independent Iceberg catalog reader with `pyiceberg` (L3)** (`strata/iceberg.py` `ensure_pyiceberg`/`verify_catalog_run_pyiceberg`, `strata/cli.py` `--verify-reader`, `pyproject.toml:34` extra `iceberg`): `strata replay <file> --verify <run> --iceberg-dir --verify-reader pyiceberg` verifies the published tables WITHOUT DuckDB (reads `*.metadata.json` via `StaticTable` + `pyarrow` scan): if the catalog is standard Iceberg, any engine reads it; the reader does not trust the writer. Fail-loud: `pyiceberg` missing → `IcebergUnavailable` (`E083`) with `pip install 'pyiceberg[pyarrow]'`. By default `--verify-reader duckdb` (zero new deps); the `iceberg` extra is optional and the interpreter without it runs 12 tests + 1 skip, with it 13/13 — `541 passed` + 1 skip in the suite (was `539`).

- **`import-dbt` CTEs (`WITH cte AS (...)`) → Strata helpers** (`strata/importdbt.py:417` `_split_with`, `:490` `_translate_sql`): each `WITH` is translated to a helper model without a contract `{model}__{cte}` that the main model reads via `from` (`import_dbt_project` prepends them to its model). Forward references / unused CTE → inert helper, green build (`529 passed`).

- **`strata catalog` — Iceberg catalog inspection (L2 §10.4)** (`strata/cli.py` `cmd_catalog`): `strata catalog <dir>` lists published runs, models per run and marks the `default` (`*`); `--json` pages the manifest; `--run <id> [--verify-reader duckdb|pyiceberg]` shows rows per model without re-running (both readers match → independent cross-check of standard Iceberg). Fail-loud: no manifest `E084`; foreign run `E081`; broken read `E083` — `545 passed` (was `541`).

- **`import-dbt` Iceberg SQL compat (L4)** (`strata/importdbt.py` `_rewrite_dml_annotation`/`_iceberg_config_notes`): the dbt-iceberg SQL constructs are translated to flat Strata — `INSERT [OVERWRITE] INTO <t> <select>` → the select (deterministic recompute, the overwrite IS the model); `MERGE INTO <t> [AS t] USING (<select>) [AS s] ON <equi-keys> WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...` → the select of the USING + `dedup by <keys>`; `config(materialized/unique_key/partition_by/ttl_days)` → `// dbt-iceberg ...` annotations (v1 writes unpartitioned, §4.2; ttl → `strata gc --iceberg-dir --keep-days`). Fail-loud E042 (no semantic drift): DELETE actions, USING without parentheses, ON not an equi-join, `{{ this }}` outside a target. `{{ this }}` is legal ONLY as a DML target (the dispatcher runs before the jinja strip). Verified green build artifact + byte-identical — `555 passed` + 2 skip (was `545`).

---

*Generated from `git log --oneline 7376003..3374913` and `pyproject.toml` / `strata.spec` / `binary.yml`.*
