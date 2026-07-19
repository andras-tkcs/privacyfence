import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { main, parseArgs } from "../src/index.js";
import { VERSION } from "../src/protocol.js";
import { FakeDaemon, makeShortSocketPath } from "./testDaemon.js";

describe("parseArgs", () => {
  it("accepts no arguments", () => {
    assert.doesNotThrow(() => parseArgs([]));
  });

  it("accepts --config <path> (daemon-side only, ignored here)", () => {
    assert.doesNotThrow(() => parseArgs(["--config", "/tmp/x.yaml"]));
  });

  it("accepts --config=<path>", () => {
    assert.doesNotThrow(() => parseArgs(["--config=/tmp/x.yaml"]));
  });

  it("rejects an unrecognized flag", () => {
    assert.throws(() => parseArgs(["--bogus"]), /unrecognized argument/);
  });
});

describe("main() end-to-end orchestration", () => {
  it("connects to the daemon, registers tools, and serves real MCP tool calls", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    const daemon = new FakeDaemon();
    await daemon.start(socketPath);

    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const waitForDisconnect = () => new Promise<void>((resolve) => (resolveDisconnect = resolve));

    const mainPromise = main([], { socketPath, transport: serverTransport, waitForDisconnect });

    // 1. bridge_main's manifest fetch (a short-lived connection, request #1).
    await daemon.waitForNRequests(1);
    assert.equal(daemon.received[0]?.method, "manifest");
    daemon.sendResponse(daemon.received[0]!.id, {
      result: {
        version: VERSION,
        connectors: [
          {
            name: "gmail",
            tools: [
              {
                name: "gmail_get_message",
                description: "Get a message",
                params: [{ name: "message_id", annotation: "str", required: true, default: null, description: "" }],
                read_only: true,
              },
            ],
          },
        ],
      },
    });

    // Now main() proceeds: registers tools/meta-tools, opens the persistent
    // IPCClient connection, and connects the MCP server to our in-memory
    // transport — at which point a real client can drive it.
    const client = new Client({ name: "index-test-client", version: "1.0.0" });
    await client.connect(clientTransport);

    const { tools } = await client.listTools();
    assert.deepEqual(
      tools.map((t) => t.name).sort(),
      [
        "gmail_get_message",
        "privacyfence_begin_unattended_session",
        "privacyfence_check_policy",
        "privacyfence_end_unattended_session",
      ]
    );

    const callPromise = client.callTool({ name: "gmail_get_message", arguments: { message_id: "m1" } });
    // 2. The tool call forwarded over IPCClient's persistent connection.
    await daemon.waitForNRequests(2);
    assert.equal(daemon.received[1]?.method, "call");
    assert.deepEqual(daemon.received[1]?.params, {
      connector: "gmail",
      tool: "gmail_get_message",
      args: { message_id: "m1" },
    });
    daemon.sendResponse(daemon.received[1]!.id, { result: { subject: "hello" } });

    const result = await callPromise;
    assert.equal(result.isError, undefined);
    assert.deepEqual(result.structuredContent, { subject: "hello" });

    await client.close();
    resolveDisconnect();
    await mainPromise;

    await daemon.stop();
    cleanup();
  });

  it("refuses to start on a daemon version mismatch", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    const daemon = new FakeDaemon();
    await daemon.start(socketPath);

    const [, serverTransport] = InMemoryTransport.createLinkedPair();
    const mainPromise = main([], { socketPath, transport: serverTransport });

    await daemon.waitForNRequests(1);
    daemon.sendResponse(daemon.received[0]!.id, {
      result: { version: "0.0.1-stale", connectors: [] },
    });

    await assert.rejects(mainPromise, (err: unknown) => {
      assert.ok(err instanceof Error);
      assert.match(err.message, /version mismatch/);
      assert.match(err.message, /0\.0\.1-stale/);
      return true;
    });

    await daemon.stop();
    cleanup();
  });
});
