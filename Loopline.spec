# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Loopline.app
#
# Produces:
#   dist/Loopline.app/
#     Contents/MacOS/Loopline          ← daemon (main app, opens menu bar)
#     Contents/MacOS/loopline-bridge   ← bridge (Claude's MCP entry point)
#     Contents/MacOS/loopline-app      ← symlink → Loopline (for daemon auto-start)
#
# Build:
#   pip install pyinstaller
#   pyinstaller Loopline.spec
#
# Notes:
#   - Run on the target architecture. For Apple Silicon: arch -arm64 pyinstaller ...
#   - Code-signing and notarization are handled by build_dmg.sh.

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)

# Use .icns built by build_dmg.sh; fall back to PNG (will error on macOS, but
# lets you run pyinstaller directly for quick dev iteration on Linux/CI).
ICON = os.environ.get("LOOPLINE_ICNS", "src/loopline/resources/icon_512.png")

# ── data files ────────────────────────────────────────────────────────────────

datas = [
    # App icons and bundled resources
    ("src/loopline/resources", "loopline/resources"),
    # google-auth needs its transport files
    *collect_data_files("google"),
    *collect_data_files("googleapiclient"),
    # fastmcp may carry JSON schema files
    *collect_data_files("fastmcp"),
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
    # cryptography (google-auth dependency)
    "cryptography",
    # telethon (optional – Telegram; bundled so the connector works)
    "telethon",
    # fastmcp transports
    "fastmcp",
    "mcp",
    # macOS tkinter
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    # loopline connectors (all loaded at runtime from _build_connectors)
    "loopline.connectors.gmail",
    "loopline.connectors.drive",
    "loopline.connectors.calendar",
    "loopline.connectors.contacts",
    "loopline.connectors.slack",
    "loopline.connectors.tasks",
    "loopline.connectors.telegram",
    "loopline.connectors.salesforce",
]

# ── daemon (main .app entry point) ────────────────────────────────────────────

daemon_a = Analysis(
    ["src/loopline/daemon_main.py"],
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
    name="Loopline",
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

# ── bridge (helper binary, run by Claude as an MCP server) ───────────────────

bridge_a = Analysis(
    ["src/loopline/bridge_main.py"],
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

bridge_pyz = PYZ(bridge_a.pure)

bridge_exe = EXE(
    bridge_pyz,
    bridge_a.scripts,
    [],
    exclude_binaries=True,
    name="loopline-bridge",
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

# ── bundle both into one .app ─────────────────────────────────────────────────

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    bridge_exe,
    bridge_a.binaries,
    bridge_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Loopline",
)

app = BUNDLE(
    coll,
    name="Loopline.app",
    icon=ICON,
    bundle_identifier="com.loopline.app",
    version="0.1.0",
    info_plist={
        "CFBundleDisplayName": "Loopline",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,          # menu bar app — no Dock icon
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        # Allow outbound network connections for OAuth + API calls
        "com.apple.security.network.client": True,
    },
)
