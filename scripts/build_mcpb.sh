#!/usr/bin/env bash
# Build PrivacyFence's Claude Desktop extension(s) — one-click .mcpb installs
# that register an MCP server for PrivacyFence with no manual
# claude_desktop_config.json edits.
#
# Until the bridge is retired (P5, see docs/https-connector-refactor-plan.md
# §12 — D11), this builds **two** .mcpb files, not one, so a user can roll
# back without needing to build anything themselves:
#
#   - PrivacyFence.mcpb                — mcpb/shim/: talks to the daemon's
#     /mcp Streamable HTTP endpoint. The default going forward; requires
#     web.mcp.enabled in config/settings.yaml (on by default as of D11/P4b).
#   - PrivacyFence (Legacy Bridge).mcpb — bridge/: talks to the daemon over
#     the original IPC socket, no /mcp needed. Install this one instead if
#     /mcp isn't an option for you yet.
#
# Both install side by side without conflicting: their manifests use
# different `name`s ("privacyfence" vs "privacyfence-legacy-bridge"), so
# Claude Desktop registers them as two distinct MCP servers.
#
# Each is a small Node/TypeScript MCP server with no connector clients, no
# PII detection, no PyObjC/AppKit — bundled by esbuild into a single
# dependency-free server/{shim,bridge}.js, so neither .mcpb ships a Python
# framework or a node_modules/ directory. Claude Desktop supplies the Node
# runtime itself (server.type = "node" in each manifest). This script does
# NOT depend on build_dmg.sh.
#
# Either extension still talks to the PrivacyFence daemon, so the daemon
# (PrivacyFenceApp.app, built separately by build_dmg.sh, still Python) must
# be installed and configured on its own — this bundle only wires up the MCP
# server entry.
#
# Prerequisites:
#   node + npm on PATH (npm installs the build-time deps for both mcpb/shim/
#   and bridge/; npx runs the @anthropic-ai/mcpb CLI).
#   python3 on PATH (only used to read the version out of pyproject.toml —
#   the daemon itself is not built by this script).
#
# Usage:
#   ./scripts/build_mcpb.sh
#
# Output: dist/PrivacyFence-<version>.mcpb
#         dist/PrivacyFence-legacy-bridge-<version>.mcpb
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$(command -v python3)"
VERSION=$("$PYTHON" -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['version'])")

echo "=== Building PrivacyFence's Claude Desktop extensions ${VERSION} ==="

# ── PrivacyFence.mcpb (mcpb/shim/, talks to /mcp) ────────────────────────────
echo ""
echo "→ Building the Node shim (mcpb/shim/dist/shim.js)…"
(
  cd mcpb/shim
  npm ci --silent
  npm run build --silent
)

SHIM_STAGE="build/mcpb-stage"
SHIM_OUT="dist/PrivacyFence-${VERSION}.mcpb"

echo "→ Staging PrivacyFence.mcpb…"
rm -rf "$SHIM_STAGE"
mkdir -p "${SHIM_STAGE}/server"
cp mcpb/shim/dist/shim.js "${SHIM_STAGE}/server/shim.js"
sed "s/__VERSION__/${VERSION}/" mcpb/manifest.json.tmpl > "${SHIM_STAGE}/manifest.json"
cp src/privacyfence/resources/icon_512.png "${SHIM_STAGE}/icon.png"

echo "→ Validating manifest…"
npx --yes @anthropic-ai/mcpb validate "${SHIM_STAGE}/manifest.json"

echo "→ Packing…"
rm -f "$SHIM_OUT"
npx --yes @anthropic-ai/mcpb pack "$SHIM_STAGE" "$SHIM_OUT"

# ── PrivacyFence (Legacy Bridge).mcpb (bridge/, talks to the IPC socket) ────
echo ""
echo "→ Building the Node bridge (bridge/dist/bridge.js)…"
(
  cd bridge
  npm ci --silent
  BRIDGE_VERSION="${VERSION}" npm run build --silent
)

BRIDGE_STAGE="build/mcpb-legacy-bridge-stage"
BRIDGE_OUT="dist/PrivacyFence-legacy-bridge-${VERSION}.mcpb"

echo "→ Staging PrivacyFence (Legacy Bridge).mcpb…"
rm -rf "$BRIDGE_STAGE"
mkdir -p "${BRIDGE_STAGE}/server"
cp bridge/dist/bridge.js "${BRIDGE_STAGE}/server/bridge.js"
sed "s/__VERSION__/${VERSION}/" mcpb/manifest-legacy-bridge.json.tmpl > "${BRIDGE_STAGE}/manifest.json"
cp src/privacyfence/resources/icon_512.png "${BRIDGE_STAGE}/icon.png"

# No code signing needed for either: both are plain JS with no Mach-O
# binaries. Only PrivacyFenceApp.app, built and signed by build_dmg.sh,
# needs a Developer ID signature and notarization.

echo "→ Validating manifest…"
npx --yes @anthropic-ai/mcpb validate "${BRIDGE_STAGE}/manifest.json"

echo "→ Packing…"
rm -f "$BRIDGE_OUT"
npx --yes @anthropic-ai/mcpb pack "$BRIDGE_STAGE" "$BRIDGE_OUT"

echo ""
echo "✓ Done:"
echo "  ${SHIM_OUT}   ($(du -sh "$SHIM_OUT" | cut -f1))"
echo "  ${BRIDGE_OUT}   ($(du -sh "$BRIDGE_OUT" | cut -f1))"
echo ""
echo "Install by double-clicking either .mcpb in Claude Desktop, or drag it"
echo "onto Settings → Extensions → Install Extension… They install side by"
echo "side without conflicting; PrivacyFence.mcpb (the /mcp shim) is the"
echo "one to use unless you have a specific reason to fall back to the"
echo "legacy bridge."
