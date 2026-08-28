import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import { readMcpToken, readMcpUrl } from "../src/protocol.js";

describe("readMcpUrl", () => {
  it("returns the trimmed URL text", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_url");
    fs.writeFileSync(file, "http://localhost:8765/mcp\n");
    assert.equal(readMcpUrl(file), "http://localhost:8765/mcp");
  });

  it("throws on an empty file", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_url");
    fs.writeFileSync(file, "\n");
    assert.throws(() => readMcpUrl(file), /Empty MCP URL/);
  });

  it("throws on a malformed URL", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_url");
    fs.writeFileSync(file, "not a url");
    assert.throws(() => readMcpUrl(file));
  });

  it("throws when the file doesn't exist", () => {
    assert.throws(() => readMcpUrl("/definitely/does/not/exist/mcp_url"));
  });
});

describe("readMcpToken", () => {
  it("returns the trimmed token text", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pf-shim-proto-"));
    const file = path.join(dir, "mcp_token");
    fs.writeFileSync(file, "abc123\n");
    assert.equal(readMcpToken(file), "abc123");
  });
});
