/**
 * Manifest fetch + version check. Ported from bridge_main.py's
 * _fetch_manifest_sync and _check_version_match.
 */

import net from "node:net";
import { BridgeExitError } from "./errors.js";
import { SOCKET_PATH, VERSION } from "./protocol.js";

export interface ToolParamDict {
  name: string;
  annotation: string;
  required: boolean;
  default: unknown;
  description: string;
}

export interface ToolSpecDict {
  name: string;
  description: string;
  params: ToolParamDict[];
  read_only: boolean;
}

export interface ConnectorManifestEntry {
  name: string;
  tools: ToolSpecDict[];
}

export interface Manifest {
  version?: string;
  connectors: ConnectorManifestEntry[];
}

/**
 * Open a short-lived connection to fetch the connector manifest, mirroring
 * bridge_main.py's _fetch_manifest_sync — a one-shot request/response
 * outside of the persistent IPCClient connection opened later for tool
 * calls (see index.ts).
 */
export function fetchManifest(socketPath = SOCKET_PATH): Promise<Manifest> {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(socketPath);
    sock.setEncoding("utf8");
    sock.setTimeout(5000);
    let buffer = "";

    sock.once("connect", () => {
      sock.write(JSON.stringify({ id: "m0", method: "manifest", params: {} }) + "\n");
    });

    sock.on("data", (chunk: string) => {
      buffer += chunk;
      const newlineIndex = buffer.indexOf("\n");
      if (newlineIndex === -1) return;
      const line = buffer.slice(0, newlineIndex);
      sock.destroy();
      try {
        const msg = JSON.parse(line) as { result?: Manifest };
        if (msg.result === undefined) {
          reject(new Error("manifest response had no 'result' field"));
          return;
        }
        resolve(msg.result);
      } catch (exc) {
        reject(exc instanceof Error ? exc : new Error(String(exc)));
      }
    });

    sock.once("timeout", () => {
      sock.destroy();
      reject(new Error(`Timed out fetching manifest from ${socketPath}`));
    });
    sock.once("error", reject);
  });
}

/**
 * Refuse to proceed if the daemon is running a different PrivacyFence
 * version. The bridge (PrivacyFence.mcpb) and the daemon (PrivacyFenceApp.app)
 * are built and updated independently, so a stale daemon process (e.g. left
 * running across an app update) can silently drift from the bridge's wire
 * format expectations. Fail loudly instead of risking a confusing crash
 * deeper inside a tool call.
 */
export function checkVersionMatch(manifest: Manifest, ownVersion = VERSION): void {
  const daemonVersion = manifest.version;
  if (daemonVersion === undefined || daemonVersion === ownVersion) return;

  throw new BridgeExitError(
    "ERROR: PrivacyFence version mismatch — refusing to start.\n" +
      `  Claude extension (privacyfence-bridge): ${ownVersion}\n` +
      `  Running daemon (PrivacyFenceApp.app):    ${daemonVersion}\n` +
      "\n" +
      "This usually happens when PrivacyFenceApp.app was updated (or " +
      "reinstalled) but the previously running daemon process was never " +
      "restarted, or when the Claude extension was updated separately " +
      "from the app.\n" +
      "\n" +
      "To fix it:\n" +
      "  1. Quit PrivacyFence from its menu bar icon (or run: " +
      "pkill -f PrivacyFenceApp)\n" +
      "  2. Relaunch PrivacyFenceApp.app so it starts on the same version " +
      "as the extension\n" +
      "  3. Restart this conversation in Claude so it reconnects\n",
    1
  );
}
