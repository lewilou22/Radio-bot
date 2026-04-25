# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: folder build (recommended for faster-whisper / ctranslate2)."""

from PyInstaller.utils.hooks import collect_all

block_cipher = None

fw_datas, fw_binaries, fw_hiddenimports = collect_all("faster_whisper")
ct_datas, ct_binaries, ct_hiddenimports = collect_all("ctranslate2")

try:
    av_datas, av_binaries, av_hiddenimports = collect_all("av")
except Exception:
    av_datas, av_binaries, av_hiddenimports = [], [], []

extra_hidden = [
    "fuzzysearch",
    "certifi",
    "charset_normalizer",
    "idna",
    "urllib3",
    "tzdata",
    "zoneinfo",
    "radio_engine",
    "transcribers",
]

hiddenimports = list(
    dict.fromkeys(fw_hiddenimports + ct_hiddenimports + av_hiddenimports + extra_hidden)
)

a = Analysis(
    ["gui_monitor.py"],
    pathex=[],
    binaries=ct_binaries + av_binaries + fw_binaries,
    datas=ct_datas + av_datas + fw_datas
    + [
        ("stations.json", "."),
        ("blacklist.json", "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RadioMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RadioMonitor",
)
