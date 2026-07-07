"""Unit tests for privacyfence.connectors.telegram.TelegramConnector.

Unlike the other connectors, TelegramPrivacyFenceClient is natively async
(Telethon/MTProto), so this connector awaits it directly instead of
wrapping a sync SDK call in asyncio.to_thread -- the mock client needs
AsyncMock methods accordingly. gate.gated_call is stubbed the same way as
the other connector tests to capture what's sent into the gate.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import telegram as telegram_module
from privacyfence.connectors.telegram import TelegramConnector
from privacyfence.telegram_client import TelegramChat, TelegramClientError, TelegramMessage

from ...helpers import assert_all_tools_leave_an_audit_trail


def make_connector():
    client = AsyncMock()
    return TelegramConnector(client), client


def make_message(**overrides):
    defaults = dict(
        id=1, chat_id=100, chat_name="Family Group", sender_id=200, sender_name="Alice",
        text="see you tomorrow", date="2026-07-06T10:00:00Z", is_outgoing=False,
        media_type="", media_filename="",
    )
    defaults.update(overrides)
    return TelegramMessage(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(telegram_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Telegram tool"):
            await connector.call("telegram_does_not_exist", {})


class TestListChats:
    async def test_auto_accepts_and_maps_fields(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_chats.return_value = [
            TelegramChat(id=100, name="Family Group", username="", chat_type="group", unread_count=3, is_self=False),
        ]

        result = await connector.call("telegram_list_chats", {"limit": 20})

        assert result == [{
            "id": 100, "name": "Family Group", "type": "group", "unread_count": 3, "is_self": False,
        }]
        client.list_chats.assert_called_once_with(20)
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_chats.side_effect = TelegramClientError("not authorized")

        with pytest.raises(RuntimeError, match="not authorized"):
            await connector.call("telegram_list_chats", {})


class TestGetMessages:
    async def test_preview_and_result_field_whitelist(self, gated_call_spy):
        connector, client = make_connector()
        client.get_messages.return_value = [
            make_message(media_type="photo", media_filename="pic.jpg", sender_id=999, is_outgoing=True),
        ]

        result = await connector.call("telegram_get_messages", {"chat_id": 100, "limit": 10})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Chat": "Family Group", "Messages": "1"}
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"chat_id": 100}
        assert "my_email" not in kwargs  # telegram has no email-based auto-accept rules
        client.get_messages.assert_called_once_with(100, 10)

        # Data minimization: media metadata and internal ids are not
        # forwarded to Claude, only the whitelisted fields below.
        assert result == [{"id": 1, "sender_name": "Alice", "text": "see you tomorrow", "date": "2026-07-06T10:00:00Z"}]

    async def test_chat_name_falls_back_to_chat_id_when_no_messages(self, gated_call_spy):
        connector, client = make_connector()
        client.get_messages.return_value = []

        await connector.call("telegram_get_messages", {"chat_id": 555})

        assert gated_call_spy[0]["preview"]["Chat"] == "555"
        assert gated_call_spy[0]["preview"]["Messages"] == "0"

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_messages.side_effect = TelegramClientError("chat not found")

        with pytest.raises(RuntimeError, match="chat not found"):
            await connector.call("telegram_get_messages", {"chat_id": 1})


class TestSearchMessages:
    async def test_preview_and_result_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.search_messages.return_value = [make_message(), make_message(id=2)]

        result = await connector.call("telegram_search_messages", {"query": "tomorrow", "limit": 5})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Query": "tomorrow", "Results": "2"}
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"query": "tomorrow"}
        client.search_messages.assert_called_once_with("tomorrow", 5)
        assert result[0] == {
            "id": 1, "chat_name": "Family Group", "sender_name": "Alice",
            "text": "see you tomorrow", "date": "2026-07-06T10:00:00Z",
        }

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.search_messages.side_effect = TelegramClientError("flood wait")

        with pytest.raises(RuntimeError, match="flood wait"):
            await connector.call("telegram_search_messages", {"query": "x"})


class TestSendMessage:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"id": 42}

        result = await connector.call("telegram_send_message", {"chat_id": 100, "text": "hi"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Chat": "100"}
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "hi"
        assert kwargs["args"] == {"chat_id": 100}
        assert result == {"id": 42}
        client.send_message.assert_called_once_with(100, "hi")

    async def test_summary_truncates_long_text(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {}
        long_text = "y" * 100

        await connector.call("telegram_send_message", {"chat_id": 1, "text": long_text})

        assert gated_call_spy[0]["summary"] == f"To 1: {'y' * 80}…"

    async def test_client_error_after_approval_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.side_effect = TelegramClientError("chat write forbidden")

        with pytest.raises(RuntimeError, match="chat write forbidden"):
            await connector.call("telegram_send_message", {"chat_id": 1, "text": "hi"})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        await assert_all_tools_leave_an_audit_trail(connector, telegram_module, monkeypatch, tmp_path)
