import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { queryStats, recordDownload } from "../src/counters";

const EVENT = {
  channel: "stable",
  version: "4.3.0",
  platform: "macos",
  architecture: "arm64",
  artifactKind: "installer",
} as const;

describe("recordDownload", () => {
  it("swallows a D1 failure instead of throwing -- must never be able to block the byte stream", async () => {
    const failingDb = {
      prepare() {
        throw new Error("D1 is down");
      },
    } as unknown as D1Database;

    await expect(recordDownload(failingDb, EVENT)).resolves.toBeUndefined();
  });

  it("increments an existing row instead of inserting a duplicate", async () => {
    const before = await queryStats(env.DB);
    await recordDownload(env.DB, EVENT);
    await recordDownload(env.DB, EVENT);
    const after = await queryStats(env.DB);
    expect(after.total).toBe(before.total + 2);
    expect(after.by_channel["stable"]).toBe((before.by_channel["stable"] ?? 0) + 2);
  });

  it("keeps distinct artifacts in distinct rows", async () => {
    await recordDownload(env.DB, EVENT);
    await recordDownload(env.DB, { ...EVENT, platform: "windows", architecture: "x64" });
    const stats = await queryStats(env.DB);
    const platforms = new Set(stats.by_platform.map((row) => `${row.platform}-${row.architecture}`));
    expect(platforms.has("macos-arm64")).toBe(true);
    expect(platforms.has("windows-x64")).toBe(true);
  });
});

describe("queryStats", () => {
  it("propagates a real D1 error instead of returning a fake empty result", async () => {
    const failingDb = {
      prepare() {
        return {
          first: () => {
            throw new Error("D1 is down");
          },
        };
      },
    } as unknown as D1Database;

    await expect(queryStats(failingDb)).rejects.toThrow("D1 is down");
  });
});
