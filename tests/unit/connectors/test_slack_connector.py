"""Unit tests for privacyfence.connectors.slack.SlackConnector.

Same approach as the other connector tests: SlackClient is mocked and
gate.gated_call is stubbed to capture what's sent into the gate.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import slack as slack_module
from privacyfence.connectors.slack import SlackConnector, _message_to_dict
from privacyfence.slack_client import SlackChannel, SlackClientError, SlackMessage


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = SlackConnector(client)
    connector.my_email = my_email
    return connector, client


def make_message(**overrides):
    defaults = dict(
        id="1720000000.000100", channel_id="C123", channel_name="general",
        user_id="U1", user_name="alice", text="hello team",
        thread_ts="", reply_count=0,
    )
    defaults.update(overrides)
    return SlackMessage(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(slack_module, "gated_call", fake_gated_call)
    return calls


class TestMessageToDict:
    def test_maps_all_fields(self):
        m = make_message(thread_ts="1720000000.0001", reply_count=3)
        assert _message_to_dict(m) == {
            "ts": "1720000000.000100", "channel_id": "C123", "channel_name": "general",
            "user_id": "U1", "user_name": "alice", "text": "hello team",
            "thread_ts": "1720000000.0001", "reply_count": 3,
        }


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Slack tool"):
            await connector.call("slack_does_not_exist", {})


class TestListChannels:
    async def test_auto_accepts_and_maps_fields(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_channels.return_value = [
            SlackChannel(id="C1", name="general", is_private=False, topic="chat", purpose="", member_count=42),
        ]

        result = await connector.call("slack_list_channels", {})

        assert result == [{
            "id": "C1", "name": "general", "is_private": False,
            "topic": "chat", "purpose": "", "member_count": 42,
        }]
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]


class TestGetChannelHistory:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_channel_history.return_value = [make_message(text="a" * 100)]

        await connector.call("slack_get_channel_history", {"channel_id": "C123", "limit": 10})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Channel"] == "C123"
        assert kwargs["preview"]["Messages"] == "1"
        assert kwargs["preview"]["First message"] == "a" * 80  # truncated to 80 chars
        assert kwargs["raw_data"] == [make_message(text="a" * 100)]
        assert kwargs["args"] == {"channel_id": "C123"}
        client.get_channel_history.assert_called_once_with("C123", 10)

    async def test_empty_channel_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_channel_history.return_value = []

        await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert gated_call_spy[0]["preview"]["First message"] == "(empty)"
        assert gated_call_spy[0]["preview"]["Messages"] == "0"

    async def test_filtered_data_uses_message_to_dict(self, gated_call_spy):
        connector, client = make_connector()
        msg = make_message()
        client.get_channel_history.return_value = [msg]

        result = await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert result == [_message_to_dict(msg)]


class TestGetThreadReplies:
    async def test_reply_count_excludes_thread_starter(self, gated_call_spy):
        connector, client = make_connector()
        client.get_thread_replies.return_value = [
            make_message(text="starter"), make_message(text="reply 1"), make_message(text="reply 2"),
        ]

        await connector.call("slack_get_thread_replies", {"channel_id": "C123", "thread_ts": "t1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Thread starter"] == "starter"
        assert kwargs["preview"]["Replies"] == "2"
        assert kwargs["args"] == {"channel_id": "C123", "thread_ts": "t1"}

    async def test_empty_thread_replies_count_never_negative(self, gated_call_spy):
        connector, client = make_connector()
        client.get_thread_replies.return_value = []

        await connector.call("slack_get_thread_replies", {"channel_id": "C123", "thread_ts": "t1"})

        assert gated_call_spy[0]["preview"]["Replies"] == "0"
        assert gated_call_spy[0]["preview"]["Thread starter"] == "(empty)"


class TestSearchMessages:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.search_messages.return_value = [make_message(), make_message(id="2")]

        await connector.call("slack_search_messages", {"query": "budget", "count": 5})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Query": "budget", "Results": "2"}
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"query": "budget"}
        client.search_messages.assert_called_once_with("budget", 5)


class TestSendMessage:
    async def test_basic_send_preview_minimal(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"ts": "123.456", "channel_id": "C123"}

        await connector.call("slack_send_message", {"channel_id": "C123", "text": "hi there"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Channel": "C123"}
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "hi there"
        assert kwargs["args"] == {"channel_id": "C123", "thread_ts": ""}
        client.mark_channel_unread_before.assert_not_called()

    async def test_thread_reply_preview_includes_thread(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"ts": "123.456", "channel_id": "C123"}

        await connector.call(
            "slack_send_message", {"channel_id": "C123", "text": "reply", "thread_ts": "100.001"}
        )

        assert gated_call_spy[0]["preview"]["In thread"] == "100.001"
        assert gated_call_spy[0]["args"]["thread_ts"] == "100.001"

    async def test_summary_truncates_long_text(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"ts": "1", "channel_id": "C1"}
        long_text = "x" * 100

        await connector.call("slack_send_message", {"channel_id": "C1", "text": long_text})

        assert gated_call_spy[0]["summary"] == f"To C1: {'x' * 80}…"

    async def test_mark_unread_triggers_follow_up_call_with_resolved_channel(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"ts": "999.001", "channel_id": "D_RESOLVED"}

        await connector.call(
            "slack_send_message", {"channel_id": "U_SELF", "text": "note to self", "mark_unread": True}
        )

        assert gated_call_spy[0]["preview"]["Mark unread"] == "after sending"
        client.mark_channel_unread_before.assert_called_once_with("D_RESOLVED", "999.001")

    async def test_mark_unread_skipped_when_send_result_has_no_ts(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"channel_id": "D_RESOLVED"}  # no "ts"

        await connector.call(
            "slack_send_message", {"channel_id": "U_SELF", "text": "note", "mark_unread": True}
        )

        client.mark_channel_unread_before.assert_not_called()

    async def test_mark_unread_skipped_when_result_is_not_a_dict(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = None

        result = await connector.call(
            "slack_send_message", {"channel_id": "U_SELF", "text": "note", "mark_unread": True}
        )

        assert result is None
        client.mark_channel_unread_before.assert_not_called()


class TestFetchErrorMapping:
    async def test_slack_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_channels.side_effect = SlackClientError("rate limited")

        with pytest.raises(RuntimeError, match="rate limited"):
            await connector.call("slack_list_channels", {})
