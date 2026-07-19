/**
 * IPC protocol constants, ported from src/privacyfence/ipc.py.
 *
 * That file's module docstring is the single source of truth for the wire
 * protocol (newline-delimited JSON over a Unix domain socket) — these are
 * just this side's copies of its two constants. protocol.test.ts asserts
 * these literals against ipc.py's source so the two can't silently drift.
 */

import os from "node:os";
import path from "node:path";

export const SOCKET_PATH = path.join(os.homedir(), ".privacyfence", "privacyfence.sock");

// The package version (not a separate protocol number), used the same way
// ipc.py's VERSION is: reported in "manifest"/"health" so each side can
// detect drift (see manifest.ts's checkVersionMatch). build.mjs statically
// replaces `process.env.BRIDGE_VERSION` at bundle time with the real
// version read from pyproject.toml — see docs/mcp-bridge-nodejs-migration.md
// §9 on why this isn't hand-maintained in package.json. Outside of a bundled
// build (e.g. running tests directly against src/ under tsx) this reads the
// literal environment variable instead, falling back to a dev placeholder.
export const VERSION = process.env.BRIDGE_VERSION ?? "0.0.0-dev";

// Messages are newline-delimited JSON. Node has no built-in per-line size
// cap the way asyncio's StreamReader does, but the read loop enforces this
// limit itself (see ipcClient.ts) so a malformed/oversized line fails the
// same way on both sides instead of growing an unbounded buffer.
export const LINE_LIMIT = 8 * 1024 * 1024;
