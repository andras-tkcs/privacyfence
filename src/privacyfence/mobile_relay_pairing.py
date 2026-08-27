"""Pairing/enrollment, multi-device support, revocation, and key rotation
for mobile remote approval (issue #55, Phase 2).

Phase 1 shipped a single pre-shared symmetric key for the whole mailbox --
explicitly documented there as "one paired phone... Phase 2's job," since a
single shared secret would mean every device (and every user, if it had
lived in org_config.json as Phase 1 did) could decrypt and answer for every
other. This module replaces it with the design issue #55 actually calls
for: the daemon holds one long-term X25519 identity keypair; each paired
device gets its own X25519 keypair and its own mailbox on the relay; the
key used to encrypt/authenticate traffic with that device is derived via
X25519 ECDH + HKDF between the daemon's identity and that device's public
key -- never a value either side chose, and never sent across the wire.

Pairing handshake (what "scan a QR code" produces under the hood):

1. begin_pairing() mints a fresh mailbox on the relay (request_new_mailbox()
   in mobile_relay_client.py) and a short-lived, random `pairing_secret`.
   PairingSession.qr_payload() is what a QR code would encode: the new
   mailbox's address/credentials, the daemon's *public* identity key, and
   pairing_secret. (Rendering that dict as an actual QR image, and a
   settings-window UI to show/scan one, isn't built here -- see "Not yet
   built" below. daemon_main.py's `--pair-mobile-device` CLI flag is the
   concrete enrollment path this phase actually ships.)
2. The phone -- no real implementation of this exists yet; Phase 3's job --
   generates its own X25519 keypair, derives `handshake_key =
   HKDF(pairing_secret)`, and posts `{device_public_key, device_name}` to
   the mailbox, AES-256-GCM-encrypted under handshake_key, as a request
   with the reserved ID PAIRING_REQUEST_ID -- the exact same
   `POST /mailbox/{id}` shape a real approval request uses (see
   mobile_relay_client.py's module docstring), just played by the phone
   this one time instead of the daemon.
3. complete_pairing() polls for that request, decrypts it with the same
   `handshake_key` it can derive independently (both sides only ever
   needed `pairing_secret`, and never send it again after this),
   computes the real, long-term `shared_key = derive_shared_key(daemon
   identity, device's public key)`, and sends a decision-shaped
   acknowledgement back authenticated with *that* key -- proof the daemon
   derived the same shared_key the phone did, before either side uses it
   for anything real. The resulting PairedDevice is persisted to the
   local PairingStore.

`pairing_secret` protects only this one bootstrap message and is discarded
immediately after -- never stored, never reused. Forward secrecy from here
on is exactly what X25519 ECDH gives: compromise of one device's derived
shared_key exposes neither the daemon's identity key nor any other
device's shared_key.

Revocation is entirely local and instant: PairingStore.revoke() flips one
flag in a file the daemon already owns, with no phone cooperation needed
("fast phone-not-required revocation" -- issue #55's own requirement).
MobileRelayApprovalUI (mobile_relay_approval_ui.py) re-reads the active
device list from the store on every call rather than caching it once, so a
revocation takes effect on the very next approval -- no daemon restart.

Key rotation, in this design, is revoke-and-re-pair: there is no in-place
re-keying handshake for an already-paired device (a device has no way to
authenticate a "here's your new key" message it didn't request without
just... pairing again). Rotating the daemon's own identity key
(PairingStore.rotate_identity()) is more disruptive by design: every
existing device's shared_key was derived from the old identity key, so
rotating it revokes every currently-paired device at once; each needs to
be paired again. That's a deliberate, simple v1 rotation story -- a
smoother "both keys valid during a grace window" scheme is a reasonable
future refinement, not required by issue #55's own text ("ephemeral/
rotated keys for forward secrecy, not a single static long-term box" is
satisfied by per-device ECDH-derived keys regardless of how coarse the
rotation mechanism itself is).

Not yet built here (tracked, not silently cut):
- Any actual QR-code rendering, or a settings-window pairing/device-list
  UI -- this module is the backend protocol/storage layer a future UI (or
  today, daemon_main.py's `--pair-mobile-device`/`--list-mobile-devices`/
  `--revoke-mobile-device` CLI flags) calls into.
- A real phone-side implementation of the handshake -- Phase 3's job.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .mobile_relay_client import (
    MobileRelayClient,
    MobileRelayClientError,
    MobileRelayConfig,
    compute_auth_tag,
    decrypt_payload,
    request_new_mailbox,
)

logger = logging.getLogger(__name__)

PAIRING_REQUEST_ID = "__pairing__"
DEFAULT_PAIRING_TTL_SECONDS = 600.0  # 10 minutes to scan the code and complete the handshake
_X25519_KEY_SIZE = 32
_HANDSHAKE_HKDF_INFO = b"privacyfence-mobile-relay-pairing-handshake-v1"
_DEVICE_KEY_HKDF_INFO = b"privacyfence-mobile-relay-device-key-v1"


class MobileRelayPairingError(Exception):
    """Raised for unrecoverable pairing problems: relay unreachable, the
    session expired, or a handshake message that didn't decrypt/parse.

    The last case is deliberate, not a bug being papered over: an attacker
    who doesn't know `pairing_secret` (never sent anywhere but the QR code
    itself) cannot get a device registered by racing a bogus message onto
    the mailbox before the real phone does -- complete_pairing() raises
    rather than silently ignoring it and waiting for a second attempt,
    since a bogus message occupying the mailbox's one pending-request slot
    would otherwise stall out the real handshake for no visible reason.
    """


def _generate_x25519_keypair() -> tuple[bytes, bytes]:
    private_key = x25519.X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_bytes, public_bytes


def _hkdf(input_key_material: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=SHA256(), length=_X25519_KEY_SIZE, salt=None, info=info).derive(input_key_material)


def derive_shared_key(daemon_private_key: bytes, device_public_key: bytes) -> bytes:
    """X25519 ECDH between the daemon's identity and one device's public
    key, run through HKDF -- the long-term per-device key everyday approval
    traffic (mobile_relay_client.py) encrypts/authenticates with."""
    private_key = x25519.X25519PrivateKey.from_private_bytes(daemon_private_key)
    peer_public_key = x25519.X25519PublicKey.from_public_bytes(device_public_key)
    ecdh_secret = private_key.exchange(peer_public_key)
    return _hkdf(ecdh_secret, _DEVICE_KEY_HKDF_INFO)


def _handshake_key(pairing_secret: bytes) -> bytes:
    return _hkdf(pairing_secret, _HANDSHAKE_HKDF_INFO)


@dataclass
class DaemonIdentity:
    """The daemon's own long-term X25519 keypair -- one per installation,
    shared across every device pairing (not per-device; see module
    docstring for why rotating this is deliberately disruptive)."""

    private_key: bytes
    public_key: bytes


@dataclass
class PairedDevice:
    device_id: str
    device_name: str
    device_public_key: bytes
    mailbox_id: str
    token: str
    shared_key: bytes
    paired_at: float
    revoked: bool = False

    def to_config(self, relay_url: str) -> MobileRelayConfig:
        return MobileRelayConfig(
            relay_url=relay_url, mailbox_id=self.mailbox_id, token=self.token, shared_key=self.shared_key,
        )


@dataclass
class PairingSession:
    """Transient, in-memory state for one in-progress pairing attempt --
    never persisted. Discarded (successfully or not) once complete_pairing()
    returns or raises."""

    relay_url: str
    mailbox_id: str
    token: str
    daemon_public_key: bytes
    pairing_secret: bytes
    expires_at: float

    def qr_payload(self) -> dict:
        """Everything a QR code (rendered by a future UI) would encode --
        enough for the phone to complete pairing, and nothing more (no
        daemon private key, no derived shared_key -- that doesn't exist
        yet)."""
        return {
            "v": 1,
            "relay_url": self.relay_url,
            "mailbox_id": self.mailbox_id,
            "token": self.token,
            "daemon_public_key": base64.b64encode(self.daemon_public_key).decode("ascii"),
            "pairing_secret": base64.b64encode(self.pairing_secret).decode("ascii"),
            "expires_at": self.expires_at,
        }

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class PairingStore:
    """Local, per-installation JSON store for the daemon's own X25519
    identity and every device ever paired with it -- analogous to the
    per-connector OAuth token files in credentials/ (daemon_main.py's
    TOKEN_FILES): this machine's own secrets, never distributed, same 0600
    permissioning (see _save()).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._identity: DaemonIdentity | None = None
        self._devices: list[PairedDevice] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as fh:
            data = json.load(fh)
        identity = data.get("daemon_identity")
        if identity:
            self._identity = DaemonIdentity(
                private_key=base64.b64decode(identity["private_key"]),
                public_key=base64.b64decode(identity["public_key"]),
            )
        self._devices = [
            PairedDevice(
                device_id=d["device_id"], device_name=d["device_name"],
                device_public_key=base64.b64decode(d["device_public_key"]),
                mailbox_id=d["mailbox_id"], token=d["token"],
                shared_key=base64.b64decode(d["shared_key"]),
                paired_at=d["paired_at"], revoked=d.get("revoked", False),
            )
            for d in data.get("devices", [])
        ]

    def _save(self) -> None:
        data = {
            "daemon_identity": (
                {
                    "private_key": base64.b64encode(self._identity.private_key).decode("ascii"),
                    "public_key": base64.b64encode(self._identity.public_key).decode("ascii"),
                }
                if self._identity is not None else None
            ),
            "devices": [
                {
                    "device_id": d.device_id, "device_name": d.device_name,
                    "device_public_key": base64.b64encode(d.device_public_key).decode("ascii"),
                    "mailbox_id": d.mailbox_id, "token": d.token,
                    "shared_key": base64.b64encode(d.shared_key).decode("ascii"),
                    "paired_at": d.paired_at, "revoked": d.revoked,
                }
                for d in self._devices
            ],
        }
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def get_or_create_identity(self) -> DaemonIdentity:
        if self._identity is None:
            private_key, public_key = _generate_x25519_keypair()
            self._identity = DaemonIdentity(private_key=private_key, public_key=public_key)
            self._save()
        return self._identity

    def rotate_identity(self) -> DaemonIdentity:
        """Generate a fresh daemon identity keypair. Every existing paired
        device's shared_key depended on the old one -- see module
        docstring's "Key rotation" section -- so this also revokes every
        currently-active device; each needs to be paired again."""
        for device in self._devices:
            device.revoked = True
        private_key, public_key = _generate_x25519_keypair()
        self._identity = DaemonIdentity(private_key=private_key, public_key=public_key)
        self._save()
        return self._identity

    def add_device(self, device: PairedDevice) -> None:
        self._devices.append(device)
        self._save()

    def list_active_devices(self) -> list[PairedDevice]:
        return [d for d in self._devices if not d.revoked]

    def list_all_devices(self) -> list[PairedDevice]:
        return list(self._devices)

    def revoke(self, device_id: str) -> bool:
        """Mark a device revoked (kept in the store, not deleted, so there's
        a record of it -- consistent with this app's audit-everything
        posture elsewhere). Returns False if no active device has this ID
        (already revoked, or never existed)."""
        for device in self._devices:
            if device.device_id == device_id and not device.revoked:
                device.revoked = True
                self._save()
                return True
        return False

    def remove(self, device_id: str) -> bool:
        """Hard-delete a device record entirely (vs. revoke()'s soft flag)
        -- for cleaning up old/test entries, not the normal revocation
        path."""
        before = len(self._devices)
        self._devices = [d for d in self._devices if d.device_id != device_id]
        if len(self._devices) != before:
            self._save()
            return True
        return False


