import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import { LINE_LIMIT, SOCKET_PATH } from "../src/protocol.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const ipcPyPath = path.join(here, "..", "..", "src", "privacyfence", "ipc.py");

describe("protocol constants", () => {
  it("SOCKET_PATH matches ~/.privacyfence/privacyfence.sock", () => {
    assert.equal(SOCKET_PATH, path.join(os.homedir(), ".privacyfence", "privacyfence.sock"));
  });

  it("LINE_LIMIT matches ipc.py's literal (regression guard against drift)", () => {
    const src = readFileSync(ipcPyPath, "utf8");
    const match = src.match(/LINE_LIMIT\s*=\s*(\d+)\s*\*\s*(\d+)\s*\*\s*(\d+)/);
    assert.ok(match, "could not find LINE_LIMIT expression in ipc.py");
    const [, a, b, c] = match;
    const expected = Number(a) * Number(b) * Number(c);
    assert.equal(LINE_LIMIT, expected);
  });

  it("SOCKET_PATH's literal segments match ipc.py's SOCKET_PATH", () => {
    const src = readFileSync(ipcPyPath, "utf8");
    assert.match(src, /SOCKET_PATH\s*=\s*os\.path\.expanduser\("~\/\.privacyfence\/privacyfence\.sock"\)/);
  });
});
