# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PrivacyFenceApp.exe on Windows (the now-removed docs/windows-support-
# plan.md Phase 2, B4 in the now-removed docs/windows-linux-support-plan.md).
#
# Produces:
#   dist/PrivacyFenceApp/
#     PrivacyFenceApp.exe        <- daemon (main app; headless background
#                                   process, reachable only over its own
#                                   embedded web approval/settings UI --
#                                   same P10-retired-native-UI story as the
#                                   macOS build, see PrivacyFenceApp.spec)
#     privacyfence-app.exe        <- copy of PrivacyFenceApp.exe (for daemon
#                                   auto-start; see scripts/build_installer.
#                                   ps1 step 4 for why this is a copy, not a
#                                   symlink like the macOS/Linux builds use)
#
# Claude's MCP entry point is the daemon's own /mcp Streamable HTTP endpoint
# (web/server.py); the stdio<->/mcp shim Claude Desktop actually spawns is
# built separately -- see mcpb/shim/ and scripts/build_mcpb.sh -- and
# distributed as a one-click Claude Desktop extension (.mcpb) alongside the
# installer, not bundled inside this .exe.
#
# Build:
#   pip install pyinstaller
#   pyinstaller PrivacyFenceApp.win.spec
#
# Notes:
#   - Must run on a Windows build host -- PyInstaller doesn't cross-compile.
#   - Unlike the macOS spec, there is no BUNDLE() step: a onedir EXE/COLLECT
#     output is the whole distributable here, packaged into an installer by
#     scripts/build_installer.ps1 + installer/privacyfence.iss instead of a
#     drag-to-Applications bundle.
#   - Code-signing (both this .exe and the installer's own .exe) is handled
#     by build_installer.ps1, not this spec -- same split of responsibility
#     as PrivacyFenceApp.spec/build_dmg.sh on macOS.

import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path("scripts").resolve()))
from pyinstaller_common import DATAS, HIDDEN_IMPORTS  # noqa: E402

# Version comes from the git tag via setuptools_scm now, not a hardcoded
# string here (see this repo's CLAUDE.md "Releasing" section) -- read back
# through the *installed* privacyfence package's own metadata
# (build_installer.ps1 and CI both `pip install -e .` before running
# PyInstaller), the same way src/privacyfence/__init__.py itself resolves
# __version__ at runtime. Unused directly below (no version resource is
# stamped onto the .exe yet -- see the module docstring), but resolving it
# here fails the build loudly if the package isn't installed, same as the
# macOS spec, rather than producing an unversioned build silently.
VERSION = _pkg_version("privacyfence")

# .ico built by scripts/build_installer.ps1's icon-conversion step (Phase
# 2.3: a Pillow-based PNG -> multi-resolution .ico conversion, since neither
# macOS's sips/iconutil nor a Windows-native equivalent is available
# cross-platform in CI). Falls back to the raw PNG so `pyinstaller
# PrivacyFenceApp.win.spec` still runs directly for quick dev iteration
# without the conversion step -- EXE() on a non-Windows host ignores the
# icon entirely either way (see PrivacyFenceApp.spec's identical fallback).
ICON = "build/privacyfence.ico"
if not Path(ICON).exists():
    ICON = "src/privacyfence/resources/icon_512.png"

# ── data files + hidden imports ─────────────────────────────────────────────
# Shared with every other platform's spec -- see scripts/pyinstaller_common.py.

datas = DATAS
hidden_imports = HIDDEN_IMPORTS

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
    console=False,      # no console window -- headless background daemon
    icon=ICON,
)

# ── onedir output ────────────────────────────────────────────────────────────
# No BUNDLE() step -- that's macOS-only (.app/.icns). This onedir tree is
# the whole Windows distributable, installed to %ProgramFiles%\PrivacyFence\
# by installer/privacyfence.iss.

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceApp",
)
