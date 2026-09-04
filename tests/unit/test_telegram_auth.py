"""Tests for telegram_auth.py's three coroutines (P8, docs/https-connector-
refactor-plan.md §9.3) -- extracted from settings_controller.py's own
telegram_start_auth/telegram_submit_code/telegram_submit_2fa work()
closures (see test_settings_controller.py's TestTelegramStartAuth/
TestTelegramSubmitCode/TestTelegramSubmit2fa, which still pass unchanged
and now exercise this module indirectly via asyncio.run()).

telethon is mocked the same way test_settings_controller.py's own Telegram
tests do: MagicMock() with AsyncMock() for the awaited methods, patching
the ``telethon.TelegramClient`` name directly (this module imports it
inline inside each coroutine, same as settings_controller.py's closures
did, so patching the module attribute is what actually takes effect).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from privacyfence import telegram_auth


class TestSendCode:
    def test_returns_phone_code_hash_and_disconnects(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash-123"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        result = asyncio.run(telegram_auth.send_code("+123456789", "session.file", 123, "apihash"))

        assert result == "hash-123"
        fake_client.send_code_request.assert_awaited_once_with("+123456789")
        fake_client.disconnect.assert_awaited_once()

    def test_disconnects_even_if_send_code_request_raises(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.send_code_request = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(telegram_auth.send_code("+1", "f", 1, "h"))

        fake_client.disconnect.assert_awaited_once()


class TestSignIn:
    def test_returns_display_name_on_success(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock()
        fake_client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        result = asyncio.run(telegram_auth.sign_in("+1", "12345", "hash", "f", 1, "h"))

        assert result == "Jane Doe"
        fake_client.sign_in.assert_awaited_once_with("+1", "12345", phone_code_hash="hash")

    def test_returns_needs_2fa_sentinel_when_password_required(self, monkeypatch):
        from telethon.errors import SessionPasswordNeededError

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        result = asyncio.run(telegram_auth.sign_in("+1", "12345", "hash", "f", 1, "h"))

        assert result == telegram_auth.NEEDS_2FA
        fake_client.disconnect.assert_awaited_once()


class TestSignIn2fa:
    def test_returns_display_name_on_success(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock()
        fake_client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        result = asyncio.run(telegram_auth.sign_in_2fa("s3cret", "f", 1, "h"))

        assert result == "Jane Doe"
        fake_client.sign_in.assert_awaited_once_with(password="s3cret")
        fake_client.disconnect.assert_awaited_once()
