"""Tests for scripts/generate_pwa_release_key.py (issue #55, Phase 3)."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import generate_pwa_release_key  # noqa: E402


class TestGenerate:
    def test_produces_a_32_byte_der_private_key_and_65_byte_raw_public_key(self):
        keys = generate_pwa_release_key.generate()
        private_der = base64.b64decode(keys["private_key_der_base64"])
        public_raw = base64.b64decode(keys["public_key_base64"])
        assert len(public_raw) == 65  # uncompressed X9.62 point: 0x04 || X(32) || Y(32)
        assert public_raw[0] == 0x04
        assert len(private_der) > 0

    def test_two_calls_produce_different_keys(self):
        first = generate_pwa_release_key.generate()
        second = generate_pwa_release_key.generate()
        assert first["private_key_der_base64"] != second["private_key_der_base64"]

    def test_private_key_actually_loads_and_matches_the_public_key(self):
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_der_private_key,
        )

        keys = generate_pwa_release_key.generate()
        private_key = load_der_private_key(base64.b64decode(keys["private_key_der_base64"]), password=None)
        derived_public = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        assert base64.b64encode(derived_public).decode("ascii") == keys["public_key_base64"]


class TestMain:
    def test_writes_a_0600_file_and_prints_the_public_key(self, tmp_path, capsys):
        out_path = tmp_path / "release_key.json"
        code = generate_pwa_release_key.main(["-o", str(out_path)])

        assert code == 0
        assert (out_path.stat().st_mode & 0o777) == 0o600
        data = json.loads(out_path.read_text())
        assert "private_key_der_base64" in data
        assert "public_key_base64" in data
        assert data["public_key_base64"] in capsys.readouterr().out

    def test_refuses_to_overwrite_an_existing_file(self, tmp_path, capsys):
        out_path = tmp_path / "release_key.json"
        out_path.write_text("existing content")

        code = generate_pwa_release_key.main(["-o", str(out_path)])

        assert code == 1
        assert "already exists" in capsys.readouterr().err
        assert out_path.read_text() == "existing content"
