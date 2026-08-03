import assert from "node:assert/strict";
import net from "node:net";
import { describe, it } from "node:test";
import { BridgeExitError } from "../src/errors.js";
import { checkVersionMatch, fetchManifest } from "../src/manifest.js";
import { getFreePort, makeTempIpcFiles } from "./testDaemon.js";

describe("fetchManifest", () => {
  it("fetches and parses the manifest result over a short-lived connection", async () => {
    const { tokenFile, portFile, writePort, token, cleanup } = makeTempIpcFiles();
    let authLine = "";
    const server = net.createServer((sock) => {
      sock.setEncoding("utf8");
      let buffer = "";
      let authSeen = false;
      sock.on("data", (chunk: string) => {
        buffer += chunk;
        let idx: number;
        while ((idx = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 1);
          if (!authSeen) {
            authSeen = true;
            authLine = line;
            continue;
          }
          const req = JSON.parse(line);
          assert.equal(req.method, "manifest");
          sock.write(
            JSON.stringify({ id: req.id, result: { version: "0.4.11", connectors: [] } }) + "\n"
          );
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writePort(port);

    try {
      const manifest = await fetchManifest("127.0.0.1", portFile, tokenFile);
      assert.deepEqual(manifest, { version: "0.4.11", connectors: [] });
      assert.equal(authLine, token); // the auth handshake's first line, before the request
    } finally {
      server.close();
      cleanup();
    }
  });

  it("rejects if nothing is listening", async () => {
    const { tokenFile, portFile, writePort, cleanup } = makeTempIpcFiles();
    const port = await getFreePort(); // freed immediately -- nothing listens on it
    writePort(port);
    try {
      await assert.rejects(fetchManifest("127.0.0.1", portFile, tokenFile));
    } finally {
      cleanup();
    }
  });

  it("rejects when the port file can't be read", async () => {
    const { tokenFile, cleanup } = makeTempIpcFiles();
    try {
      await assert.rejects(fetchManifest("127.0.0.1", "/nonexistent/ipc_port", tokenFile));
    } finally {
      cleanup();
    }
  });

  it("rejects when the token file can't be read", async () => {
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const port = await getFreePort();
    writePort(port);
    try {
      await assert.rejects(fetchManifest("127.0.0.1", portFile, "/nonexistent/ipc_token"));
    } finally {
      cleanup();
    }
  });
});

describe("checkVersionMatch", () => {
  it("does not throw when versions match", () => {
    assert.doesNotThrow(() => checkVersionMatch({ version: "1.2.3", connectors: [] }, "1.2.3"));
  });

  it("does not throw when the daemon omits a version key", () => {
    assert.doesNotThrow(() => checkVersionMatch({ connectors: [] }, "1.2.3"));
  });

  it("throws BridgeExitError(1) on a version mismatch, message names both versions", () => {
    assert.throws(
      () => checkVersionMatch({ version: "0.0.1-stale", connectors: [] }, "1.2.3"),
      (err: unknown) => {
        assert.ok(err instanceof BridgeExitError);
        assert.equal(err.code, 1);
        assert.match(err.message, /version mismatch/);
        assert.match(err.message, /1\.2\.3/);
        assert.match(err.message, /0\.0\.1-stale/);
        return true;
      }
    );
  });
});
