# Strata — Binario standalone `strata`

> Análisis de requisitos para cumplir `propuesta-lenguaje-strata.md:49` "un solo binario: compilador+LSP+runner". Estado: prototipo Python `prototype/pyproject.toml:1` `strata==0.1.0` `strata=cli:main` `pyproject.toml:50`.

## Resumen

Hoy Strata es `pip install strata` (wheel `dist/strata-0.1.0-py3-none-any.whl:147K`, `twine check PASSED`). El binario prometido por el spec no existe. El piso de tamaño lo marca `duckdb`: `_duckdb.cpython-312-x86_64-linux-gnu.so 58M`, `venv 143M` total (`psycopg2 332K`, `yaml 2.6M`, `strata 12k SLOC` `cli.py:1306` `exec.py:1550` `analysis.py:1697`). Binario empaquetado esperado: **80–120M sin comprimir, 40–60M con UPX**.

Recomendación: **no construir binario ahora**. Existe atajo `uv tool install strata` (0 costo) y `Docker` cubre reproducibilidad. Construir binario productivo cuesta `2 dev × 3 semanas` + matrix CI, y cada bump de `duckdb 1.5.5→1.6` obliga a rebuild triple (linux/macos/windows).

---

## 1. Bloqueos antes de empaquetar

| Bloqueo | Evidencia | Fix requerido para binario |
|---|---|---|
| `fcntl.flock` POSIX only | `strata/exec.py:11` `import fcntl` / `exec.py:162` `LOCK_EX` | `plan-hito-2.md:159` "no probado en Windows". En binario: `try: import fcntl except: fcntl=None` + fallback `portalocker` o `msvcrt.locking`, o deshabilitar lock en `win32` con warning. Sin esto el `.exe` crashea. |
| `Path(__file__)` y recursos | `strata/bench.py:26` `Path(__file__).parent.parent / "bench"`, `strata/analysis.py:418` `Path(module.path)` | En PyInstaller `__file__` → `sys._MEIPASS`. Reemplazar por `importlib.resources.files("strata")` con fallback `_MEIPASS`. Hoy `examples/` y `bench/golden/` no están en wheel (`pyproject.toml:53` `include=strata*`). Para binario: `datas=[("examples","examples"),("bench","bench")]`. |
| Imports dinámicos | `strata/cli.py:54` `import psycopg2`, `cli.py:61` `import duckdb`, `strata/adapters.py:92` `import duckdb` + `adapters.py:95` `__import__(mod)` | PyInstaller no detecta `__import__`. Requiere `hiddenimports=["duckdb","psycopg2","yaml._yaml"]`, `get_dialect` dinámico igual. |
| `duckdb` binario nativo | `_duckdb.so 58M` embebe ICU+parquet, `ldd` muestra glibc | PyInstaller debe copiar `.so` + extensiones `duckdb.duckdb_extension`. En `alpine/musl` falla. Nuitka lo compila pero igual carga dinámico. |
| `information_schema` / `duckdb_views()` | `strata/dbcompat.py:92` `pg_views` vs `strata/dbcompat.py:96` `duckdb_views()`, `strata/exec.py:455` | No bloquea binario, pero test de integración debe correr contra Postgres real (`tests/pg_harness.py:28` ephemeral cluster) en CI del binario. |

---

## 2. Herramientas comparadas

| Tool | Tipo | Pro | Contra | Para Strata |
|---|---|---|---|---|
| **PyInstaller** | bundle intérprete+zip | Más maduro, hook `duckdb` existente, 1 comando `pyinstaller --onefile -n strata strata/cli.py` | 90M, falsos positivos antivirus, cada OS requiere build nativo, `UPX` rompe firma macOS | **Recomendado MVP — 2 semanas** |
| **Nuitka** | Python→C→EXE | Binario 30% más rápido/pequeño, mejor `UPX`, respeta `__file__` | Compila `12k` líneas → 15–25 min/build, rompe `psycopg2` C-ext sin `--include-package-data` | Producción — 6 semanas, si PyInstaller valida demanda |
| **PyOxidizer** | Rust empaquetador | Single file real, sin `_MEIPASS` temp | Config Rust, hook `duckdb` manual, menos doc | Solo si apuntas a toolchain `cargo`-like |
| **pex / shiv / uv tool** | zipapp | No es standalone (requiere `python3.12`) | No cumple spec literal, pero `uv tool install strata` ya funciona hoy sin esfuerzo | **Interino gratis** |

