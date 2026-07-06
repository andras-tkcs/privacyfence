"""Tests for app_credentials.py: baked-in build credentials vs. dev env-var
fallback for Telegram's api_id/api_hash.

The generated `_telegram_credentials` module is git-ignored and, in this
dev checkout, may or may not actually exist on disk -- both branches are
forced deterministically via sys.modules rather than relying on that
ambient state.
"""
from __future__ import annotations

import sys
import types

import pytest

from privacyfence import app_credentials

MODULE_NAME = "privacyfence._telegram_credentials"


@pytest.fixture(autouse=True)
def _clean_module_cache(monkeypatch):
    # Ensure each test controls the import outcome explicitly rather than
    # inheriting whatever is already cached in sys.modules.
    monkeypatch.delitem(sys.modules, MODULE_NAME, raising=False)
    yield
    monkeypatch.delitem(sys.modules, MODULE_NAME, raising=False)


class TestTelegramAppCredentials:
    def test_returns_baked_in_credentials_when_module_present(self, monkeypatch):
        fake_module = types.ModuleType(MODULE_NAME)
        fake_module.API_ID = 999
        fake_module.API_HASH = "baked-in-hash"
        monkeypatch.setitem(sys.modules, MODULE_NAME, fake_module)

        assert app_credentials.telegram_app_credentials() == (999, "baked-in-hash")

    def test_baked_in_api_id_is_coerced_to_int(self, monkeypatch):
        fake_module = types.ModuleType(MODULE_NAME)
        fake_module.API_ID = "999"  # some generators might emit a string
        fake_module.API_HASH = "baked-in-hash"
        monkeypatch.setitem(sys.modules, MODULE_NAME, fake_module)

        result = app_credentials.telegram_app_credentials()

        assert result == (999, "baked-in-hash")
        assert isinstance(result[0], int)

    def test_falls_back_to_env_vars_when_module_absent(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)  # force ImportError
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_ID", "123")
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_HASH", "envhash")

        assert app_credentials.telegram_app_credentials() == (123, "envhash")

    def test_none_when_module_absent_and_no_env_vars(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_ID", raising=False)
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_HASH", raising=False)

        assert app_credentials.telegram_app_credentials() is None

    def test_none_when_only_api_id_env_var_set(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_ID", "123")
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_HASH", raising=False)

        assert app_credentials.telegram_app_credentials() is None

    def test_none_when_only_api_hash_env_var_set(self, monkeypatch):
        monkeypatch.setitem(sys.modules, MODULE_NAME, None)
        monkeypatch.delenv("PRIVACYFENCE_TELEGRAM_API_ID", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_HASH", "envhash")

        assert app_credentials.telegram_app_credentials() is None

    def test_baked_in_module_takes_priority_over_env_vars(self, monkeypatch):
        fake_module = types.ModuleType(MODULE_NAME)
        fake_module.API_ID = 999
        fake_module.API_HASH = "baked-in-hash"
        monkeypatch.setitem(sys.modules, MODULE_NAME, fake_module)
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_ID", "123")
        monkeypatch.setenv("PRIVACYFENCE_TELEGRAM_API_HASH", "envhash")

        assert app_credentials.telegram_app_credentials() == (999, "baked-in-hash")
