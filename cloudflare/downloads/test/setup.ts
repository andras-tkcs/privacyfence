/**
 * Runs once before the suite and seeds R2/D1 with the hand-written fixtures under
 * test/fixtures/ (docs/release-publishing-kpi-plan.md Phase 1: "Since no real manifest exists
 * yet, use hand-written fixture manifest.json/latest.json files in the test harness only").
 *
 * `isolatedStorage` (the vitest-pool-workers default) snapshots storage after setupFiles run and
 * resets every test back to that snapshot, so seeding once here is enough -- no test can see
 * another test's D1 counter writes or R2 mutations.
 */
import { applyD1Migrations, env } from "cloudflare:test";

import stableLatest from "./fixtures/stable/latest.json";
import stableManifest from "./fixtures/stable/manifest.json";
import stableOldManifest from "./fixtures/stable/old-manifest.json";
import betaLatest from "./fixtures/beta/latest.json";
import betaManifest from "./fixtures/beta/manifest.json";
import alphaLatest from "./fixtures/alpha/latest.json";
import alphaManifest from "./fixtures/alpha/manifest.json";
import rcLatest from "./fixtures/rc/latest.json";
import rcManifest from "./fixtures/rc/manifest.json";

await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);

// Fake installer bytes -- never real binaries, just distinct content whose length/sha256 match
// the "size"/"sha256" fields the fixture manifests above declare for the same artifact.
await Promise.all([
  env.RELEASES.put("releases/stable/latest.json", JSON.stringify(stableLatest)),
  env.RELEASES.put("releases/stable/4.3.0/manifest.json", JSON.stringify(stableManifest)),
  env.RELEASES.put("releases/stable/4.3.0/PrivacyFence-4.3.0.dmg", "FAKE-DMG-BYTES-stable-4.3.0-macos-arm64"),
  env.RELEASES.put(
    "releases/stable/4.3.0/PrivacyFence-4.3.0-setup.exe",
    "FAKE-EXE-BYTES-stable-4.3.0-windows-x64",
  ),
  env.RELEASES.put("releases/stable/4.3.0/privacyfence_4.3.0_amd64.deb", "FAKE-DEB-BYTES-stable-4.3.0-linux-x64"),

  // An older, no-longer-latest stable version -- still reachable via /download/version/....
  env.RELEASES.put("releases/stable/4.2.0/manifest.json", JSON.stringify(stableOldManifest)),
  env.RELEASES.put("releases/stable/4.2.0/PrivacyFence-4.2.0.dmg", "FAKE-DMG-BYTES-stable-4.2.0-macos-arm64"),

  env.RELEASES.put("releases/beta/latest.json", JSON.stringify(betaLatest)),
  env.RELEASES.put("releases/beta/4.4.0b1/manifest.json", JSON.stringify(betaManifest)),
  env.RELEASES.put("releases/beta/4.4.0b1/PrivacyFence-4.4.0b1.dmg", "FAKE-DMG-BYTES-beta-4.4.0b1-macos-arm64"),

  env.RELEASES.put("releases/alpha/latest.json", JSON.stringify(alphaLatest)),
  env.RELEASES.put("releases/alpha/4.4.0a1/manifest.json", JSON.stringify(alphaManifest)),
  env.RELEASES.put("releases/alpha/4.4.0a1/PrivacyFence-4.4.0a1.dmg", "FAKE-DMG-BYTES-alpha-4.4.0a1-macos-arm64"),

  env.RELEASES.put("releases/rc/latest.json", JSON.stringify(rcLatest)),
  env.RELEASES.put("releases/rc/4.4.0rc1/manifest.json", JSON.stringify(rcManifest)),
  env.RELEASES.put("releases/rc/4.4.0rc1/PrivacyFence-4.4.0rc1.dmg", "FAKE-DMG-BYTES-rc-4.4.0rc1-macos-arm64"),
  // channel "alpha" deliberately has no releases/alpha/9.9.9/... version-pinned fixture --
  // used by tests to prove a missing manifest is a safe 404, not a crash.
]);
