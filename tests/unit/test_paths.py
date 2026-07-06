"""Tests for paths.py: dev vs. bundled-.app path resolution.

A wrong answer here means credentials/config/logs end up in the wrong
place after packaging (e.g. an .app writing into its own read-only bundle
instead of ~/.privacyfence), so both branches of every function are
covered explicitly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from privacyfence import paths


class TestIsBundled:
    def test_false_when_neither_attribute_set(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert paths.is_bundled() is False

    def test_false_when_frozen_but_no_meipass(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert paths.is_bundled() is False

    def test_false_when_meipass_but_not_frozen(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/some/bundle", raising=False)
        assert paths.is_bundled() is False

    def test_true_when_both_set(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/some/bundle", raising=False)
        assert paths.is_bundled() is True


class TestDataDir:
    def test_dev_mode_resolves_to_project_root_relative_to_this_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        fake_module_file = tmp_path / "src" / "privacyfence" / "paths.py"
        fake_module_file.parent.mkdir(parents=True)
        monkeypatch.setattr(paths, "__file__", str(fake_module_file))

        result = paths.data_dir()

        assert result == tmp_path
        assert result.is_dir()  # mkdir(parents=True, exist_ok=True) was called

    def test_bundled_mode_resolves_under_home_and_creates_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = paths.data_dir()

        assert result == tmp_path / ".privacyfence"
        assert result.is_dir()


class TestOrgDir:
    def test_is_a_subdirectory_of_data_dir_and_gets_created(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.org_dir()

        assert result == tmp_path / "org"
        assert result.is_dir()


class TestBundleMacosDir:
    def test_none_when_not_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        assert paths.bundle_macos_dir() is None

    def test_parent_of_executable_when_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(sys, "executable", "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app")

        result = paths.bundle_macos_dir()

        assert result == Path("/Applications/PrivacyFenceApp.app/Contents/MacOS")


class TestAppBundlePath:
    def test_none_when_not_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: False)
        assert paths.app_bundle_path() is None

    def test_app_bundle_root_when_bundled(self, monkeypatch):
        monkeypatch.setattr(paths, "is_bundled", lambda: True)
        monkeypatch.setattr(sys, "executable", "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app")

        result = paths.app_bundle_path()

        assert result == Path("/Applications/PrivacyFenceApp.app")
