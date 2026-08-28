import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { main, parseArgs } from "../src/index.js";
import { FakeMcpDaemon } from "./fakeMcpDaemon.js";
import { makeTempMcpFiles } from "./testFiles.js";

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
  it("proxies a real MCP session between stdio and a real /mcp endpoint, bearer header attached", async () => {
    const { mcpUrlFile, mcpTokenFile, writeUrl, token, cleanup } = makeTempMcpFiles();
    const daemon = new FakeMcpDaemon(token);
    const url = await daemon.start();
    writeUrl(url);

    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let resolveDisconnect: () => void = () => {};
    const waitForDisconnect = () => new Promise<void>((resolve) => (resolveDisconnect = resolve));

    const mainPromise = main([], {
      mcpUrlFile,
      mcpTokenFile,
      transport: serverTransport,
      waitForDisconnect,
    });

    // A real Client, standing in for Claude Desktop, drives the shim over
    // the in-memory stdio replacement -- everything past this point crosses
    // the shim's real proxy and a real Streamable HTTP round trip to
    // FakeMcpDaemon.
    const client = new Client({ name: "index-test-client", version: "1.0.0" });
    await client.connect(clientTransport);

    const { tools } = await client.listTools();
    assert.deepEqual(
      tools.map((t) => t.name),
      ["shim_test_echo"]
    );

    const result = await client.callTool({ name: "shim_test_echo", arguments: {} });
    assert.equal(result.isError, undefined);
    assert.deepEqual(result.structuredContent, { echoed: true });

    // §10.3's audience separation, restated on the shim's own side: every
    // request that reached the fake daemon carried exactly the token from
    // mcp_token, never anything else (no unauthenticated request slipped
    // through, e.g. from a stray SSE probe).
    assert.ok(daemon.receivedAuthHeaders.length > 0);
    for (const header of daemon.receivedAuthHeaders) {
      assert.equal(header, `Bearer ${token}`);
    }

    await client.close();
    resolveDisconnect();
    await mainPromise;

    await daemon.stop();
    cleanup();
  });

  // The "mcp_url isn't there yet, launch the daemon" path (findDaemonCmd +
  // spawn) is covered by daemon.test.ts's ensureDaemonRunning/
  // waitForDaemonPatiently suites directly, with a fake findCmd injected --
  // main() has no seam to inject one (matching bridge/src/index.ts, which
  // has the same gap for the same reason: real production startup should
  // never be racing a fake findCmd), so exercising the launch path here
  // would spawn a real process instead of a fake one.
});
