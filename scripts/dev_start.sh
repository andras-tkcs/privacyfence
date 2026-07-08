#!/usr/bin/env bash
# Start the local source/dev build of PrivacyFence and (re-)register the dev
# bridge with Claude Code, so `claude mcp` always points at this checkout's
# venv instead of a DMG/mcpb install.
#
# Usage:
#   ./scripts/dev_start.sh
#
# Safe to re-run any time — it just makes sure the venv exists, the MCP
# registration points at the right path, then runs the daemon in the
# foreground (Ctrl-C to stop).
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

if command -v claude >/dev/null 2>&1; then
  echo "Registering dev bridge with Claude Code (privacyfence -> $BRIDGE_PATH)..."
  claude mcp remove privacyfence >/dev/null 2>&1 || true
  claude mcp add privacyfence "$BRIDGE_PATH"
else
  echo "claude CLI not found on PATH — skipping MCP registration."
  echo "Register manually with: claude mcp add privacyfence \"$BRIDGE_PATH\""
fi

echo "Starting privacyfence-app (Ctrl-C to stop)..."
exec .venv/bin/privacyfence-app
