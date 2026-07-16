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
from privacyfence.privacy_filter import init_privacy_filter
from privacyfence.slack_client import SlackChannel, SlackClientError, SlackMessage

from ...helpers import assert_all_tools_leave_an_audit_trail


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    # Default to "not resolvable" so tests that don't care about channel-name
    # resolution keep seeing the raw channel id, same as before this was added.
    client.resolve_channel_name.return_value = ""
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
        # Resolved from the fetched message's channel_name ("general", the
        # make_message() default), not the raw channel id -- no extra lookup.
        assert kwargs["preview"]["Channel"] == "#general"
        assert kwargs["preview"]["Messages"] == "1"
        assert kwargs["preview"]["First message"] == "a" * 80  # truncated to 80 chars
        assert kwargs["raw_data"] == [make_message(text="a" * 100)]
        assert kwargs["args"] == {"channel_id": "C123"}
        client.get_channel_history.assert_called_once_with("C123", 10)
        client.resolve_channel_name.assert_not_called()

    async def test_pii_scan_text_is_message_text_only_not_usernames_or_ids(self, gated_call_spy):
        # Regression: user_id/user_name are on every message regardless of
        # content, so scanning the full details_text (which prefixes each
        # line with them) could flag PII that isn't actually in what was
        # said. The scan must only see the message text.
        connector, client = make_connector()
        client.get_channel_history.return_value = [
            make_message(user_name="alice@example.com", text="nothing sensitive"),
        ]

        await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "nothing sensitive"
        assert "alice@example.com" in kwargs["details_text"]  # still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]

    async def test_empty_channel_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_channel_history.return_value = []

        await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert gated_call_spy[0]["preview"]["First message"] == "(empty)"
        assert gated_call_spy[0]["preview"]["Messages"] == "0"

    async def test_empty_channel_falls_back_to_direct_name_lookup(self, gated_call_spy):
        # No messages means no channel_name to read off a message, so the
        # connector must resolve it directly instead of leaving the raw id.
        connector, client = make_connector()
        client.get_channel_history.return_value = []
        client.resolve_channel_name.return_value = "announcements"

        await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert gated_call_spy[0]["preview"]["Channel"] == "#announcements"
        client.resolve_channel_name.assert_called_once_with("C123")

    async def test_channel_name_unresolvable_falls_back_to_raw_id(self, gated_call_spy):
        connector, client = make_connector()
        client.get_channel_history.return_value = []
        client.resolve_channel_name.return_value = ""

        await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert gated_call_spy[0]["preview"]["Channel"] == "C123"

    async def test_filtered_data_uses_message_to_dict(self, gated_call_spy):
        connector, client = make_connector()
        msg = make_message()
        client.get_channel_history.return_value = [msg]

        result = await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert result == [_message_to_dict(msg)]


