/**
 * Daemon auto-start + /mcp reachability check. Ported from bridge/src/
 * daemon.ts -- see that file's own module comment for the original
 * bridge_main.py provenance. The one thing that changes here is *what*
 * "connectable" means: the bridge discovered a bare TCP socket via
 * ipc_port; this discovers /mcp's host:port via the mcp_url file
 * (protocol.ts) written by web/server.py's WebServer.start(). The probe
 * itself stays a plain TCP connect, not an HTTP request -- the shim has no
 * HTTP/MCP protocol knowledge of its own before it hands off to
 * StreamableHTTPClientTransport in index.ts (D11,
 * docs/https-connector-refactor-plan.md §12).
 */

import { spawn } from "node:child_process";
import fs, { constants as fsConstants } from "node:fs";
import net from "node:net";
import path from "node:path";
import { ShimExitError } from "./errors.js";
import { MCP_URL_FILE } from "./protocol.js";

const CONNECT_TIMEOUT_MS = 10_000; // time to wait for daemon startup
const CONNECT_INTERVAL_MS = 400;
const PATIENT_RETRY_INTERVAL_MS = 2_000; // polling interval once the initial window has elapsed
const DEFAULT_APP_PATH = "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app";

function isExecutable(candidate: string): boolean {
  try {
    fs.accessSync(candidate, fsConstants.X_OK);
    return fs.statSync(candidate).isFile();
  } catch {
    return false;
  }
}

function which(name: string, pathEnv: string): string | null {
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, name);
    if (isExecutable(candidate)) return candidate;
  }
  return null;
}

export interface FindDaemonCmdOptions {
  /** Defaults to process.argv[1] — the path shim.js was invoked with. */
  scriptPath?: string;
  /** Defaults to process.env.PATH. */
  pathEnv?: string;
  /** Defaults to the real PrivacyFenceApp.app path; overridable for tests. */
  defaultAppPath?: string;
}

/**
 * Return the command to launch privacyfence-app. Identical reasoning to
 * bridge/src/daemon.ts's findDaemonCmd: the shim ships inside the .mcpb,
 * never as a sibling of privacyfence-app on disk, so this normally only
 * matters as a fallback -- the daemon should already be running via its
 * LaunchAgent by the time Claude Desktop spawns the shim.
 */
export function findDaemonCmd(opts: FindDaemonCmdOptions = {}): string[] {
  const scriptPath = opts.scriptPath ?? process.argv[1] ?? process.execPath;
  const pathEnv = opts.pathEnv ?? process.env.PATH ?? "";
  const defaultAppPath = opts.defaultAppPath ?? DEFAULT_APP_PATH;

  const here = path.dirname(path.resolve(scriptPath));
  const sibling = path.join(here, "privacyfence-app");
  if (isExecutable(sibling)) return [sibling];

  const found = which("privacyfence-app", pathEnv);
  if (found) return [found];

  if (isExecutable(defaultAppPath)) return [defaultAppPath];

  // Development fallback: run the daemon as a Python module. Relies on a
  // `python3` already on PATH with privacyfence installed (e.g. an
  // activated venv) -- see bridge/src/daemon.ts's identical fallback for
  // why this can't reuse an interpreter path the way the old Python bridge
  // did.
  return ["python3", "-m", "privacyfence.daemon_main"];
}

/**
 * Return true if the daemon's /mcp endpoint is reachable right now --
 * meaning the mcp_url discovery file exists and names a host:port something
 * is actually listening on. A file that hasn't been written yet (daemon not
 * started), doesn't parse as a URL, or still names an earlier launch's now-
 * dead port (daemon mid-restart) all read as false here, same as the
 * bridge's socketConnectable() did for ipc_port.
 */
