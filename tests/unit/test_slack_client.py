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
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from privacyfence.oauth_loopback import OAuthLoopbackError
from privacyfence.slack_client import (
    SlackChannel,
    SlackClient,
    SlackClientError,
    SlackDirectMessage,
    SlackFile,
    SlackGroupChat,
    SlackUser,
    authorize_interactive,
    load_token_file,
)
from slack_sdk.errors import SlackApiError

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "slack"


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until it's true or ``timeout`` elapses -- used to
    synchronize with a real background thread (see
    test_real_background_thread_eventually_refreshes_the_cache) without an
    artificial fixed sleep. Same helper as test_gate.py's own wait_until,
    duplicated rather than imported since these are independent test
    modules with no shared test-support module today.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_client(web_client: MagicMock) -> SlackClient:
    client = SlackClient(user_token="xoxp-fake-token")
    client._client = web_client
    return client


def make_client_with_caches(web_client: MagicMock, tmp_path: Path) -> SlackClient:
    client = SlackClient(
        user_token="xoxp-fake-token",
        user_cache_file=str(tmp_path / "slack_user_cache.json"),
        channel_cache_file=str(tmp_path / "slack_channel_cache.json"),
    )
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

    def test_registers_rate_limit_retry_handler(self):
        # Without this, a 429 ("ratelimited") from a paginated call like
        # conversations.list/users.list -- see refresh_channel_directory/
        # refresh_user_directory -- surfaces immediately as a
        # SlackClientError instead of retrying after Slack's Retry-After
        # window, since WebClient's own default retry handlers only cover
        # connection errors.
        client = SlackClient(user_token="xoxp-fake-token")

        assert any(
            isinstance(h, RateLimitErrorRetryHandler) for h in client._client.retry_handlers
        )


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
# build_authorize_url / exchange_code / save_token_record -- called directly
# (not through run_browser_oauth) by web/routes_connect.py's org-mode
# server-redirect flow (P8, docs/https-connector-refactor-plan.md §9.3).
# ---------------------------------------------------------------------------- #

class TestHoistedFunctions:
    def test_build_authorize_url_direct_call(self):
        from privacyfence.slack_client import build_authorize_url

        url = build_authorize_url("cid", "https://pf.example.com/oauth/callback/slack", "state-1")
        assert url.startswith("https://slack.com/oauth/v2/authorize?")
        assert "client_id=cid" in url
        assert "redirect_uri=https%3A%2F%2Fpf.example.com%2Foauth%2Fcallback%2Fslack" in url
        assert "state=state-1" in url

    def test_exchange_code_returns_the_normalized_record(self, monkeypatch):
        from privacyfence.slack_client import exchange_code

        mock_client = MagicMock()
        mock_client.oauth_v2_access.return_value = _FakeSlackResponse({
            "ok": True, "authed_user": {"id": "U1", "access_token": "xoxp-abc"}, "team": {"id": "T1", "name": "Acme"},
        })
        mock_client.users_info.return_value = {"user": {"profile": {"email": "me@acme.com"}}}
        monkeypatch.setattr("privacyfence.slack_client.WebClient", MagicMock(return_value=mock_client))

        record = exchange_code("cid", "csecret", "auth-code", "https://pf.example.com/cb")

        assert record == {
            "access_token": "xoxp-abc", "user_id": "U1", "team_id": "T1", "team_name": "Acme", "email": "me@acme.com",
        }

    def test_save_token_record_writes_with_restricted_permissions(self, tmp_path):
        from privacyfence.slack_client import save_token_record

        token_file = tmp_path / "nested" / "token.json"
        save_token_record(str(token_file), {"access_token": "xoxp-abc"})

        assert json.loads(token_file.read_text()) == {"access_token": "xoxp-abc"}
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


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


