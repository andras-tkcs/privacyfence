/**
 * IPC protocol constants, ported from src/privacyfence/ipc.py.
 *
 * That file's module docstring is the single source of truth for the wire
 * protocol (newline-delimited JSON over a 127.0.0.1 TCP loopback socket,
 * authenticated by a per-launch random token) — these are just this side's
 * copies of its constants. protocol.test.ts asserts these literals against
 * ipc.py's source so the two can't silently drift.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const HOST = "127.0.0.1";
// Per-launch files the daemon writes with 0o600 permissions, only once it's
// actually bound and listening on an OS-assigned ephemeral port (see
// ipc_server.py's start()) -- read fresh on every connection attempt via
// readIpcPort()/readIpcToken() below, never cached, since a relaunched
// daemon gets both a new port and a new token. The port is discovered
// rather than a fixed, hardcoded number both sides agree on: see ipc.py's
// module docstring for why a fixed loopback port would break the documented
// two-account dev/live setup (docs/dev-vs-live-setup.md).
export const PORT_FILE = path.join(os.homedir(), ".privacyfence", "ipc_port");
export const TOKEN_FILE = path.join(os.homedir(), ".privacyfence", "ipc_token");

/**
 * Read the current launch's IPC auth token. Called right before opening
 * each new connection (see ipcClient.ts's doConnect and manifest.ts's
 * fetchManifest) rather than cached at import time, since a daemon restart
 * writes a fresh token that a stale in-memory copy would fail against.
 */
export function readIpcToken(tokenFile = TOKEN_FILE): string {
  return fs.readFileSync(tokenFile, "utf8").trim();
}

/**
 * Read the current launch's discovered port, same freshness reasoning as
 * readIpcToken() above -- a relaunched daemon binds a new ephemeral port.
 */
export function readIpcPort(portFile = PORT_FILE): number {
  const text = fs.readFileSync(portFile, "utf8").trim();
  const port = Number(text);
  if (!Number.isInteger(port) || port <= 0) {
    throw new Error(`Invalid port in ${portFile}: ${JSON.stringify(text)}`);
  }
  return port;
}

// The package version (not a separate protocol number), used the same way
// ipc.py's VERSION is: reported in "manifest"/"health" so each side can
// detect drift (see manifest.ts's checkVersionMatch). build.mjs statically
// replaces `process.env.BRIDGE_VERSION` at bundle time with the real
// version read from pyproject.toml — this isn't hand-maintained in
// package.json (see CLAUDE.md's "Version bumps" section). Outside of a
// bundled build (e.g. running tests directly against src/ under tsx) this
// reads the literal environment variable instead, falling back to a dev
// placeholder.
export const VERSION = process.env.BRIDGE_VERSION ?? "0.0.0-dev";

// Messages are newline-delimited JSON. Node has no built-in per-line size
// cap the way asyncio's StreamReader does, but the read loop enforces this
// limit itself (see ipcClient.ts) so a malformed/oversized line fails the
// same way on both sides instead of growing an unbounded buffer.
export const LINE_LIMIT = 8 * 1024 * 1024;
