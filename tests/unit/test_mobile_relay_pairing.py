"""Tests for mobile_relay_pairing.py (issue #55, Phase 2).

Covers: the ECDH+HKDF key derivation itself (both sides must land on the
same key from only their own private key + the other's public key), the
full pairing handshake end to end (playing "the phone" with plain
MobileRelayClient calls, exactly like the Phase 0 spike's own
test_roundtrip.py played "the daemon"), and PairingStore's persistence,
revocation, and identity-rotation behavior.
"""
from __future__ import annotations

import base64
import json
import threading
import time

import pytest
import requests

from privacyfence.mobile_relay_client import (
    MobileRelayClient,
    MobileRelayConfig,
    encrypt_payload,
)
from privacyfence.mobile_relay_pairing import (
    PAIRING_REQUEST_ID,
    MobileRelayPairingError,
    PairedDevice,
    PairingSession,
    PairingStore,
    _generate_x25519_keypair,
    _handshake_key,
    begin_pairing,
    complete_pairing,
    derive_shared_key,
)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "spikes" / "mobile-relay-phase0" / "relay"))
from relay_server import make_server  # noqa: E402


@pytest.fixture()
def relay_url():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------- #
# ECDH + HKDF key derivation
# ---------------------------------------------------------------------------- #

class TestDeriveSharedKey:
    def test_both_sides_derive_the_same_key(self):
        daemon_private, daemon_public = _generate_x25519_keypair()
        device_private, device_public = _generate_x25519_keypair()

        daemon_side = derive_shared_key(daemon_private, device_public)
        device_side = derive_shared_key(device_private, daemon_public)

        assert daemon_side == device_side
        assert len(daemon_side) == 32

    def test_different_device_gets_a_different_key(self):
        daemon_private, _daemon_public = _generate_x25519_keypair()
        _p1, device1_public = _generate_x25519_keypair()
        _p2, device2_public = _generate_x25519_keypair()

        key1 = derive_shared_key(daemon_private, device1_public)
        key2 = derive_shared_key(daemon_private, device2_public)

        assert key1 != key2

    def test_different_daemon_identity_gets_a_different_key(self):
        """The basis for rotate_identity() revoking every device at once:
        the same device's public key derives a different shared_key under
        a different daemon identity."""
        _p1, device_public = _generate_x25519_keypair()
        daemon1_private, _pub1 = _generate_x25519_keypair()
        daemon2_private, _pub2 = _generate_x25519_keypair()

        key1 = derive_shared_key(daemon1_private, device_public)
        key2 = derive_shared_key(daemon2_private, device_public)

        assert key1 != key2


# ---------------------------------------------------------------------------- #
# Full pairing handshake, end to end against a real relay
# ---------------------------------------------------------------------------- #

def phone_completes_handshake(session: PairingSession, device_name: str = "Alice's iPhone") -> bytes:
    """Plays "the phone": generates its own keypair, encrypts the handshake
    payload under HKDF(pairing_secret) exactly as a real phone would, and
    posts it to the pairing mailbox as a plain HTTP call -- mirroring how
    the Phase 0 spike's own test_roundtrip.py plays "the daemon" with plain
    `requests` calls, since there's no real phone-side client in this
    codebase to instantiate (deliberately: that's Phase 3's job). Returns
    the device's private key, for tests that need to verify the resulting
    shared_key from the phone's side too.

    Posting via plain `requests` here, not MobileRelayClient.post_request(),
    is deliberate: that method always encrypts its payload under
    `config.shared_key` for the *daemon's* everyday request-posting
    direction -- reusing it here would double-encrypt this already-encrypted
    handshake payload under an unrelated placeholder key.
    """
    device_private, device_public = _generate_x25519_keypair()
    handshake_key = _handshake_key(session.pairing_secret)
    ciphertext = encrypt_payload(
        handshake_key,
        {"device_public_key": base64.b64encode(device_public).decode("ascii"), "device_name": device_name},
    )
    response = requests.post(
        f"{session.relay_url}/mailbox/{session.mailbox_id}",
        params={"token": session.token},
        json={"request_id": PAIRING_REQUEST_ID, "payload": {"ciphertext": ciphertext}, "ttl_seconds": 60},
    )
    assert response.status_code == 201, response.text
    return device_private


