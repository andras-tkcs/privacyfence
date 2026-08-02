import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import { HOST, LINE_LIMIT, PORT_FILE, TOKEN_FILE } from "../src/protocol.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const ipcPyPath = path.join(here, "..", "..", "src", "privacyfence", "ipc.py");

describe("protocol constants", () => {
  it("TOKEN_FILE matches ~/.privacyfence/ipc_token", () => {
    assert.equal(TOKEN_FILE, path.join(os.homedir(), ".privacyfence", "ipc_token"));
  });

  it("LINE_LIMIT matches ipc.py's literal (regression guard against drift)", () => {
    const src = readFileSync(ipcPyPath, "utf8");
    const match = src.match(/LINE_LIMIT\s*=\s*(\d+)\s*\*\s*(\d+)\s*\*\s*(\d+)/);
    assert.ok(match, "could not find LINE_LIMIT expression in ipc.py");
    const [, a, b, c] = match;
    const expected = Number(a) * Number(b) * Number(c);
    assert.equal(LINE_LIMIT, expected);
  });

  it("HOST matches ipc.py's HOST (regression guard against drift)", () => {
    const src = readFileSync(ipcPyPath, "utf8");
    assert.match(src, /HOST\s*=\s*"127\.0\.0\.1"/);
    assert.equal(HOST, "127.0.0.1");
  });

  it("PORT_FILE matches ~/.privacyfence/ipc_port", () => {
    assert.equal(PORT_FILE, path.join(os.homedir(), ".privacyfence", "ipc_port"));
  });

  it("TOKEN_FILE's literal segments match ipc.py's TOKEN_FILE", () => {
    const src = readFileSync(ipcPyPath, "utf8");
    assert.match(src, /TOKEN_FILE\s*=\s*os\.path\.expanduser\("~\/\.privacyfence\/ipc_token"\)/);
  });

  it("PORT_FILE's literal segments match ipc.py's PORT_FILE", () => {
    const src = readFileSync(ipcPyPath, "utf8");
    assert.match(src, /PORT_FILE\s*=\s*os\.path\.expanduser\("~\/\.privacyfence\/ipc_port"\)/);
  });
});
