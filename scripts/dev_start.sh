#!/usr/bin/env bash
# Start the local source/dev build of PrivacyFence and (re-)register the dev
# bridge with Claude, so it always points at this checkout's venv instead of
# a DMG/mcpb install. Uses `claude mcp` if the Claude Code CLI is on PATH;
# otherwise edits Claude Desktop's own config file directly.
#
# Usage:
#   ./scripts/dev_start.sh
#
# Safe to re-run any time — it just makes sure the venv exists, the MCP
# registration points at the right path, then runs the daemon in the
# foreground. Ctrl-C stops the daemon and de-registers the dev bridge again.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -d .venv ]; then
  echo "No .venv found — creating one and installing PrivacyFence in editable mode..."
  python3 -m venv .venv
  .venv/bin/pip install -e .
fi

if [ -f scripts/dev_env.sh ]; then
  source scripts/dev_env.sh
fi

if [ ! -f config/settings.yaml ]; then
  cp src/privacyfence/resources/settings.yaml.example config/settings.yaml
  echo "Created config/settings.yaml from the example — edit it before your first real test run."
fi

BRIDGE_PATH="$(pwd)/.venv/bin/privacyfence-bridge"
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
USE_CLI=0
USE_DESKTOP_CONFIG=0

set_desktop_mcp_entry() {
  # $1: "add" or "remove"
  python3 - "$DESKTOP_CONFIG" "$BRIDGE_PATH" "$1" <<'PYEOF'
import json, sys
path, bridge_path, action = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    config = json.load(f)
if action == "add":
    config.setdefault("mcpServers", {})["privacyfence"] = {"command": bridge_path}
else:
    config.get("mcpServers", {}).pop("privacyfence", None)
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PYEOF
}

if command -v claude >/dev/null 2>&1; then
  USE_CLI=1
  echo "Registering dev bridge with the Claude Code CLI (privacyfence -> $BRIDGE_PATH)..."
  claude mcp remove privacyfence >/dev/null 2>&1 || true
  claude mcp add privacyfence "$BRIDGE_PATH"
elif [ -f "$DESKTOP_CONFIG" ]; then
  USE_DESKTOP_CONFIG=1
  echo "No 'claude' CLI on PATH — registering directly in Claude Desktop's config instead:"
  echo "  $DESKTOP_CONFIG"
  cp "$DESKTOP_CONFIG" "$DESKTOP_CONFIG.privacyfence-dev.bak"
  set_desktop_mcp_entry add
  echo
  echo "Added the 'privacyfence' MCP server entry. Quit and reopen Claude Desktop now"
  read -r -p "so it picks up the change, then press Enter here to continue (Ctrl-C to abort)... "
else
  echo "claude CLI not found on PATH, and no Claude Desktop config found at:"
  echo "  $DESKTOP_CONFIG"
  echo "Register manually with: claude mcp add privacyfence \"$BRIDGE_PATH\""
  echo "or add a \"privacyfence\": {\"command\": \"$BRIDGE_PATH\"} entry under"
  echo "\"mcpServers\" in Claude Desktop's config yourself."
fi

cleanup() {
  if [ "$USE_CLI" = "1" ]; then
    echo
    echo "Removing dev bridge registration from the Claude Code CLI..."
    claude mcp remove privacyfence >/dev/null 2>&1 || true
  elif [ "$USE_DESKTOP_CONFIG" = "1" ]; then
    echo
    echo "Removing the 'privacyfence' entry from Claude Desktop's config..."
    set_desktop_mcp_entry remove
    echo "Removed. Quit and reopen Claude Desktop to fully clear it, whenever convenient."
  fi
}
trap cleanup EXIT

echo "Starting privacyfence-app (Ctrl-C to stop)..."
.venv/bin/privacyfence-app
