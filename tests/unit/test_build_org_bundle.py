"""Tests for scripts/build_org_bundle.py's SEC-05 (full signing) support.

This script deliberately doesn't import the ``privacyfence`` package (see
its own module docstring -- it's meant to be runnable standalone, without
a PrivacyFence install) and instead carries its own copy of the
canonicalization/signing logic that mirrors src/privacyfence/
org_bundle_signing.py's. The most important thing these tests establish
is that the two copies actually stay in sync: a bundle signed by the
script must verify against org_bundle_signing.verify_and_maybe_pin(), the
real function the daemon and settings_controller.py use -- if the two
canonicalizations ever drift, every bundle built with this script would
fail to install/start, silently.

Imported by file path (importlib) rather than as a package, since
scripts/ isn't part of the installed ``privacyfence`` distribution.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from privacyfence import org_bundle_signing

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_org_bundle.py"
_spec = importlib.util.spec_from_file_location("build_org_bundle", _SCRIPT_PATH)
build_org_bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_org_bundle)


class TestCanonicalPayloadBytes:
    def test_excludes_signature_field(self):
        with_sig = build_org_bundle._canonical_payload_bytes({"a": 1, "signature": "xyz"})
        without_sig = build_org_bundle._canonical_payload_bytes({"a": 1})
        assert with_sig == without_sig

    def test_key_order_does_not_affect_output(self):
        assert (
            build_org_bundle._canonical_payload_bytes({"b": 2, "a": 1})
            == build_org_bundle._canonical_payload_bytes({"a": 1, "b": 2})
        )

    def test_matches_org_bundle_signing_module_exactly(self):
        """The whole reason both copies exist -- see module docstring.
        This pins them to identical output on the same input so any
        future edit to either that breaks the other fails loudly here."""
        bundle = {"mode": "org", "server": {"issuer_url": "https://x"}, "signature": "stale"}
        assert (
            build_org_bundle._canonical_payload_bytes(bundle)
            == org_bundle_signing._canonical_payload_bytes(bundle)
        )


class TestGenerateSigningKey:
    def test_writes_a_private_key_file_with_restrictive_permissions(self, tmp_path, capsys):
        key_path = tmp_path / "signing_key.pem"

        rc = build_org_bundle._generate_signing_key(str(key_path))

        assert rc == 0
        assert key_path.exists()
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert b"PRIVATE KEY" in key_path.read_bytes()

    def test_prints_the_public_key(self, tmp_path, capsys):
        build_org_bundle._generate_signing_key(str(tmp_path / "key.pem"))
        out = capsys.readouterr().out
        assert "Public key" in out


class TestSignBundle:
    def test_signed_bundle_verifies_against_org_bundle_signing(self, tmp_path):
        """The critical cross-module check: a bundle this script signs
        must be acceptable to the real verification path the daemon and
        settings_controller.py actually run."""
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))

        signed = build_org_bundle._sign_bundle({"mode": "org", "org_name": "Acme"}, str(key_path))

        assert "signature" in signed
        assert "signing_public_key" in signed
        trust = org_bundle_signing.verify_and_maybe_pin(signed, tmp_path / "orgdir")
        assert trust.ok
        assert trust.signed

    def test_tampering_the_signed_bundle_fails_verification(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        signed = build_org_bundle._sign_bundle({"org_name": "Acme"}, str(key_path))

        signed["org_name"] = "Evil Corp"

        trust = org_bundle_signing.verify_and_maybe_pin(signed, tmp_path / "orgdir")
        assert not trust.ok

    def test_non_ed25519_key_file_is_rejected(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path = tmp_path / "rsa_key.pem"
        key_path.write_bytes(rsa_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))

        with pytest.raises(SystemExit, match="not an Ed25519"):
            build_org_bundle._sign_bundle({"org_name": "Acme"}, str(key_path))


class TestMainSigningIntegration:
    def test_mode_org_without_sign_key_is_rejected(self, tmp_path):
        out_path = tmp_path / "org_config.json"
        with pytest.raises(SystemExit):
            build_org_bundle.main([
                "-o", str(out_path), "--mode", "org",
                "--server-issuer-url", "https://pf.example.com",
                "--idp-issuer", "https://idp.example.com",
                "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            ])
        assert not out_path.exists()

    def test_mode_org_with_sign_key_writes_a_signed_bundle(self, tmp_path):
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"

        rc = build_org_bundle.main([
            "-o", str(out_path), "--mode", "org",
            "--server-issuer-url", "https://pf.example.com",
            "--idp-issuer", "https://idp.example.com",
            "--idp-client-id", "cid", "--idp-client-secret", "csecret",
            "--sign-key", str(key_path),
        ])

        assert rc == 0
        bundle = json.loads(out_path.read_text())
        assert bundle["mode"] == "org"
        assert "signature" in bundle
        trust = org_bundle_signing.verify_and_maybe_pin(bundle, tmp_path / "orgdir")
        assert trust.ok and trust.signed

    def test_merge_without_sign_key_strips_a_stale_signature(self, tmp_path):
        """Re-running without --sign-key after a bundle was previously
        signed must not leave a now-invalid signature/key pair sitting in
        the output -- see main()'s own comment on why these are always
        stripped and only re-added by an explicit (re-)sign this run."""
        key_path = tmp_path / "key.pem"
        build_org_bundle._generate_signing_key(str(key_path))
        out_path = tmp_path / "org_config.json"
        build_org_bundle.main([
            "-o", str(out_path), "--org-name", "Acme",
            "--slack-client-id", "id1", "--slack-client-secret", "secret1",
            "--sign-key", str(key_path),
        ])
        assert "signature" in json.loads(out_path.read_text())

        build_org_bundle.main([
            "-o", str(out_path), "--merge",
            "--salesforce-consumer-key", "ckey", "--salesforce-consumer-secret", "csecret",
        ])

        bundle = json.loads(out_path.read_text())
        assert "signature" not in bundle
        assert "signing_public_key" not in bundle
        assert bundle["slack"]["client_id"] == "id1"  # the earlier merge survives
        assert bundle["salesforce"]["consumer_key"] == "ckey"

    def test_sign_key_needs_cryptography_gives_a_clear_error(self, tmp_path, monkeypatch):
        # A plain sys.modules["cryptography"] = None wouldn't reliably
        # force ImportError here -- once a submodule has been imported
        # once, its parent package object already carries it as a real
        # attribute, so `from cryptography.hazmat... import ed25519` can
        # resolve via attribute access without re-checking sys.modules.
        # Evict every cached cryptography.* module first so the import
        # is actually re-attempted (and short-circuits on the None
        # sentinel) rather than served from the attribute cache.
        for name in list(sys.modules):
            if name == "cryptography" or name.startswith("cryptography."):
                monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, "cryptography", None)

        with pytest.raises(SystemExit, match="pip install cryptography"):
            build_org_bundle._generate_signing_key(str(tmp_path / "key.pem"))
