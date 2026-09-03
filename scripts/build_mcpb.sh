#!/usr/bin/env bash
# Build PrivacyFence's Claude Desktop extension — a one-click .mcpb install
# that registers an MCP server for PrivacyFence with no manual
# claude_desktop_config.json edits.
#
# Ships one file: PrivacyFence.mcpb — mcpb/shim/: talks to the daemon's
# /mcp Streamable HTTP endpoint (the only transport there is since P5
# retired the bridge, see docs/https-connector-refactor-plan.md §12, D11).
# Requires web.mcp.enabled in config/settings.yaml (on by default as of
# D11/P4b). Until P5, this script also built a second "Legacy Bridge"
# .mcpb as a rollback lever while both transports shipped; that lever isn't
# needed any more now that the bridge itself no longer exists.
#
# A small Node/TypeScript MCP server with no connector clients, no PII
# detection, no PyObjC/AppKit — bundled by esbuild into a single
# dependency-free server/shim.js, so the .mcpb ships no Python framework
# and no node_modules/ directory. Claude Desktop supplies the Node runtime
# itself (server.type = "node" in the manifest). This script does NOT
# depend on build_dmg.sh.
#
# The extension still talks to the PrivacyFence daemon, so the daemon
# (PrivacyFenceApp.app, built separately by build_dmg.sh, still Python) must
# be installed and configured on its own — this bundle only wires up the MCP
# server entry.
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

echo "=== Building PrivacyFence's Claude Desktop extension ${VERSION} ==="

echo ""
echo "→ Building the Node shim (mcpb/shim/dist/shim.js)…"
(
  cd mcpb/shim
  npm ci --silent
  npm run build --silent
)

STAGE="build/mcpb-stage"
OUT="dist/PrivacyFence-${VERSION}.mcpb"

echo "→ Staging PrivacyFence.mcpb…"
rm -rf "$STAGE"
mkdir -p "${STAGE}/server"
cp mcpb/shim/dist/shim.js "${STAGE}/server/shim.js"
sed "s/__VERSION__/${VERSION}/" mcpb/manifest.json.tmpl > "${STAGE}/manifest.json"
cp src/privacyfence/resources/icon_512.png "${STAGE}/icon.png"

# No code signing needed: plain JS with no Mach-O binaries. Only
# PrivacyFenceApp.app, built and signed by build_dmg.sh, needs a Developer
# ID signature and notarization.

echo "→ Validating manifest…"
npx --yes @anthropic-ai/mcpb validate "${STAGE}/manifest.json"

echo "→ Packing…"
rm -f "$OUT"
npx --yes @anthropic-ai/mcpb pack "$STAGE" "$OUT"

echo ""
echo "✓ Done: ${OUT}   ($(du -sh "$OUT" | cut -f1))"
echo ""
echo "Install by double-clicking the .mcpb in Claude Desktop, or drag it"
echo "onto Settings → Extensions → Install Extension…"
