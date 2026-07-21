/**
 * Daemon auto-start + socket connectivity check. Ported from bridge_main.py's
 * _find_daemon_cmd / _socket_connectable / _ensure_daemon_running.
 */

import { spawn } from "node:child_process";
import fs, { constants as fsConstants } from "node:fs";
import net from "node:net";
import path from "node:path";
import { BridgeExitError } from "./errors.js";
import { SOCKET_PATH } from "./protocol.js";

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
  /** Defaults to process.argv[1] — the path bridge.js was invoked with. */
  scriptPath?: string;
  /** Defaults to process.env.PATH. */
  pathEnv?: string;
  /** Defaults to the real PrivacyFenceApp.app path; overridable for tests. */
  defaultAppPath?: string;
}

/**
 * Return the command to launch privacyfence-app.
 *
 * The bridge is built and distributed separately from the daemon — it is
 * never a sibling of privacyfence-app on disk — so this normally only
 * matters as a fallback: the daemon should already be running via its
 * LaunchAgent by the time Claude spawns us.
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

  // Development fallback: run the daemon as a Python module. Unlike the old
  // Python bridge (which reused sys.executable — its own interpreter, so it
  // was guaranteed to share the dev venv), this bridge is not a Python
  // process, so it relies on a `python3` already on PATH with privacyfence
  // installed (e.g. an activated venv).
  return ["python3", "-m", "privacyfence.daemon_main"];
}

/** Return true if the daemon socket is accepting connections right now. */
export function socketConnectable(socketPath = SOCKET_PATH): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = net.createConnection(socketPath);
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
  socketPath?: string;
  /** Overridable for tests; defaults to the real findDaemonCmd(). */
  findCmd?: () => string[];
  connectTimeoutMs?: number;
  connectIntervalMs?: number;
}

/** Connect to the daemon, launching it first if needed. Resolves once ready. */
export async function ensureDaemonRunning(opts: EnsureDaemonRunningOptions = {}): Promise<void> {
  const socketPath = opts.socketPath ?? SOCKET_PATH;
  const findCmd = opts.findCmd ?? findDaemonCmd;
  const connectTimeoutMs = opts.connectTimeoutMs ?? CONNECT_TIMEOUT_MS;
  const connectIntervalMs = opts.connectIntervalMs ?? CONNECT_INTERVAL_MS;

  if (await socketConnectable(socketPath)) {
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
    if (await socketConnectable(socketPath)) {
      console.error("Daemon is ready");
      return;
    }
    await sleep(connectIntervalMs);
  }

  throw new BridgeExitError(
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
 * Like ensureDaemonRunning, but never gives up: if the daemon hasn't come up
 * within the initial launch-and-wait window, keep polling the socket instead
 * of throwing.
 *
 * Covers a privacyfence-app cold start (GUI launch, licensing checks, etc.)
 * that takes longer than that window. Previously that raced against a hard
 * timeout and killed the whole bridge process — since the bridge is an
 * ephemeral MCP server Claude spawns once per session, that meant the user
 * had to restart their Claude conversation even though the app was still
 * mid-launch and would have come up fine seconds later.
 *
 * findDaemonCmd/spawn only happens once, inside the initial
 * ensureDaemonRunning call — every retry after that just re-checks the
 * socket, so a slow app start never launches a second instance.
 */
export async function waitForDaemonPatiently(opts: WaitForDaemonPatientlyOptions = {}): Promise<void> {
  const socketPath = opts.socketPath ?? SOCKET_PATH;
  const retryIntervalMs = opts.retryIntervalMs ?? PATIENT_RETRY_INTERVAL_MS;

  try {
    await ensureDaemonRunning(opts);
    return;
  } catch (exc) {
    if (!(exc instanceof BridgeExitError)) throw exc;
    console.error(`${exc.message}\nWill keep retrying instead of giving up.`);
  }

  for (;;) {
    await sleep(retryIntervalMs);
    if (await socketConnectable(socketPath)) {
      console.error("Daemon is ready");
      return;
    }
    console.error("Still waiting for the PrivacyFence daemon to come up...");
  }
}
