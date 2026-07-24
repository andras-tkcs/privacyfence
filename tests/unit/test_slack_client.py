"""Tests for SlackClient's parsing/normalization logic: message/channel/user
normalization, channel-name/user-name resolution caching, pagination in
list_channels, and the error-description helper that surfaces Slack's
"needed scope" hint. These call real SlackClient methods against a
MagicMock stand-in for slack_sdk.WebClient.

Also covers ``authorize_interactive`` (the browser-loopback OAuth v2 flow --
a different shape from the Google clients' InstalledAppFlow, and from
Salesforce's Web Server + PKCE flow, since Slack's ``oauth_v2_access``
exchange goes through ``slack_sdk.WebClient`` rather than a raw HTTP POST).
As with test_salesforce_client.py, ``run_browser_oauth`` (the
``oauth_loopback`` module boundary) is mocked with a fake that invokes the
real ``exchange`` closure it receives, so the exchange/error-wrapping logic
in ``authorize_interactive`` runs for real, with only ``WebClient`` mocked
underneath. Slack user tokens have no refresh flow in this client (unlike
the Google/Salesforce clients), so there is no expired-token/refresh
lifecycle to test here -- only the initial authorize + token-file save path.
"""
from __future__ import annotations

import json
import stat
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.oauth_loopback import OAuthLoopbackError
from privacyfence.slack_client import (
    SlackChannel,
    SlackClient,
    SlackClientError,
    SlackFile,
    SlackUser,
    authorize_interactive,
    load_token_file,
)
from slack_sdk.errors import SlackApiError

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "slack"


def make_client(web_client: MagicMock) -> SlackClient:
    client = SlackClient(user_token="xoxp-fake-token")
    client._client = web_client
    return client


def slack_error(error: str = "not_authed", needed: str | None = None) -> SlackApiError:
    response = {"ok": False, "error": error}
    if needed:
        response["needed"] = needed
    return SlackApiError("request failed", response)


class _FakeSlackResponse(dict):
    """Minimal stand-in for slack_sdk's SlackResponse: dict-like .get(), plus
    the .data attribute authorize_interactive's exchange() returns."""

    @property
    def data(self):
        return dict(self)


def _invoke_exchange(build_authorize_url, exchange, port, path):
    """Fake run_browser_oauth: skip the real browser/HTTP server and just
    call the exchange closure with a fake authorization code -- this is what
    drives exchange()'s own WebClient.oauth_v2_access + error-wrapping logic."""
    redirect_uri = f"http://127.0.0.1:{port}{path}"
    return exchange("auth-code-123", redirect_uri, "code-verifier-abc")


# ---------------------------------------------------------------------------- #
# Construction
# ---------------------------------------------------------------------------- #

class TestConstruction:
    def test_empty_token_raises(self):
        with pytest.raises(SlackClientError, match="No Slack user token"):
            SlackClient(user_token="")


# ---------------------------------------------------------------------------- #
# load_token_file
# ---------------------------------------------------------------------------- #