class TestResolvePermalink:
    def test_top_level_message_no_thread_ts(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "eng-team"}}
        client = make_client(web_client)

        result = client.resolve_permalink(
            "https://acme.slack.com/archives/C0123ABCD/p1700000000123456"
        )

        assert result == {
            "channel_id": "C0123ABCD",
            "channel_name": "eng-team",
            "ts": "1700000000.123456",
            "thread_ts": "",
        }

    def test_threaded_reply_carries_thread_root_ts(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "eng-team"}}
        client = make_client(web_client)

        result = client.resolve_permalink(
            "https://acme.slack.com/archives/C0123ABCD/p1700000001000200"
            "?thread_ts=1700000000.123456&cid=C0123ABCD"
        )

        assert result["ts"] == "1700000001.000200"
        assert result["thread_ts"] == "1700000000.123456"

    def test_unresolvable_channel_name_reads_as_empty_not_raising(self):
        web_client = MagicMock()
        web_client.conversations_info.side_effect = slack_error("channel_not_found")
        client = make_client(web_client)

        result = client.resolve_permalink(
            "https://acme.slack.com/archives/C0123ABCD/p1700000000123456"
        )

        assert result["channel_name"] == ""

    def test_empty_url_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires a url"):
            client.resolve_permalink("")

    def test_non_permalink_url_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="Not a recognizable Slack message permalink"):
            client.resolve_permalink("https://example.com/not-a-permalink")

    def test_channel_link_without_message_ts_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="Not a recognizable Slack message permalink"):
            client.resolve_permalink("https://acme.slack.com/archives/C0123ABCD")


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

    def test_participant_match_past_max_results_is_still_found(self):
        # Regression for bug #1 in docs/slack-performance-review.md: a
        # participant filter used to truncate to the first max_results raw
        # channels *before* filtering, so a match sitting past that cutoff
        # was silently invisible. Page 1 is 100 non-matching channels; the
        # one matching channel is on page 2 -- with max_results=100 this
        # must still be found, not dropped for "already having enough" raw
        # channels.
        web_client = MagicMock()
        page1 = {"channels": [{"id": f"C{i:04d}", "name": f"c{i}"} for i in range(100)],
                 "response_metadata": {"next_cursor": "page2"}}
        page2 = {"channels": [{"id": "C0100", "name": "eng"}], "response_metadata": {}}
        web_client.conversations_list.side_effect = [page1, page2]
        web_client.users_conversations.return_value = {"channels": ["C0100"], "response_metadata": {}}
        client = make_client(web_client)

        channels = client.list_channels(participant="U1", max_results=100)

        assert [c.id for c in channels] == ["C0100"]

    def test_participant_match_past_max_results_is_found_via_fallback_walk_too(self):
        # Same regression as above, but through the per-channel fallback
        # path -- a name (not a raw id) with no directory configured, so
        # _resolve_participant_user_ids can't resolve it and falls back to
        # id-then-name membership matching.
        web_client = MagicMock()
        page1 = {"channels": [{"id": f"C{i:04d}", "name": f"c{i}"} for i in range(100)],
                 "response_metadata": {"next_cursor": "page2"}}
        page2 = {"channels": [{"id": "C0100", "name": "eng"}], "response_metadata": {}}
        web_client.conversations_list.side_effect = [page1, page2]
        members_by_channel = {f"C{i:04d}": [] for i in range(100)}
        members_by_channel["C0100"] = ["U1"]
        web_client.conversations_members.side_effect = (
            lambda channel=None, **k: {"members": members_by_channel[channel]}
        )
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "bob", "real_name": "Bob Smith"}}
        client = make_client(web_client)

        channels = client.list_channels(participant="bob", max_results=100)

        assert [c.id for c in channels] == ["C0100"]

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

    def test_participant_id_match_uses_users_conversations_fast_path(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "eng"}, {"id": "C2", "name": "sales"}],
            "response_metadata": {},
        }
        web_client.users_conversations.return_value = {"channels": ["C1"], "response_metadata": {}}
        client = make_client(web_client)

        channels = client.list_channels(participant="U1")

        assert [c.id for c in channels] == ["C1"]
        web_client.users_conversations.assert_called_once_with(
            user="U1", types="public_channel,private_channel", limit=200, cursor=None
        )
        # the fast path replaces the per-channel membership walk entirely.
        web_client.conversations_members.assert_not_called()
        web_client.users_info.assert_not_called()

    def test_users_conversations_failure_falls_back_to_per_channel_walk(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "eng"}], "response_metadata": {},
        }
        web_client.users_conversations.side_effect = slack_error("internal_error")
        web_client.conversations_members.return_value = {"members": ["U1"]}
        client = make_client(web_client)

        channels = client.list_channels(participant="U1")

        assert [c.id for c in channels] == ["C1"]
        web_client.conversations_members.assert_called_once()

    def test_participant_name_match_falls_back_to_resolved_names(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "eng"}], "response_metadata": {},
        }
        web_client.conversations_members.return_value = {"members": ["U1"]}
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}}
        client = make_client(web_client)

        channels = client.list_channels(participant="jane")

        assert [c.id for c in channels] == ["C1"]

    def test_participant_no_match_excludes_channel(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "eng"}], "response_metadata": {},
        }
        web_client.conversations_members.return_value = {"members": ["U1"]}
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe"}}
        client = make_client(web_client)

        assert client.list_channels(participant="nobody") == []

    def test_participant_comma_separated_intersects_users_conversations(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "eng"}, {"id": "C2", "name": "sales"}],
            "response_metadata": {},
        }

        def fake_users_conversations(user=None, **kwargs):
            channels = {"U1": ["C1", "C2"], "U2": ["C1"]}[user]
            return {"channels": channels, "response_metadata": {}}

        web_client.users_conversations.side_effect = fake_users_conversations
        client = make_client(web_client)

        channels = client.list_channels(participant="U1,U2")

        assert [c.id for c in channels] == ["C1"]

    def test_participant_comma_separated_ambiguous_name_falls_back_to_per_channel_walk(self, tmp_path):
        # Two workspace members share a first name -- "jane" can't resolve
        # to a single id via the directory, so the whole call falls back to
        # the old per-channel membership + name walk (still correct, just
        # not on the fast path).
        web_client = MagicMock()
        web_client.users_list.return_value = {
            "members": [
                {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"},
                {"id": "U2", "name": "jsmith", "real_name": "Jane Smith"},
            ],
            "response_metadata": {},
        }
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "eng"}, {"id": "C2", "name": "sales"}],
            "response_metadata": {},
        }
        members_by_channel = {"C1": ["U1", "U2"], "C2": ["U1"]}  # C1: both Janes, C2: only Jane Doe
        web_client.conversations_members.side_effect = (
            lambda channel=None, **kwargs: {"members": members_by_channel[channel]}
        )
        client = make_client_with_caches(web_client, tmp_path)
        client.refresh_user_directory()

        channels = client.list_channels(participant="jane")

        assert {c.id for c in channels} == {"C1", "C2"}
        web_client.users_conversations.assert_not_called()


# ---------------------------------------------------------------------------- #
# list_dms
# ---------------------------------------------------------------------------- #

class TestListDMs:
    def test_single_page_resolves_participant_name(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "D1", "user": "U1"}], "response_metadata": {"next_cursor": ""}
        }
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}}
        client = make_client(web_client)

        dms = client.list_dms()

        assert dms == [SlackDirectMessage(id="D1", user_id="U1", user_name="Jane Doe")]
        kwargs = web_client.conversations_list.call_args.kwargs
        assert kwargs["types"] == "im"

    def test_paginates_until_cursor_exhausted(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "D1", "user": "U1"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "D2", "user": "U2"}], "response_metadata": {"next_cursor": ""}},
        ]
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe"}}
        client = make_client(web_client)
        dms = client.list_dms(max_results=100)
        assert [d.id for d in dms] == ["D1", "D2"]
        assert web_client.conversations_list.call_count == 2

    def test_participant_filter_matches_by_id_or_name_case_insensitive(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "D1", "user": "U1"}, {"id": "D2", "user": "U2"}],
            "response_metadata": {},
        }
        web_client.users_info.side_effect = [
            {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}},
            {"user": {"id": "U2", "name": "bsmith", "real_name": "Bob Smith"}},
        ]
        client = make_client(web_client)

        assert [d.id for d in client.list_dms(participant="jane")] == ["D1"]
        # U1/U2 are now cached from the call above, so this needs no further
        # users_info calls -- proves id matching doesn't depend on re-resolving names.
        assert [d.id for d in client.list_dms(participant="U2")] == ["D2"]

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="list_dms failed"):
            client.list_dms()


# ---------------------------------------------------------------------------- #
# list_group_chats
# ---------------------------------------------------------------------------- #

