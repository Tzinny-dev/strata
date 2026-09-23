# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('examples', 'examples'), ('bench', 'bench'), ('LICENSE', '.'), ('README.md', '.')]
datas += collect_data_files('strata')


a = Analysis(
    ['strata_entry.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['duckdb', 'psycopg2', 'yaml', 'strata.adapters', 'strata.bench', 'strata.dbcompat', 'strata.analysis'],
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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