class TestLoadTokenFile:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SlackClientError, match="No Slack token found"):
            load_token_file(str(tmp_path / "nope.json"))

    def test_loads_valid_json(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text('{"access_token": "xoxp-1", "email": "me@x.com"}')
        assert load_token_file(str(path)) == {"access_token": "xoxp-1", "email": "me@x.com"}


# ---------------------------------------------------------------------------- #
# authorize_interactive: browser-loopback OAuth v2 flow. run_browser_oauth
# (the oauth_loopback module boundary) is mocked with a fake that invokes
# the real exchange closure it was given, so the WebClient.oauth_v2_access
# call and error-wrapping logic run for real, with only WebClient mocked
# below that.
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractive:
    def test_code_exchange_slack_api_error_becomes_slack_client_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", _invoke_exchange)
        mock_client = MagicMock()
        mock_client.oauth_v2_access.side_effect = slack_error("invalid_code")
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))

        with pytest.raises(SlackClientError, match="Slack OAuth exchange failed"):
            authorize_interactive("cid", "csecret", str(tmp_path / "token.json"))

    def test_code_exchange_not_ok_response_becomes_slack_client_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", _invoke_exchange)
        mock_client = MagicMock()
        mock_client.oauth_v2_access.return_value = _FakeSlackResponse({"ok": False, "error": "bad_redirect_uri"})
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))

        with pytest.raises(SlackClientError, match="Slack OAuth exchange failed: bad_redirect_uri"):
            authorize_interactive("cid", "csecret", str(tmp_path / "token.json"))

    def test_loopback_failure_becomes_slack_client_error(self, monkeypatch, tmp_path):
        def raiser(*a, **kw):
            raise OAuthLoopbackError("timed out waiting for sign-in")
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", raiser)

        with pytest.raises(SlackClientError, match="Slack sign-in failed.*timed out"):
            authorize_interactive("cid", "csecret", str(tmp_path / "token.json"))

    def test_missing_access_token_in_authed_user_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", _invoke_exchange)
        mock_client = MagicMock()
        mock_client.oauth_v2_access.return_value = _FakeSlackResponse({"ok": True, "authed_user": {}})
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))

        with pytest.raises(SlackClientError, match="did not return a user access token"):
            authorize_interactive("cid", "csecret", str(tmp_path / "token.json"))

    def test_successful_flow_saves_token_with_restricted_permissions(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", _invoke_exchange)
        mock_client = MagicMock()
        mock_client.oauth_v2_access.return_value = _FakeSlackResponse({
            "ok": True,
            "authed_user": {"id": "U1", "access_token": "xoxp-abc"},
            "team": {"id": "T1", "name": "Acme"},
        })
        mock_client.users_info.return_value = {"user": {"profile": {"email": "me@acme.com"}}}
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))
        token_file = tmp_path / "nested" / "token.json"

        result = authorize_interactive("cid", "csecret", str(token_file))

        assert result == {
            "access_token": "xoxp-abc", "user_id": "U1", "team_id": "T1",
            "team_name": "Acme", "email": "me@acme.com",
        }
        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved == result
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_account_email_lookup_failure_is_non_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", _invoke_exchange)
        mock_client = MagicMock()
        mock_client.oauth_v2_access.return_value = _FakeSlackResponse({
            "ok": True,
            "authed_user": {"id": "U1", "access_token": "xoxp-abc"},
            "team": {"id": "T1", "name": "Acme"},
        })
        mock_client.users_info.side_effect = slack_error("missing_scope")
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))

        result = authorize_interactive("cid", "csecret", str(tmp_path / "token.json"))

        assert result["email"] == ""

    def test_chmod_failure_is_non_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr("privacyfence.slack_client.run_browser_oauth", _invoke_exchange)
        mock_client = MagicMock()
        mock_client.oauth_v2_access.return_value = _FakeSlackResponse({
            "ok": True,
            "authed_user": {"id": "U1", "access_token": "xoxp-abc"},
            "team": {},
        })
        mock_client.users_info.return_value = {"user": {"profile": {}}}
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))
        monkeypatch.setattr("os.chmod", MagicMock(side_effect=OSError("read-only filesystem")))
        token_file = tmp_path / "token.json"

        result = authorize_interactive("cid", "csecret", str(token_file))  # must not raise

        assert token_file.exists()
        assert result["access_token"] == "xoxp-abc"


# ---------------------------------------------------------------------------- #
# _clamp
# ---------------------------------------------------------------------------- #

class TestClamp:
    @pytest.mark.parametrize("value,default,hi,expected", [
        (50, 50, 1000, 50), (0, 50, 1000, 1), (-5, 50, 1000, 1), (5000, 50, 1000, 1000),
        ("20", 50, 1000, 20), ("nope", 50, 1000, 50), (None, 50, 1000, 50),
    ])
    def test_clamps(self, value, default, hi, expected):
        assert SlackClient._clamp(value, default=default, hi=hi) == expected


# ---------------------------------------------------------------------------- #
# _describe_error
# ---------------------------------------------------------------------------- #

