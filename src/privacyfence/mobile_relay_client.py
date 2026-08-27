"""Outbound-only client for the mobile-approval relay's mailbox (issue #55, Phase 1).

Phase 0 (see spikes/mobile-relay-phase0/) proved the relay's pair/wake/decide
plumbing with an unencrypted, unauthenticated mailbox and no daemon
involvement. This module is the real daemon-side client Phase 1 wires in --
it speaks a superset of that wire contract, adding the two things Phase 0
explicitly deferred:

  - **Content confidentiality.** Every request `payload` is AES-256-GCM
    encrypted to a pre-shared symmetric key before it ever leaves the
    daemon -- the relay only ever stores/forwards ciphertext (issue #55's
    requirement 3: "zero third parties in the content path").
  - **Peer identity + replay protection for decisions.** A decision is only
    trusted if it carries a valid HMAC-SHA256 tag over
    `f"{request_id}:{decision}"`, keyed by the same shared key. This means:
    a device that doesn't hold the key -- the relay itself, or anyone who
    only compromised the mailbox token -- cannot forge a decision (peer
    identity); and a captured old (request_id, decision, auth) triple can't
    be replayed against a different, later request, since request_id is a
    fresh random value per approval and the tag is bound to it (replay
    protection). See _verify_auth()'s docstring for what this does and
    doesn't cover.

Deliberate Phase 1 simplification, not a final design: **one shared key for
the whole mailbox** stands in for the per-device asymmetric keypairs +
QR-code pairing flow the issue's Phase 2 owns. In practice this means one
paired phone; multiple phones with distinct identities and revocation is
Phase 2's job, not this module's. `MobileRelayConfig` is IT-provisioned and
lives in org_config.json (never settings.yaml -- see issue #55's "who hosts
the relay" resolution), matching how Slack/Salesforce/Atlassian's org-wide
credentials are already handled in daemon_main.py.

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


@dataclass
class MobileRelayConfig:
    """One paired mailbox's connection details, IT-provisioned via
    org_config.json's "mobile_relay" section -- never settings.yaml (see
    module docstring)."""

    relay_url: str
    mailbox_id: str
    token: str
    shared_key: bytes  # raw 32-byte AES-256 key

    @classmethod
    def from_org_config(cls, org_config: dict[str, Any]) -> MobileRelayConfig | None:
        """Build a config from org_config.json's "mobile_relay" section, or
        None if that section is absent, disabled, or incomplete -- the same
        never-fatal "connector skipped on missing/bad config" philosophy
        daemon_main.py's build_connectors() already uses for every other
        integration. Logs a warning (not an error) on a malformed section,
        since a typo'd bundle shouldn't crash daemon startup, only leave
        mobile approval unavailable.
        """
        section = org_config.get("mobile_relay") or {}
        if not section.get("enabled", False):
            return None
        relay_url = section.get("relay_url", "")
        mailbox_id = section.get("mailbox_id", "")
        token = section.get("token", "")
        shared_key_b64 = section.get("shared_key_base64", "")
        if not (relay_url and mailbox_id and token and shared_key_b64):
            logger.warning(
                "org_config.json's mobile_relay section is enabled but missing one of "
                "relay_url/mailbox_id/token/shared_key_base64 -- mobile approval disabled"
            )
            return None
        try:
            shared_key = base64.b64decode(shared_key_b64, validate=True)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "org_config.json's mobile_relay.shared_key_base64 is not valid base64 (%s) "
                "-- mobile approval disabled", exc,
            )
            return None
        if len(shared_key) != _AES_KEY_SIZE:
            logger.warning(
                "org_config.json's mobile_relay.shared_key_base64 decodes to %d bytes, need "
                "%d (AES-256) -- mobile approval disabled", len(shared_key), _AES_KEY_SIZE,
            )
            return None
        return cls(
            relay_url=relay_url.rstrip("/"), mailbox_id=mailbox_id, token=token, shared_key=shared_key,
        )


class MobileRelayClient:
    """Outbound-only HTTP client for one paired mailbox.

    Each instance is bound to one MobileRelayConfig (one mailbox, one shared
    key) -- see daemon_main.py for how it's constructed at startup.
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
                    "payload": {"v": 1, "ciphertext": self._encrypt(payload)},
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
        no more trust than no response at all (see _verify_auth's own
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
            if not self._verify_auth(request_id, decision, body.get("auth", "")):
                logger.warning(
                    "Discarding a mobile decision for request %s: missing or invalid auth tag "
                    "-- treating it as untrusted, never applying it", request_id,
                )
                continue
            return decision

        return None

    def _encrypt(self, payload: dict[str, Any]) -> str:
        """AES-256-GCM encrypt `payload` under the shared key. Returns
        base64(nonce || ciphertext) -- the nonce travels alongside the
        ciphertext (standard AEAD practice; it isn't secret, only ever
        reused-must-not-be) and is regenerated fresh on every call via
        os.urandom, never derived or counter-based."""
        plaintext = json.dumps(payload, default=str).encode("utf-8")
        nonce = os.urandom(_GCM_NONCE_SIZE)
        ciphertext = AESGCM(self._config.shared_key).encrypt(nonce, plaintext, None)
        return base64.b64encode(nonce + ciphertext).decode("ascii")

    def _mac(self, request_id: str, decision: str) -> str:
        digest = hmac.new(
            self._config.shared_key, f"{request_id}:{decision}".encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _verify_auth(self, request_id: str, decision: str, auth: str) -> bool:
        """True iff `auth` is a valid HMAC-SHA256(shared_key, request_id:decision) tag.

        Proves the sender holds the shared key (peer identity, in this
        single-shared-key Phase 1 design -- see module docstring) and that
        this specific (request_id, decision) pair was what they signed
        (replay protection: a tag captured for one request can't be replayed
        against a different one, since request_id is fresh and random per
        gated_call). It does NOT prove *when* the tag was made -- an old,
        valid tag for a request that's still pending would still verify.
        That's fine here specifically because request_id is single-use: the
        relay accepts only one decision per request_id, and this client's
        caller (MobileRelayApprovalUI, via gate.py's own request lifecycle)
        never reuses a request_id or applies a second decision for one
        already answered -- so there's no live request a stale-but-valid tag
        could be replayed onto.
        """
        if not auth:
            return False
        return hmac.compare_digest(self._mac(request_id, decision), auth)
