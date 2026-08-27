"""Tests for scripts/sign_pwa_bundle.py (issue #55, Phase 3).

The real correctness bar for this script is interop with mobile-approval-
pwa/js/release_verify.js's JS-side verification -- covered end to end by
mobile-approval-pwa/tests/test_release_verify_interop.py, which signs with
this script and verifies with the real JS module via Playwright. These
tests cover the script's own Python-side behavior in isolation: exactly
which files it does/doesn't include, that its manifest format matches what
release_verify.js expects to canonicalize, and its CLI error handling.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import generate_pwa_release_key  # noqa: E402
import sign_pwa_bundle  # noqa: E402


def make_release_key(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "release_key.json"
    generate_pwa_release_key.main(["-o", str(path)])
    return path


def make_bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    for name, content in files.items():
        path = bundle_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return bundle_dir


class TestBuildManifest:
    def test_includes_every_file_with_its_sha256(self, tmp_path):
        bundle_dir = make_bundle(tmp_path, {"app.js": "console.log(1)", "sw.js": "// sw"})

        manifest = sign_pwa_bundle.build_manifest(bundle_dir, "v1")

        assert manifest["files"]["app.js"] == hashlib.sha256(b"console.log(1)").hexdigest()
        assert manifest["files"]["sw.js"] == hashlib.sha256(b"// sw").hexdigest()
        assert manifest["version"] == "v1"
        assert "signed_at" in manifest

    def test_excludes_index_html_manifest_and_signature_files(self, tmp_path):
        bundle_dir = make_bundle(tmp_path, {
            "app.js": "x", "index.html": "<html></html>",
            "bundle_manifest.json": "stale", "bundle_manifest.sig": "stale",
        })

        manifest = sign_pwa_bundle.build_manifest(bundle_dir, "v1")

        assert set(manifest["files"]) == {"app.js"}

    def test_excludes_tests_directory(self, tmp_path):
        bundle_dir = make_bundle(tmp_path, {"app.js": "x", "tests/test_something.py": "y"})

        manifest = sign_pwa_bundle.build_manifest(bundle_dir, "v1")

        assert set(manifest["files"]) == {"app.js"}

    def test_includes_files_in_subdirectories_with_relative_paths(self, tmp_path):
        bundle_dir = make_bundle(tmp_path, {"js/app.js": "x", "js/crypto.js": "y"})

        manifest = sign_pwa_bundle.build_manifest(bundle_dir, "v1")

        assert set(manifest["files"]) == {"js/app.js", "js/crypto.js"}


class TestSignManifest:
    def test_produces_a_64_byte_raw_signature(self, tmp_path):
        release_key_path = make_release_key(tmp_path)
        private_key_der = base64.b64decode(json.loads(release_key_path.read_text())["private_key_der_base64"])

        signature = sign_pwa_bundle.sign_manifest(b'{"a":1}', private_key_der)

        assert len(signature) == 64  # raw r(32) || s(32), not DER

    def test_signature_verifies_against_the_public_key(self, tmp_path):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        release_key_path = make_release_key(tmp_path)
        keys = json.loads(release_key_path.read_text())
        private_key_der = base64.b64decode(keys["private_key_der_base64"])
        public_key_raw = base64.b64decode(keys["public_key_base64"])

        message = b'{"files":{"app.js":"deadbeef"}}'
        raw_sig = sign_pwa_bundle.sign_manifest(message, private_key_der)
        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)

        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_raw)
        public_key.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))  # raises on failure

    def test_wrong_key_fails_verification(self, tmp_path):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        signing_key_path = make_release_key(tmp_path / "signer")
        other_key_path = make_release_key(tmp_path / "other")
        private_key_der = base64.b64decode(json.loads(signing_key_path.read_text())["private_key_der_base64"])
        wrong_public_raw = base64.b64decode(json.loads(other_key_path.read_text())["public_key_base64"])

        message = b'{"files":{}}'
        raw_sig = sign_pwa_bundle.sign_manifest(message, private_key_der)
        r, s = int.from_bytes(raw_sig[:32], "big"), int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)

        wrong_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wrong_public_raw)
        try:
            wrong_public_key.verify(der_sig, message, ec.ECDSA(hashes.SHA256()))
            assert False, "expected InvalidSignature"
        except InvalidSignature:
            pass


class TestMainCli:
    def test_signs_and_writes_manifest_and_signature(self, tmp_path, capsys):
        release_key_path = make_release_key(tmp_path)
        bundle_dir = make_bundle(tmp_path, {"app.js": "console.log(1)"})

        code = sign_pwa_bundle.main([
            "--release-key", str(release_key_path), "--bundle-dir", str(bundle_dir), "--bundle-version", "2026.08.27",
        ])

        assert code == 0
        manifest_path = bundle_dir / "bundle_manifest.json"
        sig_path = bundle_dir / "bundle_manifest.sig"
        assert manifest_path.exists()
        assert sig_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["version"] == "2026.08.27"
        assert manifest["files"]["app.js"] == hashlib.sha256(b"console.log(1)").hexdigest()
        assert "app.js" in capsys.readouterr().out

    def test_manifest_bytes_match_canonical_sorted_compact_json(self, tmp_path):
        """release_verify.js's canonicalJson() must reproduce these exact
        bytes -- sorted keys, no whitespace -- for the signature to verify
        on the JS side. This test pins that contract from the Python side."""
        release_key_path = make_release_key(tmp_path)
        bundle_dir = make_bundle(tmp_path, {"app.js": "x"})

        sign_pwa_bundle.main([
            "--release-key", str(release_key_path), "--bundle-dir", str(bundle_dir), "--bundle-version", "v1",
        ])

        raw_bytes = (bundle_dir / "bundle_manifest.json").read_bytes()
        manifest = json.loads(raw_bytes)
        expected = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert raw_bytes == expected

    def test_nonexistent_bundle_dir_returns_1(self, tmp_path, capsys):
        release_key_path = make_release_key(tmp_path)

        code = sign_pwa_bundle.main([
            "--release-key", str(release_key_path), "--bundle-dir", str(tmp_path / "does-not-exist"),
            "--bundle-version", "v1",
        ])

        assert code == 1
        assert "not a directory" in capsys.readouterr().err

    def test_malformed_release_key_returns_1(self, tmp_path, capsys):
        bad_key_path = tmp_path / "bad_key.json"
        bad_key_path.write_text("not json")
        bundle_dir = make_bundle(tmp_path, {"app.js": "x"})

        code = sign_pwa_bundle.main([
            "--release-key", str(bad_key_path), "--bundle-dir", str(bundle_dir), "--bundle-version", "v1",
        ])

        assert code == 1
        assert "Could not read a release key" in capsys.readouterr().err
