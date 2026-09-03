/**
 * Discovery-file constants, ported from what web/server.py and
 * web/mcp_auth.py write on the daemon side (docs/https-connector-refactor-
 * plan.md §12's "Gap found while implementing P2" / D11):
 *
 * - ~/.privacyfence/mcp_url   -- written by WebServer.start() once the
 *   embedded HTTP server is actually bound, cleared on stop(). The direct
 *   successor of ipc.py's PORT_FILE (see bridge/src/protocol.ts) for a
 *   client that talks to /mcp instead of the old IPC socket.
 * - ~/.privacyfence/mcp_token -- the bearer secret for /mcp
 *   (web/mcp_auth.py's load_or_create_mcp_token()), deliberately a
 *   *different* file/secret than ipc_token or web_token (§10.3's audience
 *   separation) -- see that module's own docstring.
 *
 * Both are read fresh on every launch (this process is spawned once per
 * Claude Desktop session and exits when it ends -- see index.ts), not
 * cached beyond that, since a relaunched daemon can bind a different port
 * and rotate neither of these unless the files themselves are deleted.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const MCP_URL_FILE = path.join(os.homedir(), ".privacyfence", "mcp_url");
export const MCP_TOKEN_FILE = path.join(os.homedir(), ".privacyfence", "mcp_token");

/** Reads and validates the daemon's current /mcp URL. Throws if the file is
 * missing, empty, or doesn't parse as an absolute URL -- callers only reach
 * this after daemon.ts's socketConnectable() has already confirmed the file
 * names a reachable host:port, so a throw here means the file's *contents*
 * are malformed, not merely that the daemon hasn't started yet. */
export function readMcpUrl(mcpUrlFile = MCP_URL_FILE): string {
  const text = fs.readFileSync(mcpUrlFile, "utf8").trim();
  if (!text) {
    throw new Error(`Empty MCP URL in ${mcpUrlFile}`);
  }
  new URL(text); // throws SyntaxError / TypeError if not a valid absolute URL
  return text;
}

export function readMcpToken(mcpTokenFile = MCP_TOKEN_FILE): string {
  return fs.readFileSync(mcpTokenFile, "utf8").trim();
}
