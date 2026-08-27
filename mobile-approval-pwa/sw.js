// Service worker: app-shell caching, plus bundle-release verification
// (issue #55, Phase 3's "signed/pinned bundle-release mechanism -- not
// optional"). See js/release_verify.js's own docstring for the full trust
// model; this file is the lifecycle glue around that verification logic.
//
// Flow:
// - app.js, right after a successful pairing, postMessage()s this worker
//   the org's pwa_release_public_key (from the pairing payload -- see
//   mobile_relay_pairing.py's PairingSession.pwa_release_public_key). This
//   worker pins it in IndexedDB and immediately verifies the currently-
//   published bundle_manifest.json/.sig against it.
// - On every `activate` (i.e. whenever the browser considers installing an
//   updated copy of this worker -- a reasonable proxy for "the app was
//   updated"), re-run that same verification using whatever key is
//   already pinned, if any.
// - Verification failure never blocks the page that's already running
//   (a service worker can't retroactively un-load a page) -- it posts a
//   "bundle-verification-failed" message to every open client, which
//   app.js surfaces as a visible warning. This is the real, hard-earned
//   limit noted in release_verify.js's docstring, not something this file
//   works around.
// - Before any device has paired (no key pinned yet), there's nothing
//   sensitive to protect -- verification is skipped, not failed.

"use strict";

importScripts("./js/release_verify.js");

const DB_NAME = "privacyfence-mobile-approval-trust";
const STORE_NAME = "trust";
const KEY_RECORD_ID = "pwaReleasePublicKey";
const CACHE_NAME = "privacyfence-mobile-approval-v1";

function openTrustDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(STORE_NAME);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function getPinnedReleaseKey() {
  const db = await openTrustDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readonly");
    const req = tx.objectStore(STORE_NAME).get(KEY_RECORD_ID);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

async function setPinnedReleaseKey(publicKeyB64) {
  const db = await openTrustDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    tx.objectStore(STORE_NAME).put(publicKeyB64, KEY_RECORD_ID);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function notifyClients(message) {
  const clients = await self.clients.matchAll();
  for (const client of clients) client.postMessage(message);
}

/** Fetches bundle_manifest.json/.sig and every file it lists, verifies the
 * signature and each file's hash, and caches only files that pass. Returns
 * a result object the page can display. Never throws -- a network or
 * verification failure is a normal, reportable outcome here, not a crash. */
async function verifyAndCacheBundle() {
  const pinnedKey = await getPinnedReleaseKey();
  if (!pinnedKey) {
    return { status: "skipped", reason: "No release key pinned yet (pair a device first)." };
  }

  let manifest, signature;
  try {
    const manifestResponse = await fetch("./bundle_manifest.json", { cache: "no-store" });
    const sigResponse = await fetch("./bundle_manifest.sig", { cache: "no-store" });
    if (!manifestResponse.ok || !sigResponse.ok) {
      return { status: "failed", reason: "Could not fetch bundle_manifest.json/.sig." };
    }
    manifest = await manifestResponse.json();
    signature = (await sigResponse.text()).trim();
  } catch (err) {
    return { status: "failed", reason: `Network error fetching the manifest: ${err.message}` };
  }

  const signatureValid = await verifyManifestSignature(manifest, signature, pinnedKey);
  if (!signatureValid) {
    return { status: "failed", reason: "Bundle manifest signature does not verify against the pinned key." };
  }

  const cache = await caches.open(CACHE_NAME);
  for (const [path, expectedDigest] of Object.entries(manifest.files || {})) {
    try {
      const fileResponse = await fetch(`./${path}`, { cache: "no-store" });
      if (!fileResponse.ok) return { status: "failed", reason: `Could not fetch ${path}.` };
      const bytes = new Uint8Array(await fileResponse.clone().arrayBuffer());
      const digestOk = await verifyFileDigest(bytes, expectedDigest);
      if (!digestOk) return { status: "failed", reason: `${path} does not match the signed manifest -- possible tampering.` };
      await cache.put(`./${path}`, fileResponse);
    } catch (err) {
      return { status: "failed", reason: `Error verifying ${path}: ${err.message}` };
    }
  }

  return { status: "verified", version: manifest.version, fileCount: Object.keys(manifest.files || {}).length };
}

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    self.clients.claim().then(async () => {
      const result = await verifyAndCacheBundle();
      await notifyClients({ type: "bundle-verification-result", result });
    }),
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "pin-release-key") {
    event.waitUntil(
      setPinnedReleaseKey(event.data.publicKeyB64)
        .then(verifyAndCacheBundle)
        .then((result) => notifyClients({ type: "bundle-verification-result", result })),
    );
  }
});

self.addEventListener("fetch", (event) => {
  // Cache-first for anything this worker has already verified and cached;
  // otherwise fall straight through to the network. Never serve stale
  // cached content for the mailbox API itself (relay traffic always goes
  // straight to the network -- caching an approval request/decision would
  // be actively wrong).
  const url = new URL(event.request.url);
  if (url.pathname.includes("/mailbox/") || url.pathname === "/pair") return;

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request)),
  );
});
