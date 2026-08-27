// Pairing handshake, the phone's half of src/privacyfence/mobile_relay_pairing.py's
// protocol (see that module's docstring for the full picture). Concretely:
//
// 1. The user pastes the pairing payload `--pair-mobile-device` printed
//    (see PAIRING_REQUEST_ID and the payload shape in that CLI flag's
//    output -- no QR-code scanning yet, see README.md's "Not yet built").
// 2. This module generates a fresh X25519 keypair, derives
//    `handshakeKey = HKDF(pairingSecret)` (same one-time, discard-after-use
//    key the daemon derives independently), and posts
//    `{device_public_key, device_name}` -- encrypted under handshakeKey --
//    to the mailbox as a request with the reserved ID PAIRING_REQUEST_ID.
// 3. It then polls for the daemon's decision-shaped acknowledgement,
//    authenticated with the *real* shared_key (derived via ECDH from this
//    device's own private key and the daemon's public key) -- verifying
//    that tag is what proves the daemon derived the same key this device
//    did, before trusting the channel for anything real.
//
// On success, returns the paired connection's config for app.js to persist
// and use for every subsequent approval: {relayUrl, mailboxId, token,
// sharedKey}. Never returns a device that failed the ack check -- pairing
// must fail loudly rather than silently proceed on a channel whose keys
// don't actually agree.

"use strict";

/* global require, module, generateKeyPair, x25519, hkdfSha256, encryptPayload, decryptPayload, computeAuthTag, toBase64, fromBase64 */
// In a browser, x25519.js/crypto.js are loaded first via plain <script>
// tags, so their top-level `function` declarations are already global --
// generateKeyPair, x25519, hkdfSha256, etc. below are used as bare
// identifiers with no import step needed. The block below only runs under
// Node's `require()` (unit tests), and assigns onto `globalThis` rather
// than re-declaring same-named `const`/`let` bindings -- redeclaring an
// identifier that already exists as a global `function` (as it does in the
// browser after x25519.js/crypto.js load) throws a real SyntaxError-shaped
// runtime error ("Identifier '...' has already been declared"), which is
// exactly the bug this shape avoids.
if (typeof require !== "undefined") {
  const x25519Module = require("./x25519.js");
  const cryptoModule = require("./crypto.js");
  Object.assign(globalThis, x25519Module, cryptoModule);
}

const PAIRING_REQUEST_ID = "__pairing__";
const HANDSHAKE_HKDF_INFO = "privacyfence-mobile-relay-pairing-handshake-v1";
const DEVICE_KEY_HKDF_INFO = "privacyfence-mobile-relay-device-key-v1";
const DEFAULT_LONG_POLL_SECONDS = 25;

class PairingError extends Error {}

/** X25519 ECDH + HKDF, exactly mirroring mobile_relay_pairing.py's
 * derive_shared_key() -- must produce byte-identical output given the
 * same (privateKey, peerPublicKey) pair on both sides, verified in
 * tests/test_pairing_e2e.py. */
async function deriveSharedKey(privateKey, peerPublicKey) {
  const ecdhSecret = x25519(privateKey, peerPublicKey);
  return hkdfSha256(ecdhSecret, DEVICE_KEY_HKDF_INFO);
}

async function postJson(url, params, body) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(`${url}?${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response;
}

async function getJson(url, params) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(`${url}?${query}`);
  return response;
}

/** Parses and validates a pairing payload pasted from
 * `privacyfence-app --pair-mobile-device`'s printed output. Throws
 * PairingError on anything malformed or missing a required field, rather
 * than proceeding with a partially-understood payload. */
function parsePairingPayload(rawText) {
  let payload;
  try {
    payload = JSON.parse(rawText);
  } catch (exc) {
    throw new PairingError(`Pairing payload isn't valid JSON: ${exc.message}`);
  }
  for (const field of ["relay_url", "mailbox_id", "token", "daemon_public_key", "pairing_secret", "expires_at"]) {
    if (!(field in payload)) throw new PairingError(`Pairing payload is missing "${field}"`);
  }
  if (payload.expires_at * 1000 < Date.now()) {
    throw new PairingError("This pairing payload has already expired -- ask for a fresh one.");
  }
  return payload;
}

/** Completes pairing against an already-parsed payload. `deviceName`
 * identifies this phone in the daemon's device list (`--list-mobile-
 * devices`) and any future "answered from mobile, by device X" audit
 * entry -- see audit_log.py's answered_via field.
 *
 * Returns {relayUrl, mailboxId, token, sharedKey} on success. Raises
 * PairingError on a network failure, a timeout waiting for the daemon's
 * ack, or (importantly) an ack whose auth tag doesn't verify -- the last
 * case means the daemon didn't derive the same shared_key this device
 * did, and the pairing must not be trusted.
 */
async function completePairing(payload, deviceName) {
  const relayUrl = payload.relay_url.replace(/\/$/, "");
  const daemonPublicKey = fromBase64(payload.daemon_public_key);
  const pairingSecret = fromBase64(payload.pairing_secret);

  const { privateKey, publicKey } = generateKeyPair();
  const handshakeKey = await hkdfSha256(pairingSecret, HANDSHAKE_HKDF_INFO);
  const handshakeCiphertext = await encryptPayload(handshakeKey, {
    device_public_key: toBase64(publicKey),
    device_name: deviceName,
  });

  const postResponse = await postJson(
    `${relayUrl}/mailbox/${payload.mailbox_id}`,
    { token: payload.token },
    { request_id: PAIRING_REQUEST_ID, payload: { ciphertext: handshakeCiphertext }, ttl_seconds: 60 },
  );
  if (postResponse.status !== 201) {
    throw new PairingError(`Relay rejected the pairing handshake (HTTP ${postResponse.status})`);
  }

  const sharedKey = await deriveSharedKey(privateKey, daemonPublicKey);

  const deadline = Date.now() + Math.max(0, payload.expires_at * 1000 - Date.now());
  while (Date.now() < deadline) {
    const waitSeconds = Math.max(0, Math.min(DEFAULT_LONG_POLL_SECONDS, (deadline - Date.now()) / 1000));
    const ackResponse = await getJson(
      `${relayUrl}/mailbox/${payload.mailbox_id}/decision`,
      { token: payload.token, request_id: PAIRING_REQUEST_ID, wait: waitSeconds },
    );
    if (ackResponse.status === 204) continue;
    if (ackResponse.status !== 200) {
      throw new PairingError(`Unexpected relay response waiting for the pairing ack (HTTP ${ackResponse.status})`);
    }
    const body = await ackResponse.json();
    const expectedTag = await computeAuthTag(sharedKey, PAIRING_REQUEST_ID, "approved");
    if (body.decision !== "approved" || body.auth !== expectedTag) {
      throw new PairingError(
        "Pairing acknowledgement failed to verify -- the daemon may not have derived the same key. " +
        "Do not trust this pairing; try again.",
      );
    }
    // sharedKey is raw bytes here, matching what loadConnection() produces
    // from storage (fromBase64(...)) -- callers must never need to know
    // which path a connection object came from before using it. See
    // app.js's saveConnection() for where the base64 encoding actually
    // happens, once, at the storage boundary.
    return {
      relayUrl, mailboxId: payload.mailbox_id, token: payload.token, sharedKey,
      pwaReleasePublicKeyB64: payload.pwa_release_public_key || null,
    };
  }
  throw new PairingError("Timed out waiting for the daemon's pairing acknowledgement.");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { PairingError, parsePairingPayload, completePairing, deriveSharedKey, PAIRING_REQUEST_ID };
}
