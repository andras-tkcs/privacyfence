// Verifies a mobile-approval-pwa bundle release's signature and per-file
// integrity (issue #55, Phase 3's "signed/pinned bundle-release mechanism
// -- not optional"). Used by sw.js.
//
// The threat this defends against: whoever hosts the PWA's static files
// (which may not be the same, more-carefully-operated infrastructure as
// the relay -- see scripts/sign_pwa_bundle.py's own docstring) is
// compromised or coerced into serving a modified index.html/app.js that,
// say, exfiltrates a phone's derived shared_key or silently approves
// every request. Content served over HTTPS is protected against a
// network attacker, but not against a compromised *origin* -- that's what
// signing the bundle with a key the hosting infrastructure never holds is
// for.
//
// Trust model, stated plainly:
// - scripts/generate_pwa_release_key.py's private key never touches the
//   hosting infrastructure -- only scripts/sign_pwa_bundle.py (run by
//   whoever cuts a release, offline or on a separate trusted machine) ever
//   uses it.
// - The corresponding public key is *pinned at pairing time*
//   (mobile_relay_pairing.py's PairingSession.pwa_release_public_key,
//   carried in the same QR payload as the mailbox credentials) rather than
//   trusted from whatever the phone's first page load happened to fetch.
//   Pairing already requires physical proximity to the paired Mac (per
//   issue #55's own architecture) -- reusing that same trusted moment to
//   also pin the release key means the bundle's trust anchor never depends
//   on trusting the PWA's own hosting on first contact.
// - Every subsequent bundle_manifest.json/.sig fetch is checked against
//   that pinned key, not a value baked into the bundle itself -- a
//   compromised bundle can't simply ship its own "trusted" key.
//
// What this deliberately does NOT cover: index.html itself (the entry
// point a browser navigates to directly can't be manifest-verified before
// it's already loaded and running -- see sign_pwa_bundle.py's
// EXCLUDED_NAMES) and the service worker script itself (sw.js's own
// updates go through the browser's native SW update mechanism, which this
// scheme doesn't -- can't -- add signature verification on top of, since
// sw.js is the thing doing the verifying). Both are accepted TOFU
// boundaries of the web platform, not gaps specific to this scheme -- see
// README.md's "Known gaps" for the fuller picture.

"use strict";

/** Byte-for-byte equivalent of Python's
 * `json.dumps(obj, sort_keys=True, separators=(",", ":"))` for the plain
 * JSON shapes this manifest actually contains (nested objects of strings/
 * numbers, no floats needing locale-sensitive formatting) -- verified
 * against real Python output in tests/test_release_verify.py rather than
 * assumed. JSON.stringify's own key ordering follows insertion order, not
 * Python's sorted order, so this reimplements serialization rather than
 * just sorting keys before calling JSON.stringify. */
function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  const parts = keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`);
  return `{${parts.join(",")}}`;
}

function canonicalManifestBytes(manifest) {
  return new TextEncoder().encode(canonicalJson(manifest));
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function fromBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** True iff `signatureB64` (raw r||s ECDSA-P256-SHA256, base64) is a valid
 * signature over `manifest`'s canonical bytes under `publicKeyRawB64`
 * (raw uncompressed-point X9.62 encoding, base64 -- what
 * generate_pwa_release_key.py produces). */
async function verifyManifestSignature(manifest, signatureB64, publicKeyRawB64) {
  const publicKey = await crypto.subtle.importKey(
    "raw", fromBase64(publicKeyRawB64), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"],
  );
  return crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" }, publicKey, fromBase64(signatureB64), canonicalManifestBytes(manifest),
  );
}

/** True iff `fileBytes` hashes to `expectedHexDigest` (as recorded in the
 * manifest for this file). */
async function verifyFileDigest(fileBytes, expectedHexDigest) {
  return (await sha256Hex(fileBytes)) === expectedHexDigest;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { canonicalJson, canonicalManifestBytes, verifyManifestSignature, verifyFileDigest, sha256Hex };
}
