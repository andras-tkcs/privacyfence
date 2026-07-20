# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PrivacyFenceApp.app (the daemon)
#
# Produces:
#   dist/PrivacyFenceApp.app/
#     Contents/MacOS/PrivacyFenceApp       ← daemon (main app, opens menu bar)
#     Contents/MacOS/privacyfence-app      ← symlink → PrivacyFenceApp (for daemon auto-start)
#
# The bridge (Claude's MCP entry point) is built separately — a Node/TypeScript
# server, see bridge/ and scripts/build_mcpb.sh — and distributed as a
# one-click Claude Desktop extension (.mcpb) instead of living inside this app.
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
import tomllib
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)

# Single source of truth per CLAUDE.md's version-bump policy -- read, never
# hardcoded here, so this file never becomes a third place that needs bumping.
with open("pyproject.toml", "rb") as _f:
    VERSION = tomllib.load(_f)["project"]["version"]

# Use .icns built by build_dmg.sh; fall back to PNG (will error on macOS, but
# lets you run pyinstaller directly for quick dev iteration on Linux/CI).
ICON = os.environ.get("PRIVACYFENCE_ICNS", "src/privacyfence/resources/icon_512.png")

# ── data files ────────────────────────────────────────────────────────────────

datas = [
    # App icons and bundled resources
    ("src/privacyfence/resources", "privacyfence/resources"),
    # google-auth needs its transport files
    *collect_data_files("google"),
    *collect_data_files("googleapiclient"),
]

# ── hidden imports ────────────────────────────────────────────────────────────
# Modules loaded dynamically (importlib, __import__) that PyInstaller can miss.

hidden_imports = [
    # google API discovery
    "googleapiclient.discovery",
    "googleapiclient.http",
    "google.auth.transport.requests",
    "google_auth_oauthlib.flow",
    # yaml
    "yaml",
    # slack
    "slack_sdk",
    "slack_sdk.web",
    "slack_sdk.errors",
    # salesforce (imported lazily inside a try/except ImportError, so
    # PyInstaller's static analysis needs an explicit nudge to bundle it)
    "simple_salesforce",
    # atlassian-python-api (Jira/Confluence) -- same defensive-listing pattern
    # as the other third-party clients above.
    "atlassian",
    # cryptography (google-auth dependency)
    "cryptography",
    # openpyxl (imported lazily inside a try/except ImportError by
    # audit_log.py's weekly Excel export, so needs the same explicit nudge)
    "openpyxl",
    # telethon (optional – Telegram; bundled so the connector works)
    "telethon",
    # privacyfence connectors -- all ten, imported directly by daemon_main.py;
    # listed explicitly anyway as a defensive backstop against PyInstaller's
    # static analysis missing one.
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
]

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
        "LSUIElement": True,          # menu bar app — no Dock icon
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        # Allow outbound network connections for OAuth + API calls
        "com.apple.security.network.client": True,
    },
)
