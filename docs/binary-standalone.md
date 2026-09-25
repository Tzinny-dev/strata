# Strata — Standalone binary `strata`

> Fulfills `propuesta-lenguaje-strata.md:49` "one single binary: compiler+LSP+runner". Status: **implemented 2026-09-22** — `prototype/strata.spec:1`, `prototype/strata_entry.py:1`, `prototype/.github/workflows/binary.yml:1`, `prototype/dist/strata` `34M` (UPX), `prototype/pyproject.toml:7` `strata-lang==0.1.5`.

## Summary

Strata ships in three equivalent forms (same `strata/cli.py:1084` `main()`):

* `pip install strata-lang` — wheel `149K` `twine check PASSED` (`prototype/pyproject.toml:6`)
* `uv tool install strata-lang` — zero-cost shortcut
* **Standalone binary** `strata` — `prototype/dist/strata` `34M` with UPX (`_duckdb.so 58M` → `34M` compressed), `venv 143M` total. Without UPX expected `80–120M` (`prototype/docs-site/guide/binary-standalone.md:7` historical)
* `ghcr.io/tzinny-dev/strata:0.1.5` — reproducible Docker (`prototype/Dockerfile:1`)

The binary already exists and passes the smoke tests `help/build/compile --dialect postgres/run --seed/bench` in `binary.yml`. Current recommendation: **use the binary for distribution**; `pip`/`uv` remain valid for development.

---

## 1. Blockers — closed status (Phase 0)

| Blocker | Evidence | Fix applied |
|---|---|---|
| `fcntl.flock` POSIX only | `strata/exec.py:11` `try: import fcntl` / `exec.py:166` fallback | Closed `5572426`: `exec.py:11` `try/except ImportError` + `exec.py:173` `portalocker` fallback + `exec.py:190` warning if missing. Windows `.exe` does not crash (fail-open with warning). |
| `Path(__file__)` and resources | `strata/bench.py:26` `Path(__file__).parent.parent / "bench"` | Closed `5572426`: `bench.py:33` `sys.frozen/_MEIPASS` with a fallback to the source tree. `strata.spec:4` `datas=[('examples','examples'),('bench','bench'),('LICENSE','.'),('README.md','.')] + collect_data_files('strata')`. |
| Dynamic imports | `strata/cli.py:54` `import psycopg2`, `adapters.py:92` `__import__` | Closed `0cc832c`: `strata.spec:13` `hiddenimports=['duckdb','psycopg2','yaml','strata.adapters','strata.bench','strata.dbcompat','strata.analysis','portalocker']`. |
| Native `duckdb` binary | `_duckdb.so 58M` | Closed `0cc832c`: PyInstaller copies the `.so` via `collect_data_files`; `alpine/musl` discarded (glibc only). |
| `information_schema` / `duckdb_views()` | `strata/dbcompat.py:92` `pg_views` | Did not block the binary; the binary CI smoke uses ephemeral Postgres if `psycopg2` is available (`binary.yml` does not require it yet — see §3). |

---

## 2. Tools compared — decision taken

| Tool | Type | Pro | Con | For Strata |
|---|---|---|---|---|
| **PyInstaller** | interpreter+zip bundle | Most mature, existing `duckdb` hook, one command `pyinstaller strata.spec` | 90M without UPX / 34M with UPX, native build per OS, `UPX` breaks macOS signature | **Chosen — implemented `0cc832c`** |
| **Nuitka** | Python→C→EXE | Binary 30% faster/smaller, better `UPX`, respects `__file__` | Compiles `12k` lines → 15–25 min/build, breaks the `psycopg2` C-ext without `--include-package-data` | Discarded — not needed once PyInstaller is validated |
| **PyOxidizer** | Rust packager | Real single file, no `_MEIPASS` temp | Rust config, manual `duckdb` hook, less docs | Discarded |
| **pex / shiv / uv tool** | zipapp | Not standalone (requires `python3.12`) | Does not meet the literal spec, but `uv tool install strata` already works | **Current interim** for dev |

---

## 3. Roadmap — implemented

### Phase 0 — Preparation ✅ `5572426`

1. `strata/exec.py:11` → `try: import fcntl` + `exec.py:173` `portalocker` fallback.
2. `strata/bench.py:33` → `sys.frozen/_MEIPASS` with a source tree fallback.
3. `strata.spec:4` `datas` centralized; `pyproject.toml:53` does not require a `package-data` extra (the wheel uses `include=strata*`, the binary uses `datas`).

### Phase 1 — PyInstaller POC ✅ `0cc832c` (2 days)

Canonical spec `prototype/strata.spec:1` (no ad-hoc CLI):

```bash
pip install pyinstaller
pyinstaller strata.spec --noconfirm
./dist/strata --help
./dist/strata build examples/daily_orders.strata
./dist/strata compile examples/daily_orders.strata --dialect postgres
./dist/strata run examples/daily_orders.strata --seed -o /tmp/demo.duckdb
du -sh dist/strata  # real 34M UPX, 80–110M without UPX
```

