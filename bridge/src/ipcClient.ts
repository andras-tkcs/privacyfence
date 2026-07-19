/**
 * IPC client used by the bridge to talk to the daemon. Ported from
 * src/privacyfence/ipc_client.py — see that file and ipc.py's docstring for
 * the wire protocol this implements.
 *
 * Maintains a single persistent connection with request multiplexing:
 * multiple tool calls may be in flight simultaneously (Claude can call tools
 * concurrently). Each request gets a unique id; the read loop matches
 * responses back to their waiting promise. Node's single-threaded event loop
 * means writes from the same synchronous turn are already ordered, so unlike
 * the Python client there is no need for an explicit write lock.
 */

import net from "node:net";
import { LINE_LIMIT } from "./protocol.js";

export class IPCError extends Error {}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (reason: IPCError) => void;
}

/**
 * The subset of IPCClient that tool handlers (tools.ts) depend on. Declared
 * separately so tests can substitute a fake IPC client (a plain object) for
 * registerTools/registerMetaTools without needing a real Unix socket
 * connection — the equivalent of the Python tests' MagicMock(), which works
 * there for free since Python doesn't enforce nominal typing at runtime.
 */
export interface IPCClientLike {
  call(connector: string, tool: string, args: Record<string, unknown>): Promise<unknown>;
  checkPolicy(
    connector: string,
    tool: string,
    args: Record<string, unknown>,
    reason?: string
  ): Promise<unknown>;
  beginUnattendedSession(reason?: string): Promise<unknown>;
  endUnattendedSession(reason?: string): Promise<unknown>;
}

export class IPCClient implements IPCClientLike {
  private readonly path: string;
  private socket: net.Socket | null = null;
  private readonly pending = new Map<string, Pending>();
  private nextId = 0;
  private buffer = "";

  constructor(socketPath: string) {
    this.path = socketPath;
  }

  /** Open the connection. Must be called once before any request. */
  async connect(): Promise<void> {
    this.socket = await new Promise<net.Socket>((resolve, reject) => {
      const sock = net.createConnection(this.path);
      sock.once("connect", () => resolve(sock));
      sock.once("error", reject);
    });
    this.socket.setEncoding("utf8");
    this.socket.on("data", (chunk: string) => this.onData(chunk));
    this.socket.on("close", () => this.onClose());
    this.socket.on("error", () => {
      // Surfaced to in-flight requests via the "close" handler below; a bare
      // "error" listener just prevents Node from throwing unhandled.
    });
  }

  close(): void {
    this.socket?.destroy();
  }

  async manifest(): Promise<unknown> {
    return this.request("manifest", {});
  }

  async call(connector: string, tool: string, args: Record<string, unknown>): Promise<unknown> {
    return this.request("call", { connector, tool, args });
  }

  async checkPolicy(
    connector: string,
    tool: string,
    args: Record<string, unknown>,
    reason = ""
  ): Promise<unknown> {
    return this.request("check_policy", { connector, tool, args, reason });
  }

  async beginUnattendedSession(reason = ""): Promise<unknown> {
    return this.request("begin_unattended_session", { reason });
  }

  async endUnattendedSession(reason = ""): Promise<unknown> {
    return this.request("end_unattended_session", { reason });
  }

  // -------------------------------------------------------------------- //
  // Internals
  // -------------------------------------------------------------------- //

  private request(method: string, params: Record<string, unknown>): Promise<unknown> {
    if (!this.socket) {
      return Promise.reject(new IPCError("IPC client not connected"));
    }
    const id = String(this.nextId++);
    const promise = new Promise<unknown>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    const msg = JSON.stringify({ id, method, params }) + "\n";
    this.socket.write(msg);
    return promise;
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    if (this.buffer.length > LINE_LIMIT) {
      // Guard against an unbounded buffer if a line never terminates —
      // asyncio's StreamReader enforces this as a hard per-line limit; we
      // enforce it as a hard total-buffer limit, which is equivalent as
      // long as complete lines are drained below as soon as they arrive.
      // Torn down like any other fatal connection error: destroying the
      // socket fires "close", which rejects every pending request below.
      console.error(`IPC: line exceeded LINE_LIMIT (${LINE_LIMIT} bytes); closing connection`);
      this.socket?.destroy();
      return;
    }
    let newlineIndex: number;
    while ((newlineIndex = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newlineIndex);
      this.buffer = this.buffer.slice(newlineIndex + 1);
      this.handleLine(line);
    }
  }

  private handleLine(line: string): void {
    if (line.length === 0) return;
    let msg: { id?: string; result?: unknown; error?: string };
    try {
      msg = JSON.parse(line);
    } catch {
      // Malformed response line — same as the Python client's
      // json.JSONDecodeError handling: log and skip rather than crash the
      // whole connection over one bad line.
      console.error("IPC: malformed response line");
      return;
    }
    const reqId = msg.id;
    if (reqId === undefined) return;
    const pending = this.pending.get(reqId);
    if (!pending) return;
    this.pending.delete(reqId);
    if (msg.error !== undefined) {
      pending.reject(new IPCError(msg.error));
    } else {
      pending.resolve(msg.result);
    }
  }

  private onClose(): void {
    for (const { reject } of this.pending.values()) {
      reject(new IPCError("IPC connection closed"));
    }
    this.pending.clear();
  }
}
