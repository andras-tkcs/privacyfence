"""WebAuthn step-up (P9, docs/https-connector-refactor-plan.md §10.6, §15
D7): platform-authenticator proof (Face ID / Touch ID / Android fingerprint
/ Windows Hello) that a human -- not merely a possessed, stolen session
cookie -- is the one approving a gated *write*, in org mode. §10.6's own
framing: "a borrowed or stolen unlocked phone with a live session becomes a
remote approval instrument for live write actions ... the control that
actually closes it is a step-up check on the approval itself."

Built on the ``webauthn`` package (py_webauthn), not hand-rolled -- the same
D2 reasoning §8.2 already gives for the MCP SDK and PyJWT applies here:
parsing CBOR attestation objects and verifying COSE signatures is
security-critical, spec-governed work with a maintained implementation
already available; owning that by hand buys nothing.

Two ceremonies, both delegated straight to the library after this module
resolves *who* (``Principal``) and *what RP*
(``org_mode.StepUpConfig.rp_id``/``rp_name``) the call is for:

- **Registration** (``begin_registration``/``finish_registration``) --
  enrolling a new passkey, from web/routes_security.py's own ``/security``
  page.
- **Assertion** (``begin_assertion``/``verify_assertion``) -- proving
  possession of an already-enrolled one, from web/routes_org_approvals.py's
  decide endpoint.

Five things from §10.6 this module exists to get right, not just the happy
path:

- **User verification is checked, not just the signature.**
  ``require_user_verification=True`` on both verify calls -- a credential
  that only proved *presence* (no biometric/PIN) is rejected outright, not
  silently accepted as "good enough".
- **Platform attachment is requested, not (and cannot be) cryptographically
  enforced.** ``authenticatorSelection.authenticator_attachment=platform``
  at registration time is what stops a compliant browser from offering a
  roaming security key in the first place; WebAuthn's signed payload
  carries no attachment claim to re-verify server-side after the fact (the
  browser-reported ``authenticatorAttachment`` field on the credential is
  informational only), so this is real but client-side-enforced, the same
  posture every RP using this mechanism has.
- **The RP ID must be a real registrable domain.** D1 (§15) already pins
  local mode's own dev server to ``localhost`` for exactly this reason;
  ``StepUpConfig.rp_id`` here is org mode's own version of that constraint
  -- an IP address or a non-HTTPS origin fails the ceremony at the browser
  level, not here.
- **The challenge is bound to a specific decision, not just "a human
  tapped something."** §10.6: "make it a server nonce bound to the
  approval_id and a hash of the decision payload, and verify that
  server-side." ``decision_fingerprint``/``StepUpChallengeStore`` below are
  that binding -- see their own docstrings.
- **Sign-count regression is logged, not silently ignored** -- see
  ``verify_assertion``'s own note on why it's a warning, not a hard
  failure, for this authenticator class.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import paths
from .principal import Principal

logger = logging.getLogger(__name__)

CREDENTIALS_FILE_NAME = "webauthn_credentials.json"

# §10.6's own binding window -- generous enough to cover a real biometric
# prompt (including the 1Password hand-off delay §12's manual check found)
# but short enough that a leaked/logged challenge is useless well before
# the pending approval itself would expire.
STEP_UP_CHALLENGE_TTL_SECONDS = 2 * 60
_REGISTRATION_CHALLENGE_TTL_SECONDS = 5 * 60


class WebAuthnError(Exception):
    """Raised by finish_registration()/verify_assertion() on any ceremony
    failure -- an unverifiable signature, a challenge/RP-ID/origin
    mismatch, user verification not satisfied, or an unknown credential.
    Callers (web/routes_security.py, web/routes_org_approvals.py) turn this
    into a plain 401/400, never a stack trace reaching the browser."""


@dataclass
class WebAuthnCredential:
    """One enrolled passkey. ``credential_id``/``public_key`` are stored
    base64url-encoded (JSON has no byte-string type); everything else is
    exactly what ``VerifiedRegistration`` hands back, kept for the
    lifetime of the credential rather than re-derived."""

    credential_id: str
    public_key: str
    sign_count: int
    device_type: str  # "single_device" | "multi_device" -- the BE flag
    backed_up: bool  # the BS flag -- a synced (not device-bound) passkey
    label: str = "Passkey"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "public_key": self.public_key,
            "sign_count": self.sign_count,
            "device_type": self.device_type,
            "backed_up": self.backed_up,
            "label": self.label,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "WebAuthnCredential":
        return WebAuthnCredential(
            credential_id=str(raw["credential_id"]),
            public_key=str(raw["public_key"]),
            sign_count=int(raw.get("sign_count", 0)),
            device_type=str(raw.get("device_type", "single_device")),
            backed_up=bool(raw.get("backed_up", False)),
            label=str(raw.get("label", "Passkey")),
            created_at=float(raw.get("created_at", 0.0)),
        )


# --------------------------------------------------------------------- #
# Credential storage -- one 0600 JSON file per principal, same posture as
# every OAuth token file in this codebase (see slack_client.
# save_token_record's own comment).
# --------------------------------------------------------------------- #

def _credentials_path(principal: Principal) -> Path:
    return paths.user_dir(principal) / CREDENTIALS_FILE_NAME


def list_credentials(principal: Principal) -> list[WebAuthnCredential]:
    path = _credentials_path(principal)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read WebAuthn credentials for %s -- treating as none enrolled", principal.id)
        return []
    return [WebAuthnCredential.from_dict(item) for item in raw.get("credentials", []) if isinstance(item, dict)]


def has_credentials(principal: Principal) -> bool:
    return bool(list_credentials(principal))


def _save_credentials(principal: Principal, creds: list[WebAuthnCredential]) -> None:
    path = _credentials_path(principal)
    path.write_text(json.dumps({"credentials": [c.to_dict() for c in creds]}), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover -- best effort on non-POSIX
        logger.debug("Could not chmod WebAuthn credentials file (non-fatal)")


def add_credential(principal: Principal, credential: WebAuthnCredential) -> None:
    creds = [c for c in list_credentials(principal) if c.credential_id != credential.credential_id]
    creds.append(credential)
    _save_credentials(principal, creds)


def remove_credential(principal: Principal, credential_id: str) -> bool:
    creds = list_credentials(principal)
    remaining = [c for c in creds if c.credential_id != credential_id]
    if len(remaining) == len(creds):
        return False
    _save_credentials(principal, remaining)
    return True


def _update_sign_count(principal: Principal, credential_id: str, new_count: int) -> None:
    creds = list_credentials(principal)
    for c in creds:
        if c.credential_id == credential_id:
            c.sign_count = new_count
    _save_credentials(principal, creds)


# --------------------------------------------------------------------- #
# Registration ceremony -- enrolling a new passkey.
# --------------------------------------------------------------------- #

@dataclass
class _PendingRegistration:
    challenge: bytes
    created_at: float = field(default_factory=time.time)


class RegistrationChallengeStore:
    """One in-flight enrollment ceremony per principal at a time -- same
    "a daemon restart invalidates it, start over" posture as web/
    routes_connect.py's own _TelegramAuthStore."""

    def __init__(self, ttl: float = _REGISTRATION_CHALLENGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingRegistration] = {}

    def put(self, principal_id: str, challenge: bytes) -> None:
        with self._lock:
            self._pending[principal_id] = _PendingRegistration(challenge=challenge)

    def pop(self, principal_id: str) -> bytes | None:
        with self._lock:
            entry = self._pending.pop(principal_id, None)
        if entry is None or (time.time() - entry.created_at) > self._ttl:
            return None
        return entry.challenge


