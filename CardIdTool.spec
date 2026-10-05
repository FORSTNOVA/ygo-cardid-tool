# -*- mode: python ; coding: utf-8 -*-

import os

setnames = os.path.join(SPECPATH, 'setnames.json')

a = Analysis(
    [os.path.join(SPECPATH, 'cardid_tool.py')],
    pathex=[],
    binaries=[],
    datas=[(setnames, '.')] if os.path.isfile(setnames) else [],
    hiddenimports=[],
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
    name='CardIdTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