---

## 3. Roadmap mínimo viable

### Fase 0 — Preparación (1 día, sin binario)

1. `strata/exec.py:11` → `try: import fcntl except ImportError: fcntl = None` + rama `if fcntl is None: portalocker else fcntl`.
2. `strata/bench.py:26` → `importlib.resources.files("strata").joinpath("../../bench")` con fallback `getattr(sys, "_MEIPASS", Path(__file__).parent)`.
3. `pyproject.toml:53` añadir `package-data` para `examples/*.strata` y `bench/golden/*`.

### Fase 1 — POC PyInstaller (2–3 días)

```bash
pip install pyinstaller
pyinstaller --onefile --name strata \
  --hidden-import=duckdb --hidden-import=psycopg2 --hidden-import=yaml \
  --collect-data=strata --add-data="examples:examples" --add-data="bench:bench" \
  strata/cli.py

./dist/strata --help
./dist/strata build examples/daily_orders.strata
./dist/strata compile examples/daily_orders.strata --dialect postgres
./dist/strata run examples/daily_orders.strata --seed -o /tmp/demo.duckdb
du -sh dist/strata  # esperado 85–110M
```

Validación: `dist/strata test examples/daily_orders.strata --seed` debe dar los mismos `537 tests` que `pytest`.

### Fase 2 — CI matrix (extender `prototype/.github/workflows/ci.yml:15`)

```yaml
build:
  strategy:
    matrix:
      os: [ubuntu-latest, macos-latest, windows-latest]
  runs-on: ${{ matrix.os }}
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
    - run: pip install pyinstaller && pyinstaller --onefile --name strata strata/cli.py
    - uses: actions/upload-artifact@v4
      with: { path: dist/strata* }
    - if: startsWith(github.ref, 'refs/tags/v')
      uses: softprops/action-gh-release@v2
      with: { files: dist/strata* }
```

Tiempo CI actual `90s` → `270s` (×3 OS). Sin esto no hay `.exe`/`.dmg` publicables.

### Fase 3 — Hardening

- Firmar macOS `codesign --sign "Developer ID"` / Windows `signtool` — sin firma el OS bloquea ejecución.
- `UPX --lzma` baja a ~45M pero rompe `duckdb` en kernels <5.10 — testear en `ubuntu:20.04`.
- Incluir `strata/lsp.py:18` como `strata lsp` (ya expuesto en `cli.py:50`).

---

## 4. Costos y riesgos

- **Tamaño:** `58M` solo `duckdb` → binario nunca será "pequeño". `pip+Docker` (`ghcr.io/tzinny-dev/strata:0.1.0`) da 80% del beneficio con 10% del costo.
- **Mantenimiento:** cada bump `duckdb 1.5.5` o `psycopg2-binary 2.9.13` (ver `pyproject.toml:24`) obliga a rebuild triple + re-firma.
- **WASM/playground** `docs/warehouse-adapters.md:69` descartado — mismo costo que binario sin demanda probada.
- **Alternativa interina 0 costo:** documentar `uv tool install strata --from git+https://github.com/Tzinny-dev/strata` o `pipx install strata`. Cubre `propuesta-lenguaje-strata.md:47` "tooling día uno" sin binario.

---

## 5. Decisión recomendada

1. **Ahora:** publicar `PyPI` (wheel ya listo) + documentar `uv tool install`.
2. **Validar:** 5 entrevistas pagas con demo `strata lineage-diff` (`market/market-validation.md:141`).
3. **Si tracción >100 descargas/semana:** ejecutar Fase 0+1 PyInstaller Linux-only (1 semana, 90M) → si descarga >100, invertir en matrix + firma.

No invertir 3 semanas de binario antes de validar que alguien paga por `strata run --only-stale` vs `dbt run`.

---

*Generado 2026-09-21. Fuente: `prototype/` `7399 stmts`, `81.3%` coverage, `537 tests` verdes en `3.12` tras fix `tests/test_warehouse_partition_freshness.py:44` PEP 701.*