Validation: `dist/strata test examples/daily_orders.strata --seed` + `bench` must match `pytest` (see §5 pending).

### Phase 2 — CI matrix ✅ `f88769d` (`prototype/.github/workflows/binary.yml:1`)

Matrix `ubuntu/macos/windows` (`binary.yml:14`), `setup-python 3.12`, `pyinstaller strata.spec`, smoke `help/build/compile/run/bench`, `upload-artifact` per OS, `release` with `attest-build-provenance` + `softprops/action-gh-release` (`binary.yml:162`).

CI time `90s` → `~270s` (×3 OS).

### Phase 3 — Hardening ✅ `6dedf62` (partial)

- `strata.spec:33` `upx=True` + `binary.yml:35` `upx-ucl` on ubuntu + `sha256sum` + gate `>120M` warn (`binary.yml:108`).
- `binary.yml:88` `codesign --sign -` ad-hoc (no `Developer ID` — see §5 pending). For prod:
  ```bash
  # macOS (requires Apple Developer Program $99/year, secret APPLE_CERT + APPLE_CERT_PASSWORD)
  security import certificate.p12 -k ~/Library/Keychains/login.keychain
  codesign --sign "Developer ID Application: Tzinny (TEAMID)" --deep --timestamp --options runtime dist/strata
  xcrun notarytool submit dist/strata --apple-id "$APPLE_ID" --password "$APPLE_APP_PASSWORD" --team-id "$TEAMID" --wait
  xcrun stapler staple dist/strata
  # Windows (EV cert + AzureSignTool/signtool, secret WINDOWS_CERT)
  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx /p "$CERT_PASSWORD" dist/strata.exe
  ```
  Add as `if: startsWith(github.ref,'refs/tags/v')` steps in `binary.yml` with `secrets.APPLE_CERT`/`secrets.WINDOWS_CERT`.
- `strata/lsp.py:18` already exposed as `strata lsp` (`cli.py:50`) and packaged via `collect_data_files`.

### VS Code extension ✅ 0.1.5 (`prototype/vscode/`)

- Marketplace **`Tzinny-dev.strata-tzinny`**: TextMate grammar for `.strata`,
  language-config (`//` comments) and an LSP client over `strata lsp`
  (diagnostics from the `Checker`, completion, hover, go to definition).
- Setting `strata.binaryPath` for binaries outside the `PATH` (venvs);
  the **Strata: Show Version** command verifies the configured binary.
- Local rebuild: `cd vscode && npm install && npm run compile && npx
  vsce package` → `.vsix` → `code --install-extension <file>`.
  Full guide: `docs-site/guide/vscode.md`.

---

## 4. Costs and risks — updated

- **Size:** `58M` for `_duckdb.so` alone → `34M` binary with UPX (`CHANGELOG.md:30`), `~85M` without UPX. `pip+Docker` (`ghcr.io/tzinny-dev/strata:0.1.5`) remains current for CI.
- **Maintenance:** every `duckdb` or `psycopg2-binary` bump (`pyproject.toml:24`) forces a triple rebuild + re-sign (automated in `binary.yml` on `tag v*`).
- **WASM/playground** `docs/warehouse-adapters.md:69` discarded — same cost as the binary with no proven demand.
- **Distribution:** `install.sh:1` + `homebrew/strata-lang.rb:1` operational, pending: real Developer ID signature and automatic `REPLACE_SHA256` (see §5).

---

## 5. Decision taken + remaining debt

1. **Done 2026-09-23:** `PyPI strata-lang 0.1.5` + `binary.yml` triple matrix + `ghcr.io/tzinny-dev/strata:0.1.5` + `install.sh` + `homebrew` + `vscode Tzinny-dev.strata-tzinny 0.1.5`.
2. **Validate:** 5 interviews with the `strata lineage-diff` demo (`market/market-validation.md:141`) — product pending.
3. **Open debt (does not block usage, see detail below):**
   - **Real signature:** `binary.yml:88` ad-hoc → requires `APPLE_CERT`/`signtool` for Gatekeeper/SmartScreen (cost `99$/year`).
   - **Homebrew SHA:** `strata-lang.rb:10` `REPLACE_SHA256` manual → automate in `binary.yml:release`.
   - **binary==pytest equivalence:** missing gate `dist/strata test` vs `pytest` (`binary-standalone.md:60` historical).
   - **UPX portability:** validate on `ubuntu:20.04` (kernel `<5.10` breaks `duckdb`).
   - **arm64:** `install.sh:33` `amd64` only → add `macos-14`/`linux arm64` when there is demand.

---

*Updated 2026-09-24. Source: `prototype/` `CHANGELOG.md` Phase 0-4 → 0.1.5 + Unreleased 0.1.6, `dist/strata 34M` + `vscode 0.1.5`, `pytest 619 tests, 80% coverage` on `3.12`.*
