"""Outbound-only client for the mobile-approval relay's mailbox (issue #55).

Phase 0 (see spikes/mobile-relay-phase0/) proved the relay's pair/wake/decide
plumbing with an unencrypted, unauthenticated mailbox and no daemon
involvement. This module is the real daemon-side wire-protocol client:
every request `payload` this posts is AES-256-GCM encrypted before it ever
leaves the daemon (the relay only ever stores/forwards ciphertext -- issue
#55's requirement 3), and every decision this accepts must carry a valid
HMAC-SHA256 tag over `f"{request_id}:{decision}"` before it's trusted (peer
identity + replay protection -- see verify_auth_tag()'s own docstring for
exactly what that does and doesn't prove).

**What key, though?** Phase 1 shipped a single pre-shared symmetric key for
the whole mailbox -- documented there as "one paired phone... Phase 2's
job." Phase 2 (mobile_relay_pairing.py) replaced that with a real per-device
design: the daemon holds one long-term X25519 identity, each paired device
gets its own mailbox and its own derived shared_key (X25519 ECDH + HKDF),
and revoking one device can't affect any other. This module doesn't know or
care which scheme produced `MobileRelayConfig.shared_key` -- it's written
against "a mailbox and a 32-byte AES key," and mobile_relay_pairing.py is
what actually produces one of those per paired device. The only thing that
still lives in org_config.json (IT-provisioned, org-wide, no secrets) is
*where* the relay is (see relay_url_from_org_config() below). Everything
device-specific (mailbox_id, token, shared_key) is per-user local state
now, in mobile_relay_pairing.PairingStore, exactly like every other
connector's OAuth token file in daemon_main.py's TOKEN_FILES.

request_new_mailbox(), poll_for_request_raw(), and post_decision_raw() below
exist only for the pairing handshake (mobile_relay_pairing.py) -- everyday
approval traffic (MobileRelayApprovalUI) never calls them, since pairing
needs to play the *phone's* half of the wire protocol for its one bootstrap
message (receive a request, send a decision), the opposite direction from
post_request()/poll_decision() below.

This client never accepts an inbound connection -- every call here is a
plain outbound HTTPS request the daemon initiates, matching the
architecture's "the Mac must never accept an inbound connection beyond
localhost" invariant.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_AES_KEY_SIZE = 32          # AES-256
_GCM_NONCE_SIZE = 12        # standard AES-GCM nonce size
_DEFAULT_LONG_POLL_SECONDS = 25.0
_DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0


class MobileRelayClientError(Exception):
    """Raised for unrecoverable MobileRelayClient problems (config, network, HTTP-level).

    Deliberately NOT raised for an untrusted/unauthenticated decision --
    that's a normal, expected outcome of racing an attacker-controlled relay
    against a legitimate one, and is handled by poll_decision() returning
    None (fail closed), not by an exception. This mirrors every other
    *_client.py's own convention (see docs/coding-and-testing-guidelines.md
    §1.4) of reserving the dedicated error type for genuinely unrecoverable
    problems, not for an outcome the caller is expected to branch on.
    """


def encrypt_payload(key: bytes, payload: dict[str, Any]) -> str:
    """AES-256-GCM encrypt `payload` under `key`. Returns
    base64(nonce || ciphertext) -- the nonce travels alongside the
    ciphertext (standard AEAD practice; it isn't secret, only ever
    reused-must-not-be) and is regenerated fresh on every call via
    os.urandom, never derived or counter-based."""
    plaintext = json.dumps(payload, default=str).encode("utf-8")
    nonce = os.urandom(_GCM_NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_payload(key: bytes, ciphertext_b64: str) -> dict[str, Any]:
    """Inverse of encrypt_payload(). Raises ValueError/cryptography's own
    InvalidTag on a wrong key or corrupted/tampered ciphertext -- callers
    decide how to treat that (mobile_relay_pairing.py's pairing handshake
    treats it as an untrusted handshake attempt, not a crash)."""
    blob = base64.b64decode(ciphertext_b64)
    nonce, ciphertext = blob[:_GCM_NONCE_SIZE], blob[_GCM_NONCE_SIZE:]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


def compute_auth_tag(key: bytes, request_id: str, decision: str) -> str:
    digest = hmac.new(key, f"{request_id}:{decision}".encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_auth_tag(key: bytes, request_id: str, decision: str, auth: str) -> bool:
    """True iff `auth` is a valid HMAC-SHA256(key, request_id:decision) tag.

    Proves the sender holds `key` (peer identity -- in the per-device design,
    that's specifically the one device this shared_key was derived for) and
    that this specific (request_id, decision) pair was what they signed
    (replay protection: a tag captured for one request can't be replayed
    against a different one, since request_id is fresh and random per
    gated_call). It does NOT prove *when* the tag was made -- an old, valid
    tag for a request that's still pending would still verify. That's fine
    here specifically because request_id is single-use: the relay accepts
    only one decision per request_id, and this client's caller
    (MobileRelayApprovalUI, via gate.py's own request lifecycle) never
    reuses a request_id or applies a second decision for one already
    answered -- so there's no live request a stale-but-valid tag could be
    replayed onto.
    """
    if not auth:
        return False
    return hmac.compare_digest(compute_auth_tag(key, request_id, decision), auth)


def request_new_mailbox(relay_url: str, *, session: requests.Session | None = None) -> tuple[str, str]:
    """Mint a fresh mailbox on the relay (POST /pair). Used by
    mobile_relay_pairing.begin_pairing() to provision the mailbox a new
    device will be paired to -- everyday approval traffic reuses an
    already-paired device's existing mailbox instead of calling this.

    Raises MobileRelayClientError on any network/HTTP failure.
    """
    session = session or requests.Session()
    try:
        response = session.post(f"{relay_url.rstrip('/')}/pair", timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise MobileRelayClientError(f"Could not reach relay to mint a new mailbox: {exc}") from exc
    if response.status_code != 201:
        raise MobileRelayClientError(
            f"Relay rejected pairing request (HTTP {response.status_code}): {response.text[:200]!r}"
        )
    try:
        body = response.json()
        return body["mailbox_id"], body["token"]
    except (ValueError, KeyError) as exc:
        raise MobileRelayClientError(f"Relay's /pair response was malformed: {exc}") from exc


def relay_url_from_org_config(org_config: dict[str, Any]) -> str | None:
    """Extract the org-wide relay address from org_config.json's
    "mobile_relay" section, or None if that section is absent, disabled, or
    missing a URL -- same never-fatal "connector skipped on missing/bad
    config" philosophy daemon_main.py's build_connectors() already uses for
    every other integration.

    Deliberately the *only* thing this reads from org_config.json for this
    feature -- mailbox_id/token/shared_key used to live here too in Phase 1,
    which in hindsight was a real bug waiting to happen for any org with
    more than one user: org_config.json is the *same* file distributed to
    everyone, so a single shared mailbox+key there would have meant every
    user's daemon posting into one mailbox any of them could decrypt and
    answer for. Phase 2 fixes this: relay_url is the only thing that's
    genuinely org-wide; mailbox_id/token/shared_key are per-device secrets
    that belong in each user's own local PairingStore
    (mobile_relay_pairing.py), never in a file every employee receives an
    identical copy of.
    """
    section = org_config.get("mobile_relay") or {}
    if not section.get("enabled", False):
        return None
    relay_url = section.get("relay_url", "")
    if not relay_url:
        logger.warning(
            "org_config.json's mobile_relay section is enabled but has no relay_url "
            "-- mobile approval disabled"
        )
        return None
    return relay_url.rstrip("/")


@dataclass
class MobileRelayConfig:
    """One paired mailbox's connection details. Built per paired device by
    mobile_relay_pairing.py (PairedDevice.to_config()) -- see module
    docstring for why this no longer comes from org_config.json directly.
    """

    relay_url: str
    mailbox_id: str
    token: str
    shared_key: bytes  # raw 32-byte AES-256 key, derived per device


class MobileRelayClient:
    """Outbound-only HTTP client for one paired mailbox.

    Each instance is bound to one MobileRelayConfig (one mailbox, one shared
    key) -- MobileRelayApprovalUI constructs one per currently-active paired
    device (mobile_relay_pairing.PairingStore.list_active_devices()), fresh
    on every call, so a revoked device is simply never given one again (see
    that module's docstring on why revocation needs no phone cooperation).
    """

    def __init__(self, config: MobileRelayConfig, *, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session or requests.Session()

    def post_request(self, request_id: str, payload: dict[str, Any], *, ttl_seconds: float) -> None:
        """Encrypt `payload` to the shared key and post it as a new pending
        request on the mailbox.

        Raises MobileRelayClientError on any network/HTTP failure -- callers
        (MobileRelayApprovalUI) should treat that the same as "phone
        unreachable right now," not let it crash the whole approval flow,
        since the native desktop popup is always still available alongside
        this (issue #55's requirement 5).
        """
        try:
            response = self._session.post(
                f"{self._config.relay_url}/mailbox/{self._config.mailbox_id}",
                params={"token": self._config.token},
                json={
                    "request_id": request_id,
                    "payload": {"v": 1, "ciphertext": encrypt_payload(self._config.shared_key, payload)},
                    "ttl_seconds": ttl_seconds,
                },
                timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise MobileRelayClientError(f"Could not reach relay to post approval request: {exc}") from exc
        if response.status_code != 201:
            raise MobileRelayClientError(
                f"Relay rejected posted request (HTTP {response.status_code}): {response.text[:200]!r}"
            )

    def poll_decision(
        self,
        request_id: str,
        *,
        overall_timeout_seconds: float,
        long_poll_seconds: float = _DEFAULT_LONG_POLL_SECONDS,
        should_abandon: Callable[[], bool] | None = None,
    ) -> str | None:
        """Long-poll for a decision on `request_id`, verifying its auth tag
        before accepting it.

        Returns "approved" or "denied", or None if `overall_timeout_seconds`
        elapses or `should_abandon()` starts returning True (set by
        CompositeApprovalUI once the native desktop popup already answered
        first -- see composite_approval_ui.py) without ever seeing a trusted
        decision. Both are "no trustworthy mobile decision" outcomes that
        callers must treat as a deny, never an approval -- fail closed per
        issue #55's requirement 1.

        A decision that arrives with a missing or invalid auth tag is
        logged and discarded, not returned: the poll loop just continues as
        if nothing had arrived, since an unauthenticated "decision" carries
        no more trust than no response at all (see verify_auth_tag's own
        docstring for exactly what the tag proves).
        """
        should_abandon = should_abandon or (lambda: False)
        deadline = time.monotonic() + overall_timeout_seconds

        while time.monotonic() < deadline:
            if should_abandon():
                return None
            wait = max(0.0, min(long_poll_seconds, deadline - time.monotonic()))
            try:
                response = self._session.get(
                    f"{self._config.relay_url}/mailbox/{self._config.mailbox_id}/decision",
                    params={"token": self._config.token, "request_id": request_id, "wait": wait},
                    timeout=wait + _DEFAULT_HTTP_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                logger.warning("Mobile relay poll failed (will retry): %s", exc)
                time.sleep(1)
                continue

            if response.status_code == 204:
                continue  # nothing decided yet -- keep long-polling
            if response.status_code != 200:
                logger.warning(
                    "Unexpected relay response polling for a decision (HTTP %s) -- retrying",
                    response.status_code,
                )
                continue

            try:
                body = response.json()
            except ValueError:
                logger.warning("Relay's decision response wasn't valid JSON -- ignoring")
                continue

            decision = body.get("decision")
            if decision not in ("approved", "denied"):
                logger.warning("Relay returned a decision outside approved/denied -- ignoring")
                continue
            if not verify_auth_tag(self._config.shared_key, request_id, decision, body.get("auth", "")):
                logger.warning(
                    "Discarding a mobile decision for request %s: missing or invalid auth tag "
                    "-- treating it as untrusted, never applying it", request_id,
                )
                continue
            return decision

        return None

    def poll_for_request_raw(
        self, *, overall_timeout_seconds: float, long_poll_seconds: float = _DEFAULT_LONG_POLL_SECONDS,
    ) -> tuple[str, dict[str, Any]] | None:
        """Wait for any pending request payload on this mailbox, returning
        `(request_id, payload)` with `payload` exactly as posted -- no
        decryption attempted, since the caller may not be using this
        client's own `config.shared_key` for it.

        Used only by the pairing handshake (mobile_relay_pairing.py), which
        has its own short-lived, handshake-specific symmetric key rather
        than a device's long-term shared_key (there is no long-term key yet
        -- that's the entire point of a handshake). Everyday approval flow
        never calls this; see poll_decision() for that direction.

        Returns None if `overall_timeout_seconds` elapses with nothing
        posted.
        """
        deadline = time.monotonic() + overall_timeout_seconds
        while time.monotonic() < deadline:
            wait = max(0.0, min(long_poll_seconds, deadline - time.monotonic()))
            try:
                response = self._session.get(
                    f"{self._config.relay_url}/mailbox/{self._config.mailbox_id}",
                    params={"token": self._config.token, "wait": wait},
                    timeout=wait + _DEFAULT_HTTP_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                logger.warning("Mobile relay poll failed (will retry): %s", exc)
                time.sleep(1)
                continue
            if response.status_code == 204:
                continue
            if response.status_code != 200:
                logger.warning(
                    "Unexpected relay response polling for a request (HTTP %s) -- retrying",
                    response.status_code,
                )
                continue
            try:
                body = response.json()
                return body["request_id"], body["payload"]
            except (ValueError, KeyError) as exc:
                logger.warning("Relay's request response was malformed (%s) -- ignoring", exc)
                continue
        return None

    def post_decision_raw(self, request_id: str, decision: str, *, auth: str) -> None:
        """POST a decision with a caller-supplied auth tag, bypassing this
        client's own compute_auth_tag()/config.shared_key -- used only by
        the pairing handshake, which authenticates with a different
        (handshake-specific) key. Everyday approval flow never calls this;
        NativeApprovalUI/MobileRelayApprovalUI never touch it either.

        Raises MobileRelayClientError on any network/HTTP failure.
        """
        try:
            response = self._session.post(
                f"{self._config.relay_url}/mailbox/{self._config.mailbox_id}/decision",
                params={"token": self._config.token},
                json={"request_id": request_id, "decision": decision, "auth": auth},
                timeout=_DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise MobileRelayClientError(f"Could not reach relay to post a decision: {exc}") from exc
        if response.status_code != 200:
            raise MobileRelayClientError(
                f"Relay rejected posted decision (HTTP {response.status_code}): {response.text[:200]!r}"
            )