class TestPairingHandshakeEndToEnd:
    def test_full_handshake_produces_a_working_paired_device(self, relay_url):
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity
        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)

        session = begin_pairing(relay_url, identity)
        device_private = phone_completes_handshake(session, device_name="Test Phone")

        device = complete_pairing(session, identity, poll_timeout_seconds=5)

        assert device.device_name == "Test Phone"
        assert device.mailbox_id == session.mailbox_id
        assert device.token == session.token
        assert not device.revoked
        # The phone's own independently-derived key must match what the
        # daemon stored -- this is the actual point of the handshake.
        assert device.shared_key == derive_shared_key(device_private, identity_public)

    def test_daemon_sends_a_verifiable_pairing_ack(self, relay_url):
        """The phone side would derive shared_key itself and check this ack
        -- proof both sides agree before trusting the channel for anything
        real."""
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity
        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)

        session = begin_pairing(relay_url, identity)
        device_private = phone_completes_handshake(session)
        device = complete_pairing(session, identity, poll_timeout_seconds=5)

        config = MobileRelayConfig(
            relay_url=relay_url, mailbox_id=session.mailbox_id, token=session.token, shared_key=device.shared_key,
        )
        ack = MobileRelayClient(config).poll_decision(PAIRING_REQUEST_ID, overall_timeout_seconds=2)
        assert ack == "approved"

    def test_expired_session_raises_before_polling(self, relay_url):
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity
        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)

        session = begin_pairing(relay_url, identity, pairing_ttl_seconds=-1)

        with pytest.raises(MobileRelayPairingError, match="expired"):
            complete_pairing(session, identity)

    def test_no_handshake_within_timeout_raises(self, relay_url):
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity
        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)

        session = begin_pairing(relay_url, identity)

        with pytest.raises(MobileRelayPairingError, match="No pairing handshake"):
            complete_pairing(session, identity, poll_timeout_seconds=0.3)

    def test_garbage_on_the_mailbox_raises_rather_than_silently_ignoring(self, relay_url):
        """See MobileRelayPairingError's own docstring: a bogus message
        occupying the mailbox's one pending-request slot must not just be
        skipped, or a real handshake queued behind it would stall silently
        for no visible reason."""
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity
        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)

        session = begin_pairing(relay_url, identity)
        config = MobileRelayConfig(
            relay_url=relay_url, mailbox_id=session.mailbox_id, token=session.token, shared_key=b"\x00" * 32,
        )
        MobileRelayClient(config).post_request(PAIRING_REQUEST_ID, {"not": "encrypted-correctly"}, ttl_seconds=60)

        with pytest.raises(MobileRelayPairingError, match="Malformed or undecryptable"):
            complete_pairing(session, identity, poll_timeout_seconds=2)

    def test_wrong_request_id_raises(self, relay_url):
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity
        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)

        session = begin_pairing(relay_url, identity)
        config = MobileRelayConfig(
            relay_url=relay_url, mailbox_id=session.mailbox_id, token=session.token, shared_key=b"\x00" * 32,
        )
        MobileRelayClient(config).post_request("not-the-pairing-id", {"x": 1}, ttl_seconds=60)

        with pytest.raises(MobileRelayPairingError, match="Unexpected request"):
            complete_pairing(session, identity, poll_timeout_seconds=2)


class TestPairingSessionQrPayload:
    def test_qr_payload_carries_no_derived_or_private_key_material(self):
        session = PairingSession(
            relay_url="https://r", mailbox_id="m", token="t",
            daemon_public_key=b"\x01" * 32, pairing_secret=b"\x02" * 32, expires_at=time.time() + 60,
        )
        payload = session.qr_payload()
        serialized = json.dumps(payload)
        assert "daemon_public_key" in payload
        assert "pairing_secret" in payload
        # Nothing that looks like a raw 32-byte private key or derived
        # shared_key should ever be in scope to leak into this dict --
        # this session object doesn't even have those fields.
        assert not hasattr(session, "shared_key")
        assert json.loads(serialized) == payload  # round-trips as plain JSON

    def test_omits_pwa_release_public_key_when_not_configured(self):
        session = PairingSession(
            relay_url="https://r", mailbox_id="m", token="t",
            daemon_public_key=b"\x01" * 32, pairing_secret=b"\x02" * 32, expires_at=time.time() + 60,
        )
        assert "pwa_release_public_key" not in session.qr_payload()

    def test_includes_pwa_release_public_key_when_configured(self):
        session = PairingSession(
            relay_url="https://r", mailbox_id="m", token="t",
            daemon_public_key=b"\x01" * 32, pairing_secret=b"\x02" * 32, expires_at=time.time() + 60,
            pwa_release_public_key=b"\x05" * 65,
        )
        payload = session.qr_payload()
        assert base64.b64decode(payload["pwa_release_public_key"]) == b"\x05" * 65


class TestBeginPairingCarriesPwaReleaseKey:
    def test_pwa_release_public_key_flows_through_to_the_session(self, relay_url):
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity

        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)
        session = begin_pairing(relay_url, identity, pwa_release_public_key=b"\x09" * 65)

        assert session.pwa_release_public_key == b"\x09" * 65

    def test_defaults_to_none(self, relay_url):
        identity_private, identity_public = _generate_x25519_keypair()
        from privacyfence.mobile_relay_pairing import DaemonIdentity

        identity = DaemonIdentity(private_key=identity_private, public_key=identity_public)
        session = begin_pairing(relay_url, identity)

        assert session.pwa_release_public_key is None

    def test_is_expired(self):
        future = PairingSession("r", "m", "t", b"\x00" * 32, b"\x00" * 32, expires_at=time.time() + 60)
        past = PairingSession("r", "m", "t", b"\x00" * 32, b"\x00" * 32, expires_at=time.time() - 1)
        assert future.is_expired() is False
        assert past.is_expired() is True


