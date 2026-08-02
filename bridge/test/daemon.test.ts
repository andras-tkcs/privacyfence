import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import { BridgeExitError } from "../src/errors.js";
import { ensureDaemonRunning, findDaemonCmd, socketConnectable, waitForDaemonPatiently } from "../src/daemon.js";
import { getFreePort, makeTempIpcFiles } from "./testDaemon.js";

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
  it("is false when the port file doesn't exist yet", async () => {
    const { portFile, cleanup } = makeTempIpcFiles();
    try {
      // portFile was created by makeTempIpcFiles only as a path, not written to.
      assert.equal(await socketConnectable("127.0.0.1", portFile), false);
    } finally {
      cleanup();
    }
  });

  it("is false when the port file names a port nothing is listening on", async () => {
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const port = await getFreePort(); // freed immediately -- nothing listens on it
    writePort(port);
    try {
      assert.equal(await socketConnectable("127.0.0.1", portFile), false);
    } finally {
      cleanup();
    }
  });

  it("is true when a real listener is present", async () => {
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writePort(port);
    try {
      assert.equal(await socketConnectable("127.0.0.1", portFile), true);
    } finally {
      server.close();
      cleanup();
    }
  });
});

describe("ensureDaemonRunning", () => {
  it("returns immediately when already connectable, without spawning anything", async () => {
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writePort(port);
    let findCmdCalled = false;
    try {
      await ensureDaemonRunning({
        host: "127.0.0.1",
        portFile,
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
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const port = await getFreePort();
    // Simulate the daemon coming up shortly after being "launched": start
    // listening for real (and only then write the port file), but only
    // after ensureDaemonRunning's first connectability check has already
    // failed.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(port, "127.0.0.1", () => writePort(port));
    }, 50);

    try {
      await ensureDaemonRunning({
        host: "127.0.0.1",
        portFile,
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
    const { portFile, cleanup } = makeTempIpcFiles();
    try {
      await assert.rejects(
        ensureDaemonRunning({
          host: "127.0.0.1",
          portFile,
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
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writePort(port);
    let findCmdCalled = false;
    try {
      await waitForDaemonPatiently({
        host: "127.0.0.1",
        portFile,
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
    const { portFile, writePort, cleanup } = makeTempIpcFiles();
    const port = await getFreePort();
    // The initial launch-and-wait window (connectTimeoutMs) elapses with
    // nothing listening -- ensureDaemonRunning would normally throw here.
    // Only after that do we start listening (and write the port file),
    // simulating an app cold start slower than the initial window.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(port, "127.0.0.1", () => writePort(port));
    }, 150);

    let findCmdCalls = 0;
    try {
      await waitForDaemonPatiently({
        host: "127.0.0.1",
        portFile,
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
