import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import { BridgeExitError } from "../src/errors.js";
import { ensureDaemonRunning, findDaemonCmd, socketConnectable, waitForDaemonPatiently } from "../src/daemon.js";
import { makeShortSocketPath } from "./testDaemon.js";

describe("findDaemonCmd", () => {
  it("prefers a sibling binary next to the bridge script", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-daemon-"));
    const sibling = path.join(dir, "privacyfence-app");
    fs.writeFileSync(sibling, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({ scriptPath: path.join(dir, "bridge.js") });
    assert.deepEqual(cmd, [sibling]);
  });

  it("falls back to a PATH lookup when no sibling binary exists", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-daemon-empty-"));
    const pathDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-daemon-path-"));
    const onPath = path.join(pathDir, "privacyfence-app");
    fs.writeFileSync(onPath, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "bridge.js"),
      pathEnv: pathDir,
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
    });
    assert.deepEqual(cmd, [onPath]);
  });

  it("falls back to python3 -m privacyfence.daemon_main when nothing is found", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-daemon-empty2-"));
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "bridge.js"),
      pathEnv: emptyDir, // nothing named privacyfence-app here
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
    });
    assert.deepEqual(cmd, ["python3", "-m", "privacyfence.daemon_main"]);
  });
});

describe("socketConnectable", () => {
  it("is false when no socket file exists", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    try {
      assert.equal(await socketConnectable(socketPath), false);
    } finally {
      cleanup();
    }
  });

  it("is false when a file exists at the path but nothing is listening", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    // A stale/non-socket file at the path, not an accepting listener —
    // same intent as the Python test's raw socket.bind() without listen():
    // connect() must fail, not hang or throw out of socketConnectable.
    fs.writeFileSync(socketPath, "");
    try {
      assert.equal(await socketConnectable(socketPath), false);
    } finally {
      cleanup();
    }
  });

  it("is true when a real listener is present", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(socketPath, resolve));
    try {
      assert.equal(await socketConnectable(socketPath), true);
    } finally {
      server.close();
      cleanup();
    }
  });
});

describe("ensureDaemonRunning", () => {
  it("returns immediately when already connectable, without spawning anything", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(socketPath, resolve));
    let findCmdCalled = false;
    try {
      await ensureDaemonRunning({
        socketPath,
        findCmd: () => {
          findCmdCalled = true;
          return ["should-not-run"];
        },
      });
      assert.equal(findCmdCalled, false);
    } finally {
      server.close();
      cleanup();
    }
  });

  it("launches the daemon and waits until connectable", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    // Simulate the daemon coming up shortly after being "launched": start
    // listening for real, but only after ensureDaemonRunning's first
    // connectability check has already failed.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(socketPath);
    }, 50);

    try {
      await ensureDaemonRunning({
        socketPath,
        findCmd: () => ["true"], // a real no-op command; spawn() must succeed
        connectIntervalMs: 20,
        connectTimeoutMs: 2000,
      });
    } finally {
      clearTimeout(timer);
      lateServer?.close();
      cleanup();
    }
  });

  it("throws BridgeExitError after the timeout elapses", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    try {
      await assert.rejects(
        ensureDaemonRunning({
          socketPath,
          findCmd: () => ["true"],
          connectTimeoutMs: 100,
          connectIntervalMs: 20,
        }),
        (err: unknown) => {
          assert.ok(err instanceof BridgeExitError);
          assert.equal(err.code, 1);
          assert.match(err.message, /did not start/);
          return true;
        }
      );
    } finally {
      cleanup();
    }
  });
});

describe("waitForDaemonPatiently", () => {
  it("returns immediately when already connectable, without spawning anything", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(socketPath, resolve));
    let findCmdCalled = false;
    try {
      await waitForDaemonPatiently({
        socketPath,
        findCmd: () => {
          findCmdCalled = true;
          return ["should-not-run"];
        },
      });
      assert.equal(findCmdCalled, false);
    } finally {
      server.close();
      cleanup();
    }
  });

  it("keeps retrying past the initial timeout instead of throwing, and succeeds once the socket comes up", async () => {
    const { socketPath, cleanup } = makeShortSocketPath();
    // The initial launch-and-wait window (connectTimeoutMs) elapses with
    // nothing listening -- ensureDaemonRunning would normally throw here.
    // Only after that do we start listening, simulating an app cold start
    // slower than the initial window.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(socketPath);
    }, 150);

    let findCmdCalls = 0;
    try {
      await waitForDaemonPatiently({
        socketPath,
        findCmd: () => {
          findCmdCalls++;
          return ["true"]; // a real no-op command; spawn() must succeed
        },
        connectIntervalMs: 20,
        connectTimeoutMs: 60, // deliberately shorter than the 150ms the socket takes to appear
        retryIntervalMs: 20,
      });
      // findDaemonCmd (and therefore spawn) must only ever run once -- a
      // slow app start must not launch a second instance on every retry.
      assert.equal(findCmdCalls, 1);
    } finally {
      clearTimeout(timer);
      lateServer?.close();
      cleanup();
    }
  });
});
