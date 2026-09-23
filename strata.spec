# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('examples', 'examples'), ('bench', 'bench'), ('LICENSE', '.'), ('README.md', '.')]
datas += collect_data_files('strata')


a = Analysis(
    ['strata_entry.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['duckdb', 'psycopg2', 'yaml', 'strata.adapters', 'strata.bench', 'strata.dbcompat', 'strata.analysis', 'strata.lsp', 'portalocker', 'google.cloud.bigquery', 'snowflake.connector'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='strata',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # effective only if `upx` present on PATH (ubuntu-latest has upx-ucl, macos/windows skip)
    upx_exclude=[],  # keep empty — if UPX breaks duckdb on old glibc/kernels (<5.10), set upx=False or pin UPX off for that release (see binary-standalone.md §5)
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
