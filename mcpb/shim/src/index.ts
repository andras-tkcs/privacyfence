/**
 * PrivacyFence .mcpb shim: a thin stdio-to-Streamable-HTTP transport proxy,
 * spawned by Claude Desktop exactly the way it used to spawn
 * privacyfence-bridge (mcpb/manifest.json.tmpl's server.mcp_config -- same
 * shape, only the staged file changed, see scripts/build_mcpb.sh). D11 in
 * docs/https-connector-refactor-plan.md §12: what replaces the bridge for
 * Desktop once tool calls go over /mcp (P2) instead of the IPC socket.
 *
 * Unlike the bridge (bridge/src/index.ts), this process has no knowledge of
 * ToolSpec, no manifest fetch, no tool registration, and no JSON-RPC framing
 * of its own -- see proxy.ts's module docstring for why that's a deliberate
 * design constraint, not an oversight. Its whole job:
 *
 * 1. Wait for the daemon's /mcp endpoint to be reachable, launching
 *    privacyfence-app first if it isn't running yet (daemon.ts -- the one
 *    piece of the bridge whose job survives verbatim, per D11).
 * 2. Read the daemon's current /mcp URL and bearer token from the discovery
 *    files web/server.py and web/mcp_auth.py write (protocol.ts).
 * 3. Open a Streamable HTTP client connection to /mcp, authenticated by
 *    that bearer token.
 * 4. Proxy MCP frames between that connection and this process's own stdio
 *    transport (proxy.ts) -- Claude Desktop can now call tools, exactly as
 *    it could through the bridge.
 *
 * Logs go to stderr only (stdout is the MCP protocol channel) -- see
 * setupLogging(), same reasoning as bridge_main.py's original stderr-only
 * logging.StreamHandler setup.
 */

import { pathToFileURL } from "node:url";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { waitForDaemonPatiently } from "./daemon.js";
import { ShimExitError } from "./errors.js";
import { MCP_TOKEN_FILE, MCP_URL_FILE, readMcpToken, readMcpUrl } from "./protocol.js";
import { proxyTransports } from "./proxy.js";

/**
 * Redirect console.log/info/debug/warn to stderr. stdout is the MCP wire
 * channel (StdioServerTransport owns it); a stray console.log from this
 * code or a dependency would corrupt the protocol stream, so every logging
 * path is forced through stderr instead -- identical to bridge/src/
 * index.ts's setupLogging().
 */
function setupLogging(): void {
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;
  console.warn = console.error;
}

/** Validates flags; --config is daemon-side only, accepted here for CLI
 * compatibility with how the bridge was invoked. */
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
    throw new Error(`privacyfence-mcpb-shim: unrecognized argument: ${arg}`);
  }
}

export interface MainOptions {
  /** Overridable for tests; defaults to the real ~/.privacyfence/mcp_url. */
  mcpUrlFile?: string;
  /** Overridable for tests; defaults to the real ~/.privacyfence/mcp_token. */
  mcpTokenFile?: string;
  /** Overridable for tests (e.g. a fake Transport); defaults to a real
   * StreamableHTTPClientTransport pointed at the discovered mcp_url. */
  daemonTransport?: Transport;
  /** Overridable for tests (e.g. InMemoryTransport); defaults to real stdio. */
  transport?: Transport;
  /** Overridable for tests; defaults to waiting on stdin closing (real Claude Desktop disconnect). */
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

  const mcpUrlFile = opts.mcpUrlFile ?? MCP_URL_FILE;
  const mcpTokenFile = opts.mcpTokenFile ?? MCP_TOKEN_FILE;
  await waitForDaemonPatiently({ mcpUrlFile });

  const mcpUrl = readMcpUrl(mcpUrlFile);
  const mcpToken = readMcpToken(mcpTokenFile);
  console.error(`Proxying stdio <-> ${mcpUrl}`);

  const daemonSide =
    opts.daemonTransport ??
    new StreamableHTTPClientTransport(new URL(mcpUrl), {
      requestInit: { headers: { Authorization: `Bearer ${mcpToken}` } },
    });
  const desktopSide = opts.transport ?? new StdioServerTransport();

  // Must run before either side's start() -- see proxy.ts's own doc comment.
  proxyTransports(desktopSide, daemonSide);

  let closed = false;
  const closeBoth = async (): Promise<void> => {
    if (closed) return;
    closed = true;
    await Promise.allSettled([desktopSide.close(), daemonSide.close()]);
  };
  // Either side dropping (Claude Desktop closing stdin, or /mcp becoming
  // unreachable mid-session) tears down the other -- there is no partial
  // proxy state worth keeping alive.
  desktopSide.onclose = () => {
    void closeBoth();
  };
  daemonSide.onclose = () => {
    void closeBoth();
  };
  desktopSide.onerror = (err) => console.error("stdio side error:", err);
  daemonSide.onerror = (err) => console.error("/mcp side error:", err);

  await daemonSide.start();
  await desktopSide.start();

  const waitForDisconnect = opts.waitForDisconnect ?? defaultWaitForDisconnect;
  try {
    await waitForDisconnect();
  } finally {
    await closeBoth();
  }
}

// Only auto-run when this module is the actual entry point (the bundled
// dist/shim.js Claude Desktop spawns, or `node`/`tsx src/index.ts` in dev)
// -- not when index.test.ts imports main() directly to drive it in an
// in-process integration test.
const isEntryPoint = process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href;

if (isEntryPoint) {
  main().catch((exc: unknown) => {
    // ShimExitError carries its own fully-formatted, user-facing message
    // (see daemon.ts) -- print it plainly, no "Error:" prefix/stack trace,
    // matching the bridge's equivalent BridgeExitError handling.
    if (exc instanceof ShimExitError) {
      console.error(exc.message);
      process.exit(exc.code);
    }
    console.error(exc instanceof Error ? (exc.stack ?? exc.message) : String(exc));
    process.exit(1);
  });
}
