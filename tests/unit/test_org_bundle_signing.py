"""Tests for org_bundle_signing.py -- SEC-05 (full signing)'s trust-on-
first-use verification core, shared by daemon_main.load_org_config and
settings_controller.install_org_config_bytes. See that module's own
docstring for the model these tests exercise directly (test_daemon_main.py
and test_settings_controller.py cover the two call sites' own wiring)."""
from __future__ import annotations

import base64

import sys

import pytest

pytest.importorskip("cryptography")

from privacyfence import org_bundle_signing as obs


def _signed(bundle: dict, private_key=None):
    if private_key is None:
        private_key, _ = obs.generate_keypair()
    return obs.sign_bundle(bundle, private_key), private_key


class TestShaHex:
    def test_matches_hashlib(self):
        import hashlib

        assert obs.sha256_hex(b"hello") == hashlib.sha256(b"hello").hexdigest()


class TestSignAndVerifyRoundTrip:
    def test_freshly_signed_bundle_verifies_against_its_own_key(self, tmp_path):
        signed, _ = _signed({"mode": "org", "org_name": "Acme"})
        trust = obs.verify_and_maybe_pin(signed, tmp_path)
        assert trust.ok
        assert trust.signed
        assert trust.newly_pinned

    def test_signing_is_deterministic_over_field_order(self):
        private_key, _ = obs.generate_keypair()
        a, _ = _signed({"a": 1, "b": 2}, private_key)
        b, _ = _signed({"b": 2, "a": 1}, private_key)
        assert a["signature"] == b["signature"]

    def test_re_signing_replaces_any_prior_signature(self):
        private_key, _ = obs.generate_keypair()
        once, _ = _signed({"org_name": "Acme"}, private_key)
        twice = obs.sign_bundle(once, private_key)
        assert twice["signature"] == once["signature"]  # same payload -> same signature
        assert "signature" in twice


class TestVerifyAndMaybePin:
    def test_unsigned_bundle_passes_when_nothing_pinned_yet(self, tmp_path):
        trust = obs.verify_and_maybe_pin({"slack": {"client_id": "x"}}, tmp_path)
        assert trust.ok
        assert not trust.signed
        assert not trust.newly_pinned
        assert obs.load_pinned_public_key(tmp_path) is None

    def test_first_signed_bundle_pins_its_key(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)
        assert obs.load_pinned_public_key(tmp_path) is not None

    def test_pinned_key_matches_the_signing_public_key_field(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)
        pinned = obs.load_pinned_public_key(tmp_path)
        assert base64.b64encode(pinned).decode("ascii") == signed["signing_public_key"]

    def test_subsequent_identical_bundle_verifies_without_re_pinning(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        first = obs.verify_and_maybe_pin(signed, tmp_path)
        second = obs.verify_and_maybe_pin(signed, tmp_path)
        assert first.newly_pinned
        assert second.ok and not second.newly_pinned

    def test_tampered_payload_after_pin_fails(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        tampered = dict(signed)
        tampered["org_name"] = "Evil Corp"
        trust = obs.verify_and_maybe_pin(tampered, tmp_path)

        assert not trust.ok

    def test_bundle_signed_by_a_different_key_after_pin_fails(self, tmp_path):
        first, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(first, tmp_path)

        second, _ = _signed({"org_name": "Acme"})  # fresh (different) keypair
        trust = obs.verify_and_maybe_pin(second, tmp_path)

        assert not trust.ok

    def test_downgrade_to_unsigned_after_pin_fails(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        trust = obs.verify_and_maybe_pin({"org_name": "Acme"}, tmp_path)

        assert not trust.ok

    def test_signature_without_matching_embedded_key_is_rejected_pre_pin(self, tmp_path):
        trust = obs.verify_and_maybe_pin({"signature": base64.b64encode(b"x" * 64).decode()}, tmp_path)
        assert not trust.ok

    def test_garbage_signing_public_key_is_rejected_pre_pin(self, tmp_path):
        trust = obs.verify_and_maybe_pin(
            {"signature": base64.b64encode(b"x" * 64).decode(), "signing_public_key": "not-base64!!"},
            tmp_path,
        )
        assert not trust.ok

    def test_self_signed_with_invalid_signature_is_rejected_pre_pin(self, tmp_path):
        signed, _ = _signed({"org_name": "Acme"})
        signed["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
        trust = obs.verify_and_maybe_pin(signed, tmp_path)
        assert not trust.ok
        assert obs.load_pinned_public_key(tmp_path) is None  # never pinned on a failed self-check


class TestPinnedPublicKeyFile:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap, docs/windows-linux-support-plan.md Track B3)",
    )
    def test_pin_file_is_written_with_restrictive_permissions(self, tmp_path):
        import stat

        signed, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(signed, tmp_path)

        path = obs.pinned_public_key_path(tmp_path)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_deleting_the_pin_file_allows_a_new_key_to_be_trusted(self, tmp_path):
        first, _ = _signed({"org_name": "Acme"})
        obs.verify_and_maybe_pin(first, tmp_path)

        obs.pinned_public_key_path(tmp_path).unlink()

        second, _ = _signed({"org_name": "Acme"})  # different keypair
        trust = obs.verify_and_maybe_pin(second, tmp_path)
        assert trust.ok
        assert trust.newly_pinned