class TestListGroupChats:
    def test_resolves_members_per_group_chat(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "mpdm-jdoe--bsmith-1"}],
            "response_metadata": {"next_cursor": ""},
        }
        web_client.conversations_members.return_value = {"members": ["U1", "U2"]}
        web_client.users_info.side_effect = [
            {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}},
            {"user": {"id": "U2", "name": "bsmith", "real_name": "Bob Smith"}},
        ]
        client = make_client(web_client)

        chats = client.list_group_chats()

        assert chats == [
            SlackGroupChat(
                id="G1", name="mpdm-jdoe--bsmith-1",
                member_ids=["U1", "U2"], member_names=["Jane Doe", "Bob Smith"],
            )
        ]
        assert web_client.conversations_list.call_args.kwargs["types"] == "mpim"
        web_client.conversations_members.assert_called_once_with(channel="G1", limit=1000, cursor=None)

    def test_participant_filter_matches_any_member(self):
        # conversations_members/users_info are keyed by argument rather than
        # call order -- list_group_chats now parses candidate chats
        # concurrently (see _map_concurrent), so a plain ordered side_effect
        # list would race between threads.
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "g1"}, {"id": "G2", "name": "g2"}],
            "response_metadata": {},
        }
        members_by_channel = {"G1": ["U1", "U2"], "G2": ["U3"]}
        web_client.conversations_members.side_effect = (
            lambda channel=None, **kwargs: {"members": members_by_channel[channel]}
        )
        names_by_user = {
            "U1": {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}},
            "U2": {"user": {"id": "U2", "name": "bsmith", "real_name": "Bob Smith"}},
            "U3": {"user": {"id": "U3", "name": "carol"}},
        }
        web_client.users_info.side_effect = lambda user=None, **kwargs: names_by_user[user]
        client = make_client(web_client)

        chats = client.list_group_chats(participant="bob")

        assert [c.id for c in chats] == ["G1"]

    def test_participant_comma_separated_requires_all_in_same_chat(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "g1"}, {"id": "G2", "name": "g2"}],
            "response_metadata": {},
        }
        members_by_channel = {"G1": ["U1", "U2"], "G2": ["U1"]}  # G1: bob + jane, G2: bob only
        web_client.conversations_members.side_effect = (
            lambda channel=None, **kwargs: {"members": members_by_channel[channel]}
        )
        names_by_user = {
            "U1": {"user": {"id": "U1", "name": "bob", "real_name": "Bob Smith"}},
            "U2": {"user": {"id": "U2", "name": "jane", "real_name": "Jane Doe"}},
        }
        web_client.users_info.side_effect = lambda user=None, **kwargs: names_by_user[user]
        client = make_client(web_client)

        chats = client.list_group_chats(participant="bob,jane")

        assert [c.id for c in chats] == ["G1"]

    def test_participant_id_uses_users_conversations_fast_path(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "g1"}, {"id": "G2", "name": "g2"}],
            "response_metadata": {},
        }
        web_client.users_conversations.return_value = {"channels": ["G1"], "response_metadata": {}}
        web_client.conversations_members.return_value = {"members": ["U1"]}
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "u1"}}
        client = make_client(web_client)

        chats = client.list_group_chats(participant="U1")

        assert [c.id for c in chats] == ["G1"]
        web_client.users_conversations.assert_called_once_with(
            user="U1", types="mpim", limit=200, cursor=None
        )
        # only the matching chat's membership is ever resolved -- G2 is
        # filtered out before it's parsed at all.
        web_client.conversations_members.assert_called_once_with(channel="G1", limit=1000, cursor=None)

    def test_unresolvable_members_reads_as_empty_not_raising(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "g1"}], "response_metadata": {},
        }
        web_client.conversations_members.side_effect = slack_error("channel_not_found")
        client = make_client(web_client)

        chats = client.list_group_chats()

        assert chats == [SlackGroupChat(id="G1", name="g1", member_ids=[], member_names=[])]

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="list_group_chats failed"):
            client.list_group_chats()


# ---------------------------------------------------------------------------- #
# Participant resolution helpers (the users.conversations fast path) and the
# other small P0-performance pieces underneath list_channels/list_group_chats/
# _search_by_participant -- see docs/slack-performance-review.md.
# ---------------------------------------------------------------------------- #

class TestBuildUserNameIndex:
    def test_indexes_handle_real_name_and_email(self):
        client = make_client(MagicMock())
        client._user_cache = {
            "U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe", email="jane@x.com"),
        }
        index = client._build_user_name_index()
        assert index == {"jdoe": "U1", "jane doe": "U1", "jane@x.com": "U1"}

    def test_name_collision_keeps_first_seen(self):
        client = make_client(MagicMock())
        client._user_cache = {
            "U1": SlackUser(id="U1", name="jane", real_name="Jane Doe"),
            "U2": SlackUser(id="U2", name="jane", real_name="Jane Smith"),
        }
        index = client._build_user_name_index()
        assert index["jane"] == "U1"


class TestResolveParticipantUserIds:
    def test_empty_participant_returns_none(self):
        client = make_client(MagicMock())
        assert client._resolve_participant_user_ids("") is None

    def test_raw_id_passes_through_without_a_directory(self):
        client = make_client(MagicMock())  # no cache file, empty directory
        assert client._resolve_participant_user_ids("U1") == ["U1"]

    def test_exact_name_match(self):
        client = make_client(MagicMock())
        client._user_cache = {"U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe")}
        assert client._resolve_participant_user_ids("Jane Doe") == ["U1"]

    def test_unique_substring_match(self):
        client = make_client(MagicMock())
        client._user_cache = {"U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe")}
        assert client._resolve_participant_user_ids("jane") == ["U1"]

    def test_ambiguous_substring_returns_none(self):
        client = make_client(MagicMock())
        client._user_cache = {
            "U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe"),
            "U2": SlackUser(id="U2", name="jsmith", real_name="Jane Smith"),
        }
        assert client._resolve_participant_user_ids("jane") is None

    def test_unresolvable_name_returns_none(self):
        client = make_client(MagicMock())
        client._user_cache = {"U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe")}
        assert client._resolve_participant_user_ids("nobody") is None

    def test_comma_separated_resolves_each_needle(self):
        client = make_client(MagicMock())
        client._user_cache = {
            "U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe"),
            "U2": SlackUser(id="U2", name="bsmith", real_name="Bob Smith"),
        }
        assert client._resolve_participant_user_ids("jane,U2") == ["U1", "U2"]

    def test_one_unresolvable_needle_fails_the_whole_call(self):
        client = make_client(MagicMock())
        client._user_cache = {"U1": SlackUser(id="U1", name="jdoe", real_name="Jane Doe")}
        assert client._resolve_participant_user_ids("jane,nobody") is None


class TestConversationIdsForUser:
    def test_paginates_and_collects_ids(self):
        web_client = MagicMock()
        web_client.users_conversations.side_effect = [
            {"channels": ["C1"], "response_metadata": {"next_cursor": "page2"}},
            {"channels": ["C2"], "response_metadata": {"next_cursor": ""}},
        ]
        client = make_client(web_client)

        ids = client._conversation_ids_for_user("U1", types="public_channel,private_channel")

        assert ids == ["C1", "C2"]
        assert web_client.users_conversations.call_count == 2

    def test_api_error_returns_none(self):
        web_client = MagicMock()
        web_client.users_conversations.side_effect = slack_error("internal_error")
        client = make_client(web_client)

        assert client._conversation_ids_for_user("U1", types="mpim") is None

    def test_unexpected_response_shape_returns_none_instead_of_raising(self):
        web_client = MagicMock()
        web_client.users_conversations.return_value = "not a mapping"
        client = make_client(web_client)

        assert client._conversation_ids_for_user("U1", types="mpim") is None


