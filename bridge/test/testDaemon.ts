/**
 * A scriptable fake daemon shared by the test suite: records every request
 * it receives and lets the test decide what (and when) to write back, so we
 * can test ordering, malformed lines, and disconnects precisely — the same
 * approach tests/unit/test_ipc_client.py's FakeDaemon takes on the Python
 * side, exercising real framing/routing instead of mocking the socket.
 *
 * Listens on a real TCP loopback port (127.0.0.1:0, OS-assigned) rather than
 * a Unix socket, mirroring the real daemon's own transport (see
 * src/privacyfence/ipc_server.py) -- including that the port is discovered
 * via a file (PORT_FILE) rather than fixed, since a hardcoded shared port
 * would collide across local accounts (see ipc.py's module docstring). The
 * real daemon also requires the first line on every connection to be an
 * auth token (see ipc.py's module docstring) before it processes anything
 * else; this fake doesn't validate that token's value (there's no real
 * secret to check here), it just consumes it as the first line of every
 * connection so real IPCClient/fetchManifest traffic against it looks
 * exactly like it would against the real daemon -- receivedTokens records
 * what each connection sent, for tests that want to assert on it.
 */

import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

export interface ReceivedRequest {
  id: string;
  method: string;
  params: unknown;
}

/** A short-lived temp directory holding a fake ipc_token/ipc_port pair, plus
 * cleanup. writePort() lets a test fill in the port file once it knows the
 * real bound port (e.g. after FakeDaemon.start() resolves) -- mirroring how
 * the real daemon only writes PORT_FILE once it's actually listening. */
export function makeTempIpcFiles(token = "test-token"): {
  tokenFile: string;
  portFile: string;
  token: string;
  writePort: (port: number) => void;
  cleanup: () => void;
} {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-"));
  const tokenFile = path.join(dir, "ipc_token");
  const portFile = path.join(dir, "ipc_port");
  fs.writeFileSync(tokenFile, token);
  return {
    tokenFile,
    portFile,
    token,
    writePort: (port: number) => fs.writeFileSync(portFile, String(port)),
    cleanup: () => {
      for (const f of [tokenFile, portFile]) {
        try {
          fs.unlinkSync(f);
        } catch {
          // already gone
        }
      }
      try {
        fs.rmdirSync(dir);
      } catch {
        // not empty / already gone
      }
    },
  };
}

/** Bind an ephemeral TCP port on 127.0.0.1, immediately free it, and return
 * the number -- a well-established "probably still free" trick for tests
 * that need a real port number before anything is listening on it (e.g. to
 * assert connectability is false, or to have a "late" listener bind to the
 * exact same port a bit later). */
export async function getFreePort(): Promise<number> {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      const port = typeof address === "object" && address ? address.port : 0;
      srv.close(() => resolve(port));
    });
  });
}

export class FakeDaemon {
  received: ReceivedRequest[] = [];
  receivedTokens: string[] = [];
  port = 0;
  private server: net.Server | null = null;
  private conn: net.Socket | null = null;
  private buffer = "";
  private authSeen = false;
  private connectedResolvers: Array<() => void> = [];

  /** Start listening. Pass a specific port to bind that exact one (e.g. to
   * simulate a slow-starting daemon reusing a port a test already probed);
   * omit it (or pass 0) for an OS-assigned ephemeral port, read back via
   * the returned/``.port`` value. */
  async start(port = 0): Promise<number> {
    await new Promise<void>((resolve) => {
      this.server = net.createServer((sock) => {
        this.conn = sock;
        this.buffer = "";
        this.authSeen = false;
        sock.setEncoding("utf8");
        sock.on("data", (chunk: string) => {
          this.buffer += chunk;
          let idx: number;
          while ((idx = this.buffer.indexOf("\n")) !== -1) {
            const line = this.buffer.slice(0, idx);
            this.buffer = this.buffer.slice(idx + 1);
            if (line.length === 0) continue;
            if (!this.authSeen) {
              this.authSeen = true;
              this.receivedTokens.push(line);
              continue;
            }
            this.received.push(JSON.parse(line));
          }
        });
        for (const resolve of this.connectedResolvers.splice(0)) resolve();
      });
      this.server.listen(port, "127.0.0.1", () => resolve());
    });
    const address = this.server!.address();
    this.port = typeof address === "object" && address ? address.port : 0;
    return this.port;
  }

  async waitForConnection(timeoutMs = 2000): Promise<void> {
    if (this.conn) return;
    await Promise.race([
      new Promise<void>((resolve) => this.connectedResolvers.push(resolve)),
      timeoutPromise(timeoutMs, "waitForConnection"),
    ]);
  }

  async waitForNRequests(n: number, timeoutMs = 2000): Promise<void> {
    const start = Date.now();
    while (this.received.length < n) {
      if (Date.now() - start > timeoutMs) {
        throw new Error(`timed out waiting for ${n} request(s); got ${this.received.length}`);
      }
      await sleep(5);
    }
  }

  sendRaw(raw: string): void {
    if (!this.conn) throw new Error("no connection yet");
    this.conn.write(raw);
  }

  sendResponse(id: string, opts: { result?: unknown; error?: string }): void {
    const msg: Record<string, unknown> = { id };
    if (opts.error !== undefined) msg.error = opts.error;
    else msg.result = opts.result;
    this.sendRaw(JSON.stringify(msg) + "\n");
  }

  disconnect(): void {
    this.conn?.end();
  }

  async stop(): Promise<void> {
    this.conn?.destroy();
    await new Promise<void>((resolve) => {
      if (!this.server) return resolve();
      this.server.close(() => resolve());
    });
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function timeoutPromise(ms: number, label: string): Promise<never> {
  return new Promise((_, reject) => setTimeout(() => reject(new Error(`timeout: ${label}`)), ms));
}
