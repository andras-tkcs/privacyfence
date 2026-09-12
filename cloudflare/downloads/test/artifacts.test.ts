import { describe, expect, it } from "vitest";
import { isDownloadStart, parseRangeHeader, resolveRange } from "../src/artifacts";

describe("parseRangeHeader", () => {
  it("returns undefined for a missing header", () => {
    expect(parseRangeHeader(null)).toBeUndefined();
  });

  it("parses a closed range", () => {
    expect(parseRangeHeader("bytes=0-9")).toEqual({ offset: 0, length: 10 });
  });

  it("parses an open-ended range", () => {
    expect(parseRangeHeader("bytes=10-")).toEqual({ offset: 10 });
  });

  it("parses a suffix (tail) range", () => {
    expect(parseRangeHeader("bytes=-500")).toEqual({ suffix: 500 });
  });

  it("treats a malformed header as no range at all", () => {
    expect(parseRangeHeader("not-a-range")).toBeUndefined();
    expect(parseRangeHeader("bytes=")).toBeUndefined();
  });
});

describe("resolveRange", () => {
  it("resolves an offset+length range", () => {
    expect(resolveRange({ offset: 10, length: 5 }, 100)).toEqual({ start: 10, end: 14 });
  });

  it("resolves an offset-only range to the end of the object", () => {
    expect(resolveRange({ offset: 90 }, 100)).toEqual({ start: 90, end: 99 });
  });

  it("resolves a suffix (tail) range", () => {
    expect(resolveRange({ suffix: 10 }, 100)).toEqual({ start: 90, end: 99 });
  });
});

describe("isDownloadStart", () => {
  it("counts a full request with no Range header", () => {
    expect(isDownloadStart(undefined)).toBe(true);
  });

  it("counts a range that starts at byte 0", () => {
    expect(isDownloadStart({ offset: 0, length: 10 })).toBe(true);
  });

  it("does not count a resume range starting past byte 0", () => {
    expect(isDownloadStart({ offset: 100 })).toBe(false);
  });

  it("does not count a suffix (tail) range", () => {
    expect(isDownloadStart({ suffix: 10 })).toBe(false);
  });
});