class TestDescribeError:
    def test_includes_needed_scope_when_present(self):
        exc = slack_error("missing_scope", needed="channels:read")
        assert SlackClient._describe_error(exc) == "missing_scope (needed scope: channels:read)"

    def test_error_only_without_needed_scope(self):
        exc = slack_error("not_authed")
        assert SlackClient._describe_error(exc) == "not_authed"


# ---------------------------------------------------------------------------- #
# _parse_channel / _parse_user / _parse_file / _parse_ts
# ---------------------------------------------------------------------------- #

class TestParseChannel:
    def test_full_channel(self):
        client = make_client(MagicMock())
        raw = {
            "id": "C1", "name": "general", "is_private": False,
            "topic": {"value": "general chat"}, "purpose": {"value": "everything"},
            "num_members": 42,
        }
        assert client._parse_channel(raw) == SlackChannel(
            id="C1", name="general", is_private=False, topic="general chat", purpose="everything", member_count=42,
        )

    def test_missing_topic_and_purpose_default_empty(self):
        client = make_client(MagicMock())
        channel = client._parse_channel({"id": "C1", "name": "x"})
        assert channel.topic == ""
        assert channel.purpose == ""

    def test_short_summary_reflects_privacy_and_member_count(self):
        priv = SlackChannel(id="C1", name="secret", is_private=True, member_count=3)
        pub = SlackChannel(id="C2", name="general", is_private=False, member_count=100)
        assert priv.short_summary() == "#secret (private, 3 members)"
        assert pub.short_summary() == "#general (public, 100 members)"


class TestParseUser:
    def test_full_user(self):
        client = make_client(MagicMock())
        raw = {"id": "U1", "name": "jdoe", "real_name": "Jane Doe", "is_bot": False,
               "profile": {"email": "jane@x.com"}}
        assert client._parse_user(raw) == SlackUser(
            id="U1", name="jdoe", real_name="Jane Doe", email="jane@x.com", is_bot=False,
        )

    def test_real_name_falls_back_to_profile_when_top_level_missing(self):
        client = make_client(MagicMock())
        raw = {"id": "U1", "name": "jdoe", "profile": {"real_name": "Profile Name"}}
        user = client._parse_user(raw)
        assert user.real_name == "Profile Name"

    def test_short_summary_prefers_real_name_then_name_then_id(self):
        assert SlackUser(id="U1", name="jdoe", real_name="Jane").short_summary() == "Jane"
        assert SlackUser(id="U1", name="jdoe").short_summary() == "jdoe"
        assert SlackUser(id="U1", name="").short_summary() == "U1"


class TestParseFile:
    def test_full_file(self):
        raw = {"id": "F1", "name": "a.png", "title": "Screenshot", "mimetype": "image/png",
               "size": 2048, "url_private": "https://x/a.png"}
        assert SlackClient._parse_file(raw) == SlackFile(
            id="F1", name="a.png", title="Screenshot", mimetype="image/png", size=2048,
            url_private="https://x/a.png",
        )

    def test_missing_size_defaults_zero(self):
        assert SlackClient._parse_file({}).size == 0


class TestParseTs:
    def test_valid_ts_parses_to_utc_datetime(self):
        dt = SlackClient._parse_ts("1697030400.001500")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_empty_ts_returns_none(self):
        assert SlackClient._parse_ts("") is None

    def test_garbage_ts_returns_none(self):
        assert SlackClient._parse_ts("not-a-number") is None


# ---------------------------------------------------------------------------- #
# _parse_message: user resolution, files, thread info
# ---------------------------------------------------------------------------- #

