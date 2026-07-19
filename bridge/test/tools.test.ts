import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { IPCClientLike } from "../src/ipcClient.js";
import { IPCError } from "../src/ipcClient.js";
import type { ToolSpecDict } from "../src/manifest.js";
import { registerMetaTools, registerTools } from "../src/tools.js";

function spec(over: Partial<ToolSpecDict>): ToolSpecDict {
  return { name: "x", description: "d", params: [], read_only: false, ...over };
}

class FakeIPC implements IPCClientLike {
  calls: Array<{ method: string; args: unknown[] }> = [];
  callResult: unknown = { ok: true };
  callError: IPCError | null = null;

  async call(connector: string, tool: string, args: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ method: "call", args: [connector, tool, args] });
    if (this.callError) throw this.callError;
    return this.callResult;
  }
  async checkPolicy(
    connector: string,
    tool: string,
    args: Record<string, unknown>,
    reason?: string
  ): Promise<unknown> {
    this.calls.push({ method: "checkPolicy", args: [connector, tool, args, reason] });
    if (this.callError) throw this.callError;
    return this.callResult;
  }
  async beginUnattendedSession(reason?: string): Promise<unknown> {
    this.calls.push({ method: "beginUnattendedSession", args: [reason] });
    if (this.callError) throw this.callError;
    return this.callResult;
  }
  async endUnattendedSession(reason?: string): Promise<unknown> {
    this.calls.push({ method: "endUnattendedSession", args: [reason] });
    if (this.callError) throw this.callError;
    return this.callResult;
  }
}

async function connectedClient(server: McpServer): Promise<{ client: Client; close: () => Promise<void> }> {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test-client", version: "1.0.0" });
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  return {
    client,
    close: async () => {
      await client.close();
      await server.close();
    },
  };
}

describe("registerTools", () => {
  it("registers every tool from every connector", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    registerTools(server, ipc, {
      connectors: [
        { name: "gmail", tools: [spec({ name: "gmail_a" }), spec({ name: "gmail_b" })] },
        { name: "drive", tools: [spec({ name: "drive_a" })] },
      ],
    });

    const { client, close } = await connectedClient(server);
    try {
      const { tools } = await client.listTools();
      const names = tools.map((t) => t.name).sort();
      assert.deepEqual(names, ["drive_a", "gmail_a", "gmail_b"]);
    } finally {
      await close();
    }
  });

  it("advertises every tool as read-only regardless of read_only:false", async () => {
    // The real gate is enforced daemon-side; the client-facing annotation is
    // deliberately always read-only/non-destructive (see the comment above
    // UNIFORM_READ_ONLY_ANNOTATIONS in tools.ts) so Cowork doesn't
    // double-prompt.
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    registerTools(server, ipc, {
      connectors: [
        { name: "drive", tools: [spec({ name: "drive_delete_everything", read_only: false })] },
      ],
    });

    const { client, close } = await connectedClient(server);
    try {
      const { tools } = await client.listTools();
      const tool = tools.find((t) => t.name === "drive_delete_everything");
      assert.deepEqual(tool?.annotations, {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
      });
    } finally {
      await close();
    }
  });

  it("an empty manifest registers nothing, without throwing", () => {
    // Doesn't exercise tools/list here: the SDK only wires up that request
    // handler once at least one tool has ever been registered on the
    // server, so a genuinely tool-less McpServer returns "Method not
    // found" for it — not a scenario the real bridge hits, since
    // registerMetaTools always registers three tools regardless of the
    // connector manifest (see index.ts). This just mirrors the Python
    // test's actual intent: registration itself must not throw.
    const server = new McpServer({ name: "test", version: "0.0.0" });
    assert.doesNotThrow(() => registerTools(server, new FakeIPC(), { connectors: [] }));
  });

  it("forwards a call to the IPC client and returns its result as tool content", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    ipc.callResult = { ok: true };
    registerTools(server, ipc, {
      connectors: [
        {
          name: "gmail",
          tools: [
            spec({
              name: "gmail_get_message",
              params: [{ name: "message_id", annotation: "str", required: true, default: null, description: "" }],
            }),
          ],
        },
      ],
    });

    const { client, close } = await connectedClient(server);
    try {
      const result = await client.callTool({ name: "gmail_get_message", arguments: { message_id: "m1" } });
      assert.equal(result.isError, undefined);
      assert.deepEqual(ipc.calls, [{ method: "call", args: ["gmail", "gmail_get_message", { message_id: "m1" }] }]);
      assert.deepEqual(result.structuredContent, { ok: true });
    } finally {
      await close();
    }
  });

  it("an IPCError from the daemon becomes an isError tool result carrying its message", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    ipc.callError = new IPCError("daemon says no");
    registerTools(server, ipc, {
      connectors: [{ name: "gmail", tools: [spec({ name: "gmail_x" })] }],
    });

    const { client, close } = await connectedClient(server);
    try {
      const result = await client.callTool({ name: "gmail_x", arguments: {} });
      assert.equal(result.isError, true);
      const content = result.content as Array<{ type: string; text: string }>;
      assert.match(content[0]!.text, /daemon says no/);
    } finally {
      await close();
    }
  });

  it("a missing required argument is rejected before the IPC client is called", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    registerTools(server, ipc, {
      connectors: [
        {
          name: "gmail",
          tools: [
            spec({
              name: "gmail_search",
              params: [{ name: "query", annotation: "str", required: true, default: null, description: "" }],
            }),
          ],
        },
      ],
    });

    const { client, close } = await connectedClient(server);
    try {
      const result = await client.callTool({ name: "gmail_search", arguments: {} });
      assert.equal(result.isError, true);
      assert.deepEqual(ipc.calls, []);
    } finally {
      await close();
    }
  });

  it("an optional param carries its default when omitted", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    registerTools(server, ipc, {
      connectors: [
        {
          name: "gmail",
          tools: [
            spec({
              name: "gmail_list",
              params: [
                { name: "limit", annotation: "int", required: false, default: 10, description: "" },
              ],
            }),
          ],
        },
      ],
    });

    const { client, close } = await connectedClient(server);
    try {
      await client.callTool({ name: "gmail_list", arguments: {} });
      assert.deepEqual(ipc.calls, [{ method: "call", args: ["gmail", "gmail_list", { limit: 10 }] }]);
    } finally {
      await close();
    }
  });
});

