# Strata — Binario standalone `strata`

> Cumple `propuesta-lenguaje-strata.md:49` "un solo binario: compilador+LSP+runner". Estado: **implementado 2026-09-22** — `prototype/strata.spec:1`, `prototype/strata_entry.py:1`, `prototype/.github/workflows/binary.yml:1`, `prototype/dist/strata` `34M` (UPX), `prototype/pyproject.toml:7` `strata-lang==0.1.5`.

## Resumen

Strata se distribuye en tres formas equivalentes (mismo `strata/cli.py:1084` `main()`):

* `pip install strata-lang` — wheel `149K` `twine check PASSED` (`prototype/pyproject.toml:6`)
* `uv tool install strata-lang` — atajo 0 costo
* **Binario standalone** `strata` — `prototype/dist/strata` `34M` con UPX (`_duckdb.so 58M` → `34M` comprimido), `venv 143M` total. Sin UPX esperado `80–120M` (`prototype/docs-site/guide/binary-standalone.md:7` histórico)
* `ghcr.io/tzinny-dev/strata:0.1.5` — Docker reproducible (`prototype/Dockerfile:1`)

El binario ya existe y pasa smoke `help/build/compile --dialect postgres/run --seed/bench` en `binary.yml`. Recomendación actual: **usar binario para distribución**; `pip`/`uv` siguen válidos para desarrollo.

---

## 1. Bloqueos — estado cerrado (Fase 0)

| Bloqueo | Evidencia | Fix aplicado |
|---|---|---|
| `fcntl.flock` POSIX only | `strata/exec.py:11` `try: import fcntl` / `exec.py:166` fallback | Cerrado `5572426`: `exec.py:11` `try/except ImportError` + `exec.py:173` `portalocker` fallback + `exec.py:190` warning si falta. Windows `.exe` no crashea (fail-open con warning). |
| `Path(__file__)` y recursos | `strata/bench.py:26` `Path(__file__).parent.parent / "bench"` | Cerrado `5572426`: `bench.py:33` `sys.frozen/_MEIPASS` con fallback a source tree. `strata.spec:4` `datas=[('examples','examples'),('bench','bench'),('LICENSE','.'),('README.md','.')] + collect_data_files('strata')`. |
| Imports dinámicos | `strata/cli.py:54` `import psycopg2`, `adapters.py:92` `__import__` | Cerrado `0cc832c`: `strata.spec:13` `hiddenimports=['duckdb','psycopg2','yaml','strata.adapters','strata.bench','strata.dbcompat','strata.analysis','portalocker']`. |
| `duckdb` binario nativo | `_duckdb.so 58M` | Cerrado `0cc832c`: PyInstaller copia `.so` via `collect_data_files`; `alpine/musl` descartado (glibc only). |
| `information_schema` / `duckdb_views()` | `strata/dbcompat.py:92` `pg_views` | No bloqueó binario; CI binario smoke usa Postgres efímero si `psycopg2` disponible (`binary.yml` no lo exige aún — ver §3). |

---

## 2. Herramientas comparadas — decisión tomada

| Tool | Tipo | Pro | Contra | Para Strata |
|---|---|---|---|---|
| **PyInstaller** | bundle intérprete+zip | Más maduro, hook `duckdb` existente, 1 comando `pyinstaller strata.spec` | 90M sin UPX / 34M con UPX, cada OS build nativo, `UPX` rompe firma macOS | **Elegido — implementado `0cc832c`** |
| **Nuitka** | Python→C→EXE | Binario 30% más rápido/pequeño, mejor `UPX`, respeta `__file__` | Compila `12k` líneas → 15–25 min/build, rompe `psycopg2` C-ext sin `--include-package-data` | Descartado — no necesario con PyInstaller validado |
| **PyOxidizer** | Rust empaquetador | Single file real, sin `_MEIPASS` temp | Config Rust, hook `duckdb` manual, menos doc | Descartado |
| **pex / shiv / uv tool** | zipapp | No es standalone (requiere `python3.12`) | No cumple spec literal, pero `uv tool install strata` ya funciona | **Interino vigente** para dev |

---

## 3. Roadmap — implementado

### Fase 0 — Preparación ✅ `5572426`

1. `strata/exec.py:11` → `try: import fcntl` + `exec.py:173` `portalocker` fallback.
2. `strata/bench.py:33` → `sys.frozen/_MEIPASS` con fallback source tree.
3. `strata.spec:4` `datas` centralizado; `pyproject.toml:53` no requiere `package-data` extra (wheel usa `include=strata*`, binario usa `datas`).

### Fase 1 — POC PyInstaller ✅ `0cc832c` (2 días)

Spec canónico `prototype/strata.spec:1` (no CLI ad-hoc):

```bash
pip install pyinstaller
pyinstaller strata.spec --noconfirm
./dist/strata --help
./dist/strata build examples/daily_orders.strata
./dist/strata compile examples/daily_orders.strata --dialect postgres
./dist/strata run examples/daily_orders.strata --seed -o /tmp/demo.duckdb
du -sh dist/strata  # real 34M UPX, 80–110M sin UPX
```

