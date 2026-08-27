// WebCrypto-based helpers mirroring src/privacyfence/mobile_relay_client.py's
// encrypt_payload/decrypt_payload/compute_auth_tag/verify_auth_tag exactly,
// plus HKDF (used by pairing.js, mirroring mobile_relay_pairing.py's
// _hkdf/derive_shared_key/_handshake_key). Every function here has been
// cross-checked against the Python implementation for byte-identical
// output on the same input -- see tests/test_crypto_interop.py.
//
// HKDF, AES-GCM, and HMAC-SHA256 are all native SubtleCrypto algorithms,
// supported the same way in every target browser (including Safari/
// WebKit) -- unlike X25519 (see x25519.js's own docstring for why that one
// needed a hand-rolled implementation instead).

"use strict";

const GCM_NONCE_SIZE = 12;

function toBase64(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function fromBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function concatBytes(...arrays) {
  const total = arrays.reduce((n, a) => n + a.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const a of arrays) {
    out.set(a, offset);
    offset += a.length;
  }
  return out;
}

/** HKDF-SHA256(inputKeyMaterial, info) -> 32 raw bytes. Matches
 * mobile_relay_pairing.py's _hkdf() (HKDF(algorithm=SHA256(), length=32,
 * salt=None, info=info)) -- an empty salt here is mathematically
 * equivalent to Python's salt=None (both zero-pad to the same HMAC key),
 * verified empirically in tests/test_crypto_interop.py rather than just
 * assumed. `info` is a plain JS string, UTF-8 encoded here to match
 * Python's own `info` byte strings. */
async function hkdfSha256(inputKeyMaterial, info) {
  const key = await crypto.subtle.importKey("raw", inputKeyMaterial, "HKDF", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: new TextEncoder().encode(info) },
    key,
    256,
  );
  return new Uint8Array(bits);
}

/** AES-256-GCM encrypt `payload` (any JSON-serializable value) under `key`
 * (32 raw bytes). Returns base64(nonce || ciphertext || tag) -- exactly
 * mobile_relay_client.py's encrypt_payload() wire format. */
async function encryptPayload(key, payload) {
  const cryptoKey = await crypto.subtle.importKey("raw", key, "AES-GCM", false, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(GCM_NONCE_SIZE));
  const plaintext = new TextEncoder().encode(JSON.stringify(payload));
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, cryptoKey, plaintext));
  return toBase64(concatBytes(nonce, ciphertext));
}

/** Inverse of encryptPayload()/mobile_relay_client.py's decrypt_payload().
 * Throws (SubtleCrypto's own error) on a wrong key or tampered ciphertext
 * -- callers decide how to treat that, same as the Python side. */
async function decryptPayload(key, ciphertextB64) {
  const cryptoKey = await crypto.subtle.importKey("raw", key, "AES-GCM", false, ["decrypt"]);
  const blob = fromBase64(ciphertextB64);
  const nonce = blob.subarray(0, GCM_NONCE_SIZE);
  const ciphertext = blob.subarray(GCM_NONCE_SIZE);
  const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce }, cryptoKey, ciphertext);
  return JSON.parse(new TextDecoder().decode(plaintext));
}

/** HMAC-SHA256(key, "request_id:decision"), base64 -- matches
 * mobile_relay_client.py's compute_auth_tag() exactly. This is what proves
 * a decision came from the device holding `key` and is bound to this
 * specific request_id (see that function's own docstring for what it
 * does and doesn't prove). */
async function computeAuthTag(key, requestId, decision) {
  const cryptoKey = await crypto.subtle.importKey(
    "raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const message = new TextEncoder().encode(`${requestId}:${decision}`);
  const tag = await crypto.subtle.sign("HMAC", cryptoKey, message);
  return toBase64(new Uint8Array(tag));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { hkdfSha256, encryptPayload, decryptPayload, computeAuthTag, toBase64, fromBase64 };
}
