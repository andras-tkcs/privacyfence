# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the standalone privacyfence-bridge binary.
#
# The bridge only speaks MCP-over-stdio to Claude and JSON-over-Unix-socket to
# the daemon (see bridge_main.py) — it never imports the connector clients,
# google-auth, slack_sdk, telethon, atlassian-python-api, rumps, or tkinter.
# Building it separately from PrivacyFence.spec keeps the .mcpb bundle small
# instead of dragging in the daemon's entire dependency tree.
#
# Produces:
#   dist/PrivacyFenceBridge/privacyfence-bridge   ← packed into PrivacyFence.mcpb
#
# Build:
#   pyinstaller PrivacyFenceBridge.spec

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)

datas = [
    *collect_data_files("fastmcp"),
    *copy_metadata("fastmcp"),
    *copy_metadata("fastmcp-slim"),
]

hidden_imports = [
    "fastmcp",
    "mcp",
    "mcp.types",
]

bridge_a = Analysis(
    ["src/_bridge_entry.py"],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "rumps"],
    noarchive=False,
)

bridge_pyz = PYZ(bridge_a.pure)

bridge_exe = EXE(
    bridge_pyz,
    bridge_a.scripts,
    [],
    exclude_binaries=True,
    name="privacyfence-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,       # bridge speaks MCP over stdio — must be a console app
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    bridge_exe,
    bridge_a.binaries,
    bridge_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceBridge",
)
