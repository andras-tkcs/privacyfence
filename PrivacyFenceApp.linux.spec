# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PrivacyFenceApp (Linux onedir build) -- the Linux equivalent of
# PrivacyFenceApp.spec's macOS .app, packaged into a .deb by scripts/build_deb.sh (see
# the now-removed docs/linux-local-deb-packaging-plan.md, Phase 1).
#
# Produces:
#   dist/PrivacyFenceApp/
#     PrivacyFenceApp        ← daemon (main entry point; headless background process,
#                                reachable only over its own embedded web approval/settings UI --
#                                P10 retired the native menu bar/dialogs)
#     privacyfence-app       ← symlink → PrivacyFenceApp (for daemon auto-start; the mcpb shim's
#                                findDaemonCmd() and the .deb's autostart entry both look for
#                                this name specifically)
#     <bundled libs>
#
# Same Analysis/PYZ/EXE/COLLECT structure as PrivacyFenceApp.spec, and the same datas/
# hidden_imports list (scripts/pyinstaller_common.py -- kept in one place so the two specs can't
# drift). What's deliberately *not* here, versus the macOS spec:
#   - No BUNDLE() step -- that's macOS .app bundling only; a Linux onedir output is the final
#     artifact as-is, no further PyInstaller-level wrapping.
#   - No .icns / entitlements / codesign arguments -- Linux's EXE() doesn't embed an exe icon the
#     way Windows/.app builds do; the existing PNG icons in src/privacyfence/resources/ are used
#     as-is by the .desktop entry (resources/linux/privacyfence.desktop) and app-menu icon
#     instead, no conversion step needed (contrast Windows, which does need a generated .ico).
#
# Claude's MCP entry point is the daemon's own /mcp Streamable HTTP endpoint (web/server.py); the
# stdio<->/mcp shim Claude Desktop actually spawns is built separately -- see mcpb/shim/ and
# scripts/build_mcpb.sh -- and isn't part of this build.
#
# Build:
#   pip install pyinstaller
#   pyinstaller PrivacyFenceApp.linux.spec
#
# Notes:
#   - Run on the target architecture -- PyInstaller doesn't cross-compile, so an arm64 build
#     needs an arm64 build host (the now-removed docs/linux-local-deb-packaging-plan.md P4.2).
#   - `.deb` packaging (debian/ metadata, dpkg-deb, lintian) is handled by scripts/build_deb.sh,
#     not this spec -- this spec's only job is producing the onedir bundle.

import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

SRC = str(Path("src").resolve())
sys.path.insert(0, SRC)
sys.path.insert(0, str(Path("scripts").resolve()))

# Shared with PrivacyFenceApp.spec -- see scripts/pyinstaller_common.py's docstring for why the
# list itself lives there instead of being hand-copied into both specs.
from pyinstaller_common import DATAS, HIDDEN_IMPORTS

# Version comes from the git tag via setuptools_scm now, not a hardcoded string here (see this
# repo's CLAUDE.md "Releasing" section) -- read back through the *installed* privacyfence
# package's own metadata (scripts/build_deb.sh and CI both `pip install -e .` before running
# PyInstaller), same as PrivacyFenceApp.spec and src/privacyfence/__init__.py itself. Not used
# directly in this spec (no BUNDLE()/plist to stamp a version into, unlike macOS) -- read anyway
# so a missing `pip install -e .` fails loudly here too, before the Analysis step, rather than
# only inside scripts/build_deb.sh's own version-string handling.
VERSION = _pkg_version("privacyfence")

datas = DATAS
hidden_imports = HIDDEN_IMPORTS

# ── daemon (main entry point) ─────────────────────────────────────────────────

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
    # Unlike PrivacyFenceApp.spec's strip=False (kept that way there to stay safe for macOS
    # codesigning), Linux binaries built here get stripped -- there's no signing step to worry
    # about, and lintian's `unstripped-binary-or-object` check (scripts/build_deb.sh's lint gate,
    # P4.3 in the now-removed docs/linux-local-deb-packaging-plan.md) treats leaving debug symbols in as an error
    # for a shipped .deb. Verified this doesn't break the frozen app (see that plan's Phase 1/7
    # notes).
    strip=True,
    upx=True,
    console=False,      # no terminal window
    target_arch=None,
)

# ── collect into onedir output ────────────────────────────────────────────────
# No BUNDLE() step here (macOS-only .app bundling) -- dist/PrivacyFenceApp/ *is* the shippable
# artifact; scripts/build_deb.sh stages it straight into the .deb under /opt/privacyfence.

coll = COLLECT(
    daemon_exe,
    daemon_a.binaries,
    daemon_a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name="PrivacyFenceApp",
)