class TestParticipantConversationIds:
    def test_empty_participant_returns_none(self):
        client = make_client(MagicMock())
        assert client._participant_conversation_ids("", types="mpim") is None

    def test_single_needle(self):
        web_client = MagicMock()
        web_client.users_conversations.return_value = {"channels": ["C1", "C2"], "response_metadata": {}}
        client = make_client(web_client)

        assert set(client._participant_conversation_ids("U1", types="mpim")) == {"C1", "C2"}

    def test_multiple_needles_intersect(self):
        web_client = MagicMock()
        by_user = {"U1": ["C1", "C2"], "U2": ["C2", "C3"]}
        web_client.users_conversations.side_effect = (
            lambda user=None, **k: {"channels": by_user[user], "response_metadata": {}}
        )
        client = make_client(web_client)

        assert client._participant_conversation_ids("U1,U2", types="mpim") == ["C2"]

    def test_one_needles_lookup_failing_fails_the_whole_call(self):
        web_client = MagicMock()

        def fake(user=None, **k):
            if user == "U2":
                raise slack_error("internal_error")
            return {"channels": ["C1"], "response_metadata": {}}

        web_client.users_conversations.side_effect = fake
        client = make_client(web_client)

        assert client._participant_conversation_ids("U1,U2", types="mpim") is None


class TestResolveUserNameCached:
    def test_no_cache_file_falls_back_to_live_resolution(self):
        web_client = MagicMock()
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}}
        client = make_client(web_client)

        assert client._resolve_user_name_cached("U1") == "Jane Doe"
        web_client.users_info.assert_called_once()

    def test_cache_file_configured_hit_makes_no_live_call(self, tmp_path):
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)
        client._user_cache["U1"] = SlackUser(id="U1", name="jdoe", real_name="Jane Doe")
        client._user_directory_fetched_at = datetime.now(timezone.utc)
        client._user_directory_loaded_from_disk = True

        assert client._resolve_user_name_cached("U1") == "Jane Doe"
        web_client.users_info.assert_not_called()

    def test_cache_file_configured_miss_returns_empty_without_a_live_call(self, tmp_path):
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)
        client._user_directory_fetched_at = datetime.now(timezone.utc)
        client._user_directory_loaded_from_disk = True

        assert client._resolve_user_name_cached("U9") == ""
        web_client.users_info.assert_not_called()

    def test_empty_id_returns_empty(self):
        client = make_client(MagicMock())
        assert client._resolve_user_name_cached("") == ""


class TestResolveMembersCache:
    def test_paginates_past_the_first_page(self):
        # Regression for bug #2 in docs/slack-performance-review.md: a
        # channel with more than 1000 members used to silently lose
        # everyone past the first page.
        web_client = MagicMock()
        web_client.conversations_members.side_effect = [
            {"members": [f"U{i:04d}" for i in range(1000)], "response_metadata": {"next_cursor": "page2"}},
            {"members": ["U1000"], "response_metadata": {}},
        ]
        client = make_client(web_client)

        members = client._resolve_members("C1")

        assert len(members) == 1001
        assert "U1000" in members
        assert web_client.conversations_members.call_count == 2

    def test_second_call_within_ttl_reuses_the_cached_result(self):
        web_client = MagicMock()
        web_client.conversations_members.return_value = {"members": ["U1"]}
        client = make_client(web_client)

        assert client._resolve_members("C1") == ["U1"]
        assert client._resolve_members("C1") == ["U1"]

        web_client.conversations_members.assert_called_once()

    def test_call_past_the_ttl_refreshes(self):
        web_client = MagicMock()
        web_client.conversations_members.return_value = {"members": ["U1"]}
        client = make_client(web_client)

        with freeze_time("2026-01-01 00:00:00"):
            client._resolve_members("C1")
        with freeze_time("2026-01-01 00:20:00"):  # past _MEMBERSHIP_CACHE_TTL (10 min)
            client._resolve_members("C1")

        assert web_client.conversations_members.call_count == 2


class TestMapConcurrent:
    def test_single_item_skips_the_pool(self):
        from privacyfence.slack_client import _map_concurrent

        seen = []

        def fn(x):
            seen.append(threading.current_thread().name)
            return x * 2

        assert _map_concurrent([5], fn) == [10]
        assert seen == [threading.current_thread().name]  # ran on the caller's own thread

    def test_preserves_input_order(self):
        from privacyfence.slack_client import _map_concurrent

        def fn(x):
            time.sleep(0.01 if x == 0 else 0)  # first item finishes last
            return x

        assert _map_concurrent([0, 1, 2, 3], fn) == [0, 1, 2, 3]

    def test_empty_input(self):
        from privacyfence.slack_client import _map_concurrent

        assert _map_concurrent([], lambda x: x) == []


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

    def test_has_more_reflects_slacks_own_pagination_signal(self):
        # Not a len(messages) vs. limit comparison -- a channel with fewer
        # messages than limit is not truncated; has_more is Slack's own say.
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        web_client.conversations_history.return_value = {
            "messages": [{"text": "hi", "ts": "1"}], "has_more": True,
        }
        client = make_client(web_client)
        messages, has_more = client.get_channel_history("C1")
        assert len(messages) == 1
        assert has_more is True

    def test_has_more_defaults_false_when_absent(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        web_client.conversations_history.return_value = {"messages": []}
        client = make_client(web_client)
        _messages, has_more = client.get_channel_history("C1")
        assert has_more is False


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
        replies, has_more = client.get_thread_replies("C1", "1.0")
        assert replies[0].text == "reply"
        assert has_more is False

    def test_has_more_reflects_slacks_own_pagination_signal(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        web_client.conversations_replies.return_value = {
            "messages": [{"text": "reply", "ts": "1"}], "has_more": True,
        }
        client = make_client(web_client)
        _replies, has_more = client.get_thread_replies("C1", "1.0")
        assert has_more is True


class TestGetMessage:
    def test_finds_the_message_via_a_single_history_call(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "eng"}}
        web_client.conversations_history.return_value = {
            "messages": [{"text": "root message", "ts": "100.001", "user": "U1"}],
        }
        client = make_client(web_client)

        message = client.get_message("C1", "100.001")

        assert message.text == "root message"
        kwargs = web_client.conversations_history.call_args.kwargs
        assert kwargs["latest"] == "100.001"
        assert kwargs["oldest"] == "100.001"
        assert kwargs["inclusive"] is True
        assert kwargs["limit"] == 1

    def test_missing_message_returns_none(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "eng"}}
        web_client.conversations_history.return_value = {"messages": []}
        client = make_client(web_client)

        assert client.get_message("C1", "100.001") is None

    def test_api_error_returns_none_without_raising(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "eng"}}
        web_client.conversations_history.side_effect = slack_error("channel_not_found")
        client = make_client(web_client)

        assert client.get_message("C1", "100.001") is None

    def test_empty_channel_id_or_ts_returns_none_without_an_api_call(self):
        web_client = MagicMock()
        client = make_client(web_client)

        assert client.get_message("", "100.001") is None
        assert client.get_message("C1", "") is None
        web_client.conversations_history.assert_not_called()


