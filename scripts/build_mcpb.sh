#!/usr/bin/env bash
# Build PrivacyFence.mcpb — a one-click Claude Desktop extension that installs
# the privacyfence-bridge MCP server (no manual claude_desktop_config.json edits).
#
# This builds the bridge on its own, from PrivacyFenceBridge.spec — a much
# smaller dependency set than the daemon (no google-auth, slack_sdk, telethon,
# atlassian-python-api, rumps, or tkinter). It does NOT depend on build_dmg.sh.
#
# The bridge still talks to the PrivacyFence daemon over a Unix socket, so the
# daemon (PrivacyFence.app, built separately by build_dmg.sh) must be installed
# and configured on its own — this bundle only wires up the MCP server entry.
#
# Prerequisites:
#   pip install pyinstaller
#   node + npx on PATH (used to run the @anthropic-ai/mcpb CLI via npx).
#
# Usage:
#   ./scripts/build_mcpb.sh
#
# Output: dist/PrivacyFence-<version>.mcpb
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -x ".venv/bin/pyinstaller" ]; then
  PYTHON=".venv/bin/python"
  PYINSTALLER=".venv/bin/pyinstaller"
elif command -v pyinstaller &>/dev/null; then
  PYTHON="$(command -v python3)"
  PYINSTALLER="$(command -v pyinstaller)"
else
  echo "PyInstaller not found — installing into .venv…"
  .venv/bin/pip install --quiet pyinstaller
  PYTHON=".venv/bin/python"
  PYINSTALLER=".venv/bin/pyinstaller"
fi

VERSION=$("$PYTHON" -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['version'])")
ONEDIR="dist/PrivacyFenceBridge"
STAGE="build/mcpb-stage"
OUT="dist/PrivacyFence-${VERSION}.mcpb"

echo "=== Building PrivacyFence.mcpb ${VERSION} ==="

echo "→ Running PyInstaller (bridge only)…"
"$PYINSTALLER" --noconfirm PrivacyFenceBridge.spec

echo "→ Staging bundle contents…"
rm -rf "$STAGE"
mkdir -p "${STAGE}/server"
rsync -a --exclude ".DS_Store" "${ONEDIR}/" "${STAGE}/server/"

sed "s/__VERSION__/${VERSION}/" mcpb/manifest.json.tmpl > "${STAGE}/manifest.json"
cp src/privacyfence/resources/icon_512.png "${STAGE}/icon.png"

# ── Code sign bundled binaries ────────────────────────────────────────────────
# The bridge is a PyInstaller onedir build: a loose directory of Mach-O
# executables, dylibs, and native extensions (its own Python.framework,
# cryptography/rust extensions, etc.), not a single .app bundle. Notarization
# scans inside the .mcpb archive and rejects any Mach-O binary that isn't
# individually signed with a Developer ID cert + hardened runtime + secure
# timestamp — codesign on the top-level directory alone doesn't reach these.
if [ -n "${SIGN_IDENTITY:-}" ]; then
  echo "→ Code-signing bridge binaries with: ${SIGN_IDENTITY}…"
  find "${STAGE}/server" -type f -print0 | while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q "Mach-O"; then
      codesign --force --options runtime --sign "$SIGN_IDENTITY" "$f"
    fi
  done
fi

echo "→ Validating manifest…"
npx --yes @anthropic-ai/mcpb validate "${STAGE}/manifest.json"

echo "→ Packing…"
rm -f "$OUT"
npx --yes @anthropic-ai/mcpb pack "$STAGE" "$OUT"

echo ""
echo "✓ Done: ${OUT}"
echo "  Size: $(du -sh "$OUT" | cut -f1)"
echo ""
echo "Install by double-clicking the .mcpb in Claude Desktop, or drag it onto"
echo "Settings → Extensions → Install Extension…"
