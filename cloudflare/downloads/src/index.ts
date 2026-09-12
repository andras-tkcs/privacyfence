/**
 * Worker backing https://downloads.privacyfence.eu -- the only public path to the private
 * `privacyfence-releases` R2 bucket (docs/release-publishing-kpi-plan.md Phase 1). The browser
 * never receives R2 credentials or a direct bucket URL: every installer download streams
 * through this Worker, which also records a privacy-preserving download count in D1 (no IP,
 * cookie, fingerprint, or User-Agent -- see migrations/0001_download_counts.sql).
 *
 * Routes:
 *   GET/HEAD /download/<channel>/<artifact-id>            -- e.g. /download/stable/macos-arm64
 *   GET/HEAD /download/version/<version>/<artifact-id>    -- pin to an exact, possibly older, version
 *   GET      /api/releases                                -- latest manifest per channel
 *   GET      /api/releases/<channel>                       -- latest manifest for one channel
 *   GET      /api/stats/downloads                          -- aggregated D1 counters
 *   GET      /health                                       -- liveness probe, touches no binding
 *
 * `<artifact-id>` matches a manifest artifact's own `id` field (e.g. "macos-arm64",
 * "windows-x64", "linux-x64") -- see manifest.ts's Manifest/ManifestArtifact types, which match
 * Phase 2's manifest schema exactly.
 */
import { CHANNELS, isChannel, type Channel } from "./channel.js";
import { findArtifact, resolveLatestManifest, resolveVersionManifest, type Manifest } from "./manifest.js";
import { recordDownload, queryStats } from "./counters.js";
import { artifactHeaders, isDownloadStart, parseRangeHeader } from "./artifacts.js";
import { corsPreflight, jsonResponse, methodNotAllowed, notFound, withCors } from "./http.js";

// `Env` (RELEASES/DB bindings) is declared globally in ../worker-configuration.d.ts, matching
// wrangler.toml -- no import needed, same as any other Workers project's generated types.

function hasBody(object: R2Object | R2ObjectBody): object is R2ObjectBody {
  return "body" in object;
}

async function serveArtifact(
  request: Request,
  env: Env,
  ctx: ExecutionContext,
  channel: Channel,
  manifest: Manifest,
  artifactId: string,
): Promise<Response> {
  const artifact = findArtifact(manifest, artifactId);
  if (!artifact) return notFound(`no such artifact: ${artifactId}`);

  if (request.method === "HEAD") {
    const head = await env.RELEASES.head(artifact.key);
    if (!head) return notFound("artifact is missing from storage");
    return new Response(null, { status: 200, headers: artifactHeaders(artifact, undefined, head.httpEtag) });
  }

  // This Worker parses `Range` itself (see parseRangeHeader's docstring for why) and only passes
  // R2 the resolved range, rather than handing it the raw request Headers to parse internally.
  // `onlyIf` still gets the request's own Headers directly -- R2's conditional-request parsing
  // (If-Match/If-None-Match/...) has no equivalent ambiguity to work around.
  const requestedRange = parseRangeHeader(request.headers.get("Range"));
  const getOptions: R2GetOptions & { onlyIf: Headers } = { onlyIf: request.headers };
  if (requestedRange) getOptions.range = requestedRange;
  const object = await env.RELEASES.get(artifact.key, getOptions);
  if (!object) return notFound("artifact is missing from storage");
  if (!hasBody(object)) {
    // A conditional request's precondition failed (e.g. If-None-Match matched) -- R2 returns
    // metadata with no body in that case. Never counts: nothing was actually downloaded.
    return new Response(null, { status: 304 });
  }

  if (isDownloadStart(requestedRange)) {
    ctx.waitUntil(
      recordDownload(env.DB, {
        channel,
        version: manifest.version,
        platform: artifact.platform,
        architecture: artifact.architecture,
        artifactKind: artifact.kind,
      }),
    );
  }

  return new Response(object.body, {
    status: requestedRange ? 206 : 200,
    headers: artifactHeaders(artifact, requestedRange, object.httpEtag),
  });
}

async function routeDownload(path: string, request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const versionMatch = /^\/download\/version\/([^/]+)\/([^/]+)$/.exec(path);
  if (versionMatch) {
    const version = decodeURIComponent(versionMatch[1]!);
    const rawArtifactId = versionMatch[2]!;
    let resolved;
    try {
      resolved = await resolveVersionManifest(env.RELEASES, version);
    } catch (err) {
      return notFound(err instanceof Error ? err.message : `invalid version: ${version}`);
    }
    if (!resolved) return notFound(`no such release version: ${version}`);
    return serveArtifact(request, env, ctx, resolved.channel, resolved.manifest, decodeURIComponent(rawArtifactId));
  }

  const channelMatch = /^\/download\/([^/]+)\/([^/]+)$/.exec(path);
  if (channelMatch) {
    const channel = channelMatch[1]!;
    const rawArtifactId = channelMatch[2]!;
    if (!isChannel(channel)) return notFound(`unknown channel: ${channel}`);
    const manifest = await resolveLatestManifest(env.RELEASES, channel);
    if (!manifest) return notFound(`no published release for channel: ${channel}`);
    return serveArtifact(request, env, ctx, channel, manifest, decodeURIComponent(rawArtifactId));
  }

  return notFound("no such download route");
}

async function handleStats(env: Env): Promise<Response> {
  try {
    return jsonResponse(await queryStats(env.DB));
  } catch (err) {
    console.error("privacyfence-downloads: stats query failed", err);
    // A stats failure must never look like "zero downloads" -- surface it so the website
    // (Phase 5) can hide the stats section instead of showing a wrong number.
    return jsonResponse({ error: "stats temporarily unavailable" }, 503);
  }
}

async function routeApi(path: string, env: Env): Promise<Response> {
  if (path === "/api/releases") {
    const entries = await Promise.all(
      CHANNELS.map(async (channel) => [channel, await resolveLatestManifest(env.RELEASES, channel)] as const),
    );
    return jsonResponse({ channels: Object.fromEntries(entries) });
  }

  const channelMatch = /^\/api\/releases\/([^/]+)$/.exec(path);
  if (channelMatch) {
    const channel = channelMatch[1]!;
    if (!isChannel(channel)) return notFound(`unknown channel: ${channel}`);
    const manifest = await resolveLatestManifest(env.RELEASES, channel);
    if (!manifest) return notFound(`no published release for channel: ${channel}`);
    return jsonResponse(manifest);
  }

  if (path === "/api/stats/downloads") return handleStats(env);

  return notFound("no such API route");
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);

    if (pathname === "/health") {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      // Deliberately touches neither binding: a real outage in R2/D1 shouldn't also take down
      // the liveness probe used to check the Worker itself deployed correctly.
      return jsonResponse({ status: "ok" });
    }

    if (pathname.startsWith("/api/")) {
      if (request.method === "OPTIONS") return corsPreflight(request);
      if (request.method !== "GET") return withCors(request, methodNotAllowed(["GET", "OPTIONS"]));
      return withCors(request, await routeApi(pathname, env));
    }

    if (pathname.startsWith("/download/")) {
      if (request.method !== "GET" && request.method !== "HEAD") return methodNotAllowed(["GET", "HEAD"]);
      return routeDownload(pathname, request, env, ctx);
    }

    return notFound("no such route");
  },
} satisfies ExportedHandler<Env>;
