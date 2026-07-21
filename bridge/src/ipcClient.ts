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
 *
 * The daemon can restart or crash mid-conversation (e.g. PrivacyFenceApp.app
 * updating or being relaunched). Rather than every subsequent tool call
 * failing until the whole bridge process is restarted, request() transparently
 * reconnects on demand — see ensureConnected().
 */

import net from "node:net";
import { LINE_LIMIT } from "./protocol.js";

export class IPCError extends Error {}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (reason: IPCError) => void;
}

/**
 * Params for proposeRuleChange() — mirrors ipc.py's propose_rule_change
 * field list (see that module's docstring and gate.propose_rule_change()'s
 * own docstring for what each field means per target). camelCase here,
 * translated to the wire protocol's snake_case in IPCClient.proposeRuleChange.
 */
export interface ProposeRuleChangeParams {
  target: "rule" | "grant";
  operation: "add" | "update" | "remove";
  reason: string;
  operationKey?: string | undefined;
  ruleName?: string | undefined;
  value?: unknown;
  oldValue?: unknown;
  connector?: string | undefined;
  configKey?: string | undefined;
  resourceId?: string | undefined;
  name?: string | undefined;
  tab?: string | undefined;
  capabilities?: Record<string, boolean> | undefined;
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
  listRules(reason?: string): Promise<unknown>;
  proposeRuleChange(params: ProposeRuleChangeParams): Promise<unknown>;
  beginUnattendedSession(reason?: string): Promise<unknown>;
  endUnattendedSession(reason?: string): Promise<unknown>;
}

export class IPCClient implements IPCClientLike {
  private readonly path: string;
  private socket: net.Socket | null = null;
  private connecting: Promise<void> | null = null;
  private readonly pending = new Map<string, Pending>();
  private nextId = 0;
  private buffer = "";

  constructor(socketPath: string) {
    this.path = socketPath;
  }

  /** Open the connection. Must be called once before any request. */
  async connect(): Promise<void> {
    await this.ensureConnected();
  }

  /** True while a live socket is open to the daemon. */
  isConnected(): boolean {
    return this.socket !== null;
  }

  close(): void {
    this.socket?.destroy();
    this.socket = null;
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

  async listRules(reason = ""): Promise<unknown> {
    return this.request("list_rules", { reason });
  }

  async proposeRuleChange(params: ProposeRuleChangeParams): Promise<unknown> {
    return this.request("propose_rule_change", {
      target: params.target,
      operation: params.operation,
      reason: params.reason,
      operation_key: params.operationKey ?? "",
      rule_name: params.ruleName ?? "",
      value: params.value ?? null,
      old_value: params.oldValue ?? null,
      connector: params.connector ?? "",
      config_key: params.configKey ?? "",
      resource_id: params.resourceId ?? "",
      name: params.name ?? null,
      tab: params.tab ?? null,
      capabilities: params.capabilities ?? null,
    });
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

  private async request(method: string, params: Record<string, unknown>): Promise<unknown> {
    await this.ensureConnected();
    const id = String(this.nextId++);
    const promise = new Promise<unknown>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    const msg = JSON.stringify({ id, method, params }) + "\n";
    this.socket!.write(msg);
    return promise;
  }

  /**
   * (Re)establish the connection on demand. Concurrent callers share the
   * same in-flight attempt rather than each opening their own socket.
   */
  private async ensureConnected(): Promise<void> {
    if (this.socket) return;
    if (!this.connecting) {
      this.connecting = this.doConnect().finally(() => {
        this.connecting = null;
      });
    }
    await this.connecting;
  }

  private async doConnect(): Promise<void> {
    let sock: net.Socket;
    try {
      sock = await new Promise<net.Socket>((resolve, reject) => {
        const s = net.createConnection(this.path);
        s.once("connect", () => resolve(s));
        s.once("error", reject);
      });
    } catch (exc) {
      throw new IPCError(
        `Could not connect to PrivacyFence daemon: ${exc instanceof Error ? exc.message : String(exc)}`
      );
    }
    sock.setEncoding("utf8");
    this.buffer = "";
    sock.on("data", (chunk: string) => this.onData(chunk));
    sock.once("close", () => this.onClose());
    sock.on("error", () => {
      // Surfaced to in-flight requests via the "close" handler below; a bare
      // "error" listener just prevents Node from throwing unhandled.
    });
    this.socket = sock;
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
    this.socket = null;
    for (const { reject } of this.pending.values()) {
      reject(new IPCError("IPC connection closed"));
    }
    this.pending.clear();
  }
}
