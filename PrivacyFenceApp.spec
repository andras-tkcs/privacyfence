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
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)

# Version comes from the git tag via setuptools_scm now, not a hardcoded
# string here (see this repo's CLAUDE.md "Releasing" section) -- read back
# through the *installed* privacyfence package's own metadata (build_dmg.sh
# and CI both `pip install -e .` before running PyInstaller), the same way
# src/privacyfence/__init__.py itself resolves __version__ at runtime.
VERSION = _pkg_version("privacyfence")

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
    # PyInstaller doesn't bundle a package's own .dist-info by default --
    # without this, src/privacyfence/__init__.py's
    # importlib.metadata.version("privacyfence") call would raise
    # PackageNotFoundError at runtime *inside the frozen app* (it worked fine
    # a moment ago in this very spec file, above, only because that ran
    # unfrozen against the build machine's installed package).
    *copy_metadata("privacyfence"),
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
        "LSUIElement": True,          # headless background daemon — no Dock icon, no menu bar item
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        # Allow outbound network connections for OAuth + API calls
        "com.apple.security.network.client": True,
    },
)