# ---------------------------------------------------------------------------- #
# PairingStore
# ---------------------------------------------------------------------------- #

def make_device(device_id="dev1", revoked=False) -> PairedDevice:
    return PairedDevice(
        device_id=device_id, device_name="Test Phone", device_public_key=b"\x03" * 32,
        mailbox_id="mbox1", token="tok1", shared_key=b"\x04" * 32, paired_at=time.time(), revoked=revoked,
    )


class TestPairingStoreIdentity:
    def test_get_or_create_identity_creates_on_first_call(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        identity = store.get_or_create_identity()
        assert len(identity.private_key) == 32
        assert len(identity.public_key) == 32

    def test_get_or_create_identity_is_stable_across_calls(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        first = store.get_or_create_identity()
        second = store.get_or_create_identity()
        assert first.private_key == second.private_key

    def test_identity_persists_across_store_instances(self, tmp_path):
        path = str(tmp_path / "store.json")
        first_identity = PairingStore(path).get_or_create_identity()

        reloaded = PairingStore(path).get_or_create_identity()

        assert reloaded.private_key == first_identity.private_key

    def test_store_file_is_created_with_restrictive_permissions(self, tmp_path):
        path = tmp_path / "store.json"
        PairingStore(str(path)).get_or_create_identity()
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_rotate_identity_changes_the_key(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        original = store.get_or_create_identity()

        rotated = store.rotate_identity()

        assert rotated.private_key != original.private_key

    def test_rotate_identity_revokes_every_active_device(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.get_or_create_identity()
        store.add_device(make_device("dev1"))
        store.add_device(make_device("dev2"))

        store.rotate_identity()

        assert store.list_active_devices() == []
        assert all(d.revoked for d in store.list_all_devices())


class TestPairingStoreDevices:
    def test_add_and_list_active_devices(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(make_device("dev1"))

        active = store.list_active_devices()

        assert len(active) == 1
        assert active[0].device_id == "dev1"

    def test_devices_persist_across_store_instances(self, tmp_path):
        path = str(tmp_path / "store.json")
        PairingStore(path).add_device(make_device("dev1"))

        reloaded = PairingStore(path)

        assert [d.device_id for d in reloaded.list_active_devices()] == ["dev1"]
        assert reloaded.list_all_devices()[0].shared_key == make_device().shared_key

    def test_revoke_removes_device_from_active_list_but_keeps_the_record(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(make_device("dev1"))

        revoked = store.revoke("dev1")

        assert revoked is True
        assert store.list_active_devices() == []
        assert len(store.list_all_devices()) == 1
        assert store.list_all_devices()[0].revoked is True

    def test_revoke_takes_effect_immediately_for_a_freshly_loaded_store(self, tmp_path):
        """"Fast, phone-not-required revocation" -- another process/session
        reading the same file sees the revocation without any special
        signal, since it's just a flag in a file the daemon already owns."""
        path = str(tmp_path / "store.json")
        writer = PairingStore(path)
        writer.add_device(make_device("dev1"))
        writer.revoke("dev1")

        reader = PairingStore(path)

        assert reader.list_active_devices() == []

    def test_revoke_unknown_device_returns_false(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        assert store.revoke("does-not-exist") is False

    def test_revoke_already_revoked_device_returns_false(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(make_device("dev1"))
        store.revoke("dev1")
        assert store.revoke("dev1") is False

    def test_remove_deletes_the_record_entirely(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(make_device("dev1"))

        removed = store.remove("dev1")

        assert removed is True
        assert store.list_all_devices() == []

    def test_remove_unknown_device_returns_false(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        assert store.remove("does-not-exist") is False

    def test_multiple_devices_independent_revocation(self, tmp_path):
        """Multi-device support: revoking one device leaves others active."""
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(make_device("dev1"))
        store.add_device(make_device("dev2"))

        store.revoke("dev1")

        active_ids = {d.device_id for d in store.list_active_devices()}
        assert active_ids == {"dev2"}


class TestPairedDeviceToConfig:
    def test_to_config_carries_the_devices_own_connection_details(self):
        device = make_device("dev1")
        config = device.to_config("https://relay.example.org")

        assert config.relay_url == "https://relay.example.org"
        assert config.mailbox_id == device.mailbox_id
        assert config.token == device.token
        assert config.shared_key == device.shared_key