def begin_registration(principal: Principal, *, rp_id: str, rp_name: str) -> tuple[str, bytes]:
    """Returns ``(options_json, challenge)`` -- ``options_json`` goes
    straight to the browser (``navigator.credentials.create()``);
    ``challenge`` is what the caller must hand to
    ``RegistrationChallengeStore.put()`` to verify against later."""
    existing = list_credentials(principal)
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_name=principal.email or principal.display_name or principal.id,
        user_id=principal.id.encode("utf-8"),
        user_display_name=principal.display_name or principal.email or principal.id,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
    )
    return webauthn.options_to_json(options), options.challenge


def finish_registration(
    principal: Principal,
    credential_json: dict[str, Any],
    *,
    expected_challenge: bytes,
    rp_id: str,
    origin: str,
    label: str = "Passkey",
) -> WebAuthnCredential:
    try:
        verified = webauthn.verify_registration_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 -- the library's own exception hierarchy isn't public API to pin to
        raise WebAuthnError(f"Registration could not be verified: {exc}") from exc
    credential = WebAuthnCredential(
        credential_id=bytes_to_base64url(verified.credential_id),
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
        label=label or "Passkey",
    )
    add_credential(principal, credential)
    return credential


# --------------------------------------------------------------------- #
# Assertion ceremony -- proving possession of an already-enrolled passkey,
# for a specific gated decision.
# --------------------------------------------------------------------- #

def begin_assertion(principal: Principal, *, rp_id: str) -> tuple[str, bytes] | None:
    """``None`` when this principal has no enrolled credential -- the
    caller (web/routes_org_approvals.py) falls back to offering the IdP
    step-up/re-auth path instead (§10.6: "OIDC re-auth as the fallback for
    a user with no passkey enrolled")."""
    creds = list_credentials(principal)
    if not creds:
        return None
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in creds],
    )
    return webauthn.options_to_json(options), options.challenge


