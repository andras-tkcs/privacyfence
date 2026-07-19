import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { IPCClient, IPCError } from "../src/ipcClient.js";
import { FakeDaemon, makeShortSocketPath } from "./testDaemon.js";

async function setup() {
  const { socketPath, cleanup } = makeShortSocketPath();
  const daemon = new FakeDaemon();
  await daemon.start(socketPath);
  const client = new IPCClient(socketPath);
  await client.connect();
  await daemon.waitForConnection();
  return {
    daemon,
    client,
    async teardown() {
      client.close();
      await daemon.stop();
      cleanup();
    },
  };
}

describe("IPCClient request framing", () => {
  it("call() sends newline-delimited JSON with method and params", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const callPromise = client.call("gmail", "gmail_get_message", { message_id: "abc" });
      await daemon.waitForNRequests(1);

      assert.equal(daemon.received[0]?.method, "call");
      assert.deepEqual(daemon.received[0]?.params, {
        connector: "gmail",
        tool: "gmail_get_message",
        args: { message_id: "abc" },
      });
      assert.ok(daemon.received[0]?.id !== undefined);

      daemon.sendResponse(daemon.received[0]!.id, { result: { ok: true } });
      assert.deepEqual(await callPromise, { ok: true });
    } finally {
      await teardown();
    }
  });

  it("manifest() sends the method with empty params", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const task = client.manifest();
      await daemon.waitForNRequests(1);
      assert.equal(daemon.received[0]?.method, "manifest");
      assert.deepEqual(daemon.received[0]?.params, {});
      daemon.sendResponse(daemon.received[0]!.id, { result: { version: "0.4.10", connectors: [] } });
      const result = (await task) as { version: string };
      assert.equal(result.version, "0.4.10");
    } finally {
      await teardown();
    }
  });

  it("request ids are unique and increment", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const t1 = client.call("gmail", "x", {});
      await daemon.waitForNRequests(1);
      const t2 = client.call("gmail", "x", {});
      await daemon.waitForNRequests(2);

      const ids = daemon.received.map((r) => r.id);
      assert.equal(new Set(ids).size, 2);

      for (const r of daemon.received) daemon.sendResponse(r.id, { result: "done" });
      await t1;
      await t2;
    } finally {
      await teardown();
    }
  });
});

describe("IPCClient response routing", () => {
  it("an error response rejects with IPCError carrying the message", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const task = client.call("gmail", "x", {});
      await daemon.waitForNRequests(1);
      daemon.sendResponse(daemon.received[0]!.id, { error: "Unknown connector: 'gmail'" });

      await assert.rejects(task, (err: unknown) => {
        assert.ok(err instanceof IPCError);
        assert.match(err.message, /Unknown connector/);
        return true;
      });
    } finally {
      await teardown();
    }
  });

  it("out-of-order responses route to the correct caller", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const t1 = client.call("a", "x", {});
      const t2 = client.call("b", "x", {});
      await daemon.waitForNRequests(2);

      const [id1, id2] = [daemon.received[0]!.id, daemon.received[1]!.id];
      daemon.sendResponse(id2, { result: "second-result" });
      daemon.sendResponse(id1, { result: "first-result" });

      assert.equal(await t1, "first-result");
      assert.equal(await t2, "second-result");
    } finally {
      await teardown();
    }
  });

  it("a malformed response line is ignored, not fatal", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const task = client.call("gmail", "x", {});
      await daemon.waitForNRequests(1);

      daemon.sendRaw("{not valid json\n");
      daemon.sendResponse(daemon.received[0]!.id, { result: "ok" });
      assert.equal(await task, "ok");
    } finally {
      await teardown();
    }
  });

  it("a response with an unknown id is ignored", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const task = client.call("gmail", "x", {});
      await daemon.waitForNRequests(1);

      daemon.sendResponse("some-other-id-nobody-is-waiting-on", { result: "orphan" });
      daemon.sendResponse(daemon.received[0]!.id, { result: "ok" });
      assert.equal(await task, "ok");
    } finally {
      await teardown();
    }
  });
});

describe("IPCClient line limit", () => {
  it("a response larger than 64KiB is read intact", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const bigPayload = "x".repeat(200 * 1024);
      const task = client.call("drive", "x", {});
      await daemon.waitForNRequests(1);
      daemon.sendResponse(daemon.received[0]!.id, { result: { content: bigPayload } });
      const result = (await task) as { content: string };
      assert.equal(result.content, bigPayload);
    } finally {
      await teardown();
    }
  });
});

describe("IPCClient disconnect handling", () => {
  it("daemon disconnect fails all pending calls", async () => {
    const { daemon, client, teardown } = await setup();
    try {
      const t1 = client.call("a", "x", {});
      const t2 = client.call("b", "x", {});
      await daemon.waitForNRequests(2);

      daemon.disconnect();

      await assert.rejects(t1, /IPC connection closed/);
      await assert.rejects(t2, /IPC connection closed/);
    } finally {
      await teardown();
    }
  });

  it("a request made before connect() rejects instead of hanging", async () => {
    const client = new IPCClient("/nonexistent/does-not-matter.sock");
    await assert.rejects(client.call("a", "x", {}), IPCError);
  });
});
