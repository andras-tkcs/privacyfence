/**
 * Integration tests against the Worker's own `fetch()` handler, running inside the real Workers
 * runtime (workerd via Miniflare) with the R2/D1 fixtures test/setup.ts seeds. Covers every bullet
 * in docs/release-publishing-kpi-plan.md Phase 1's test list.
 */
import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index";

const ORIGIN = "https://downloads.privacyfence.eu";

async function call(path: string, init?: RequestInit): Promise<Response> {
  const ctx = createExecutionContext();
  // `cf`-properties generics differ between a plain `new Request()` and the incoming-request type
  // `ExportedHandler#fetch` expects; workerd fills in real `cf` properties for us at the HTTP
  // layer, so this cast is just bridging two TS-only shapes of the same runtime `Request`.
  const request = new Request(`${ORIGIN}${path}`, init) as Request<unknown, IncomingRequestCfProperties>;
  const response = await worker.fetch(request, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

async function statsTotal(): Promise<number> {
  const response = await call("/api/stats/downloads");
  const body = (await response.json()) as { total: number };
  return body.total;
}

describe("GET /health", () => {
  it("returns 200 without touching R2/D1", async () => {
    const response = await call("/health");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });
});

describe("channel resolution", () => {
  it.each([
    ["stable", "PrivacyFence-4.3.0.dmg"],
    ["beta", "PrivacyFence-4.4.0b1.dmg"],
    ["alpha", "PrivacyFence-4.4.0a1.dmg"],
    ["rc", "PrivacyFence-4.4.0rc1.dmg"],
  ])("resolves the %s channel's latest macos-arm64 installer", async (channel, filename) => {
    const response = await call(`/download/${channel}/macos-arm64`);
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe(`attachment; filename="${filename}"`);
  });

  it("404s for an unknown channel", async () => {
    const response = await call("/download/nightly/macos-arm64");
    expect(response.status).toBe(404);
  });
});

describe("artifact classification", () => {
  it.each([
    ["macos-arm64", "PrivacyFence-4.3.0.dmg", "application/x-apple-diskimage"],
    ["windows-x64", "PrivacyFence-4.3.0-setup.exe", "application/vnd.microsoft.portable-executable"],
    ["linux-x64", "privacyfence_4.3.0_amd64.deb", "application/vnd.debian.binary-package"],
  ])("serves the %s artifact with the right filename and content type", async (id, filename, contentType) => {
    const response = await call(`/download/stable/${id}`);
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe(`attachment; filename="${filename}"`);
    expect(response.headers.get("Content-Type")).toBe(contentType);
    expect(await response.text()).toMatch(new RegExp(`^FAKE-.+-stable-4\\.3\\.0-`));
  });

  it("404s for an artifact id the manifest doesn't have", async () => {
    const response = await call("/download/stable/linux-arm64");
    expect(response.status).toBe(404);
  });
});

describe("version-pinned downloads", () => {
  it("serves an older version directly, independent of what's current on the channel", async () => {
    const response = await call("/download/version/4.2.0/macos-arm64");
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe('attachment; filename="PrivacyFence-4.2.0.dmg"');
    expect(await response.text()).toBe("FAKE-DMG-BYTES-stable-4.2.0-macos-arm64");
  });

  it("still serves the latest version by its exact version number too", async () => {
    const response = await call("/download/version/4.3.0/macos-arm64");
    expect(response.status).toBe(200);
  });

  it("404s -- safely, not a crash -- for a version that was never published", async () => {
    const response = await call("/download/version/9.9.9/macos-arm64");
    expect(response.status).toBe(404);
  });

  it("404s -- safely -- for a string that isn't a real version", async () => {
    const response = await call("/download/version/not-a-version/macos-arm64");
    expect(response.status).toBe(404);
  });
});

describe("HEAD requests", () => {
  it("returns metadata with no body and the expected filename", async () => {
    const response = await call("/download/stable/macos-arm64", { method: "HEAD" });
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toBe('attachment; filename="PrivacyFence-4.3.0.dmg"');
    expect(response.headers.get("Content-Length")).toBe("39");
    expect(await response.text()).toBe("");
  });
});

describe("download counting", () => {
  it("a full GET increments the counter; a HEAD does not", async () => {
    const before = await statsTotal();

    await call("/download/stable/macos-arm64");
    expect(await statsTotal()).toBe(before + 1);

    await call("/download/stable/macos-arm64", { method: "HEAD" });
    expect(await statsTotal()).toBe(before + 1);
  });

  it("Range: bytes=0-... counts; Range: bytes=<N>- (N>0) does not", async () => {
    const before = await statsTotal();

    await call("/download/stable/macos-arm64", { headers: { Range: "bytes=0-9" } });
    expect(await statsTotal()).toBe(before + 1);

    await call("/download/stable/macos-arm64", { headers: { Range: "bytes=10-" } });
    expect(await statsTotal()).toBe(before + 1);
  });

  it("a 404 (missing artifact) never increments the counter", async () => {
    const before = await statsTotal();
    const response = await call("/download/stable/does-not-exist");
    expect(response.status).toBe(404);
    expect(await statsTotal()).toBe(before);
  });

  it("the metadata and stats APIs never increment the counter", async () => {
    const before = await statsTotal();
    await call("/api/releases");
    await call("/api/releases/stable");
    await call("/api/stats/downloads");
    expect(await statsTotal()).toBe(before);
  });
});

describe("range responses", () => {
  it("returns 206 with a matching Content-Range for a partial request", async () => {
    const response = await call("/download/stable/macos-arm64", { headers: { Range: "bytes=0-9" } });
    expect(response.status).toBe(206);
    expect(response.headers.get("Content-Range")).toBe("bytes 0-9/39");
    expect(response.headers.get("Content-Length")).toBe("10");
    expect(await response.text()).toBe("FAKE-DMG-B");
  });
});

describe("GET /api/releases", () => {
  it("reports the latest manifest for every channel", async () => {
    const response = await call("/api/releases");
    expect(response.status).toBe(200);
    const body = (await response.json()) as { channels: Record<string, { version: string } | null> };
    expect(body.channels.stable?.version).toBe("4.3.0");
    expect(body.channels.beta?.version).toBe("4.4.0b1");
    expect(body.channels.alpha?.version).toBe("4.4.0a1");
    expect(body.channels.rc?.version).toBe("4.4.0rc1");
  });
});

describe("GET /api/releases/:channel", () => {
  it("returns that channel's manifest", async () => {
    const response = await call("/api/releases/beta");
    expect(response.status).toBe(200);
    expect((await response.json()) as { version: string }).toMatchObject({ version: "4.4.0b1", channel: "beta" });
  });

  it("404s for an unknown channel", async () => {
    expect((await call("/api/releases/nightly")).status).toBe(404);
  });
});

describe("GET /api/stats/downloads", () => {
  it("returns an aggregate shape even with no downloads yet in this test's isolated storage", async () => {
    const response = await call("/api/stats/downloads");
    expect(response.status).toBe(200);
    const body = (await response.json()) as { total: number; by_channel: object; by_platform: unknown[] };
    expect(typeof body.total).toBe("number");
    expect(typeof body.by_channel).toBe("object");
    expect(Array.isArray(body.by_platform)).toBe(true);
  });

  it("returns 503, never a fake zero, when D1 itself is broken", async () => {
    const realDb = env.DB;
    // @ts-expect-error -- intentionally swapping in a broken stand-in for this one test
    env.DB = {
      prepare() {
        throw new Error("D1 is down");
      },
    };
    try {
      const response = await call("/api/stats/downloads");
      expect(response.status).toBe(503);
    } finally {
      env.DB = realDb;
    }
  });
});

describe("CORS", () => {
  it("allows privacyfence.eu on /api/* routes", async () => {
    const response = await call("/api/releases", { headers: { Origin: "https://privacyfence.eu" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("https://privacyfence.eu");
  });

  it("does not reflect an unrecognized origin", async () => {
    const response = await call("/api/releases", { headers: { Origin: "https://evil.example" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("never sets CORS headers on /download/* routes", async () => {
    const response = await call("/download/stable/macos-arm64", { headers: { Origin: "https://privacyfence.eu" } });
    expect(response.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });
});

describe("method handling", () => {
  it("rejects POST to a download route", async () => {
    const response = await call("/download/stable/macos-arm64", { method: "POST" });
    expect(response.status).toBe(405);
  });

  it("rejects POST to an API route", async () => {
    const response = await call("/api/releases", { method: "POST" });
    expect(response.status).toBe(405);
  });

  it("404s an unknown top-level route", async () => {
    expect((await call("/nonsense")).status).toBe(404);
  });
});
