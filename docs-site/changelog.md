# Changelog — strata-lang

Formato: `0.1.x` pre-1.0, `feat`/`fix`/`chore` por commit. Tag `v0.1.0`/`v0.1.1` en `origin`.

## 0.1.1 — 2026-09-22

**PyPI `strata-lang 0.1.1` (`sdist 215K + wheel 149K`, `twine PASSED`)**

- `pyproject.toml:8` `license {text}+classifier` → `license="MIT"` + `license-files=["LICENSE*"]` (setuptools≥77, elimina deprecation `2027-02-18` que rompía `build` en `f838337`).
- `strata/__init__.py:3` `0.1.0→0.1.1` sync.
- `.github/workflows/publish.yml:1` `on: tag v*` → `build sdist+wheel` → `pypa/gh-action-pypi-publish` OIDC trusted publishing (`environment: pypi`) + `gh-release` con `dist/*` + attestations SLSA. Requiere *Trusted publisher* en PyPI (`Tzinny-dev/strata` / `publish.yml` / `pypi`).
- Verificado `strata_lang-0.1.1.tar.gz` + `whl` en PyPI, `gh release v0.1.1` con ambos assets.

## 0.1.0 — 2026-09-22

**Primera publicación `strata-lang 0.1.0` (PyPI + GH Release + Docker)**

- **Packaging:** `pyproject.toml:1` `name strata-lang` (`strata` ocupado 2013 `clastic`), `console_scripts strata=cli:main` `pyproject.toml:50`, `extras postgres/bigquery/snowflake/airflow/prefect/portalocker`, `README.md:19` `pip install strata-lang` + `uv tool install strata-lang`, `.gitignore:26` `dist/build/*.egg-info`, `LICENSE MIT`, wheel `149K` `twine PASSED`.
- **CI:** `.github/workflows/ci.yml:1` matrix `3.10-3.12` + `postgresql-16` (`pg_harness` ephemeral), `pip install -e .[dev]` + `pytest --cov-fail-under=80` (`537 passed 81%`).
- **Warehouse honesty:** `docs/warehouse-adapters.md:11` tabla 4 cols (adapter ABC vs engine `cli.open_warehouse` `dbcompat.PGConn`), `adapters.py:103` `E095` honesto (postgres sin `Warehouse` ABC aun, `bigquery/snowflake` sql-emit only), `README.md:57` alineado.
- **Stubs:** `integrations.py:1` scaffolding only, `testing.py:1` fixture helpers, `observability.py:1` connected (`exec.py:27` `MetricsCollector`).
- **Fix 3.10:** `tests/test_warehouse_partition_freshness.py:44` `f"..., {", ".join` → `f"..., {', '.join` (PEP 701, `e8dec1d`, `ci 3.10` `SyntaxError`).
- **Docker:** `Dockerfile:1` `python:3.12-slim` `pip install strata-lang[postgres]==0.1.0`, `.dockerignore`, `.github/workflows/docker.yml:1` `on tag v*` `buildx` + `GHCR` `strata:0.1.0/latest`.

## Binary standalone — Fase 0-4 (2026-09-22)

Documentado `docs/binary-standalone.md:1`.

- **Fase 0 prep** `5572426`: `exec.py:11` `fcntl` → `try/except ImportError` + `portalocker` fallback + warning único (`plan-hito-2.md:159` Windows), `bench.py:26` `_bench_dir()` `sys.frozen/_MEIPASS`, `pyproject.toml:32` extra `portalocker`.
- **Fase 1 POC** `0cc832c`: `strata_entry.py:1` entry absoluto + `strata.spec:4` onefile `hiddenimports duckdb/psycopg2/yaml/strata.*`, `datas examples+bench`, `upx True` → `dist/strata 34M` (`_duckdb.so 58M` → `34M` comprimido), smoke `help/build/compile postgres/run --seed 3 rows/bench all green`.
- **Fase 2 CI** `f88769d`: `.github/workflows/binary.yml:1` matrix `ubuntu/macos/windows` `setup-python 3.12` `pyinstaller strata.spec`, smoke 4 comandos, `upload-artifact` por OS, `release` mergea `ELF/Mach-O/.exe` → `strata-linux/macos/windows-amd64`.
- **Fase 3 hardening** `6dedf62`: `strata.spec:4` añade `LICENSE+README` al bundle, `binary.yml:34` `upx-ucl` en ubuntu + `sha256sum` + gate `>120M` warn, `codesign --sign -` ad-hoc macOS, `attest-build-provenance` SLSA.
- **Fase 4 distribución** `3374913`: `install.sh:1` `curl|bash` detecta OS/arch, pull `GH Releases v$VERSION/strata-*`, `homebrew/strata-lang.rb:1` tap formula (`version 0.1.1` `REPLACE_SHA256`), `README.md:72` sección Standalone binary, attest ya en Fase 3.

## Unreleased — próximo 0.1.2

- Docs polish en curso: `CHANGELOG.md` (este archivo), badges, `mkdocs` site.
- Pendiente producto: `BigQuery/Snowflake` adapters reales `adapters.py:74`, `Iceberg` `propuesta §6`, `LSP` `vscode`, `WASM` descartado.

---

*Generado desde `git log --oneline 7376003..3374913` y `pyproject.toml` / `strata.spec` / `binary.yml`.*