class TestSlackPrivacyFilter:
    """slack_privacy.categories, enforced -- see privacy_filter.py. Without
    calling init_privacy_filter (every other test class here), every
    category resolves to "allow" and behaves exactly as before this existed;
    these tests are the ones that actually turn a policy on."""

    async def test_message_content_blocked_replaces_text_everywhere(self, gated_call_spy):
        init_privacy_filter({"slack_privacy": {"categories": {"message_content": "block"}}})
        connector, client = make_connector()
        client.get_channel_history.return_value = [make_message(text="the actual secret")]

        result = await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        kwargs = gated_call_spy[0]
        assert "the actual secret" not in kwargs["details_text"]
        assert "the actual secret" not in kwargs["pii_scan_text"]
        assert result[0]["text"] == "[BLOCKED BY PRIVACY FILTER]"

    async def test_user_identity_blocked_replaces_name_and_id(self, gated_call_spy):
        init_privacy_filter({"slack_privacy": {"categories": {"user_identity": "block"}}})
        connector, client = make_connector()
        client.get_channel_history.return_value = [
            make_message(user_name="alice", user_id="U1", text="hello")
        ]

        result = await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert "alice" not in gated_call_spy[0]["details_text"]
        assert result[0]["user_name"] == "[BLOCKED BY PRIVACY FILTER]"
        assert result[0]["user_id"] == "[BLOCKED BY PRIVACY FILTER]"
        assert result[0]["text"] == "hello"  # message_content untouched by this category

    async def test_thread_content_uses_its_own_category_not_message_content(self, gated_call_spy):
        # thread_content and message_content are documented as distinct
        # categories (settings.yaml.example) -- blocking one must not affect
        # the other.
        init_privacy_filter({"slack_privacy": {"categories": {"message_content": "block"}}})
        connector, client = make_connector()
        client.get_thread_replies.return_value = [make_message(text="reply text")]

        result = await connector.call(
            "slack_get_thread_replies", {"channel_id": "C123", "thread_ts": "123.456"}
        )

        assert result[0]["text"] == "reply text"

    async def test_channel_list_blocked_empties_auto_accepted_result(self):
        init_privacy_filter({"slack_privacy": {"categories": {"channel_list": "block"}}})
        connector, client = make_connector()
        client.list_channels.return_value = [
            SlackChannel(id="C1", name="general", is_private=False, topic="", purpose="", member_count=5)
        ]

        result = await connector.call("slack_list_channels", {})

        assert result == []

    async def test_allow_is_the_default_when_unconfigured(self, gated_call_spy):
        # No init_privacy_filter call in this test -- conftest's autouse
        # reset leaves _GROUPS empty, which must resolve to "allow", not
        # "block" -- this module must never fail closed on missing config.
        connector, client = make_connector()
        client.get_channel_history.return_value = [make_message(text="business as usual")]

        result = await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        assert result[0]["text"] == "business as usual"

    async def test_visibility_checklist_reflects_resolved_policy(self, gated_call_spy):
        init_privacy_filter({"slack_privacy": {"categories": {"message_content": "block", "user_identity": "allow"}}})
        connector, client = make_connector()
        client.get_channel_history.return_value = [make_message(text="secret")]

        await connector.call("slack_get_channel_history", {"channel_id": "C123"})

        visibility = gated_call_spy[0]["visibility"]
        assert visibility["Message text"] == "block"
        assert visibility["Usernames"] == "allow"

    async def test_thread_visibility_uses_thread_content_not_message_content(self, gated_call_spy):
        connector, client = make_connector()
        client.get_thread_replies.return_value = [make_message(text="reply")]

        await connector.call("slack_get_thread_replies", {"channel_id": "C123", "thread_ts": "123.456"})

        assert "Reply text" in gated_call_spy[0]["visibility"]
        assert "Message text" not in gated_call_spy[0]["visibility"]


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

    async def test_pii_scan_text_is_message_text_only(self, gated_call_spy):
        connector, client = make_connector()
        client.get_thread_replies.return_value = [
            make_message(user_name="alice@example.com", text="starter"),
            make_message(user_name="bob@example.com", text="reply"),
        ]

        await connector.call("slack_get_thread_replies", {"channel_id": "C123", "thread_ts": "t1"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "starter\nreply"


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

    async def test_pii_scan_text_is_message_text_only(self, gated_call_spy):
        connector, client = make_connector()
        client.search_messages.return_value = [
            make_message(user_name="alice@example.com", text="nothing sensitive"),
        ]

        await connector.call("slack_search_messages", {"query": "budget"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "nothing sensitive"
        assert "alice@example.com" not in kwargs["pii_scan_text"]


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

    async def test_channel_name_resolved_in_preview_and_summary(self, gated_call_spy):
        connector, client = make_connector()
        client.send_message.return_value = {"ts": "123.456", "channel_id": "C123"}
        client.resolve_channel_name.return_value = "team-updates"

        await connector.call("slack_send_message", {"channel_id": "C123", "text": "hi there"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Channel": "#team-updates"}
        assert kwargs["summary"] == "To #team-updates: hi there"
        # The raw id is still what's sent to Slack and what auto-accept rules match on.
        client.send_message.assert_called_once_with("C123", "hi there", "")
        assert kwargs["sender"] == "C123"

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


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        await assert_all_tools_leave_an_audit_trail(connector, slack_module, monkeypatch, tmp_path)
