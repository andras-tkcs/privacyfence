/**
 * PrivacyFence bridge: ephemeral stdio MCP server spawned by Claude.
 * (The shebang line in the bundled dist/bridge.js comes from build.mjs's
 * esbuild banner, not from this source file.)
 * Ported from src/privacyfence/bridge_main.py — see that file's module
 * docstring for the full startup sequence description, which this mirrors:
 *
 * 1. Try to connect to the daemon socket.
 * 2. If the daemon is not running, launch it (privacyfence-app) and wait up to 10 s.
 * 3. Fetch the connector manifest from the daemon.
 * 4. Register all connector tools with the MCP server dynamically.
 * 5. Run the MCP server on the stdio transport — Claude can now call tools.
 *
 * Each tool call is forwarded to the daemon over the persistent socket
 * connection. The bridge carries no state of its own; it is safe for Claude
 * to kill and restart it at any time.
 *
 * Logs go to stderr only (stdout is the MCP protocol channel) — see
 * setupLogging().
 */

import { pathToFileURL } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { ensureDaemonRunning } from "./daemon.js";
import { BridgeExitError } from "./errors.js";
import { IPCClient } from "./ipcClient.js";
import { checkVersionMatch, fetchManifest } from "./manifest.js";
import { SOCKET_PATH, VERSION } from "./protocol.js";
import { registerMetaTools, registerTools } from "./tools.js";

/**
 * Redirect console.log/info/debug/warn to stderr. stdout is the MCP wire
 * channel (StdioServerTransport owns it); a stray console.log from this
 * code or a dependency would corrupt the protocol stream, so every logging
 * path is forced through stderr instead, matching bridge_main.py's
 * stderr-only logging.StreamHandler setup.
 */
function setupLogging(): void {
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;
  console.warn = console.error;
}

/** Validates flags; --config is daemon-side only, accepted here for CLI compatibility. */
export function parseArgs(argv: string[]): void {
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--config") {
      i++; // consume the value
      continue;
    }
    if (arg?.startsWith("--config=")) {
      continue;
    }
    throw new Error(`privacyfence-bridge: unrecognized argument: ${arg}`);
  }
}

export interface MainOptions {
  /** Overridable for tests; defaults to the real ~/.privacyfence socket. */
  socketPath?: string;
  /** Overridable for tests (e.g. InMemoryTransport); defaults to real stdio. */
  transport?: Transport;
  /** Overridable for tests; defaults to waiting on stdin closing (real Claude disconnect). */
  waitForDisconnect?: () => Promise<void>;
}

function defaultWaitForDisconnect(): Promise<void> {
  return new Promise<void>((resolve) => {
    process.stdin.once("close", resolve);
    process.stdin.once("end", resolve);
  });
}

export async function main(argv = process.argv.slice(2), opts: MainOptions = {}): Promise<void> {
  setupLogging();
  parseArgs(argv);

  const socketPath = opts.socketPath ?? SOCKET_PATH;
  await ensureDaemonRunning({ socketPath });

  const manifest = await fetchManifest(socketPath);
  checkVersionMatch(manifest, VERSION);
  console.error(
    `Got manifest: connectors=${JSON.stringify((manifest.connectors ?? []).map((c) => c.name))}`
  );

  const server = new McpServer({ name: "privacyfence", version: VERSION });
  const ipc = new IPCClient(socketPath);
  registerTools(server, ipc, manifest);
  registerMetaTools(server, ipc);

  await ipc.connect();
  console.error("IPC client connected; starting stdio MCP");
  const transport = opts.transport ?? new StdioServerTransport();
  const waitForDisconnect = opts.waitForDisconnect ?? defaultWaitForDisconnect;
  try {
    await server.connect(transport);
    // Keep the process alive until the client disconnects.
    await waitForDisconnect();
  } finally {
    ipc.close();
  }
}

// Only auto-run when this module is the actual entry point (the bundled
// dist/bridge.js Claude Desktop spawns, or `node`/`tsx src/index.ts` in
// dev) — not when index.test.ts imports main() directly to drive it in an
// in-process integration test.
const isEntryPoint = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;

if (isEntryPoint) {
  main().catch((exc: unknown) => {
    // BridgeExitError carries its own fully-formatted, user-facing message
    // (see daemon.ts/manifest.ts) — print it plainly, no "Error:"
    // prefix/stack trace, matching bridge_main.py's sys.exit(1) call sites.
    if (exc instanceof BridgeExitError) {
      console.error(exc.message);
      process.exit(exc.code);
    }
    console.error(exc instanceof Error ? (exc.stack ?? exc.message) : String(exc));
    process.exit(1);
  });
}