class TestSearchMessages:
    def test_requires_query_or_participant(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires a query, a participant, or both"):
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

    def test_default_days_appends_after_modifier_to_query(self):
        web_client = MagicMock()
        web_client.search_messages.return_value = {"messages": {"matches": []}}
        client = make_client(web_client)

        client.search_messages("budget")

        query = web_client.search_messages.call_args.kwargs["query"]
        assert query.startswith("budget after:")

    def test_days_zero_leaves_query_unmodified(self):
        web_client = MagicMock()
        web_client.search_messages.return_value = {"messages": {"matches": []}}
        client = make_client(web_client)

        client.search_messages("budget", days=0)

        assert web_client.search_messages.call_args.kwargs["query"] == "budget"

    def test_participant_given_skips_slack_search_api_entirely(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {"channels": [], "response_metadata": {}}
        client = make_client(web_client)

        client.search_messages(participant="bob")

        web_client.search_messages.assert_not_called()

    def test_participant_matches_dm_and_reads_its_history(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "D1", "user": "U1"}], "response_metadata": {}},  # list_dms
            {"channels": [], "response_metadata": {}},  # list_group_chats
        ]
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "bob", "real_name": "Bob Smith"}}
        web_client.conversations_info.return_value = {"channel": {"name": "bob-dm"}}
        web_client.conversations_history.return_value = {
            "messages": [{"user": "U1", "text": "hi there", "ts": "1"}]
        }
        client = make_client(web_client)

        results = client.search_messages(participant="bob")

        assert [m.text for m in results] == ["hi there"]
        web_client.conversations_history.assert_called_once()
        assert web_client.conversations_history.call_args.kwargs["channel"] == "D1"
        # Default days=90 -> oldest is passed through to bound the history fetch.
        assert "oldest" in web_client.conversations_history.call_args.kwargs

    def test_participant_days_zero_omits_oldest_cutoff(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "D1", "user": "U1"}], "response_metadata": {}},
            {"channels": [], "response_metadata": {}},
        ]
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "bob"}}
        web_client.conversations_info.return_value = {"channel": {"name": "bob-dm"}}
        web_client.conversations_history.return_value = {"messages": []}
        client = make_client(web_client)

        client.search_messages(participant="bob", days=0)

        assert "oldest" not in web_client.conversations_history.call_args.kwargs

    def test_participant_comma_separated_requires_all_in_same_group_chat(self):
        web_client = MagicMock()
        # 2 needles -> list_dms is skipped entirely, so only one
        # conversations_list call happens, for list_group_chats.
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "g1"}, {"id": "G2", "name": "g2"}],
            "response_metadata": {},
        }
        # Keyed by argument, not call order -- list_group_chats parses
        # candidate chats concurrently (see _map_concurrent).
        members_by_channel = {"G1": ["U1", "U2"], "G2": ["U1"]}  # G1: bob + jane, G2: bob only
        web_client.conversations_members.side_effect = (
            lambda channel=None, **kwargs: {"members": members_by_channel[channel]}
        )
        names_by_user = {
            "U1": {"user": {"id": "U1", "name": "bob", "real_name": "Bob Smith"}},
            "U2": {"user": {"id": "U2", "name": "jane", "real_name": "Jane Doe"}},
        }
        web_client.users_info.side_effect = lambda user=None, **kwargs: names_by_user[user]
        web_client.conversations_info.return_value = {"channel": {"name": "g1"}}
        web_client.conversations_history.return_value = {"messages": []}
        client = make_client(web_client)

        client.search_messages(participant="bob,jane")

        web_client.conversations_history.assert_called_once()
        assert web_client.conversations_history.call_args.kwargs["channel"] == "G1"

    def test_participant_with_query_filters_text_client_side(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "D1", "user": "U1"}], "response_metadata": {}},
            {"channels": [], "response_metadata": {}},
        ]
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "bob"}}
        web_client.conversations_info.return_value = {"channel": {"name": "bob-dm"}}
        web_client.conversations_history.return_value = {
            "messages": [
                {"user": "U1", "text": "let's discuss the budget", "ts": "1"},
                {"user": "U1", "text": "lunch?", "ts": "2"},
            ]
        }
        client = make_client(web_client)

        results = client.search_messages(query="budget", participant="bob")

        assert [m.text for m in results] == ["let's discuss the budget"]

    def test_participant_no_match_returns_empty_without_history_call(self):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {"channels": [], "response_metadata": {}}
        client = make_client(web_client)

        results = client.search_messages(participant="nobody")

        assert results == []
        web_client.conversations_history.assert_not_called()

    def test_participant_results_sorted_most_recent_first_and_capped_to_count(self):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "D1", "user": "U1"}], "response_metadata": {}},
            {"channels": [], "response_metadata": {}},
        ]
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "bob"}}
        web_client.conversations_info.return_value = {"channel": {"name": "bob-dm"}}
        web_client.conversations_history.return_value = {
            "messages": [
                {"user": "U1", "text": "older", "ts": "100"},
                {"user": "U1", "text": "newer", "ts": "200"},
            ]
        }
        client = make_client(web_client)

        results = client.search_messages(participant="bob", count=1)

        assert [m.text for m in results] == ["newer"]


# ---------------------------------------------------------------------------- #
# send_message / open_conversation / resolve_user_name / mark_channel_unread_before / get_user_info
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


class TestOpenConversation:
    def test_requires_at_least_one_user_id(self):
        client = make_client(MagicMock())
        with pytest.raises(SlackClientError, match="requires at least one user_id"):
            client.open_conversation([])

    def test_opens_group_dm_and_resolves_members(self):
        web_client = MagicMock()
        web_client.conversations_open.return_value = {
            "channel": {"id": "G1", "name": "mpdm-jdoe--bsmith-1"}
        }
        web_client.users_info.side_effect = [
            {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}},
            {"user": {"id": "U2", "name": "bsmith", "real_name": "Bob Smith"}},
        ]
        client = make_client(web_client)

        chat = client.open_conversation(["U1", "U2"])

        assert chat == SlackGroupChat(
            id="G1", name="mpdm-jdoe--bsmith-1",
            member_ids=["U1", "U2"], member_names=["Jane Doe", "Bob Smith"],
        )
        assert web_client.conversations_open.call_args.kwargs["users"] == "U1,U2"

    def test_falls_back_to_resolving_name_when_response_has_none(self):
        web_client = MagicMock()
        web_client.conversations_open.return_value = {"channel": {"id": "G1"}}
        web_client.conversations_info.return_value = {"channel": {"name": "resolved-name"}}
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe"}}
        client = make_client(web_client)

        chat = client.open_conversation(["U1"])

        assert chat.name == "resolved-name"

    def test_api_error_becomes_slack_client_error(self):
        web_client = MagicMock()
        web_client.conversations_open.side_effect = slack_error("channel_not_found")
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="open_conversation"):
            client.open_conversation(["U1", "U2"])


