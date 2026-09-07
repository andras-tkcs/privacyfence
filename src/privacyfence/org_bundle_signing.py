"""SEC-05 (full): Ed25519 signing/verification for organization config
bundles (org_config.json), plus the plain-hash helper the SEC-05 interim
fix (daemon_main.py's startup logging) uses.

Threat model this closes: org_config.json carries the org's IdP client
secret, the Google/Slack/Salesforce/Atlassian app registrations, and (in
org mode) this daemon's own OAuth 2.1 authorization-server trust
configuration (``idp.issuer``, ``server.issuer_url``). Before this,
anything that can write to this file -- a compromised backup, a
misconfigured file share the bundle is distributed over, brief physical/
admin access to the machine -- could silently redirect this daemon's
whole trust root, and a tampered-but-still-valid-JSON replacement was
indistinguishable from the real thing (SEC-04 only catches malformed/
unreadable files, not a well-formed-but-hostile one).

Design -- trust-on-first-use (TOFU), the same model SSH host keys use:
each organization generates an Ed25519 keypair once
(``scripts/build_org_bundle.py --generate-signing-key``) and signs every
bundle it builds with it (``--sign-key``). The *first* signed bundle an
install ever sees has its embedded public key trusted on that basis
alone and pinned to disk (``pinned_public_key_path()``); every bundle
after that -- installed via PrivacyFence Settings' "Install/Update
Organization Config..." (``settings_controller.install_org_config_
bytes``) or dropped onto disk by hand and picked up at the next daemon
start (``daemon_main.load_org_config``) -- must verify against that
pinned key or is rejected outright. That closes the obvious downgrade
this would otherwise still allow (an attacker replacing a signed bundle
with an unsigned or self-signed one) without requiring any out-of-band
key distribution beyond the bundle itself.

An install that never adopts signing (no ``--sign-key`` ever used) never
pins a key and keeps working exactly as before -- signing is opt-in for
local mode. It's mandatory for ``mode: org`` (see ``daemon_main.
load_org_config``'s and ``settings_controller.install_org_config_bytes``'s
own enforcement of that), since org mode is the surface this phase is
hardening towards enterprise-production readiness, not every existing
local install.

This module intentionally takes each caller's own ``org_dir`` as a
parameter rather than importing ``paths.org_dir`` itself -- both
``daemon_main.py`` and ``settings_controller.py`` already import ``org_dir``
into their own module namespace and their tests monkeypatch it there
(see e.g. ``test_daemon_main.py``'s ``monkeypatch.setattr(daemon_main,
"org_dir", ...)``); importing a second, independent reference to
``paths.org_dir`` here would silently bypass that and point the pinned-
key file at a different (real) location during tests.

Ed25519 (``cryptography.hazmat.primitives.asymmetric.ed25519``) rather
than RSA: fixed-size 32-byte keys/64-byte signatures make embedding both
directly in the JSON bundle (base64) simple, and it's the same
"security-critical, spec-governed, don't hand-roll it" primitive class
``pyproject.toml``'s own dependency comments already invoke for JWT/
WebAuthn verification elsewhere in this codebase -- ``cryptography`` is
already a runtime dependency there, so this adds nothing new to what's
installed.

``scripts/build_org_bundle.py`` does NOT import this module (see its own
module docstring: it's meant to be runnable standalone, without a
PrivacyFence install) -- it re-implements the same canonicalization/
signing logic against ``cryptography`` directly. The two MUST stay
byte-for-byte identical or a bundle signed by the script will fail to
verify here; ``_canonical_payload_bytes`` below is deliberately simple
(sorted-key compact JSON) to make that easy to keep in sync.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Bundle fields written by scripts/build_org_bundle.py --sign-key, read
# here. Both are standard-alphabet base64 of the raw fixed-size Ed25519
# bytes -- 32 for the public key, 64 for the signature.
SIGNATURE_FIELD = "signature"
SIGNING_PUBLIC_KEY_FIELD = "signing_public_key"

# Lives alongside org_config.json itself (in the caller's own org_dir),
# not inside org_config.json -- the whole point is that once a key is
# pinned, this file's trust doesn't come from anything the bundle itself
# claims.
PINNED_PUBKEY_FILENAME = "org_config_signing_pubkey.txt"


def sha256_hex(raw: bytes) -> str:
    """SEC-05 interim: a hash any admin can compare by eye/script against
    a known-good value, independent of whether the bundle is signed at
    all -- see daemon_main.py's startup logging."""
    return hashlib.sha256(raw).hexdigest()