def verify_assertion(
    principal: Principal, credential_json: dict[str, Any], *, expected_challenge: bytes, rp_id: str, origin: str,
) -> None:
    """Raises WebAuthnError on any failure; returns (updating the stored
    sign count as a side effect) on success."""
    cred_id = credential_json.get("id") if isinstance(credential_json, dict) else None
    stored = next((c for c in list_credentials(principal) if c.credential_id == cred_id), None)
    if stored is None:
        raise WebAuthnError("Unknown WebAuthn credential")
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 -- see finish_registration's own note
        raise WebAuthnError(f"Step-up could not be verified: {exc}") from exc
    # Clone-detection: many platform authenticators always report a sign
    # count of 0 (informational only for those -- both sides being 0 is
    # normal, not suspicious), but a *nonzero* count that fails to advance
    # is the classic signal a credential's private key material was cloned
    # rather than used from the one real authenticator. Logged, not a hard
    # failure: the assertion signature itself already verified, and
    # treating this as fatal would lock a legitimate user out of their own
    # passkey on a spec-compliant authenticator that simply doesn't
    # increment (a real, common case, not hypothetical).
    if (stored.sign_count != 0 or verified.new_sign_count != 0) and verified.new_sign_count <= stored.sign_count:
        logger.warning(
            "WebAuthn sign count did not advance for principal %s, credential %s -- "
            "possible cloned credential", principal.id, stored.credential_id,
        )
    _update_sign_count(principal, stored.credential_id, verified.new_sign_count)


# --------------------------------------------------------------------- #
# Decision binding -- §10.6: "make it a server nonce bound to the
# approval_id and a hash of the decision payload, and verify that
# server-side." web/routes_org_approvals.py's decide endpoint is the one
# caller of both halves below.
# --------------------------------------------------------------------- #

def decision_fingerprint(*, approval_id: str, principal_id: str, result: str, choice: int | None) -> str:
    """A stand-in for "this exact decision" -- not a secret, just a
    collision-resistant tag over (approval, principal, decision) so a
    step-up ceremony started for one decision can't be replayed to
    authorize a *different* decision on the same approval (e.g. a
    WebAuthn assertion obtained while approving gets silently reused to
    authorize a deny, or a different ``choice`` index)."""
    payload = f"{approval_id}|{principal_id}|{result}|{'' if choice is None else choice}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _PendingStepUp:
    challenge: bytes
    fingerprint: str
    created_at: float = field(default_factory=time.time)


class StepUpChallengeStore:
    """Server-side binding for one in-flight decide-time WebAuthn ceremony.
    Single-use (``pop``, not a read) and short-lived; keyed by
    ``(principal_id, approval_id)`` since only one step-up ceremony is ever
    meaningfully in flight for a given approval at a time -- a second
    ``options`` request for the same approval simply overwrites the first
    rather than needing its own separate slot."""

    def __init__(self, ttl: float = STEP_UP_CHALLENGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingStepUp] = {}

    def put(self, principal_id: str, approval_id: str, *, challenge: bytes, fingerprint: str) -> None:
        with self._lock:
            self._pending[(principal_id, approval_id)] = _PendingStepUp(challenge=challenge, fingerprint=fingerprint)

    def pop(self, principal_id: str, approval_id: str) -> _PendingStepUp | None:
        with self._lock:
            entry = self._pending.pop((principal_id, approval_id), None)
        if entry is None or (time.time() - entry.created_at) > self._ttl:
            return None
        return entry


def is_step_up_required(*, gate_kind: str, pii_detected: bool, scope: str) -> bool:
    """§10.6: "scope it to writes, or to writes plus PII-flagged reads."
    ``scope`` is ``org_mode.StepUpConfig.scope`` -- kept as a bare string
    parameter here (rather than importing ``org_mode.StepUpScope``) so this
    module has no dependency on org_mode.py at all; the two string literals
    are the whole of that type."""
    if gate_kind == "popup":
        return True
    if scope == "writes_and_pii_reads" and gate_kind == "review" and pii_detected:
        return True
    return False


__all__ = [
    "STEP_UP_CHALLENGE_TTL_SECONDS",
    "RegistrationChallengeStore",
    "StepUpChallengeStore",
    "WebAuthnCredential",
    "WebAuthnError",
    "add_credential",
    "begin_assertion",
    "begin_registration",
    "decision_fingerprint",
    "finish_registration",
    "has_credentials",
    "is_step_up_required",
    "list_credentials",
    "remove_credential",
    "verify_assertion",
]
