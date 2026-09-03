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
from privacyfence.principal import Principal, principal_scope


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


class TestSafePrincipalId:
    """P7, org_identity.py's principal_from_claims: an OIDC `sub` claim is
    opaque per spec and may not be filesystem-safe."""

    @pytest.mark.parametrize("safe_id", ["alice", "alice@example.com", "a1b2-c3_d4.e5"])
    def test_already_safe_ids_pass_through_unchanged(self, safe_id):
        assert paths.safe_principal_id(safe_id) == safe_id

    @pytest.mark.parametrize("unsafe_id", ["cn=alice,dc=example,dc=com", "../etc", "a/b", ".."])
    def test_unsafe_ids_are_hashed(self, unsafe_id):
        result = paths.safe_principal_id(unsafe_id)
        assert result != unsafe_id
        assert result.startswith("idp-")
        assert paths._is_safe_principal_id(result)

    def test_hashing_is_deterministic(self):
        assert paths.safe_principal_id("cn=alice") == paths.safe_principal_id("cn=alice")

    def test_different_unsafe_ids_hash_differently(self):
        assert paths.safe_principal_id("cn=alice") != paths.safe_principal_id("cn=bob")


class TestUserDir:
    """P6, docs/https-connector-refactor-plan.md §9.2's storage layout."""

    def test_local_principal_is_data_dir_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        assert paths.user_dir(Principal(id="local")) == tmp_path
        # Not a users/local/ subdirectory -- an existing single-user install
        # needs no migration.
        assert not (tmp_path / "users").exists()

    def test_defaults_to_current_principal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        with principal_scope(Principal(id="local")):
            assert paths.user_dir() == tmp_path

    def test_other_principal_gets_a_users_subdirectory_and_it_is_created(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        result = paths.user_dir(Principal(id="alice@example.com"))

        assert result == tmp_path / "users" / "alice@example.com"
        assert result.is_dir()

    def test_two_principals_get_different_directories(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        alice = paths.user_dir(Principal(id="alice"))
        bob = paths.user_dir(Principal(id="bob"))

        assert alice != bob
        assert alice == tmp_path / "users" / "alice"
        assert bob == tmp_path / "users" / "bob"

    @pytest.mark.parametrize("bad_id", ["../etc", "a/b", "..", ".", ""])
    def test_rejects_unsafe_principal_ids(self, monkeypatch, tmp_path, bad_id):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

        with pytest.raises(ValueError):
            paths.user_dir(Principal(id=bad_id))


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