Validación: `dist/strata test examples/daily_orders.strata --seed` + `bench` deben igualar `pytest` (ver §5 pendiente).

### Fase 2 — CI matrix ✅ `f88769d` (`prototype/.github/workflows/binary.yml:1`)

Matrix `ubuntu/macos/windows` (`binary.yml:14`), `setup-python 3.12`, `pyinstaller strata.spec`, smoke `help/build/compile/run/bench`, `upload-artifact` por OS, `release` con `attest-build-provenance` + `softprops/action-gh-release` (`binary.yml:162`).

Tiempo CI `90s` → `~270s` (×3 OS).

### Fase 3 — Hardening ✅ `6dedf62` (parcial)

- `strata.spec:33` `upx=True` + `binary.yml:35` `upx-ucl` en ubuntu + `sha256sum` + gate `>120M` warn (`binary.yml:108`).
- `binary.yml:88` `codesign --sign -` ad-hoc (sin `Developer ID` — ver §5 pendiente). Para prod:
  ```bash
  # macOS (requiere Apple Developer Program $99/año, secret APPLE_CERT + APPLE_CERT_PASSWORD)
  security import certificate.p12 -k ~/Library/Keychains/login.keychain
  codesign --sign "Developer ID Application: Tzinny (TEAMID)" --deep --timestamp --options runtime dist/strata
  xcrun notarytool submit dist/strata --apple-id "$APPLE_ID" --password "$APPLE_APP_PASSWORD" --team-id "$TEAMID" --wait
  xcrun stapler staple dist/strata
  # Windows (EV cert + AzureSignTool/signtool, secret WINDOWS_CERT)
  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx /p "$CERT_PASSWORD" dist/strata.exe
  ```
  Añadir como `if: startsWith(github.ref,'refs/tags/v')` steps en `binary.yml` con `secrets.APPLE_CERT`/`secrets.WINDOWS_CERT`.
- `strata/lsp.py:18` ya expuesto como `strata lsp` (`cli.py:50`) y empaquetado via `collect_data_files`.

### Extensión VS Code ✅ 0.1.5 (`prototype/vscode/`)

- Marketplace **`Tzinny-dev.strata-tzinny`**: grammar TextMate `.strata`,
  language-config (`//` comentarios) y cliente LSP sobre `strata lsp`
  (diagnostics del `Checker`, completion, hover, salto a definición).
- Setting `strata.binaryPath` para binarios fuera del `PATH` (venvs);
  comando **Strata: Show Version** verifica el binario configurado.
- Rebuild local: `cd vscode && npm install && npm run compile && npx
  vsce package` → `.vsix` → `code --install-extension <archivo>`.
  Guía completa: `docs-site/guide/vscode.md`.

---

## 4. Costos y riesgos — actualizados

- **Tamaño:** `58M` solo `_duckdb.so` → binario `34M` con UPX (`CHANGELOG.md:30`), `~85M` sin UPX. `pip+Docker` (`ghcr.io/tzinny-dev/strata:0.1.5`) sigue vigente para CI.
- **Mantenimiento:** cada bump `duckdb` o `psycopg2-binary` (`pyproject.toml:24`) obliga a rebuild triple + re-firma (automatizado en `binary.yml` on `tag v*`).
- **WASM/playground** `docs/warehouse-adapters.md:69` descartado — mismo costo que binario sin demanda probada.
- **Distribución:** `install.sh:1` + `homebrew/strata-lang.rb:1` operativos, pendientes: firma real Developer ID y `REPLACE_SHA256` automático (ver §5).

---

## 5. Decisión tomada + deuda restante

1. **Hecho 2026-09-23:** `PyPI strata-lang 0.1.5` + `binary.yml` matrix triple + `ghcr.io/tzinny-dev/strata:0.1.5` + `install.sh` + `homebrew` + `vscode Tzinny-dev.strata-tzinny 0.1.5`.
2. **Validar:** 5 entrevistas con demo `strata lineage-diff` (`market/market-validation.md:141`) — pendiente producto.
3. **Deuda abierta (no bloquea uso, ver detalle abajo):**
   - **Firma real:** `binary.yml:88` ad-hoc → requiere `APPLE_CERT`/`signtool` para Gatekeeper/SmartScreen (coste `99$/año`).
   - **Homebrew SHA:** `strata-lang.rb:10` `REPLACE_SHA256` manual → automatizar en `binary.yml:release`.
   - **Equivalencia binario==pytest:** falta gate `dist/strata test` vs `pytest` (`binary-standalone.md:60` histórico).
   - **UPX portabilidad:** validar en `ubuntu:20.04` (kernel `<5.10` rompe `duckdb`).
   - **arm64:** `install.sh:33` solo `amd64` → añadir `macos-14`/`linux arm64` cuando haya demanda.

---

*Actualizado 2026-09-24. Fuente: `prototype/` `CHANGELOG.md` Fase 0-4 → 0.1.5 + Unreleased 0.1.6, `dist/strata 34M` + `vscode 0.1.5`, `pytest 619 tests, 80% coverage` en `3.12`.*
