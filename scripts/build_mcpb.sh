#!/usr/bin/env bash
# Build PrivacyFence.mcpb — a one-click Claude Desktop extension that installs
# the privacyfence-bridge MCP server (no manual claude_desktop_config.json edits).
#
# This builds bridge/ on its own — a Node/TypeScript MCP server with no
# connector clients, no PII detection, no PyObjC/AppKit — bundled by esbuild
# into a single dependency-free server/bridge.js, so the .mcpb ships with
# neither a Python framework nor a node_modules/ directory. Claude Desktop
# supplies the Node runtime itself (server.type = "node" in the manifest —
# see mcpb/manifest.json.tmpl). This script does NOT depend on build_dmg.sh.
#
# The bridge still talks to the PrivacyFence daemon over a Unix socket, so the
# daemon (PrivacyFence.app, built separately by build_dmg.sh, still Python)
# must be installed and configured on its own — this bundle only wires up the
# MCP server entry.
#
# Prerequisites:
#   node + npm on PATH (npm installs bridge/'s build-time deps; npx runs the
#   @anthropic-ai/mcpb CLI).
#   python3 on PATH (only used to read the version out of pyproject.toml —
#   the daemon itself is not built by this script).
#
# Usage:
#   ./scripts/build_mcpb.sh
#
# Output: dist/PrivacyFence-<version>.mcpb
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$(command -v python3)"
VERSION=$("$PYTHON" -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['version'])")
STAGE="build/mcpb-stage"
OUT="dist/PrivacyFence-${VERSION}.mcpb"

echo "=== Building PrivacyFence.mcpb ${VERSION} ==="

echo "→ Building the Node bridge (bridge/dist/bridge.js)…"
(
  cd bridge
  npm ci --silent
  BRIDGE_VERSION="${VERSION}" npm run build --silent
)

echo "→ Staging bundle contents…"
rm -rf "$STAGE"
mkdir -p "${STAGE}/server"
cp bridge/dist/bridge.js "${STAGE}/server/bridge.js"

sed "s/__VERSION__/${VERSION}/" mcpb/manifest.json.tmpl > "${STAGE}/manifest.json"
cp src/privacyfence/resources/icon_512.png "${STAGE}/icon.png"

# No code signing needed here: bridge.js is plain JS with no Mach-O binaries
# (see the file header — the PyInstaller-era Python runtime this used to
# bundle, and the signing workarounds its packaging required, are gone as of
# the Node bridge rewrite). Only PrivacyFenceApp.app, built and signed by
# build_dmg.sh, needs a Developer ID signature and notarization.

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
