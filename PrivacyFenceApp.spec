# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PrivacyFenceApp.app (the daemon)
#
# Produces:
#   dist/PrivacyFenceApp.app/
#     Contents/MacOS/PrivacyFenceApp       ← daemon (main app; headless background
#                                             process, reachable only over its own
#                                             embedded web approval/settings UI --
#                                             P10 retired the native menu bar/dialogs)
#     Contents/MacOS/privacyfence-app      ← symlink → PrivacyFenceApp (for daemon auto-start)
#
# Claude's MCP entry point is the daemon's own /mcp Streamable HTTP endpoint
# (web/server.py); the stdio<->/mcp shim Claude Desktop actually spawns is
# built separately — a small Node/TypeScript proxy, see mcpb/shim/ and
# scripts/build_mcpb.sh — and distributed as a one-click Claude Desktop
# extension (.mcpb) instead of living inside this app.
#
# Build:
#   pip install pyinstaller
#   pyinstaller PrivacyFenceApp.spec
#
# Notes:
#   - Run on the target architecture. For Apple Silicon: arch -arm64 pyinstaller ...
#   - Code-signing and notarization are handled by build_dmg.sh.

import os
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path("scripts").resolve()))
from pyinstaller_common import HIDDEN_IMPORTS, collect_datas  # noqa: E402

# Version comes from the git tag via setuptools_scm now, not a hardcoded
# string here (see this repo's CLAUDE.md "Releasing" section) -- read back
# through the *installed* privacyfence package's own metadata (build_dmg.sh
# and CI both `pip install -e .` before running PyInstaller), the same way
# src/privacyfence/__init__.py itself resolves __version__ at runtime.
VERSION = _pkg_version("privacyfence")

# Use .icns built by build_dmg.sh; fall back to PNG (will error on macOS, but
# lets you run pyinstaller directly for quick dev iteration on Linux/CI).
ICON = os.environ.get("PRIVACYFENCE_ICNS", "src/privacyfence/resources/icon_512.png")

# ── data files + hidden imports ─────────────────────────────────────────────
# Shared with every other platform's spec -- see scripts/pyinstaller_common.py.

datas = collect_datas()
hidden_imports = HIDDEN_IMPORTS

# ── daemon (main .app entry point) ────────────────────────────────────────────

daemon_a = Analysis(
    ["src/_daemon_entry.py"],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

daemon_pyz = PYZ(daemon_a.pure)

daemon_exe = EXE(
    daemon_pyz,
    daemon_a.scripts,
    [],
    exclude_binaries=True,
    name="PrivacyFenceApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,      # no terminal window
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

# ── bundle into .app ──────────────────────────────────────────────────────────

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceApp",
)

app = BUNDLE(
    coll,
    name="PrivacyFenceApp.app",
    icon=ICON,
    bundle_identifier="com.privacyfence.app",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "PrivacyFence",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": "1",
        "LSUIElement": True,          # headless background daemon — no Dock icon, no menu bar item
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        # Allow outbound network connections for OAuth + API calls
        "com.apple.security.network.client": True,
    },
)
