/**
 * A scriptable fake daemon shared by the test suite: records every request
 * it receives and lets the test decide what (and when) to write back, so we
 * can test ordering, malformed lines, and disconnects precisely — the same
 * approach tests/unit/test_ipc_client.py's FakeDaemon takes on the Python
 * side, exercising real framing/routing instead of mocking the socket.
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

export function makeShortSocketPath(): { socketPath: string; cleanup: () => void } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-"));
  const socketPath = path.join(dir, "s.sock");
  return {
    socketPath,
    cleanup: () => {
      try {
        fs.unlinkSync(socketPath);
      } catch {
        // already gone
      }
      try {
        fs.rmdirSync(dir);
      } catch {
        // not empty / already gone
      }
    },
  };
}

export class FakeDaemon {
  received: ReceivedRequest[] = [];
  private server: net.Server | null = null;
  private conn: net.Socket | null = null;
  private buffer = "";
  private connectedResolvers: Array<() => void> = [];

  async start(socketPath: string): Promise<void> {
    await new Promise<void>((resolve) => {
      this.server = net.createServer((sock) => {
        this.conn = sock;
        sock.setEncoding("utf8");
        sock.on("data", (chunk: string) => {
          this.buffer += chunk;
          let idx: number;
          while ((idx = this.buffer.indexOf("\n")) !== -1) {
            const line = this.buffer.slice(0, idx);
            this.buffer = this.buffer.slice(idx + 1);
            if (line.length === 0) continue;
            this.received.push(JSON.parse(line));
          }
        });
        for (const resolve of this.connectedResolvers.splice(0)) resolve();
      });
      this.server.listen(socketPath, () => resolve());
    });
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
