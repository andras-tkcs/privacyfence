import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { proxyTransports } from "../src/proxy.js";

/** A minimal hand-rolled Transport double -- proxy.ts's only real dependency
 * is the shared Transport shape (send/onmessage/onerror), so a fake this
 * small is enough to prove the wiring without any real I/O. */
class FakeTransport implements Transport {
  sent: JSONRPCMessage[] = [];
  onmessage?: (message: JSONRPCMessage) => void;
  onerror?: (error: Error) => void;
  onclose?: () => void;
  sendShouldReject = false;

  async start(): Promise<void> {}

  async send(message: JSONRPCMessage): Promise<void> {
    if (this.sendShouldReject) {
      throw new Error("send failed");
    }
    this.sent.push(message);
  }

  async close(): Promise<void> {
    this.onclose?.();
  }

  receive(message: JSONRPCMessage): void {
    this.onmessage?.(message);
  }
}

const REQUEST: JSONRPCMessage = { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} };
const RESPONSE: JSONRPCMessage = { jsonrpc: "2.0", id: 1, result: { tools: [] } };

describe("proxyTransports", () => {
  it("forwards a message from the desktop side to the daemon side", () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);

    desktop.receive(REQUEST);

    assert.deepEqual(daemon.sent, [REQUEST]);
    assert.deepEqual(desktop.sent, []);
  });

  it("forwards a message from the daemon side to the desktop side", () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);

    daemon.receive(RESPONSE);

    assert.deepEqual(desktop.sent, [RESPONSE]);
    assert.deepEqual(daemon.sent, []);
  });

  it("does not cross-wire a side to itself", () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    proxyTransports(desktop, daemon);

    desktop.receive(REQUEST);
    daemon.receive(RESPONSE);

    assert.deepEqual(daemon.sent, [REQUEST]);
    assert.deepEqual(desktop.sent, [RESPONSE]);
  });

  it("routes a forwarding failure to the source side's onerror, not a thrown/unhandled rejection", async () => {
    const desktop = new FakeTransport();
    const daemon = new FakeTransport();
    daemon.sendShouldReject = true;
    proxyTransports(desktop, daemon);

    let captured: Error | undefined;
    desktop.onerror = (err) => {
      captured = err;
    };

    desktop.receive(REQUEST);
    // send() rejects asynchronously; let the microtask queue drain.
    await Promise.resolve();
    await Promise.resolve();

    assert.ok(captured);
    assert.match(captured!.message, /send failed/);
  });
});
