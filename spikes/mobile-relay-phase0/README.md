# Mobile relay approval spike -- Phase 0 (issue #55)

This is throwaway prototype code, not part of the shipped PrivacyFence app.
Nothing here is imported by, or wired into, `src/privacyfence` -- per
[issue #55](https://github.com/andras-tkcs/privacyfence/issues/55)'s own
phasing, Phase 0 has "no daemon involvement yet." It exists to answer one
question before any of that real integration work starts: **is the
relay + tunnel + PWA architecture actually workable end to end?**

Read the issue for the full architecture, threat model, and rejected
alternatives (MCP elicitation, a Telegram bot). The one-paragraph version:
a phone and the Mac running the PrivacyFence daemon are both pure clients of
an on-prem relay's encrypted mailbox, reachable over a pinned-key WireGuard
tunnel the relay's operator controls end to end -- so a gate's approval
prompt can be answered from a phone without any third party ever holding
plaintext of the request or the decision.

## What Phase 0 proves (and how it was verified)

1. **The mailbox API's pair -> wake -> decide loop works**, including the
   fail-closed and idempotent-decision invariants issue #55's comment
   thread calls out as easy to get wrong (`relay/relay_server.py`,
   proven by `tests/test_roundtrip.py` -- 12 tests, all passing).
2. **A bare, installable PWA can actually drive that loop from a browser**,
   including rendering the red-tinted PII banner requirement (#55's
   requirement 4) -- verified by scripting a real headless Chromium against
   a running relay instance (pairing, long-poll wake, rendering a fake
   PII-flagged request, tapping Approve, and the "daemon" side successfully
   retrieving the decision). That run also caught and fixed a real bug: the
   relay was re-surfacing an already-decided request to the phone's next
   poll as if it were newly pending (see `TestDecidedRequestStopsBeingPending`
   in `tests/test_roundtrip.py` for the regression test).
3. **WireGuard keys can be minted without any manual tooling install**
   (`wireguard/generate_keys.py`, pure Python, byte-compatible with
   `wg genkey`/`wg pubkey` output) and the config skeleton for a direct,
   port-forwarded, pinned-key tunnel is ready to drop onto a real box
   (`wireguard/*.conf.template`).

## What Phase 0 deliberately does not include

These are explicitly later phases in issue #55, not gaps in this one:

| Left out here                                              | Owned by |
|--------------------------------------------------------------|----------|
| Any real daemon wiring (`gate.py`, `approval_ui.py`)          | Phase 1  |
| End-to-end encryption of the mailbox payload (X25519)         | Phase 1/2 |
| Real pairing UX (QR code, multi-device, revocation, key rotation) | Phase 2  |
| The production PWA with a signed/pinned bundle release mechanism | Phase 3  |
| Real APNs/Web Push wake delivery (this spike wakes via long-poll, an explicitly endorsed fallback -- see `pwa/sw.js`'s comment) | later, if pursued |
| Deploying the relay onto dedicated, network-segmented hardware | Phase 4  |
| A native iOS app                                              | Phase 5 (optional) |

Nothing here should be mistaken for progress on those -- this spike answers
"is the shape of the thing workable," not "is it production-ready."

## Layout

```
relay/       Stdlib-only mailbox server + its own README/API table
pwa/         Bare HTML/JS/manifest/service-worker PWA + its own README
wireguard/   Key generation + config templates for a real deployment
tests/       End-to-end proof of the pair/wake/decide loop (not part of
             the main `pytest tests/` suite -- outside pyproject.toml's
             `testpaths`, on purpose, since this isn't `src/privacyfence`)
```

## Try it locally

```sh
# terminal 1
cd relay && python3 relay_server.py --host 127.0.0.1 --port 8765

# terminal 2
cd pwa && python3 -m http.server 8080
# open http://localhost:8080/index.html, pair against http://127.0.0.1:8765

# terminal 3 -- play "the daemon" by hand
curl -X POST "http://127.0.0.1:8765/mailbox/<mailbox_id>?token=<token>" \
  -H 'Content-Type: application/json' \
  -d '{"request_id": "r1", "payload": {"tool": "gmail.send_message", "pii_flagged": true, "preview": "To: alice@example.com"}}'
```

Approve or Deny in the browser, then confirm the decision from terminal 3:

```sh
curl "http://127.0.0.1:8765/mailbox/<mailbox_id>/decision?token=<token>&request_id=r1&wait=0"
```

To actually exercise the WireGuard leg (rather than plain loopback), follow
`wireguard/README.md` on a real throwaway box, then bind the relay to its
tunnel address and point the PWA/curl calls at that address instead of
`127.0.0.1`.

## Recommendation for what comes next

The architecture holds up in practice, not just on paper. The natural next
step is Phase 1 exactly as issue #55 scopes it: a `MobileRelayApprovalUI`
implementing the `ApprovalUI` seam in `src/privacyfence/approval_ui.py`,
raced against the existing `NativeApprovalUI` via
`asyncio.wait(..., return_when=FIRST_COMPLETED)`, reusing this relay's API
shape (it will need E2E encryption of the payload and real peer-identity
checking added on top, per the issue's requirements). This directory can
stay as a reference for that work, or be deleted once Phase 1 supersedes it
-- unlike the original design-doc branch, there's no urgency to delete it
immediately, since it's inert (untouched by `src/privacyfence`) either way.