class TestResolveUserNamePublicWrapper:
    """`resolve_user_name` is the public wrapper `open_conversation`'s
    callers (outside this module) use -- `TestResolveUserName` above already
    covers the private `_resolve_user_name` it delegates to."""

    def test_resolves_via_get_user_info(self):
        web_client = MagicMock()
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe", "real_name": "Jane Doe"}}
        client = make_client(web_client)
        assert client.resolve_user_name("U1") == "Jane Doe"

    def test_error_is_swallowed_returns_empty(self):
        web_client = MagicMock()
        web_client.users_info.side_effect = slack_error()
        client = make_client(web_client)
        assert client.resolve_user_name("U1") == ""


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

    def test_bot_id_fails_without_an_api_call(self):
        web_client = MagicMock()
        client = make_client(web_client)
        with pytest.raises(SlackClientError, match="not a user id"):
            client.get_user_info("B0FEEDBOT")
        web_client.users_info.assert_not_called()

    def test_unresolvable_id_is_remembered_within_the_negative_cache_ttl(self):
        web_client = MagicMock()
        web_client.users_info.side_effect = slack_error("user_not_found")
        client = make_client(web_client)

        with freeze_time("2026-01-01 00:00:00"):
            with pytest.raises(SlackClientError, match="get_user_info"):
                client.get_user_info("U1")
            with pytest.raises(SlackClientError, match="previously unresolvable"):
                client.get_user_info("U1")
            assert web_client.users_info.call_count == 1

        with freeze_time("2026-01-01 02:00:00"):  # past _NEGATIVE_LOOKUP_TTL (1h)
            with pytest.raises(SlackClientError, match="get_user_info"):
                client.get_user_info("U1")
            assert web_client.users_info.call_count == 2


# ---------------------------------------------------------------------------- #
# Weekly directory caches: refresh_user_directory / refresh_channel_directory
# ---------------------------------------------------------------------------- #

