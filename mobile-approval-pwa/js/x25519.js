// X25519 (RFC 7748) key agreement, implemented directly rather than via
// Web Crypto's SubtleCrypto.
//
// Why hand-rolled: issue #55's pairing protocol (see
// src/privacyfence/mobile_relay_pairing.py) uses X25519 to match the
// architecture the issue itself specifies ("daemon and phone each generate
// an X25519 keypair"). Chrome added native X25519 support to
// crypto.subtle relatively recently; Safari/WebKit -- the actual target
// platform for this feature (issue #55 is explicitly about iPhone/iPad --
// has no native X25519 in SubtleCrypto as of this writing. Switching the
// daemon side to a browser-native curve (e.g. P-256) instead would break
// interop with the already-shipped Phase 2 Python code for no real gain,
// and vendoring a third-party JS crypto library conflicts with this PWA's
// "vanilla, no build step" design (see README.md). X25519 is a small,
// precisely-specified algorithm (RFC 7748) -- implementing it directly
// with BigInt (native 255-bit-clean arithmetic, none of the classic 32-bit-
// limb bignum complexity the original C reference implementation needs)
// is the least-risk way to get real interop on the real target platform.
//
// Correctness: verified against Python's `cryptography` library (the same
// one src/privacyfence/mobile_relay_pairing.py uses) two ways -- scalar
// multiplication by the base point (key generation) and a full two-party
// ECDH exchange, both producing byte-identical results on both sides. See
// tests/test_x25519_interop.py for the automated version of that check;
// it's what this file's correctness claim actually rests on.

"use strict";

const P = (1n << 255n) - 19n;
const A24 = 121665n;

function mod(a, m) {
  const r = a % m;
  return r >= 0n ? r : r + m;
}

function decodeLittleEndian(bytes) {
  let n = 0n;
  for (let i = bytes.length - 1; i >= 0; i--) n = (n << 8n) | BigInt(bytes[i]);
  return n;
}

function encodeLittleEndian(n, len) {
  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = Number(n & 0xffn);
    n >>= 8n;
  }
  return out;
}

function decodeUCoordinate(bytes) {
  const clamped = Uint8Array.from(bytes);
  clamped[31] &= 0x7f; // RFC 7748: mask the MSB when decoding a u-coordinate
  return decodeLittleEndian(clamped);
}

function decodeScalar(bytes) {
  const k = Uint8Array.from(bytes);
  k[0] &= 248;
  k[31] &= 127;
  k[31] |= 64;
  return decodeLittleEndian(k);
}

function powmod(base, exp, m) {
  let result = 1n;
  base = mod(base, m);
  while (exp > 0n) {
    if (exp & 1n) result = mod(result * base, m);
    exp >>= 1n;
    base = mod(base * base, m);
  }
  return result;
}

function inv(x) {
  return powmod(x, P - 2n, P);
}

/** RFC 7748's Montgomery-ladder X25519 function, operating on raw BigInts. */
function x25519Raw(k, u) {
  let x1 = u, x2 = 1n, z2 = 0n, x3 = u, z3 = 1n, swap = 0n;
  for (let t = 254; t >= 0; t--) {
    const kt = (k >> BigInt(t)) & 1n;
    swap ^= kt;
    if (swap) {
      [x2, x3] = [x3, x2];
      [z2, z3] = [z3, z2];
    }
    swap = kt;

    const A = mod(x2 + z2, P);
    const AA = mod(A * A, P);
    const B = mod(x2 - z2, P);
    const BB = mod(B * B, P);
    const E = mod(AA - BB, P);
    const C = mod(x3 + z3, P);
    const D = mod(x3 - z3, P);
    const DA = mod(D * A, P);
    const CB = mod(C * B, P);
    x3 = mod((DA + CB) * (DA + CB), P);
    z3 = mod(x1 * mod((DA - CB) * (DA - CB), P), P);
    x2 = mod(AA * BB, P);
    z2 = mod(E * mod(AA + A24 * E, P), P);
  }
  if (swap) {
    [x2, x3] = [x3, x2];
    [z2, z3] = [z3, z2];
  }
  return mod(x2 * inv(z2), P);
}

/** X25519(scalarBytes, uBytes) -- both 32-byte Uint8Arrays, little-endian
 * per RFC 7748. Returns a 32-byte Uint8Array. */
function x25519(scalarBytes, uBytes) {
  const k = decodeScalar(scalarBytes);
  const u = decodeUCoordinate(uBytes);
  return encodeLittleEndian(x25519Raw(k, u), 32);
}

/** Scalar multiplication by the standard base point (u=9) -- i.e. deriving
 * a public key from a private scalar. */
function scalarMultBase(scalarBytes) {
  const base = new Uint8Array(32);
  base[0] = 9;
  return x25519(scalarBytes, base);
}

/** Generate a fresh X25519 keypair. Returns {privateKey, publicKey}, both
 * 32-byte Uint8Arrays. The private key is clamped implicitly by x25519()/
 * scalarMultBase() on use -- callers don't need to clamp it themselves. */
function generateKeyPair() {
  const privateKey = crypto.getRandomValues(new Uint8Array(32));
  const publicKey = scalarMultBase(privateKey);
  return { privateKey, publicKey };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { x25519, scalarMultBase, generateKeyPair };
}