class TestParseMessage:
    def test_full_message_with_user_resolution(self):
        web_client = MagicMock()
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane"}}
        client = make_client(web_client)

        raw = {
            "user": "U1", "text": "hello", "ts": "1697030400.000100",
            "thread_ts": "1697030400.000100", "reply_count": 3,
            "attachments": [{"fallback": "x"}],
            "files": [{"id": "F1", "name": "a.png"}],
        }
        msg = client._parse_message(raw, "C1", "general")

        assert msg.channel_id == "C1"
        assert msg.channel_name == "general"
        assert msg.user_id == "U1"
        assert msg.user_name == "Jane"
        assert msg.text == "hello"
        assert msg.reply_count == 3
        assert msg.files == [SlackFile(id="F1", name="a.png", title="", mimetype="", size=0)]

    def test_bot_message_uses_bot_id_as_user_id(self):
        client = make_client(MagicMock())
        raw = {"bot_id": "B1", "text": "automated", "ts": "1697030400.0"}
        msg = client._parse_message(raw, "C1", "general")
        assert msg.user_id == "B1"

    def test_no_user_or_bot_id_yields_empty_user_name_without_api_call(self):
        web_client = MagicMock()
        client = make_client(web_client)
        raw = {"text": "system message", "ts": "1697030400.0"}
        msg = client._parse_message(raw, "C1", "general")
        assert msg.user_id == ""
        assert msg.user_name == ""
        web_client.users_info.assert_not_called()

    def test_short_summary_truncates_long_text(self):
        client = make_client(MagicMock())
        raw = {"text": "x" * 100, "ts": "1"}
        msg = client._parse_message(raw, "C1", "general")
        assert msg.short_summary().endswith("…")
        assert len(msg.short_summary()) <= len("(unknown user): ") + 60

    def test_short_summary_no_text_shows_placeholder(self):
        client = make_client(MagicMock())
        msg = client._parse_message({"ts": "1"}, "C1", "general")
        assert "(no text)" in msg.short_summary()


# ---------------------------------------------------------------------------- #
# resolve_channel_name / _resolve_user_name: caching + error swallowing
# ---------------------------------------------------------------------------- #

class TestResolveChannelName:
    def test_empty_channel_id_returns_empty_without_api_call(self):
        web_client = MagicMock()
        client = make_client(web_client)
        assert client.resolve_channel_name("") == ""
        web_client.conversations_info.assert_not_called()

    def test_resolves_and_caches(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        client = make_client(web_client)

        assert client.resolve_channel_name("C1") == "general"
        assert client.resolve_channel_name("C1") == "general"
        web_client.conversations_info.assert_called_once()

    def test_api_error_is_swallowed_returns_empty(self):
        web_client = MagicMock()
        web_client.conversations_info.side_effect = slack_error()
        client = make_client(web_client)
        assert client.resolve_channel_name("C1") == ""


class TestResolveIsGroupDm:
    def test_empty_channel_id_returns_false_without_api_call(self):
        web_client = MagicMock()
        client = make_client(web_client)
        assert client.resolve_is_group_dm("") is False
        web_client.conversations_info.assert_not_called()

    def test_mpim_channel_resolves_true_and_caches(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"is_mpim": True}}
        client = make_client(web_client)

        assert client.resolve_is_group_dm("G1") is True
        assert client.resolve_is_group_dm("G1") is True
        web_client.conversations_info.assert_called_once()

    def test_non_mpim_channel_resolves_false(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"is_mpim": False, "is_private": True}}
        client = make_client(web_client)
        assert client.resolve_is_group_dm("G2") is False

    def test_api_error_is_swallowed_returns_false(self):
        web_client = MagicMock()
        web_client.conversations_info.side_effect = slack_error()
        client = make_client(web_client)
        assert client.resolve_is_group_dm("G1") is False

    def test_group_dm_cache_is_independent_of_channel_name_cache(self):
        # Same conversations.info response backs both resolvers, but each
        # keeps its own cache -- calling one must not short-circuit the other.
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "", "is_mpim": True}}
        client = make_client(web_client)

        assert client.resolve_channel_name("G1") == ""
        assert client.resolve_is_group_dm("G1") is True
        assert web_client.conversations_info.call_count == 2


class TestResolveUserName:
    def test_empty_user_id_returns_empty(self):
        client = make_client(MagicMock())
        assert client._resolve_user_name("") == ""

    def test_error_is_swallowed_returns_empty(self):
        web_client = MagicMock()
        web_client.users_info.side_effect = slack_error()
        client = make_client(web_client)
        assert client._resolve_user_name("U1") == ""