describe("registerMetaTools", () => {
  it("registers all three meta-tools without needing a manifest", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    registerMetaTools(server, new FakeIPC());
    const { client, close } = await connectedClient(server);
    try {
      const { tools } = await client.listTools();
      assert.deepEqual(
        tools.map((t) => t.name).sort(),
        [
          "privacyfence_begin_unattended_session",
          "privacyfence_check_policy",
          "privacyfence_end_unattended_session",
        ]
      );
    } finally {
      await close();
    }
  });

  it("reason is a required param on all three meta-tools", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    registerMetaTools(server, new FakeIPC());
    const { client, close } = await connectedClient(server);
    try {
      const { tools } = await client.listTools();
      for (const t of tools) {
        assert.deepEqual(t.inputSchema.required, expectedRequired(t.name));
      }
    } finally {
      await close();
    }

    function expectedRequired(name: string): string[] {
      if (name === "privacyfence_check_policy") return ["connector", "tool", "reason"];
      return ["reason"];
    }
  });

  it("privacyfence_check_policy is advertised read-only with no side effects", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    registerMetaTools(server, new FakeIPC());
    const { client, close } = await connectedClient(server);
    try {
      const { tools } = await client.listTools();
      const tool = tools.find((t) => t.name === "privacyfence_check_policy");
      assert.deepEqual(tool?.annotations, { readOnlyHint: true, destructiveHint: false, idempotentHint: true });
    } finally {
      await close();
    }
  });

  it("begin/end unattended session are NOT advertised read-only (they have a real side effect)", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    registerMetaTools(server, new FakeIPC());
    const { client, close } = await connectedClient(server);
    try {
      const { tools } = await client.listTools();
      for (const name of ["privacyfence_begin_unattended_session", "privacyfence_end_unattended_session"]) {
        const tool = tools.find((t) => t.name === name);
        assert.equal(tool?.annotations?.readOnlyHint, false);
      }
    } finally {
      await close();
    }
  });

  it("privacyfence_check_policy forwards connector/tool/args/reason to the IPC client", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    ipc.callResult = { verdict: "auto_accept" };
    registerMetaTools(server, ipc);
    const { client, close } = await connectedClient(server);
    try {
      const result = await client.callTool({
        name: "privacyfence_check_policy",
        arguments: { connector: "gmail", tool: "gmail_get_message", reason: "Planning a scheduled run.", args: { message_id: "m1" } },
      });
      assert.equal(result.isError, undefined);
      assert.deepEqual(ipc.calls, [
        {
          method: "checkPolicy",
          args: ["gmail", "gmail_get_message", { message_id: "m1" }, "Planning a scheduled run."],
        },
      ]);
    } finally {
      await close();
    }
  });

  it("privacyfence_check_policy defaults args to {} when omitted", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    registerMetaTools(server, ipc);
    const { client, close } = await connectedClient(server);
    try {
      await client.callTool({
        name: "privacyfence_check_policy",
        arguments: { connector: "gmail", tool: "gmail_list_messages", reason: "Planning a scheduled run." },
      });
      assert.deepEqual(ipc.calls, [
        { method: "checkPolicy", args: ["gmail", "gmail_list_messages", {}, "Planning a scheduled run."] },
      ]);
    } finally {
      await close();
    }
  });

  it("privacyfence_begin_unattended_session forwards reason and an IPCError surfaces as isError", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    ipc.callError = new IPCError("unattended sessions disabled");
    registerMetaTools(server, ipc);
    const { client, close } = await connectedClient(server);
    try {
      const result = await client.callTool({
        name: "privacyfence_begin_unattended_session",
        arguments: { reason: "Nightly digest Routine." },
      });
      assert.equal(result.isError, true);
      const content = result.content as Array<{ type: string; text: string }>;
      assert.match(content[0]!.text, /disabled/);
      assert.deepEqual(ipc.calls, [{ method: "beginUnattendedSession", args: ["Nightly digest Routine."] }]);
    } finally {
      await close();
    }
  });

  it("privacyfence_end_unattended_session forwards reason", async () => {
    const server = new McpServer({ name: "test", version: "0.0.0" });
    const ipc = new FakeIPC();
    ipc.callResult = { unattended: false };
    registerMetaTools(server, ipc);
    const { client, close } = await connectedClient(server);
    try {
      const result = await client.callTool({
        name: "privacyfence_end_unattended_session",
        arguments: { reason: "Nightly digest Routine finished." },
      });
      assert.equal(result.isError, undefined);
      assert.deepEqual(ipc.calls, [
        { method: "endUnattendedSession", args: ["Nightly digest Routine finished."] },
      ]);
    } finally {
      await close();
    }
  });
});