class TestEnsureDirectoriesFresh:
    """ensure_directories_fresh() -- the eager startup-time counterpart to
    the lazy per-lookup refresh, called once by daemon_main.py right after
    connecting so a snapshot gone stale while the app was closed doesn't
    make the first Slack tool call after restart pay for the refresh."""

    def test_missing_snapshots_refresh_both_on_first_call(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.return_value = {"members": [{"id": "U1", "name": "a"}], "response_metadata": {}}
        web_client.conversations_list.return_value = {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {}}
        client = make_client_with_caches(web_client, tmp_path)

        client.ensure_directories_fresh()

        web_client.users_list.assert_called_once()
        web_client.conversations_list.assert_called_once()
        assert client._user_cache["U1"].name == "a"
        assert client._channel_name_cache["C1"] == "general"

    def test_stale_disk_snapshot_refreshes_on_restart(self, tmp_path):
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        (tmp_path / "slack_user_cache.json").write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        (tmp_path / "slack_channel_cache.json").write_text(json.dumps({
            "fetched_at": stale, "channels": {"C1": {"name": "old-name", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.users_list.return_value = {"members": [{"id": "U1", "name": "fresh"}], "response_metadata": {}}
        web_client.conversations_list.return_value = {"channels": [{"id": "C1", "name": "renamed"}], "response_metadata": {}}
        client = make_client_with_caches(web_client, tmp_path)

        client.ensure_directories_fresh()

        assert client._user_cache["U1"].name == "fresh"
        assert client._channel_name_cache["C1"] == "renamed"

    def test_fresh_disk_snapshots_make_no_network_calls(self, tmp_path):
        fresh = datetime.now(timezone.utc).isoformat()
        (tmp_path / "slack_user_cache.json").write_text(json.dumps({
            "fetched_at": fresh, "users": {"U1": {"id": "U1", "name": "cached"}},
        }), encoding="utf-8")
        (tmp_path / "slack_channel_cache.json").write_text(json.dumps({
            "fetched_at": fresh, "channels": {"C1": {"name": "general", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)

        client.ensure_directories_fresh()

        web_client.users_list.assert_not_called()
        web_client.conversations_list.assert_not_called()

    def test_no_cache_files_configured_is_a_pure_no_op(self):
        web_client = MagicMock()
        client = make_client(web_client)  # no user_cache_file/channel_cache_file

        client.ensure_directories_fresh()

        web_client.users_list.assert_not_called()
        web_client.conversations_list.assert_not_called()

    def test_never_raises_on_failed_refresh(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.side_effect = slack_error("ratelimited")
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client_with_caches(web_client, tmp_path)

        client.ensure_directories_fresh()  # must not raise


class TestRefreshUserDirectory:
    def test_paginates_and_caches(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.side_effect = [
            {"members": [{"id": "U1", "name": "a"}], "response_metadata": {"next_cursor": "page2"}},
            {"members": [{"id": "U2", "name": "b"}], "response_metadata": {"next_cursor": ""}},
        ]
        client = make_client_with_caches(web_client, tmp_path)

        count = client.refresh_user_directory()

        assert count == 2
        assert set(client._user_cache) == {"U1", "U2"}
        assert web_client.users_list.call_count == 2
        assert web_client.users_list.call_args_list[1].kwargs["cursor"] == "page2"

    def test_saves_snapshot_to_disk(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.return_value = {
            "members": [{"id": "U1", "name": "jdoe", "real_name": "Jane Doe", "profile": {"email": "jane@x.com"}}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)

        client.refresh_user_directory()

        data = json.loads((tmp_path / "slack_user_cache.json").read_text(encoding="utf-8"))
        assert data["users"]["U1"]["email"] == "jane@x.com"
        assert "fetched_at" in data

    def test_api_error_raises(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.side_effect = slack_error("ratelimited")
        client = make_client_with_caches(web_client, tmp_path)
        with pytest.raises(SlackClientError, match="refresh_user_directory"):
            client.refresh_user_directory()


class TestGetUserInfoWithDirectoryCache:
    def test_directory_hit_skips_live_call(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.return_value = {
            "members": [{"id": "U1", "name": "jdoe"}], "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        client.refresh_user_directory()

        user = client.get_user_info("U1")

        assert user.name == "jdoe"
        web_client.users_info.assert_not_called()

    def test_directory_miss_falls_back_to_live_call(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.return_value = {"members": [], "response_metadata": {"next_cursor": ""}}
        web_client.users_info.return_value = {"user": {"id": "U9", "name": "newhire"}}
        client = make_client_with_caches(web_client, tmp_path)
        client.refresh_user_directory()

        user = client.get_user_info("U9")

        assert user.name == "newhire"
        web_client.users_info.assert_called_once()

    def test_no_cache_file_never_calls_users_list(self):
        # Default make_client() (no user_cache_file) -- behavior identical
        # to before this cache existed.
        web_client = MagicMock()
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "jdoe"}}
        client = make_client(web_client)

        client.get_user_info("U1")

        web_client.users_list.assert_not_called()

    def test_stale_disk_snapshot_serves_stale_value_and_schedules_background_refresh(self, tmp_path):
        # A snapshot that already loaded (even if stale) is never worth
        # blocking a gated tool call behind -- get_user_info returns the
        # existing value immediately and the refresh runs in the
        # background instead (see docs/slack-performance-review.md's R5).
        cache_file = tmp_path / "slack_user_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.users_list.return_value = {
            "members": [{"id": "U1", "name": "fresh"}], "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        spawned = []
        client._spawn_background = lambda name, target: spawned.append(name)  # don't actually run it

        user = client.get_user_info("U1")

        assert user.name == "old"
        assert spawned == ["slack-user-dir-refresh"]
        web_client.users_list.assert_not_called()

    def test_stale_disk_snapshot_background_refresh_updates_the_cache(self, tmp_path):
        cache_file = tmp_path / "slack_user_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.users_list.return_value = {
            "members": [{"id": "U1", "name": "fresh"}], "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        # Run the background refresh inline so its effect can be asserted
        # deterministically, without sleeping on or joining a real thread.
        client._spawn_background = lambda name, target: target()

        client.get_user_info("U1")

        assert client._user_cache["U1"].name == "fresh"
        web_client.users_list.assert_called_once()

    def test_stale_disk_snapshot_background_refresh_is_single_flight(self, tmp_path):
        # A rapid second call is already stopped by the retry cooldown; the
        # refresh lock this exercises additionally guards the case the
        # cooldown alone can't (two calls racing before either has recorded
        # an attempt) -- not reproducible from a single thread, but the
        # observable guarantee (at most one refresh spawned) holds either
        # way, which is what this asserts.
        cache_file = tmp_path / "slack_user_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)
        spawned = []
        # Never runs the target -- simulates a refresh that's still in
        # flight for the whole duration of this test.
        client._spawn_background = lambda name, target: spawned.append(name)

        client.get_user_info("U1")
        client.get_user_info("U1")

        assert len(spawned) == 1

    def test_refresh_lock_already_held_skips_spawning(self, tmp_path):
        # The genuine concurrent case the lock (as opposed to the retry
        # cooldown) exists for: a refresh already in flight when the lock
        # itself -- not the timestamp -- is what says so.
        cache_file = tmp_path / "slack_user_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)
        spawned = []
        client._spawn_background = lambda name, target: spawned.append(name)
        client._user_directory_refresh_lock.acquire()

        client.get_user_info("U1")

        assert spawned == []

    def test_background_refresh_failure_is_logged_and_releases_the_lock(self, tmp_path):
        cache_file = tmp_path / "slack_user_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.users_list.side_effect = slack_error("ratelimited")
        client = make_client_with_caches(web_client, tmp_path)
        client._spawn_background = lambda name, target: target()  # run inline

        user = client.get_user_info("U1")  # must not raise -- best-effort

        assert user.name == "old"
        assert not client._user_directory_refresh_lock.locked()

    def test_real_background_thread_eventually_refreshes_the_cache(self, tmp_path):
        # The one test that lets _spawn_background do what it does in
        # production (every other test above substitutes a synchronous or
        # no-op stand-in for determinism) -- proves the real threading.Thread
        # call site itself works, not just the seam around it.
        cache_file = tmp_path / "slack_user_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "users": {"U1": {"id": "U1", "name": "old"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.users_list.return_value = {
            "members": [{"id": "U1", "name": "fresh"}], "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)

        client.get_user_info("U1")

        assert wait_until(lambda: client._user_cache.get("U1") and client._user_cache["U1"].name == "fresh")

    def test_fresh_disk_snapshot_skips_refresh(self, tmp_path):
        cache_file = tmp_path / "slack_user_cache.json"
        fresh = datetime.now(timezone.utc).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": fresh, "users": {"U1": {"id": "U1", "name": "cached"}},
        }), encoding="utf-8")
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)

        user = client.get_user_info("U1")

        assert user.name == "cached"
        web_client.users_list.assert_not_called()

    def test_failed_refresh_respects_retry_cooldown(self, tmp_path):
        web_client = MagicMock()
        web_client.users_list.side_effect = slack_error("ratelimited")
        web_client.users_info.return_value = {"user": {"id": "U1", "name": "u1"}}
        client = make_client_with_caches(web_client, tmp_path)

        with freeze_time("2026-01-01 00:00:00"):
            client.get_user_info("U1")
            assert web_client.users_list.call_count == 1

        with freeze_time("2026-01-01 00:01:00"):  # within the cooldown
            client.get_user_info("U1")
            assert web_client.users_list.call_count == 1

        with freeze_time("2026-01-01 00:10:00"):  # past the cooldown
            client.get_user_info("U1")
            assert web_client.users_list.call_count == 2


class TestRefreshChannelDirectory:
    def test_paginates_across_all_conversation_types_and_caches(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "G1", "name": "mpdm-a--b-1", "is_mpim": True}], "response_metadata": {"next_cursor": ""}},
        ]
        client = make_client_with_caches(web_client, tmp_path)

        count, has_more = client.refresh_channel_directory()

        assert count == 2
        assert has_more is False
        assert client._channel_name_cache == {"C1": "general", "G1": "mpdm-a--b-1"}
        assert client._channel_is_mpim_cache == {"C1": False, "G1": True}
        call_kwargs = web_client.conversations_list.call_args_list[0].kwargs
        assert call_kwargs["types"] == "public_channel,private_channel,mpim,im"

    def test_saves_snapshot_to_disk(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general", "is_mpim": False}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)

        client.refresh_channel_directory()

        data = json.loads((tmp_path / "slack_channel_cache.json").read_text(encoding="utf-8"))
        assert data["channels"]["C1"] == {"name": "general", "is_mpim": False}

    def test_api_error_raises(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client_with_caches(web_client, tmp_path)
        with pytest.raises(SlackClientError, match="refresh_channel_directory"):
            client.refresh_channel_directory()


class TestRefreshChannelDirectoryPagination:
    """max_pages bounds each call to that many conversations.list pages,
    resuming from where the previous call left off -- what the
    slack_refresh_channel_cache bridge tool uses so a workspace with enough
    channels to blow past the calling MCP client's tool-call timeout
    completes over a few bounded calls instead of one unbounded one."""

    def test_stops_after_max_pages_and_reports_has_more(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "C2", "name": "random"}], "response_metadata": {"next_cursor": "page3"}},
        ]
        client = make_client_with_caches(web_client, tmp_path)

        count, has_more = client.refresh_channel_directory(max_pages=2)

        assert count == 2
        assert has_more is True
        assert web_client.conversations_list.call_count == 2
        assert client._channel_name_cache == {"C1": "general", "C2": "random"}

    def test_partial_refresh_persists_progress_but_not_ttl(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "page2"},
        }
        client = make_client_with_caches(web_client, tmp_path)

        client.refresh_channel_directory(max_pages=1)

        # Not finalized -- the weekly TTL only advances once a walk
        # actually finishes.
        assert client._channel_directory_fetched_at is None
        # But the merged-so-far snapshot and resume cursor ARE persisted --
        # see docs/slack-performance-review.md's P1 item on a bounded walk
        # surviving a daemon restart.
        cache_file = tmp_path / "slack_channel_cache.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data["channels"] == {"C1": {"name": "general", "is_mpim": False}}
        assert data["partial_cursor"] == "page2"
        assert "fetched_at" not in data

    def test_partial_refresh_survives_a_process_restart(self, tmp_path):
        # The scenario this whole feature exists for: max_pages=1 across
        # two SEPARATE SlackClient instances sharing the same cache file --
        # not two calls on the same client (already covered by
        # test_next_call_resumes_from_saved_cursor) -- simulating the daemon
        # restarting between bounded slack_refresh_channel_cache calls.
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "C2", "name": "random"}], "response_metadata": {"next_cursor": ""}},
        ]
        first_client = make_client_with_caches(web_client, tmp_path)
        first_count, first_has_more = first_client.refresh_channel_directory(max_pages=1)
        assert (first_count, first_has_more) == (1, True)

        second_client = make_client_with_caches(web_client, tmp_path)  # fresh process
        second_count, second_has_more = second_client.refresh_channel_directory(max_pages=1)

        assert (second_count, second_has_more) == (2, False)
        # The second client's own first call resumed from page2, not page1.
        assert web_client.conversations_list.call_args_list[1].kwargs["cursor"] == "page2"
        assert second_client._channel_name_cache == {"C1": "general", "C2": "random"}
        assert second_client._channel_directory_fetched_at is not None

    def test_next_call_resumes_from_saved_cursor(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "C2", "name": "random"}], "response_metadata": {"next_cursor": ""}},
        ]
        client = make_client_with_caches(web_client, tmp_path)

        first_count, first_has_more = client.refresh_channel_directory(max_pages=1)
        second_count, second_has_more = client.refresh_channel_directory(max_pages=1)

        assert (first_count, first_has_more) == (1, True)
        assert (second_count, second_has_more) == (2, False)
        assert web_client.conversations_list.call_args_list[1].kwargs["cursor"] == "page2"
        # Only finalized (disk snapshot + TTL) once the walk actually finished.
        assert client._channel_directory_fetched_at is not None
        data = json.loads((tmp_path / "slack_channel_cache.json").read_text(encoding="utf-8"))
        assert set(data["channels"]) == {"C1", "C2"}

    def test_unbounded_call_ignores_max_pages_limit(self, tmp_path):
        # max_pages=None (the default) is what ensure_directories_fresh()
        # uses -- walks every page in one call regardless of count.
        web_client = MagicMock()
        web_client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "name": "general"}], "response_metadata": {"next_cursor": "page2"}},
            {"channels": [{"id": "C2", "name": "random"}], "response_metadata": {"next_cursor": ""}},
        ]
        client = make_client_with_caches(web_client, tmp_path)

        count, has_more = client.refresh_channel_directory()

        assert count == 2
        assert has_more is False
        assert web_client.conversations_list.call_count == 2