export function socketConnectable(mcpUrlFile = MCP_URL_FILE): Promise<boolean> {
  return new Promise((resolve) => {
    let url: URL;
    try {
      const text = fs.readFileSync(mcpUrlFile, "utf8").trim();
      if (!text) {
        resolve(false);
        return;
      }
      url = new URL(text);
    } catch {
      resolve(false);
      return;
    }
    const port = url.port ? Number(url.port) : url.protocol === "https:" ? 443 : 80;
    const sock = net.createConnection({ host: url.hostname, port });
    const done = (ok: boolean) => {
      sock.removeAllListeners();
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(1000);
    sock.once("connect", () => done(true));
    sock.once("timeout", () => done(false));
    sock.once("error", () => done(false));
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface EnsureDaemonRunningOptions {
  mcpUrlFile?: string;
  /** Overridable for tests; defaults to the real findDaemonCmd(). */
  findCmd?: () => string[];
  connectTimeoutMs?: number;
  connectIntervalMs?: number;
}

/** Connect to the daemon's /mcp endpoint, launching it first if needed.
 * Resolves once reachable. */
export async function ensureDaemonRunning(opts: EnsureDaemonRunningOptions = {}): Promise<void> {
  const mcpUrlFile = opts.mcpUrlFile ?? MCP_URL_FILE;
  const findCmd = opts.findCmd ?? findDaemonCmd;
  const connectTimeoutMs = opts.connectTimeoutMs ?? CONNECT_TIMEOUT_MS;
  const connectIntervalMs = opts.connectIntervalMs ?? CONNECT_INTERVAL_MS;

  if (await socketConnectable(mcpUrlFile)) {
    console.error("Daemon already running");
    return;
  }

  console.error("Daemon not running — launching it now");
  const [cmd, ...args] = findCmd();
  if (!cmd) {
    throw new Error("findDaemonCmd() returned an empty command");
  }
  const child = spawn(cmd, args, {
    stdio: "ignore",
    detached: true, // detach from our process group
  });
  child.unref();

  const deadline = Date.now() + connectTimeoutMs;
  while (Date.now() < deadline) {
    if (await socketConnectable(mcpUrlFile)) {
      console.error("Daemon is ready");
      return;
    }
    await sleep(connectIntervalMs);
  }

  throw new ShimExitError(
    "ERROR: PrivacyFence daemon did not start within " +
      `${connectTimeoutMs / 1000} seconds.\n` +
      "Try running 'privacyfence-app' manually and check the logs.",
    1
  );
}

export interface WaitForDaemonPatientlyOptions extends EnsureDaemonRunningOptions {
  /** Polling interval used once the initial launch-and-wait window has elapsed. */
  retryIntervalMs?: number;
}

/**
 * Like ensureDaemonRunning, but never gives up -- same reasoning as bridge/
 * src/daemon.ts's waitForDaemonPatiently: a privacyfence-app cold start
 * (GUI launch, licensing checks, etc.) can outlast the initial window, and
 * since this process is an ephemeral MCP server Claude Desktop spawns once
 * per session, giving up early would force the user to restart their Claude
 * conversation instead of just waiting a few more seconds.
 *
 * findDaemonCmd/spawn only happens once, inside the initial
 * ensureDaemonRunning call — every retry after that just re-checks
 * reachability, so a slow app start never launches a second instance.
 */
export async function waitForDaemonPatiently(opts: WaitForDaemonPatientlyOptions = {}): Promise<void> {
  const mcpUrlFile = opts.mcpUrlFile ?? MCP_URL_FILE;
  const retryIntervalMs = opts.retryIntervalMs ?? PATIENT_RETRY_INTERVAL_MS;

  try {
    await ensureDaemonRunning(opts);
    return;
  } catch (exc) {
    if (!(exc instanceof ShimExitError)) throw exc;
    console.error(`${exc.message}\nWill keep retrying instead of giving up.`);
  }

  for (;;) {
    await sleep(retryIntervalMs);
    if (await socketConnectable(mcpUrlFile)) {
      console.error("Daemon is ready");
      return;
    }
    console.error("Still waiting for the PrivacyFence daemon to come up...");
  }
}
