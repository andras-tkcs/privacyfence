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

# ── Code sign bundled binaries ────────────────────────────────────────────────
# The bridge is a PyInstaller onedir build: a loose directory of Mach-O
# executables, dylibs, and native extensions (its own Python.framework,
# cryptography/rust extensions, etc.), not a single .app bundle. Notarization
# scans inside the .mcpb archive and rejects any Mach-O binary that isn't
# individually signed with a Developer ID cert + hardened runtime + secure
# timestamp — codesign on the top-level directory alone doesn't reach these.
#
# `@anthropic-ai/mcpb pack` dereferences symlinks into independent file
# copies rather than preserving them as symlinks in the archive (confirmed by
# inspecting the packed .mcpb's zip entries). Two PyInstaller-generated
# symlinks don't survive that dereferencing as valid signed binaries:
#   - _internal/Python (the @rpath/Python target every extension module
#     dynamically loads against, confirmed via `lsof` on a running bridge
#     process) gets a bundle-sealed signature when signed in place inside
#     Python.framework/Versions/X/, which only validates at that exact path —
#     the packer's flattened copy at _internal/Python fails
#     `codesign --verify` ("invalid Info.plist") because the sealed Resources
#     dir isn't there. Fixed by materializing it into a real file and signing
#     it at its actual flat destination, so the signature matches where it
#     ends up.
#   - Python.framework/Python (the framework's own top-level symlink) isn't
#     loaded by anything at runtime (same `lsof` check shows nothing opens
#     it) and can't be signed as a flat file at all — codesign refuses any
#     regular file living directly inside a `*.framework` directory
#     ("bundle format is ambiguous"). Simplest fix: drop it, since it's
#     dead weight the running process never touches.
PY_RPATH_TARGET="${STAGE}/server/_internal/Python"
if [ -L "$PY_RPATH_TARGET" ]; then
  real_python="$(readlink -f "$PY_RPATH_TARGET")"
  rm "$PY_RPATH_TARGET"
  cp "$real_python" "$PY_RPATH_TARGET"
fi
rm -f "${STAGE}/server/_internal/Python.framework/Python"

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
