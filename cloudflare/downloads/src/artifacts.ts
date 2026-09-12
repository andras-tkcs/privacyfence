/**
 * Streaming an installer artifact out of R2: headers, range resolution, and the "did this
 * request actually start a download" rule the counting logic in index.ts depends on.
 *
 * Range handling is driven entirely by this Worker's own parse of the incoming `Range` header
 * (`parseRangeHeader`), not by R2's returned `R2Object#range` -- the R2 binding (at least under
 * local Miniflare emulation, and per its own type as merely `readonly range?: R2Range` rather
 * than a guarantee) may report a range describing the *whole* object even for a request that
 * supplied no `Range` header at all, which would make "is this a partial response" and "did this
 * download start at byte 0" impossible to answer correctly from the response object alone.
 */
import type { ManifestArtifact } from "./manifest.js";

const CONTENT_TYPES: Record<string, string> = {
  dmg: "application/x-apple-diskimage",
  exe: "application/vnd.microsoft.portable-executable",
  deb: "application/vnd.debian.binary-package",
};

function contentTypeFor(filename: string): string {
  const ext = filename.slice(filename.lastIndexOf(".") + 1).toLowerCase();
  return CONTENT_TYPES[ext] ?? "application/octet-stream";
}

/** The byte range actually being served, resolved against the artifact's full size. */
export interface ResolvedRange {
  start: number;
  end: number; // inclusive
}

/**
 * Parses an HTTP `Range` request header into an `R2Range`. Only a single `bytes=` range is
 * supported (installer downloaders never send multi-range requests); anything else -- absent,
 * malformed, or multi-range -- is treated as "no range requested" (a full response), same as a
 * real HTTP server falling back to serving the whole resource for a `Range` header it can't honor.
 */
export function parseRangeHeader(header: string | null): R2Range | undefined {
  if (!header) return undefined;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!match) return undefined;
  const startStr = match[1] ?? "";
  const endStr = match[2] ?? "";
  if (startStr === "" && endStr === "") return undefined;
  if (startStr === "") return { suffix: Number(endStr) }; // "bytes=-500" -- last 500 bytes
  const offset = Number(startStr);
  if (endStr === "") return { offset }; // "bytes=500-" -- from offset 500 to the end
  return { offset, length: Number(endStr) - offset + 1 }; // "bytes=500-999"
}

export function resolveRange(range: R2Range, totalSize: number): ResolvedRange {
  if ("suffix" in range) {
    const start = Math.max(totalSize - range.suffix, 0);
    return { start, end: totalSize - 1 };
  }
  const start = range.offset ?? 0;
  const end = range.length !== undefined ? start + range.length - 1 : totalSize - 1;
  return { start, end };
}

/**
 * Whether a request for `range` (the result of `parseRangeHeader`) should count as a real
 * download start: a full request (no range), or a range that starts at byte 0
 * (docs/release-publishing-kpi-plan.md Phase 1's counting semantics). A `Range: bytes=N-` resume
 * with N>0, or a `suffix` (tail) range, never counts.
 */
export function isDownloadStart(range: R2Range | undefined): boolean {
  if (!range) return true;
  if ("suffix" in range) return false;
  return (range.offset ?? 0) === 0;
}

/** Builds the response headers for a full or partial artifact download, or a HEAD probe. */
export function artifactHeaders(artifact: ManifestArtifact, range: R2Range | undefined, etag: string): Headers {
  const headers = new Headers();
  headers.set("Content-Type", contentTypeFor(artifact.filename));
  headers.set("Content-Disposition", `attachment; filename="${artifact.filename}"`);
  headers.set("ETag", etag);
  headers.set("Accept-Ranges", "bytes");
  if (range) {
    const { start, end } = resolveRange(range, artifact.size);
    headers.set("Content-Length", String(end - start + 1));
    headers.set("Content-Range", `bytes ${start}-${end}/${artifact.size}`);
  } else {
    headers.set("Content-Length", String(artifact.size));
  }
  return headers;
}
