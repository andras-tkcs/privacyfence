import { describe, expect, it } from "vitest";
import { CHANNELS, channelForVersion, isChannel } from "../src/channel";

describe("channelForVersion", () => {
  it.each([
    ["4.3.0", "stable"],
    ["v4.3.0", "stable"],
    ["4.4.0a1", "alpha"],
    ["4.4.0b2", "beta"],
    ["4.4.0rc3", "rc"],
  ] as const)("resolves %s to %s", (version, expected) => {
    expect(channelForVersion(version)).toBe(expected);
  });

  it("rejects a between-tags dev build", () => {
    expect(() => channelForVersion("4.2.1.dev3+gabc1234")).toThrow(/dev build/);
  });

  it("rejects a string that isn't a version at all", () => {
    expect(() => channelForVersion("not-a-version")).toThrow(/doesn't look like a release version/);
  });
});

describe("isChannel", () => {
  it("accepts every known channel", () => {
    for (const channel of CHANNELS) expect(isChannel(channel)).toBe(true);
  });

  it("rejects an unknown channel name", () => {
    expect(isChannel("nightly")).toBe(false);
  });
});