def _canonical_payload_bytes(bundle: dict[str, Any]) -> bytes:
    """Deterministic bytes to sign/verify: every field except
    ``signature`` itself (which obviously can't be part of what it
    signs), sorted-key compact JSON so the exact same logical bundle
    always canonicalizes identically regardless of how it was
    constructed or re-serialized. ``signing_public_key`` IS included --
    an attacker can't pair their own key with their own signature and
    have it verify against the *pinned* key (verification below always
    checks the pinned key, never a key the bundle merely claims), and
    folding the embedded key into what's signed also means that key
    can't be tampered with independent of the signature at the one
    moment it IS trusted on its own say-so -- first-pin (see
    ``verify_and_maybe_pin``).
    """
    payload = {k: v for k, v in bundle.items() if k != SIGNATURE_FIELD}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_against(bundle: dict[str, Any], public_key_bytes: bytes) -> bool:
    signature_b64 = bundle.get(SIGNATURE_FIELD)
    if not isinstance(signature_b64, str) or not signature_b64:
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, _canonical_payload_bytes(bundle))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def pinned_public_key_path(org_dir: Path) -> Path:
    return org_dir / PINNED_PUBKEY_FILENAME


def load_pinned_public_key(org_dir: Path) -> bytes | None:
    path = pinned_public_key_path(org_dir)
    if not path.exists():
        return None
    try:
        return base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
    except (OSError, ValueError):
        return None


def _pin_public_key(org_dir: Path, raw_public_key: bytes) -> None:
    """Writes the pinned-key file. Not yet routed through a shared
    atomic-write helper (SEC-09, Phase 1 item 1.4, hasn't landed) --
    write-to-temp-then-replace here is deliberately kept self-contained
    rather than reaching ahead for that not-yet-existing module."""
    path = pinned_public_key_path(org_dir)
    org_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(base64.b64encode(raw_public_key).decode("ascii") + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


@dataclass(frozen=True)
class BundleTrust:
    ok: bool
    detail: str
    signed: bool = False
    newly_pinned: bool = False


def verify_and_maybe_pin(bundle: dict[str, Any], org_dir: Path) -> BundleTrust:
    """The one trust decision every org_config.json load or install goes
    through. See module docstring for the TOFU model.

    Mutates disk state (pins a new key) only in the "first signed bundle
    this install has ever seen" case below -- every other path is a pure
    check. Callers (daemon_main.load_org_config, settings_controller.
    install_org_config_bytes) both call this on every load/install, so
    the very first signed bundle wins the pin regardless of which of the
    two documented install methods (web upload vs. hand-editing the
    file) it arrived through.
    """
    has_signature_fields = bool(bundle.get(SIGNATURE_FIELD)) or bool(bundle.get(SIGNING_PUBLIC_KEY_FIELD))
    pinned = load_pinned_public_key(org_dir)

    if pinned is not None:
        if _verify_against(bundle, pinned):
            return BundleTrust(ok=True, detail="signature verified against the pinned signing key", signed=True)
        if has_signature_fields:
            return BundleTrust(
                ok=False,
                detail="signature does not verify against the previously trusted (pinned) signing key",
            )
        return BundleTrust(
            ok=False,
            detail="a signing key is already trusted for this install, but this bundle is unsigned",
        )

    if not has_signature_fields:
        return BundleTrust(ok=True, detail="unsigned (no signing key trusted yet)", signed=False)

    embedded_key_b64 = bundle.get(SIGNING_PUBLIC_KEY_FIELD)
    if not isinstance(embedded_key_b64, str) or not embedded_key_b64:
        return BundleTrust(ok=False, detail="carries a signature but no signing_public_key")
    try:
        embedded_key = base64.b64decode(embedded_key_b64, validate=True)
    except ValueError:
        return BundleTrust(ok=False, detail="signing_public_key is not valid base64")
    if len(embedded_key) != 32 or not _verify_against(bundle, embedded_key):
        return BundleTrust(ok=False, detail="signature does not verify against its own embedded signing_public_key")

    _pin_public_key(org_dir, embedded_key)
    return BundleTrust(
        ok=True,
        detail="first signed bundle seen for this install -- its signing key is now pinned (TOFU)",
        signed=True,
        newly_pinned=True,
    )


# ---------------------------------------------------------------------------- #
# Signing side -- used by tests exercising the verification path above
# against a real signature (scripts/build_org_bundle.py has its own,
# deliberately independent copy of this for actual bundle production --
# see module docstring).
# ---------------------------------------------------------------------------- #

def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign_bundle(bundle: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Returns a copy of ``bundle`` with ``signing_public_key``/
    ``signature`` added (replacing any that were already present). Any
    change to the bundle after this invalidates the signature, by
    design."""
    signed = dict(bundle)
    signed.pop(SIGNATURE_FIELD, None)
    signed[SIGNING_PUBLIC_KEY_FIELD] = public_key_b64(private_key.public_key())
    signature = private_key.sign(_canonical_payload_bytes(signed))
    signed[SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")
    return signed


__all__ = [
    "PINNED_PUBKEY_FILENAME",
    "SIGNATURE_FIELD",
    "SIGNING_PUBLIC_KEY_FIELD",
    "BundleTrust",
    "generate_keypair",
    "load_pinned_public_key",
    "pinned_public_key_path",
    "public_key_b64",
    "sha256_hex",
    "sign_bundle",
    "verify_and_maybe_pin",
]
