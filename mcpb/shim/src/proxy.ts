/**
 * The shim's actual job: a pure message pump between two MCP ``Transport``
 * objects, nothing else. Whatever ``desktopSide`` (Claude Desktop's stdio
 * connection to this process) receives is forwarded to ``daemonSide`` (the
 * outbound Streamable HTTP connection to /mcp), and vice versa.
 *
 * Deliberately *not* built on the SDK's ``Client``/``Server`` classes: those
 * re-run the initialize handshake and cache tool schemas on this process,
 * which is exactly the "protocol/manifest/tool-schema knowledge" D11
 * (docs/https-connector-refactor-plan.md §12) says the shim must not carry
 * -- it is what keeps the class of bug bridge/test/manifest.test.ts and
 * tests/integration/test_bridge_daemon_contract.py exist to catch (one
 * side's wire format drifting from the other's) structurally impossible for
 * this transport, rather than something the shim also has to get right.
 * Both ``Transport`` implementations already do their own JSON-RPC framing
 * (line-delimited stdio / Streamable HTTP's SSE-or-JSON) -- this only moves
 * the decoded ``JSONRPCMessage`` values between them.
 *
 * Typed against ``MessageTransport`` -- a minimal local shape covering only
 * ``send``/``onmessage``/``onerror`` -- rather than importing the SDK's own
 * ``Transport`` interface directly: every concrete transport class already
 * satisfies it structurally, and stating only the fields this file actually
 * touches keeps the proxy's real dependency surface honest (matching how
 * little of "a transport" it needs to know about to do its job).
 */
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";

export interface MessageTransport {
  send(message: JSONRPCMessage): Promise<void>;
  onmessage?: (message: JSONRPCMessage) => void;
  onerror?: (error: Error) => void;
}

function toError(exc: unknown): Error {
  return exc instanceof Error ? exc : new Error(String(exc));
}

/** Installs ``onmessage`` on both sides. Must be called before either side's
 * ``start()`` -- see the ``Transport`` interface's own doc comment: "This
 * method should only be called after callbacks are installed, or else
 * messages may be lost." */
export function proxyTransports(desktopSide: MessageTransport, daemonSide: MessageTransport): void {
  desktopSide.onmessage = (message) => {
    daemonSide.send(message).catch((exc: unknown) => desktopSide.onerror?.(toError(exc)));
  };
  daemonSide.onmessage = (message) => {
    desktopSide.send(message).catch((exc: unknown) => daemonSide.onerror?.(toError(exc)));
  };
}
