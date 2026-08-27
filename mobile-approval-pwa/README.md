# Mobile approval PWA -- Phase 3 (issue #55)

This is the real, shipped approval-inbox PWA -- unlike
[`spikes/mobile-relay-phase0/pwa/`](../spikes/mobile-relay-phase0/), which was a throwaway
prototype never pointed at a real deployment. This one is what a paired phone actually installs
and runs against a real daemon, over a real relay, per
[issue #55](https://github.com/andras-tkcs/privacyfence/issues/55)'s phasing:

- **Phase 1** wired the daemon in as a pure outbound mailbox client (`ApprovalUI` seam).
- **Phase 2** replaced Phase 1's single pre-shared key with real X25519 pairing, multi-device
  support, revocation, and key rotation (`src/privacyfence/mobile_relay_pairing.py`).
- **Phase 3 (this directory)** is the actual phone-side app: real pairing, real approval
  round-trips, and the signed/pinned bundle-release mechanism issue #55 calls out as
  "not optional" -- a phone must be able to tell a genuine PrivacyFence update from a
  tampered-with or substituted one, since it's the one piece of this system a network attacker
  who compromises the PWA's host could otherwise abuse for free.

Deliberately no build step: plain HTML/CSS/JS, loaded via `<script>` tags in dependency order
(`js/x25519.js` -> `js/crypto.js` -> `js/pairing.js` -> `js/release_verify.js` -> `js/app.js`),
no bundler, no npm dependency, no transpilation. A phone fetches exactly the files in this
directory.

## Layout

```
index.html            Three-screen UI (pair / waiting / pending-approval), dark theme
manifest.json          Standard PWA manifest (installable to a home screen)
sw.js                  Service worker: app-shell caching + bundle-release verification
js/x25519.js           Pure-JS RFC 7748 X25519 (BigInt Montgomery ladder)
js/crypto.js           WebCrypto wrappers: HKDF-SHA256, AES-256-GCM, HMAC-SHA256
js/pairing.js          Pairing handshake (the phone's half of mobile_relay_pairing.py)
js/release_verify.js   Canonical-JSON manifest verification (the phone's half of sign_pwa_bundle.py)
js/app.js              Main app: connection persistence, long-poll, render, approve/deny
tests/                 Real end-to-end Playwright tests (browser + real daemon-side Python)
```

## Why X25519 is hand-rolled instead of using `crypto.subtle`

`crypto.subtle` has no X25519 support in Safari/WebKit -- the actual target platform here (an
iPhone/iPad, per issue #55's own framing) -- so `js/x25519.js` implements RFC 7748 directly with
BigInt. It's verified byte-identical to Python's `cryptography` library for both scalar-mult-by-base
and full ECDH (see `tests/test_pairing_and_approval_e2e.py`'s real pairing tests, which exercise it
against the real `mobile_relay_pairing.py` code, not a mock). HKDF, AES-GCM, and HMAC are all
natively supported via `crypto.subtle` in every target browser, so `js/crypto.js` just wraps those
directly rather than reimplementing them too.

## Pairing

No settings-window UI or QR-code scanning exists yet (see "Not yet built" below) -- pairing is
copy-paste:

1. On the Mac: `privacyfence-app --pair-mobile-device` prints a JSON pairing payload and waits.
2. On the phone: open the PWA, paste that JSON into the pairing screen, give the device a name,
   tap Pair.
3. The phone generates a fresh X25519 keypair, derives a one-time handshake key from the payload's
   `pairing_secret`, and posts its public key + device name to the mailbox under it. The daemon
   derives the real per-device `shared_key` via ECDH + HKDF and posts back a signed
   acknowledgement. The phone only trusts the pairing once that ack's auth tag verifies --
   see `js/pairing.js`'s docstring for exactly what that buys and why an unverified ack must never
   be treated as a successful pairing.

## Signed/pinned bundle releases

The pairing payload also carries the organization's PWA release **public** key
(`pwa_release_public_key`, from `org_config.json`'s `mobile_relay.pwa_release_public_key_base64` --
see `scripts/build_org_bundle.py --pwa-release-public-key`), so trust in that key is bootstrapped at
the same physically-authenticated moment as the mailbox pairing itself, never from the PWA's own
first page load (which a network attacker controlling the PWA's host could otherwise forge).

Right after a successful pairing, `js/app.js` hands that key to the service worker
(`sw.js`), which pins it in IndexedDB (not `localStorage` -- a service worker needs direct,
synchronous-feeling access without going through the page) and immediately verifies the currently
published bundle against it. From then on, every time the browser considers installing an updated
service worker (a reasonable proxy for "the app was updated"), that same verification re-runs
against whatever key is already pinned.

**Releasing a signed update:**

```sh
# Once per organization, offline, on a machine you trust -- this is a real code-signing key:
python3 scripts/generate_pwa_release_key.py -o pwa_release_key.json
# -> keep pwa_release_key.json itself (private key included) offline, out of version control
# -> pass its public_key_base64 to scripts/build_org_bundle.py --pwa-release-public-key

# Every time you publish a new build of this directory to wherever your org hosts it:
python3 scripts/sign_pwa_bundle.py --release-key pwa_release_key.json \
    --bundle-dir mobile-approval-pwa --bundle-version 2026.08.27
# -> writes bundle_manifest.json + bundle_manifest.sig into the bundle, alongside everything else
```

`sign_pwa_bundle.py` hashes every file in the bundle (except `index.html`, and its own two output
files) into a manifest and signs the manifest's canonical JSON bytes with ECDSA-P256-SHA256 --
P-256, not the X25519/Ed25519 the rest of issue #55's crypto uses, because it's the curve
`crypto.subtle` supports reliably in every target browser including Safari. `js/release_verify.js`
re-serializes that same manifest with a hand-written `canonicalJson()` (byte-identical to Python's
`json.dumps(..., sort_keys=True, separators=(",", ":"))`, since `JSON.stringify` doesn't sort keys)
before checking the signature -- the two sides must agree on the exact bytes that were signed.

**Accepted trust-on-first-use (TOFU) boundaries, not gaps:**
- `index.html` and the service worker's own bytes can't be manifest-verified -- the verifier can't
  verify itself before it's running. This is the same limit any client-side integrity check has
  (it's why native app stores, not the app itself, are the actual trust anchor for a native app).
- Before any device has paired, there's nothing sensitive to protect yet, so verification reports
  `"skipped"`, not `"verified"` -- an unearned trust claim would be worse than an honest one.
- A verification failure can't retroactively un-load a page that's already running; it surfaces as
  a visible warning banner (`#bundle-warning`) telling the user not to trust this app instance.

## Not yet built

- **QR-code pairing.** The daemon's `--pair-mobile-device` payload is pasted as raw JSON today,
  not scanned from a QR code -- no camera access, no QR encode/decode in either direction.
- **A settings-window pairing/device-list UI.** Enrollment and revocation are both CLI-only
  (`--pair-mobile-device`, `--list-mobile-devices`, `--revoke-mobile-device`).
- **Push-based wake.** The phone long-polls the mailbox (25s waits); there's no APNs/Web Push
  integration, so battery/network behavior is whatever long-polling costs, not zero.
- **Full content parity.** Approval is text-only -- `preview_bytes`/`pdf_bytes` (image/PDF embeds)
  and `preview_tables`/`preview_blocks` (structured previews) don't cross the relay yet, matching
  the same documented gap in Phase 1/2's `MobileRelayApprovalUI`.
- **A native iOS app wrapper.** This is a PWA (installable to the home screen, no App Store
  distribution) -- issue #55 lists a native wrapper as a later, optional phase.
- **Early popup close-out.** If the phone answers first, a native desktop popup that's already
  open doesn't close itself -- see the "Known gaps" list in
  [`docs/TECHNICAL_REFERENCE.md`](../docs/TECHNICAL_REFERENCE.md#mobile-remote-approval-issue-55--experimental)
  for the full, current list (shared with Phase 1/2, since it's the same daemon-side approval
  loop underneath).

## Testing

`tests/test_pairing_and_approval_e2e.py` is real end-to-end verification, not mocks on either
side: a real headless Chromium (via Playwright) runs this exact PWA's actual files, against a real
instance of the Phase 0 spike's relay server, driving a real pairing handshake and approval
round-trip with the real `src/privacyfence/mobile_relay_pairing.py` /
`mobile_relay_client.py` code on the daemon side of the wire. It also proves the actual security
property the signing mechanism exists for: a bundle file tampered with after signing is detected
and reported, not silently served.

```sh
python3 -m pytest mobile-approval-pwa/tests/test_pairing_and_approval_e2e.py -v
```

Requires Playwright with a Chromium binary available (see the repo's dev setup) -- the test file
`importorskip`s `playwright.sync_api` and skips cleanly if it isn't installed.

This project is macOS-only (`pyobjc`/AppKit) for everything under `src/privacyfence`, but this
test file only imports `mobile_relay_pairing.py` and `mobile_relay_client.py`, which don't touch
AppKit -- it runs on any platform Playwright supports.
