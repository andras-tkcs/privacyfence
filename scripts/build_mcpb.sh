#!/usr/bin/env bash
# Build PrivacyFence.mcpb — a one-click Claude Desktop extension that installs
# the privacyfence-mcpb-shim MCP server (no manual claude_desktop_config.json
# edits).
#
# This builds mcpb/shim/ on its own — a Node/TypeScript stdio-to-Streamable-
# HTTP transport proxy with no connector clients, no PII detection, no
# PyObjC/AppKit, and (unlike the retired bridge/) no tool-schema or manifest
# knowledge at all — bundled by esbuild into a single dependency-free
# server/shim.js, so the .mcpb ships with neither a Python framework nor a
# node_modules/ directory. Claude Desktop supplies the Node runtime itself
# (server.type = "node" in the manifest — see mcpb/manifest.json.tmpl). This
# script does NOT depend on build_dmg.sh.
#
# The shim talks to the PrivacyFence daemon's /mcp endpoint over HTTP, so the
# daemon (PrivacyFence.app, built separately by build_dmg.sh, still Python)
# must be installed, configured, and running with web.mcp.enabled on its own
# — this bundle only wires up the MCP server entry (D11,
# docs/https-connector-refactor-plan.md §12).
#
# Prerequisites:
#   node + npm on PATH (npm installs mcpb/shim/'s build-time deps; npx runs
#   the @anthropic-ai/mcpb CLI).
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

echo "→ Building the Node shim (mcpb/shim/dist/shim.js)…"
(
  cd mcpb/shim
  npm ci --silent
  npm run build --silent
)

echo "→ Staging bundle contents…"
rm -rf "$STAGE"
mkdir -p "${STAGE}/server"
cp mcpb/shim/dist/shim.js "${STAGE}/server/shim.js"

sed "s/__VERSION__/${VERSION}/" mcpb/manifest.json.tmpl > "${STAGE}/manifest.json"
cp src/privacyfence/resources/icon_512.png "${STAGE}/icon.png"

# No code signing needed here: shim.js is plain JS with no Mach-O binaries.
# Only PrivacyFenceApp.app, built and signed by build_dmg.sh, needs a
# Developer ID signature and notarization.

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
