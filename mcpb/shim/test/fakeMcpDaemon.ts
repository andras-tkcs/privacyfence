/**
 * A real /mcp server -- built on the same official SDK classes
 * web/routes_mcp.py uses server-side in Python (McpServer +
 * StreamableHTTPServerTransport there too, just the Python builds) -- that
 * plays the role of "the PrivacyFence daemon" for index.test.ts, so the
 * shim is driven against a real Streamable HTTP endpoint end to end rather
 * than a hand-mocked Transport. Bearer-token checking is done by hand here
 * (a couple of lines in the request handler) since this fixture only needs
 * to prove the shim actually attaches the header, not exercise a real auth
 * stack -- see mcp_auth.py's StaticTokenVerifier for what the real daemon
 * does instead.
 */
import { randomUUID } from "node:crypto";
import http from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";

export class FakeMcpDaemon {
  url = "";
  receivedAuthHeaders: (string | undefined)[] = [];
  echoedArgs: unknown[] = [];
  private server: http.Server | null = null;
  private mcpServer: McpServer;
  private transport: StreamableHTTPServerTransport;

  constructor(private readonly requiredToken: string) {
    this.mcpServer = new McpServer({ name: "fake-privacyfence", version: "0.0.0-test" });
    this.mcpServer.tool("shim_test_echo", async (extra) => {
      this.echoedArgs.push(extra);
      return { content: [{ type: "text", text: "echoed" }], structuredContent: { echoed: true } };
    });
    this.transport = new StreamableHTTPServerTransport({ sessionIdGenerator: () => randomUUID() });
  }

  async start(): Promise<string> {
    // The SDK's own StreamableHTTPServerTransport doesn't quite satisfy its
    // own Transport interface under exactOptionalPropertyTypes (its
    // accessor-based onclose/onmessage/sessionId are typed `X | undefined`
    // where Transport declares them optional-absent, `X?`) -- an SDK-side
    // typing gap unrelated to anything this fixture does, and irrelevant at
    // runtime (mcp.js only ever calls these members, never inspects
    // presence). See src/proxy.ts's own MessageTransport for how production
    // code sidesteps the same gap instead of casting.
    await this.mcpServer.connect(this.transport as any);
    await new Promise<void>((resolve) => {
      this.server = http.createServer((req, res) => {
        this.receivedAuthHeaders.push(req.headers.authorization);
        if (req.headers.authorization !== `Bearer ${this.requiredToken}`) {
          res.writeHead(401).end();
          return;
        }
        void this.transport.handleRequest(req, res);
      });
      this.server.listen(0, "127.0.0.1", resolve);
    });
    const address = this.server!.address();
    const port = typeof address === "object" && address ? address.port : 0;
    this.url = `http://127.0.0.1:${port}/mcp`;
    return this.url;
  }

  async stop(): Promise<void> {
    await this.transport.close();
    await new Promise<void>((resolve) => {
      if (!this.server) return resolve();
      this.server.close(() => resolve());
    });
  }
}
