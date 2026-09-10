import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import { ensureDaemonRunning, findDaemonCmd, socketConnectable, waitForDaemonPatiently } from "../src/daemon.js";
import { ShimExitError } from "../src/errors.js";
import { getFreePort, makeTempMcpFiles } from "./testFiles.js";

describe("findDaemonCmd", () => {
  it("prefers a sibling binary next to the shim script", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-"));
    const sibling = path.join(dir, "privacyfence-app");
    fs.writeFileSync(sibling, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({ scriptPath: path.join(dir, "shim.js") });
    assert.deepEqual(cmd, [sibling]);
  });

  it("falls back to a PATH lookup when no sibling binary exists", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty-"));
    const pathDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-path-"));
    const onPath = path.join(pathDir, "privacyfence-app");
    fs.writeFileSync(onPath, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: pathDir,
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
    });
    assert.deepEqual(cmd, [onPath]);
  });

  it("falls back to python3 -m privacyfence.daemon_main when nothing is found", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty2-"));
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir, // nothing named privacyfence-app here
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "linux",
      homeDir: emptyDir, // no ~/.local/bin/privacyfence-app here either
    });
    assert.deepEqual(cmd, ["python3", "-m", "privacyfence.daemon_main"]);
  });

  it("on Linux, falls back to the pipx default (~/.local/bin/privacyfence-app) before python3 -m", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty3-"));
    const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-home-"));
    const localBin = path.join(homeDir, ".local", "bin");
    fs.mkdirSync(localBin, { recursive: true });
    const pipxDefault = path.join(localBin, "privacyfence-app");
    fs.writeFileSync(pipxDefault, "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir, // ~/.local/bin deliberately not on PATH here
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "linux",
      homeDir,
    });
    assert.deepEqual(cmd, [pipxDefault]);
  });

  it("does not check the Linux pipx default on other platforms", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-empty4-"));
    const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-home2-"));
    const localBin = path.join(homeDir, ".local", "bin");
    fs.mkdirSync(localBin, { recursive: true });
    fs.writeFileSync(path.join(localBin, "privacyfence-app"), "#!/bin/sh\n", { mode: 0o755 });

    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      defaultAppPath: "/definitely/does/not/exist/privacyfence-app",
      platform: "darwin",
      homeDir,
    });
    assert.deepEqual(cmd, ["python3", "-m", "privacyfence.daemon_main"]);
  });

  // Windows branch (docs/windows-support-plan.md Phase 7 / B6 in docs/
  // windows-linux-support-plan.md) -- `platform` is injectable specifically
  // so these can run on any CI host, not just a real Windows one.
  it("falls back to python -m privacyfence.daemon_main (not python3) on win32", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-"));
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir, // nothing named privacyfence-app here
      defaultAppPath: "C:\\definitely\\does\\not\\exist\\privacyfence-app.exe",
      platform: "win32",
    });
    assert.deepEqual(cmd, ["python", "-m", "privacyfence.daemon_main"]);
  });

  it("uses the Windows Program Files default app path when platform is win32 and no defaultAppPath override is given", () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-daemon-win32-default-"));
    // No defaultAppPath override: the real Windows default
    // (C:\Program Files\PrivacyFence\privacyfence-app.exe) definitely
    // doesn't exist on this (non-Windows) test host either, so this still
    // exercises the platform-conditional default falling through to the
    // python fallback -- confirming the default itself, not just that an
    // override is honored, is platform-conditional.
    const cmd = findDaemonCmd({
      scriptPath: path.join(emptyDir, "shim.js"),
      pathEnv: emptyDir,
      platform: "win32",
    });
    assert.deepEqual(cmd, ["python", "-m", "privacyfence.daemon_main"]);
  });
});

describe("socketConnectable", () => {
  it("is false when the mcp_url file doesn't exist yet", async () => {
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      // mcpUrlFile was created by makeTempMcpFiles only as a path, not written to.
      assert.equal(await socketConnectable(mcpUrlFile), false);
    } finally {
      cleanup();
    }
  });

  it("is false when the mcp_url file doesn't parse as a URL", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    writeUrl("not a url");
    try {
      assert.equal(await socketConnectable(mcpUrlFile), false);
    } finally {
      cleanup();
    }
  });

  it("is false when the URL names a port nothing is listening on", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort(); // freed immediately -- nothing listens on it
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    try {
      assert.equal(await socketConnectable(mcpUrlFile), false);
    } finally {
      cleanup();
    }
  });

  it("is true when a real listener is present", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    try {
      assert.equal(await socketConnectable(mcpUrlFile), true);
    } finally {
      server.close();
      cleanup();
    }
  });
});

describe("ensureDaemonRunning", () => {
  it("returns immediately when already connectable, without spawning anything", async () => {
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    let findCmdCalled = false;
    try {
      await ensureDaemonRunning({
        mcpUrlFile,
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
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort();
    // Simulate the daemon coming up shortly after being "launched": start
    // listening for real (and only then write mcp_url), but only after
    // ensureDaemonRunning's first connectability check has already failed.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(port, "127.0.0.1", () => writeUrl(`http://127.0.0.1:${port}/mcp`));
    }, 50);

    try {
      await ensureDaemonRunning({
        mcpUrlFile,
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

  it("throws ShimExitError after the timeout elapses", async () => {
    const { mcpUrlFile, cleanup } = makeTempMcpFiles();
    try {
      await assert.rejects(
        ensureDaemonRunning({
          mcpUrlFile,
          findCmd: () => ["true"],
          connectTimeoutMs: 100,
          connectIntervalMs: 20,
        }),
        (err: unknown) => {
          assert.ok(err instanceof ShimExitError);
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
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const server = net.createServer();
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    writeUrl(`http://127.0.0.1:${port}/mcp`);
    let findCmdCalled = false;
    try {
      await waitForDaemonPatiently({
        mcpUrlFile,
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
    const { mcpUrlFile, writeUrl, cleanup } = makeTempMcpFiles();
    const port = await getFreePort();
    // The initial launch-and-wait window (connectTimeoutMs) elapses with
    // nothing listening -- ensureDaemonRunning would normally throw here.
    // Only after that do we start listening (and write mcp_url), simulating
    // an app cold start slower than the initial window.
    let lateServer: net.Server | undefined;
    const timer = setTimeout(() => {
      lateServer = net.createServer();
      lateServer.listen(port, "127.0.0.1", () => writeUrl(`http://127.0.0.1:${port}/mcp`));
    }, 150);

    let findCmdCalls = 0;
    try {
      await waitForDaemonPatiently({
        mcpUrlFile,
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
