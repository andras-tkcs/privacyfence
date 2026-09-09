"""Tests for scripts/r2_release.py's version -> channel mapping.

This script deliberately doesn't import the ``privacyfence`` package (see its own module
docstring) and instead carries its own copy of the version-parsing regex, mirroring
src/privacyfence/update_checker.py's. Imported by file path (importlib) rather than as a package,
since scripts/ isn't part of the installed ``privacyfence`` distribution -- same pattern as
tests/unit/test_build_org_bundle.py.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "r2_release.py"
_spec = importlib.util.spec_from_file_location("r2_release", _SCRIPT_PATH)
r2_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r2_release)


class TestChannelForVersion:
    @pytest.mark.parametrize(
        ("version", "expected_channel"),
        [
            ("4.1.0", "stable"),
            ("v4.1.0", "stable"),  # tolerates a leading "v" the same way update_checker.py does
            ("4.2.0a1", "alpha"),
            ("4.2.0a13", "alpha"),
            ("4.2.0b1", "beta"),
            ("4.2.0rc1", "rc"),
        ],
    )
    def test_maps_version_to_channel(self, version, expected_channel):
        assert r2_release.channel_for_version(version) == expected_channel

    def test_rejects_dev_build(self):
        with pytest.raises(ValueError, match="between-tags dev build"):
            r2_release.channel_for_version("4.2.1.dev3+gabc1234")

    def test_rejects_unparseable_string(self):
        with pytest.raises(ValueError, match="doesn't look like a release version"):
            r2_release.channel_for_version("not-a-version")


class TestUploadRejectsDevBuild:
    def test_upload_raises_before_touching_r2(self, monkeypatch):
        # A dev-build version should fail fast on channel_for_version(), before upload() ever
        # tries to build an R2 client (which would otherwise require R2 credentials just to hit
        # this error path).
        def _fail_if_called():
            raise AssertionError("_r2_client() should not be called for a dev-build version")

        monkeypatch.setattr(r2_release, "_r2_client", _fail_if_called)

        with pytest.raises(ValueError, match="between-tags dev build"):
            r2_release.upload("4.2.1.dev3+gabc1234", ["setup.py"])