# ---------------------------------------------------------------------------- #
# check_connection
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_team_name(self):
        web_client = MagicMock()
        web_client.auth_test.return_value = {"team": "Acme Corp", "user": "jdoe"}
        client = make_client(web_client)
        assert client.check_connection() == "Acme Corp"

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.auth_test.side_effect = slack_error("invalid_auth")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="Slack connection check failed"):
            client.check_connection()


# ---------------------------------------------------------------------------- #
# list_channels: pagination
# ---------------------------------------------------------------------------- #

class TestListChannels:
    def test_single_page_no_cursor(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": ""}
        }
        client = make_client(web_client)
        channels = client.list_channels()
        assert len(channels) == 1
        assert web_client.conversations_list.call_count == 1

    def test_paginates_until_cursor_exhausted(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "name": "a"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "C2", "name": "b"}], "response_metadata": {"next_cursor": ""}},
        ]
        client = make_client(web_client)
        channels = client.list_channels(max_results=100)
        assert [c.id for c in channels] == ["C1", "C2"]
        assert web_client.conversations_list.call_count == 2

    def test_stops_once_max_results_reached_even_with_more_pages(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": f"C{i}", "name": f"c{i}"} for i in range(5)],
            "response_metadata": {"next_cursor": "more"},
        }
        client = make_client(web_client)
        channels = client.list_channels(max_results=5)
        assert len(channels) == 5

    def test_channel_name_cache_populated_during_listing(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general"}], "response_metadata": {}
        }
        client = make_client(web_client)
        client.list_channels()
        assert client._channel_name_cache["C1"] == "general"

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="list_channels failed"):
            client.list_channels()


# ---------------------------------------------------------------------------- #
# get_channel_history / get_thread_replies / search_messages
# ---------------------------------------------------------------------------- #

