# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for a Windows onedir build of the daemon (issue #121).
#
# Produces:
#   dist/PrivacyFenceApp/
#     PrivacyFenceApp.exe        <- daemon (tray + connectors, same entry point)
#     _internal/                 <- PyInstaller's own support files
#
# Unlike PrivacyFenceApp.spec (macOS), this has no BUNDLE step -- a
# PyInstaller Windows build has no equivalent of a .app bundle at all, it's
# just an exe next to its own support directory. There is also, correspondingly,
# no privacyfence-app symlink to create for autostart naming -- the Windows
# Task Scheduler entry (com.privacyfence.app.task.xml) just points its
# <Command> straight at PrivacyFenceApp.exe. Everything else (Analysis's
# hidden_imports, the entry point, certifi/SSL_CERT_FILE handling in
# src/_daemon_entry.py) is identical to the macOS spec -- none of it is
# platform-specific.
#
# The bridge (Claude's MCP entry point) is built separately, same as on
# macOS -- see bridge/ and scripts/build_mcpb.sh; it's plain Node/TypeScript
# and needs no per-OS build step of its own.
#
# Build (from a Windows machine or a Windows PyInstaller-capable CI runner --
# PyInstaller always targets the OS it runs on, this can't cross-compile
# from macOS/Linux):
#   pip install -e ".[dev]"
#   pyinstaller PrivacyFenceApp.windows.spec
#
# Notes:
#   - PyInstaller's icon= parameter needs a .ico on Windows, not the .icns
#     build_dmg.sh's `sips`/`iconutil` steps produce for macOS. Set
#     PRIVACYFENCE_ICO to a real .ico (e.g. converted from
#     resources/icon_512.png via Pillow's Image.save(..., format="ICO"));
#     the PNG fallback below will error out here the same way the macOS
#     spec's own PNG fallback comment already documents for .icns.
#   - Authenticode code-signing (the Windows analog of build_dmg.sh's
#     codesign/notarytool steps) isn't wired up yet -- see
#     docs/windows-port-status.md.

import os
import sys
import tomllib
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)

# Single source of truth per CLAUDE.md's version-bump policy -- read, never
# hardcoded here, same as the macOS spec.
with open("pyproject.toml", "rb") as _f:
    VERSION = tomllib.load(_f)["project"]["version"]

# See module docstring above -- PyInstaller wants a .ico here, not the
# macOS spec's .icns.
ICON = os.environ.get("PRIVACYFENCE_ICO", "src/privacyfence/resources/icon_512.png")

# ── data files ────────────────────────────────────────────────────────────────
# Identical to the macOS spec -- none of this is platform-specific.

datas = [
    ("src/privacyfence/resources", "privacyfence/resources"),
    *collect_data_files("google"),
    *collect_data_files("googleapiclient"),
]

# ── hidden imports ────────────────────────────────────────────────────────────
# Copied verbatim from PrivacyFenceApp.spec -- see that file's own comments
# per entry. None of these are macOS-specific (they're all dynamically-
# imported third-party/connector modules PyInstaller's static analysis can
# miss regardless of OS), so there's deliberately no separate "Windows
# hidden imports" list to keep in sync with the macOS one by hand.

hidden_imports = [
    "googleapiclient.discovery",
    "googleapiclient.http",
    "google.auth.transport.requests",
    "google_auth_oauthlib.flow",
    "yaml",
    "slack_sdk",
    "slack_sdk.web",
    "slack_sdk.errors",
    "simple_salesforce",
    "atlassian",
    "cryptography",
    "openpyxl",
    "telethon",
    "privacyfence.connectors.gmail",
    "privacyfence.connectors.drive",
    "privacyfence.connectors.calendar",
    "privacyfence.connectors.contacts",
    "privacyfence.connectors.slack",
    "privacyfence.connectors.tasks",
    "privacyfence.connectors.telegram",
    "privacyfence.connectors.salesforce",
    "privacyfence.connectors.jira",
    "privacyfence.connectors.confluence",
    # Windows-only UI backends (tray_windows.py/approval_window_windows.py/
    # dialog_window_windows.py/settings_window_windows.py) -- guarded
    # imports elsewhere in the codebase (try/except ImportError) are
    # exactly the pattern PyInstaller's static analysis can miss, same
    # reasoning as simple_salesforce/openpyxl above.
    "pystray",
    "pystray._win32",
    "webview",
    "webview.platforms.edgechromium",
    "PIL",
]

# ── daemon (main exe entry point) ─────────────────────────────────────────────

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
    console=False,       # no console window -- this is a tray app
    # argv_emulation/codesign_identity/entitlements_file (the macOS spec's
    # EXE kwargs) are all macOS-only PyInstaller options with no Windows
    # meaning, so deliberately omitted here rather than passed as no-ops.
    icon=ICON,
)

# ── collect into onedir build ─────────────────────────────────────────────────
# No BUNDLE step -- see module docstring; a Windows onedir build's own
# directory (dist/PrivacyFenceApp/) is already the whole distributable unit.

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceApp",
)