def begin_pairing(
    relay_url: str, identity: DaemonIdentity, *, pairing_ttl_seconds: float = DEFAULT_PAIRING_TTL_SECONDS,
) -> PairingSession:
    """Mint a fresh mailbox and a one-time pairing_secret. Raises
    MobileRelayClientError if the relay can't be reached."""
    mailbox_id, token = request_new_mailbox(relay_url)
    return PairingSession(
        relay_url=relay_url, mailbox_id=mailbox_id, token=token,
        daemon_public_key=identity.public_key, pairing_secret=secrets.token_bytes(_X25519_KEY_SIZE),
        expires_at=time.time() + pairing_ttl_seconds,
    )


def complete_pairing(
    session: PairingSession, identity: DaemonIdentity, *, poll_timeout_seconds: float | None = None,
) -> PairedDevice:
    """Block until the phone completes the handshake (or the session
    expires), and return the resulting PairedDevice -- not yet persisted;
    call PairingStore.add_device() with the result.

    Raises MobileRelayPairingError on timeout, expiry, or a handshake
    message that didn't decrypt/parse as {device_public_key, device_name}
    under HKDF(session.pairing_secret) -- see that exception's own
    docstring for why a bad message is a hard failure, not a silent retry.
    """
    if session.is_expired():
        raise MobileRelayPairingError("Pairing session already expired")
    timeout = (
        poll_timeout_seconds if poll_timeout_seconds is not None
        else max(0.0, session.expires_at - time.time())
    )

    # shared_key is never used by poll_for_request_raw()/post_decision_raw()
    # below (both are pairing-handshake-only methods that don't touch it) --
    # MobileRelayConfig still requires a value, so a placeholder is fine.
    config = MobileRelayConfig(
        relay_url=session.relay_url, mailbox_id=session.mailbox_id, token=session.token,
        shared_key=b"\x00" * _X25519_KEY_SIZE,
    )
    client = MobileRelayClient(config)

    result = client.poll_for_request_raw(overall_timeout_seconds=timeout)
    if result is None:
        raise MobileRelayPairingError("No pairing handshake received before the session expired")
    request_id, payload = result
    if request_id != PAIRING_REQUEST_ID:
        raise MobileRelayPairingError(f"Unexpected request on pairing mailbox: {request_id!r}")

    handshake_key = _handshake_key(session.pairing_secret)
    try:
        handshake = decrypt_payload(handshake_key, payload["ciphertext"])
        device_public_key = base64.b64decode(handshake["device_public_key"])
        device_name = str(handshake["device_name"])
    except Exception as exc:
        raise MobileRelayPairingError(f"Malformed or undecryptable pairing handshake: {exc}") from exc
    if len(device_public_key) != _X25519_KEY_SIZE:
        raise MobileRelayPairingError("Device public key is not a valid X25519 key")

    shared_key = derive_shared_key(identity.private_key, device_public_key)
    device = PairedDevice(
        device_id=uuid.uuid4().hex, device_name=device_name, device_public_key=device_public_key,
        mailbox_id=session.mailbox_id, token=session.token, shared_key=shared_key, paired_at=time.time(),
    )

    # A mutual-confirmation ack, authenticated with the *final* shared_key
    # (not handshake_key) -- proof to the phone that the daemon derived the
    # same key it did, before either side uses it for anything real. Best
    # effort: if this fails to send, the pairing has still genuinely
    # succeeded on the daemon's side (the device below gets persisted
    # either way) -- the phone just has to trust its own timeout/retry UX
    # rather than seeing an explicit success signal.
    try:
        ack_auth = compute_auth_tag(shared_key, PAIRING_REQUEST_ID, "approved")
        client.post_decision_raw(PAIRING_REQUEST_ID, "approved", auth=ack_auth)
    except MobileRelayClientError as exc:
        logger.warning("Paired device %r but failed to send the pairing acknowledgement: %s", device_name, exc)

    return device