class TestGetChannelHistory:
    def test_requires_channel_id(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires a channel_id"):
            client.get_channel_history("")

    def test_oldest_latest_passed_through_only_when_given(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        web_client.conversations_history.return_value = {"messages": []}
        client = make_client(web_client)
        client.get_channel_history("C1")
        kwargs = web_client.conversations_history.call_args.kwargs
        assert "oldest" not in kwargs and "latest" not in kwargs

        client.get_channel_history("C1", oldest="100", latest="200")
        kwargs = web_client.conversations_history.call_args.kwargs
        assert kwargs["oldest"] == "100"
        assert kwargs["latest"] == "200"

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        web_client.conversations_history.side_effect = slack_error()
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="get_channel_history"):
            client.get_channel_history("C1")


class TestGetThreadReplies:
    def test_requires_channel_id_and_thread_ts(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires a channel_id and thread_ts"):
            client.get_thread_replies("", "1.0")
        with pytest.raises(SlackClientError, match="requires a channel_id and thread_ts"):
            client.get_thread_replies("C1", "")

    def test_maps_replies(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        web_client.conversations_replies.return_value = {"messages": [{"text": "reply", "ts": "1"}]}
        client = make_client(web_client)
        replies = client.get_thread_replies("C1", "1.0")
        assert replies[0].text == "reply"


class TestSearchMessages:
    def test_requires_non_empty_query(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="non-empty query"):
            client.search_messages("")

    def test_uses_channel_name_from_match_when_present(self):
        web_client = MagicMock()
        web_client.search_messages.return_value = {
            "messages": {"matches": [{"text": "found", "ts": "1", "channel": {"id": "C1", "name": "general"}}]}
        }
        client = make_client(web_client)
        results = client.search_messages("query")
        assert results[0].channel_name == "general"
        web_client.conversations_info.assert_not_called()

    def test_falls_back_to_resolving_channel_name_when_absent(self):
        web_client = MagicMock()
        web_client.search_messages.return_value = {
            "messages": {"matches": [{"text": "found", "ts": "1", "channel": {"id": "C1"}}]}
        }
        web_client.conversations_info.return_value = {"channel": {"name": "resolved"}}
        client = make_client(web_client)
        results = client.search_messages("query")
        assert results[0].channel_name == "resolved"

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.search_messages.side_effect = slack_error()
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="search_messages failed"):
            client.search_messages("q")


# ---------------------------------------------------------------------------- #
# send_message / mark_channel_unread_before / get_user_info
# ---------------------------------------------------------------------------- #

class TestSendMessage:
    def test_requires_channel_id_and_text(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires a channel_id"):
            client.send_message("", "hi")
        with pytest.raises(SlackClientError, match="non-empty text"):
            client.send_message("C1", "")

    def test_thread_ts_included_only_when_given(self):
        web_client = MagicMock()
        web_client.chat_postMessage.return_value = {"ts": "1", "channel": "C1"}
        client = make_client(web_client)
        client.send_message("C1", "hi")
        assert "thread_ts" not in web_client.chat_postMessage.call_args.kwargs

        client.send_message("C1", "hi", thread_ts="1.0")
        assert web_client.chat_postMessage.call_args.kwargs["thread_ts"] == "1.0"

    def test_returns_resolved_channel_from_response(self):
        web_client = MagicMock()
        web_client.chat_postMessage.return_value = {"ts": "1", "channel": "D999"}
        client = make_client(web_client)
        result = client.send_message("U1", "hi")
        assert result == {"channel_id": "D999", "ts": "1", "text": "hi"}

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.chat_postMessage.side_effect = slack_error("channel_not_found")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="send_message"):
            client.send_message("C1", "hi")


class TestMarkChannelUnreadBefore:
    def test_requires_channel_id_and_ts(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires channel_id and ts"):
            client.mark_channel_unread_before("", "1.0")
        with pytest.raises(SlackClientError, match="requires channel_id and ts"):
            client.mark_channel_unread_before("C1", "")

    def test_marks_just_before_given_ts(self):
        web_client = MagicMock()
        client = make_client(web_client)
        client.mark_channel_unread_before("C1", "1697030400.000000")
        call_kwargs = web_client.conversations_mark.call_args.kwargs
        assert call_kwargs["channel"] == "C1"
        assert float(call_kwargs["ts"]) < 1697030400.000000

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.conversations_mark.side_effect = slack_error()
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="mark_channel_unread_before"):
            client.mark_channel_unread_before("C1", "1.0")


class TestGetUserInfo:
    def test_requires_user_id(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires a user_id"):
            client.get_user_info("")

    def test_caches_across_calls(self):
        web_client = MagicMock()
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe"}}
        client = make_client(web_client)

        first = client.get_user_info("U1")
        second = client.get_user_info("U1")

        assert first == second
        web_client.users_info.assert_called_once()

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.users_info.side_effect = slack_error("user_not_found")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="get_user_info"):
            client.get_user_info("U1")


# ---------------------------------------------------------------------------- #
# Live fixture replay
# ---------------------------------------------------------------------------- #

class TestLiveFixtureParsing:
    """Replays a fixture recorded from the real, [QATEST]-tagged seed thread
    in privacyfence-qa-control by scripts/qa_fixture_recorder.py --record
    slack -- real API shape, not hand-authored. Skipped (not failed) until
    that fixture exists; see tests/fixtures/live/README.md and
    docs/testing-policy.md. Re-record via that
    script if this ever starts failing after a genuine Slack API change.
    """

    def test_get_thread_replies_fixture_still_parses(self):
        path = LIVE_FIXTURES_DIR / "get_thread_replies.json"
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record slack` locally first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        messages = raw.get("messages", [])
        assert messages, "recorded fixture has no messages"

        # The recorded fixture's author id is already the redaction
        # placeholder, not a real user -- users.info is mocked the same way
        # TestParseMessage does above, rather than hitting the network.
        web_client = MagicMock()
        web_client.users_info.return_value = {"user": {"id": messages[0].get("user", ""), "name": "qauser"}}
        client = make_client(web_client)

        starter = client._parse_message(messages[0], "C1", "privacyfence-qa-control")

        assert starter.text and "[QATEST]" in starter.text
