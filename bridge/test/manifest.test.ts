import assert from "node:assert/strict";
import net from "node:net";
import { describe, it } from "node:test";
import { BridgeExitError } from "../src/errors.js";
import { checkVersionMatch, fetchManifest } from "../src/manifest.js";
import { makeShortSocketPath } from "./testDaemon.js";

describe("fetchManifest", () => {
  it("fetches and parses the manifest result over a short-lived connection", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    const server = net.createServer((sock) => {
      sock.setEncoding("utf8");
      sock.once("data", (line: string) => {
        const req = JSON.parse(line);
        assert.equal(req.method, "manifest");
        sock.write(
          JSON.stringify({ id: req.id, result: { version: "0.4.11", connectors: [] } }) + "\n"
        );
      });
    });
    await new Promise<void>((resolve) => server.listen(socketPath, resolve));

    try {
      const manifest = await fetchManifest(socketPath);
      assert.deepEqual(manifest, { version: "0.4.11", connectors: [] });
    } finally {
      server.close();
      cleanup();
    }
  });

  it("rejects if nothing is listening", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    try {
      await assert.rejects(fetchManifest(socketPath));
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