class TestChannelResolutionWithDirectoryCache:
    def test_resolve_channel_name_directory_hit_skips_live_call(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "general", "is_mpim": False}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        client.refresh_channel_directory()

        assert client.resolve_channel_name("C1") == "general"
        web_client.conversations_info.assert_not_called()

    def test_resolve_is_group_dm_directory_hit_skips_live_call(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "G1", "name": "mpdm-a--b-1", "is_mpim": True}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        client.refresh_channel_directory()

        assert client.resolve_is_group_dm("G1") is True
        web_client.conversations_info.assert_not_called()

    def test_no_cache_file_never_calls_conversations_list_for_resolution(self):
        web_client = MagicMock()
        web_client.conversations_info.return_value = {"channel": {"name": "general"}}
        client = make_client(web_client)

        client.resolve_channel_name("C1")

        web_client.conversations_list.assert_not_called()

    def test_stale_disk_snapshot_serves_stale_value_and_schedules_background_refresh(self, tmp_path):
        cache_file = tmp_path / "slack_channel_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "channels": {"C1": {"name": "old-name", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "renamed", "is_mpim": False}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        spawned = []
        client._spawn_background = lambda name, target: spawned.append(name)

        assert client.resolve_channel_name("C1") == "old-name"
        assert spawned == ["slack-channel-dir-refresh"]
        web_client.conversations_list.assert_not_called()

    def test_stale_disk_snapshot_background_refresh_updates_the_cache(self, tmp_path):
        cache_file = tmp_path / "slack_channel_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "channels": {"C1": {"name": "old-name", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "renamed", "is_mpim": False}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)
        client._spawn_background = lambda name, target: target()

        client.resolve_channel_name("C1")

        assert client._channel_name_cache["C1"] == "renamed"
        web_client.conversations_list.assert_called_once()

    def test_refresh_lock_already_held_skips_spawning(self, tmp_path):
        cache_file = tmp_path / "slack_channel_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "channels": {"C1": {"name": "old-name", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        client = make_client_with_caches(web_client, tmp_path)
        spawned = []
        client._spawn_background = lambda name, target: spawned.append(name)
        client._channel_directory_refresh_lock.acquire()

        client.resolve_channel_name("C1")

        assert spawned == []

    def test_background_refresh_failure_is_logged_and_releases_the_lock(self, tmp_path):
        cache_file = tmp_path / "slack_channel_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "channels": {"C1": {"name": "old-name", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client_with_caches(web_client, tmp_path)
        client._spawn_background = lambda name, target: target()

        name = client.resolve_channel_name("C1")  # must not raise -- best-effort

        assert name == "old-name"
        assert not client._channel_directory_refresh_lock.locked()

    def test_real_background_thread_eventually_refreshes_the_cache(self, tmp_path):
        cache_file = tmp_path / "slack_channel_cache.json"
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache_file.write_text(json.dumps({
            "fetched_at": stale, "channels": {"C1": {"name": "old-name", "is_mpim": False}},
        }), encoding="utf-8")
        web_client = MagicMock()
        web_client.conversations_list.return_value = {
            "channels": [{"id": "C1", "name": "renamed", "is_mpim": False}],
            "response_metadata": {"next_cursor": ""},
        }
        client = make_client_with_caches(web_client, tmp_path)

        client.resolve_channel_name("C1")

        assert wait_until(lambda: client._channel_name_cache.get("C1") == "renamed")

    def test_failed_refresh_respects_retry_cooldown(self, tmp_path):
        web_client = MagicMock()
        web_client.conversations_list.side_effect = slack_error("ratelimited")
        client = make_client_with_caches(web_client, tmp_path)

        with freeze_time("2026-01-01 00:00:00"):
            client.resolve_channel_name("C1")
            assert web_client.conversations_list.call_count == 1

        with freeze_time("2026-01-01 00:01:00"):  # within the cooldown
            client.resolve_channel_name("C1")
            assert web_client.conversations_list.call_count == 1

        with freeze_time("2026-01-01 00:10:00"):  # past the cooldown
            client.resolve_channel_name("C1")
            assert web_client.conversations_list.call_count == 2


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
