"""Tests for scripts/qa_fixture_recorder.py -- the local-only fixture
recorder (see docs/external-api-contract-testing.md). Everything here runs
offline, with no live credentials and no network:

- redact()/redact_gmail_message() are pure functions, tested directly.
- RawCapture/RawCaptureCall are tested against real ConfluenceClient/
  JiraClient/SalesforceClient instances with only the underlying
  third-party SDK object mocked -- the same make_client()/with_fake_sf()
  pattern each connector's own client tests already use.
- RawCaptureExecute is tested against a *real* googleapiclient HttpRequest,
  built fully offline (static_discovery=True) with a fake httplib2
  transport. A MagicMock service double -- the pattern every other client
  test in this repo uses for the Google connectors -- never actually
  constructs an HttpRequest, so it can't exercise this class at all; that's
  why this one test module builds a real (offline) service instead.
- Each check_<connector>() is tested end to end against these same fakes,
  proving the guardrail (refuses to record an untagged/stale-ID fetch) and
  the redaction pass both fire correctly before anything would be written
  to disk.

This module intentionally does not import scripts/qa_fixture_recorder.py's
own daemon_main-dependent bits (_build_*_client) directly in most tests --
those are monkeypatched per test, the same way the script itself is meant
to be pointed at a real, already-authenticated account.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import qa_fixture_recorder as recorder  # noqa: E402

from privacyfence.confluence_client import ConfluenceClient  # noqa: E402
from privacyfence.jira_client import JiraClient  # noqa: E402
from privacyfence.salesforce_client import SalesforceClient  # noqa: E402
from privacyfence.gmail_client import GmailClient  # noqa: E402
from privacyfence.drive_client import DriveClient  # noqa: E402
from privacyfence.calendar_client import CalendarClient  # noqa: E402
from privacyfence.contacts_client import ContactsClient  # noqa: E402
from privacyfence.tasks_client import TasksClient  # noqa: E402
from privacyfence.slack_client import SlackClient  # noqa: E402
from privacyfence.telegram_client import TelegramPrivacyFenceClient  # noqa: E402


def _offline_google_service(api: str, version: str, raw_response):
    """A real googleapiclient service, built fully offline (no network, no
    credentials) with a fake httplib2 transport. Used for every Google-API
    connector test here, since a MagicMock service double never constructs
    a real HttpRequest and so can't exercise RawCaptureExecute at all.

    ``raw_response`` is either a single dict (returned for every call --
    fine when a check only ever makes one .execute() call) or a list of
    dicts, consumed one per call in order (for a resolve-then-get sequence,
    which needs a different response per step).
    """
    from googleapiclient.discovery import build

    responses = list(raw_response) if isinstance(raw_response, list) else None

    class FakeHttp:
        def request(self, uri, method="GET", body=None, headers=None, **kw):
            resp = type("Resp", (), {"status": 200, "reason": "OK"})()
            body_obj = responses.pop(0) if responses is not None else raw_response
            return resp, json.dumps(body_obj).encode()

    return build(api, version, static_discovery=True, cache_discovery=False, http=FakeHttp(), developerKey="unused")


def _fake_slack_web_client(responses: dict[str, dict]):
    """A real slack_sdk WebClient with api_call() monkeypatched to return a
    canned response per Slack API method name -- lets conversations_list/
    conversations_history/conversations_replies/conversations_info/
    users_info (all thin wrappers around api_call()) run for real, with no
    network access, the same reasoning as _offline_google_service above but
    for the SDK that RawCaptureApiCall's choke point belongs to.
    """
    from slack_sdk import WebClient
    from slack_sdk.web.slack_response import SlackResponse

    web_client = WebClient(token="xoxp-fake-token")

    def fake_api_call(api_method, **kwargs):
        return SlackResponse(
            client=web_client, http_verb="GET", api_url=api_method, req_args={},
            data={"ok": True, **responses.get(api_method, {})}, headers={}, status_code=200,
        )

    web_client.api_call = fake_api_call
    return web_client


class _FakeTelethonMessage:
    """Minimal stand-in for a Telethon Message: has the attributes
    _parse_message() reads (id/sender/date/text/message/out/media) *and* a
    real ``to_dict()`` -- not a MagicMock's auto-generated one, which would
    return another MagicMock rather than a real dict -- so
    _telethon_to_jsonable's recursion is genuinely exercised, the same
    reasoning RawCaptureExecute's tests use a real offline HttpRequest
    instead of a MagicMock service double.
    """

    def __init__(self, id: int, text: str, date, user_id: int = 111222333):
        self.id = id
        self.text = text
        self.message = text
        self.date = date
        self.out = False
        self.media = None
        self.sender = SimpleNamespace(id=user_id, username="qauser", first_name="", last_name="") if user_id else None
        self._user_id = user_id

    def to_dict(self) -> dict:
        return {
            "_": "Message",
            "id": self.id,
            "date": self.date,
            "message": self.text,
            "peer_id": {"_": "PeerUser", "user_id": self._user_id},
            "from_id": {"_": "PeerUser", "user_id": self._user_id},
        }


# ---------------------------------------------------------------------------- #
# redact()
# ---------------------------------------------------------------------------- #

class TestRedact:
    def test_flat_account_id_fields_redacted(self):
        raw = {
            "authorId": "acc-1", "accountId": "acc-2", "ownerId": "acc-3",
            "createdById": "acc-4", "lastModifiedById": "acc-5",
        }
        out = recorder.redact(raw)
        assert all(v == recorder._REDACTED_ACCOUNT_ID for v in out.values())

    def test_email_fields_redacted(self):
        raw = {"email": "a@b.com", "emailAddress": "c@d.com"}
        out = recorder.redact(raw)
        assert all(v == recorder._REDACTED_EMAIL for v in out.values())

    def test_display_name_fields_redacted(self):
        raw = {"displayName": "Real Name", "publicName": "Real Public"}
        out = recorder.redact(raw)
        assert all(v == recorder._REDACTED_NAME for v in out.values())

    def test_bare_name_key_is_not_redacted(self):
        # A bare "name" is legitimate content (a space's name, a record's
        # Name field) at least as often as it's a person's -- deliberately
        # excluded, see the comment above _REDACT_NAME_KEYS.
        raw = {"name": "PrivacyFence QA Primary"}
        assert recorder.redact(raw) == raw

    def test_whole_object_redacted_for_relationship_keys(self):
        # The gap found while verifying Salesforce support: Owner/CreatedBy/
        # LastModifiedBy can be nested {Id, Name, Email} objects, not flat
        # *Id strings -- the whole object is replaced, not scanned key by
        # key, since a bare "Name" sub-key would otherwise survive.
        raw = {"Owner": {"Id": "005xx", "Name": "Real Owner", "Email": "real@company.com"}}
        out = recorder.redact(raw)
        assert out["Owner"] == {"Id": recorder._REDACTED_ACCOUNT_ID, "Name": recorder._REDACTED_NAME}

    def test_recurses_into_nested_lists_and_dicts(self):
        raw = {"results": [{"authorId": "acc-1"}, {"authorId": "acc-2"}]}
        out = recorder.redact(raw)
        assert out["results"][0]["authorId"] == recorder._REDACTED_ACCOUNT_ID
        assert out["results"][1]["authorId"] == recorder._REDACTED_ACCOUNT_ID

    def test_content_fields_are_left_alone(self):
        raw = {"title": "PrivacyFence QA seed page [QATEST]", "body": "synthetic content"}
        assert recorder.redact(raw) == raw


class TestRedactGmailMessage:
    """Gmail's identity data lives inside payload.headers -- a *list* of
    {"name": "From", "value": "..."} objects where the interesting key is
    always the generic "value", never a distinctively-named field. The
    key-based redact() structurally cannot see this; found before shipping
    Gmail support, not discovered after a fixture was already committed.
    """

    def test_from_and_to_headers_redacted(self):
        raw = {"payload": {"headers": [
            {"name": "From", "value": "Real User <real@company.com>"},
            {"name": "To", "value": "Real User <real@company.com>"},
        ]}}
        out = recorder.redact_gmail_message(raw)
        assert out["payload"]["headers"][0]["value"] == recorder._REDACTED_EMAIL
        assert out["payload"]["headers"][1]["value"] == recorder._REDACTED_EMAIL

    def test_subject_and_date_headers_are_left_alone(self):
        raw = {"payload": {"headers": [
            {"name": "Subject", "value": "PrivacyFence QA seed message [QATEST]"},
            {"name": "Date", "value": "Wed, 15 Jul 2026 10:00:00 +0000"},
        ]}}
        assert recorder.redact_gmail_message(raw) == raw

    def test_missing_payload_does_not_raise(self):
        assert recorder.redact_gmail_message({}) == {}

    def test_does_not_mutate_input(self):
        raw = {"payload": {"headers": [{"name": "From", "value": "real@company.com"}]}}
        recorder.redact_gmail_message(raw)
        assert raw["payload"]["headers"][0]["value"] == "real@company.com"


class TestRedactSlackMessages:
    """Slack identifies message authors through a single generic "user" (or
    "bot_id") key -- too generic to add to the shared _REDACT_ACCOUNT_ID_KEYS
    without risking over-redaction elsewhere; found the same way as the
    Salesforce/Gmail gaps, by building a realistic response and checking the
    redacted output before shipping.
    """

    def test_user_and_bot_id_redacted(self):
        raw = {"messages": [{"ts": "1.1", "user": "U123", "text": "hi"}, {"ts": "1.2", "bot_id": "B456", "text": "bot hi"}]}
        out = recorder.redact_slack_messages(raw)
        assert out["messages"][0]["user"] == recorder._REDACTED_SLACK_USER_ID
        assert out["messages"][1]["bot_id"] == recorder._REDACTED_SLACK_USER_ID

    def test_nested_identity_in_edited_reactions_and_files_redacted(self):
        raw = {"messages": [{
            "ts": "1.1", "user": "U123", "text": "hi",
            "edited": {"user": "U123", "ts": "1.2"},
            "reactions": [{"name": "thumbsup", "users": ["U123", "U456"], "count": 2}],
            "files": [{"id": "F1", "user": "U123", "name": "notes.txt"}],
        }]}
        out = recorder.redact_slack_messages(raw)
        message = out["messages"][0]
        assert message["edited"]["user"] == recorder._REDACTED_SLACK_USER_ID
        assert message["reactions"][0]["users"] == [recorder._REDACTED_SLACK_USER_ID] * 2
        assert message["files"][0]["user"] == recorder._REDACTED_SLACK_USER_ID
        assert message["files"][0]["name"] == "notes.txt"  # content, untouched

    def test_text_and_ts_are_left_alone(self):
        raw = {"messages": [{"ts": "1.1", "user": "U123", "text": "PrivacyFence QA seed message [QATEST]"}]}
        out = recorder.redact_slack_messages(raw)
        assert out["messages"][0]["text"] == "PrivacyFence QA seed message [QATEST]"
        assert out["messages"][0]["ts"] == "1.1"

    def test_does_not_mutate_input(self):
        raw = {"messages": [{"ts": "1.1", "user": "U123", "text": "hi"}]}
        recorder.redact_slack_messages(raw)
        assert raw["messages"][0]["user"] == "U123"

    def test_missing_messages_does_not_raise(self):
        assert recorder.redact_slack_messages({}) == {}


class TestTelethonToJsonable:
    """Telethon returns typed TL objects, not JSON dicts -- this is the
    function that bridges the two before anything gets written to a
    fixture file.
    """

    def test_converts_object_via_to_dict(self):
        msg = _FakeTelethonMessage(id=1, text="hi", date=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc))
        out = recorder._telethon_to_jsonable(msg)
        assert out["id"] == 1
        assert out["message"] == "hi"
        assert out["date"] == "2026-07-16T10:00:00+00:00"  # datetime -> isoformat string

    def test_recurses_into_a_list_of_objects(self):
        msgs = [
            _FakeTelethonMessage(id=1, text="a", date=None),
            _FakeTelethonMessage(id=2, text="b", date=None),
        ]
        out = recorder._telethon_to_jsonable(msgs)
        assert [m["id"] for m in out] == [1, 2]

    def test_converts_bytes_to_hex(self):
        assert recorder._telethon_to_jsonable(b"\x01\x02") == "0102"

    def test_recurses_into_nested_objects_with_their_own_to_dict(self):
        # Telethon's own custom.Dialog.to_dict() doesn't recursively resolve
        # nested TL objects the way generated to_dict()s do -- this is the
        # case that matters most: a dict value that is itself still a raw
        # object, not yet a dict.
        class _Nested:
            def to_dict(self):
                return {"user_id": 5}

        out = recorder._telethon_to_jsonable({"peer": _Nested()})
        assert out == {"peer": {"user_id": 5}}

    def test_leaves_plain_values_alone(self):
        assert recorder._telethon_to_jsonable({"a": 1, "b": "x", "c": None}) == {"a": 1, "b": "x", "c": None}


class TestRedactTelegramMessages:
    """Telegram identifies chat/sender through numeric Peer sub-objects
    ({"user_id": 123...}) nested under peer_id/from_id/saved_peer_id, not a
    distinctively-named top-level field -- the same kind of gap as Slack's
    generic "user" key, found the same way: building a realistic response
    and checking the redacted output before shipping.
    """

    def test_peer_and_from_user_ids_redacted(self):
        raw = [{
            "id": 1, "message": "hi",
            "peer_id": {"_": "PeerUser", "user_id": 111222333},
            "from_id": {"_": "PeerUser", "user_id": 111222333},
        }]
        out = recorder.redact_telegram_messages(raw)
        assert out[0]["peer_id"]["user_id"] == recorder._REDACTED_TELEGRAM_USER_ID
        assert out[0]["from_id"]["user_id"] == recorder._REDACTED_TELEGRAM_USER_ID

    def test_message_text_and_id_are_left_alone(self):
        raw = [{"id": 1, "peer_id": {"user_id": 111}, "message": "PrivacyFence QA seed message [QATEST]"}]
        out = recorder.redact_telegram_messages(raw)
        assert out[0]["message"] == "PrivacyFence QA seed message [QATEST]"
        assert out[0]["id"] == 1

    def test_does_not_mutate_input(self):
        raw = [{"id": 1, "peer_id": {"user_id": 111}}]
        recorder.redact_telegram_messages(raw)
        assert raw[0]["peer_id"]["user_id"] == 111

    def test_missing_peer_fields_do_not_raise(self):
        assert recorder.redact_telegram_messages([{"id": 1}]) == [{"id": 1}]


# ---------------------------------------------------------------------------- #
# RawCapture / RawCaptureCall -- against real clients, mocked SDK object
# ---------------------------------------------------------------------------- #

class TestRawCapture:
    def test_captures_the_request_result(self):
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1"})
        client._client = MagicMock()
        client._client.get.return_value = {"key": "PFQA"}

        with recorder.RawCapture(client) as cap:
            result = client._request(client._client.get, "some/path")

        assert cap.captured == {"key": "PFQA"}
        assert result == {"key": "PFQA"}

    def test_capture_does_not_alias_later_mutation(self):
        # copy.deepcopy at capture time -- a caller mutating the returned
        # object afterward must not silently change what gets recorded.
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1"})
        client._client = MagicMock()
        mutable = {"results": [{"key": "PFQA"}]}
        client._client.get.return_value = mutable

        with recorder.RawCapture(client) as cap:
            result = client._request(client._client.get, "x")
        result["results"][0]["key"] = "MUTATED"

        assert cap.captured["results"][0]["key"] == "PFQA"

    def test_restores_original_request_on_exit(self):
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1"})
        client._client = MagicMock()
        client._client.get.return_value = {"key": "PFQA"}

        with recorder.RawCapture(client) as cap:
            client._request(client._client.get, "x")
        client._request(client._client.get, "y")  # outside the with block

        assert cap.captured == {"key": "PFQA"}  # unchanged by the second call


class TestRawCaptureCall:
    def test_captures_the_call_result(self):
        client = SalesforceClient(config={"access_token": "t", "instance_url": "https://my.salesforce.com"})
        sf = MagicMock()
        sf.query.return_value = {"records": [{"Id": "r1"}]}
        client._get_sf = lambda: sf

        with recorder.RawCaptureCall(client) as cap:
            result = client._call(lambda s: s.query("SELECT Id FROM Report"))

        assert cap.captured == {"records": [{"Id": "r1"}]}
        assert result == {"records": [{"Id": "r1"}]}

    def test_restores_original_call_on_exit(self):
        client = SalesforceClient(config={"access_token": "t", "instance_url": "https://my.salesforce.com"})
        sf = MagicMock()
        sf.query.return_value = {"records": []}
        client._get_sf = lambda: sf

        with recorder.RawCaptureCall(client) as cap:
            client._call(lambda s: s.query("x"))
        client._call(lambda s: s.query("y"))

        assert cap.captured == {"records": []}


class TestRawCaptureExecute:
    """The one capture class that can't be verified against a MagicMock
    service double -- a mock never touches googleapiclient.http.HttpRequest
    at all, so this builds a real one, fully offline.
    """

    def test_captures_a_real_http_request_execute_result(self):
        from googleapiclient.discovery import build

        class FakeHttp:
            def request(self, uri, method="GET", body=None, headers=None, **kw):
                resp = type("Resp", (), {"status": 200, "reason": "OK"})()
                return resp, json.dumps({"id": "m1", "snippet": "hi"}).encode()

        service = build(
            "gmail", "v1", static_discovery=True, cache_discovery=False,
            http=FakeHttp(), developerKey="unused",
        )
        req = service.users().messages().get(userId="me", id="m1")

        with recorder.RawCaptureExecute() as cap:
            result = req.execute()

        assert cap.captured == {"id": "m1", "snippet": "hi"}
        assert result == {"id": "m1", "snippet": "hi"}

    def test_restores_original_execute_on_exit(self):
        from googleapiclient.discovery import build
        from googleapiclient.http import HttpRequest

        class FakeHttp:
            def request(self, uri, method="GET", body=None, headers=None, **kw):
                resp = type("Resp", (), {"status": 200, "reason": "OK"})()
                return resp, b"{}"

        service = build(
            "gmail", "v1", static_discovery=True, cache_discovery=False,
            http=FakeHttp(), developerKey="unused",
        )
        original = HttpRequest.execute
        with recorder.RawCaptureExecute():
            pass
        assert HttpRequest.execute is original

        req = service.users().messages().get(userId="me", id="m1")
        req.execute()  # must not raise / must not still be wrapped


class TestRawCaptureApiCall:
    """SlackClient's choke point is slack_sdk's own WebClient.api_call(), not
    a PrivacyFence-level method the way ConfluenceClient/JiraClient/
    SalesforceClient have -- tested against a real WebClient (api_call
    monkeypatched, no network), not a MagicMock, so conversations_replies()
    etc. genuinely route through it.
    """

    def test_captures_by_api_method_name(self):
        client = SlackClient(user_token="xoxp-fake-token")
        client._client = _fake_slack_web_client({
            "conversations.replies": {"messages": [{"ts": "1.1", "text": "hi", "user": "U1"}]},
        })

        with recorder.RawCaptureApiCall(client) as cap:
            result = client.get_thread_replies("C1", "1.1")

        assert cap.captured["conversations.replies"]["messages"][0]["text"] == "hi"
        assert result[0].text == "hi"

    def test_keeps_the_matching_call_when_a_second_api_call_happens_inside(self):
        # get_thread_replies() triggers conversations.replies for the
        # messages *and* users.info to resolve the author's display name --
        # a capture that only kept the most recent result would end up
        # holding users.info's response instead.
        client = SlackClient(user_token="xoxp-fake-token")
        client._client = _fake_slack_web_client({
            "conversations.replies": {"messages": [{"ts": "1.1", "text": "hi", "user": "U1"}]},
            "users.info": {"user": {"id": "U1", "name": "qauser", "profile": {}}},
        })

        with recorder.RawCaptureApiCall(client) as cap:
            client.get_thread_replies("C1", "1.1")

        assert "conversations.replies" in cap.captured
        assert "users.info" in cap.captured
        assert cap.captured["conversations.replies"]["messages"][0]["ts"] == "1.1"

    def test_capture_does_not_alias_later_mutation(self):
        client = SlackClient(user_token="xoxp-fake-token")
        mutable_messages = [{"ts": "1.1", "text": "hi", "user": "U1"}]
        client._client = _fake_slack_web_client({"conversations.replies": {"messages": mutable_messages}})

        with recorder.RawCaptureApiCall(client) as cap:
            client.get_thread_replies("C1", "1.1")
        mutable_messages[0]["text"] = "MUTATED"

        assert cap.captured["conversations.replies"]["messages"][0]["text"] == "hi"

    def test_restores_original_api_call_on_exit(self):
        client = SlackClient(user_token="xoxp-fake-token")
        client._client = _fake_slack_web_client({
            "conversations.replies": {"messages": [{"ts": "1.1", "text": "hi", "user": "U1"}]},
        })
        original = client._client.api_call

        with recorder.RawCaptureApiCall(client):
            pass

        assert client._client.api_call is original


class TestRawCaptureTelethon:
    """TelegramPrivacyFenceClient's choke point is a single bound async
    method on the underlying telethon.TelegramClient instance -- tested
    against a MagicMock-backed fake Telethon client (async methods
    explicitly set to AsyncMock), the same pattern test_telegram_client.py
    already uses for this client.
    """

    def _client_with(self, fake_telethon_client: MagicMock) -> TelegramPrivacyFenceClient:
        client = TelegramPrivacyFenceClient(api_id=1, api_hash="h", session_file="/tmp/unused.session")
        client._client = fake_telethon_client
        client._connected = True
        return client

    async def test_captures_and_converts_the_result(self):
        msg = _FakeTelethonMessage(id=1, text="hi", date=datetime(2026, 7, 16, tzinfo=timezone.utc))
        fake = MagicMock()
        fake.get_messages = AsyncMock(return_value=[msg])
        client = self._client_with(fake)

        with recorder.RawCaptureTelethon(client, "get_messages") as cap:
            result = await client.get_messages(5, 10)

        assert cap.captured[0]["id"] == 1
        assert cap.captured[0]["message"] == "hi"
        assert result[0].text == "hi"  # the real TelegramMessage, unaffected by the capture

    async def test_restores_original_method_on_exit(self):
        fake = MagicMock()
        original = AsyncMock(return_value=[])
        fake.get_messages = original
        client = self._client_with(fake)

        with recorder.RawCaptureTelethon(client, "get_messages"):
            pass

        assert fake.get_messages is original


# ---------------------------------------------------------------------------- #
# check_confluence / check_jira / check_salesforce / check_gmail --
# guardrail + redaction, end to end
# ---------------------------------------------------------------------------- #

class TestCheckConfluence:
    def _client(self) -> ConfluenceClient:
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1", "site_url": "https://acme.atlassian.net"})
        client._client = MagicMock()
        return client

    def test_tagged_seed_page_records_successfully(self, monkeypatch):
        client = self._client()
        client._client.get.side_effect = [
            {"results": [{"key": "PFQA", "name": "Primary"}]},
            {"results": [{"id": "999"}]},
            {"results": [{
                "id": "123", "title": "PrivacyFence QA seed page [QATEST]", "spaceId": "999",
                "version": {"number": 3, "createdAt": "2026-07-01T00:00:00Z"},
                "authorId": "acc-1", "createdAt": "2026-01-01T00:00:00Z",
                "_links": {"webui": "/spaces/PFQA/pages/123"},
            }]},
        ]
        monkeypatch.setattr(recorder, "_build_confluence_client", lambda: client)

        results = recorder.check_confluence(record=True, manifest={"confluence": {}})

        get_page = next(r for r in results if r.method == "get_page")
        assert get_page.ok
        assert get_page.raw["results"][0]["authorId"] == recorder._REDACTED_ACCOUNT_ID

    def test_untagged_page_is_refused(self, monkeypatch):
        client = self._client()
        client._client.get.side_effect = [
            {"id": "999", "title": "Some real unrelated page", "spaceId": "999",
             "version": {"number": 1, "createdAt": "x"}, "authorId": "acc-1", "createdAt": "y",
             "_links": {"webui": "/x"}},
            {"key": "PFQA"},
        ]
        monkeypatch.setattr(recorder, "_build_confluence_client", lambda: client)

        results = recorder.check_confluence(record=True, manifest={"confluence": {"seed_page_id": "999"}})

        get_page = next(r for r in results if r.method == "get_page")
        assert not get_page.ok
        assert get_page.raw is None
        assert "does not carry" in get_page.note


class TestCheckJira:
    def _client(self) -> JiraClient:
        client = JiraClient(config={"access_token": "t", "cloud_id": "c1", "site_url": "https://acme.atlassian.net"})
        client._client = MagicMock()
        return client

    def test_reporter_identity_redacted(self, monkeypatch):
        client = self._client()
        client._client.projects.return_value = [{"key": "PFQA", "name": "PrivacyFence QA"}]
        client._client.issue.return_value = {
            "key": "PFQA-1",
            "fields": {
                "summary": "PrivacyFence QA seed issue [QATEST]",
                "status": {"name": "To Do"},
                "reporter": {"displayName": "Real Reporter", "emailAddress": "real@company.com"},
                "assignee": None, "created": "x", "updated": "y",
            },
        }
        monkeypatch.setattr(recorder, "_build_jira_client", lambda: client)

        results = recorder.check_jira(record=True, manifest={"jira": {"seed_issue_key": "PFQA-1"}})

        get_issue = next(r for r in results if r.method == "get_issue")
        assert get_issue.ok
        assert get_issue.raw["fields"]["reporter"]["displayName"] == recorder._REDACTED_NAME
        assert get_issue.raw["fields"]["reporter"]["emailAddress"] == recorder._REDACTED_EMAIL

    def test_stale_seed_issue_key_is_refused(self, monkeypatch):
        client = self._client()
        client._client.issue.return_value = {
            "key": "PFQA-99",
            "fields": {"summary": "Some real unrelated issue", "status": {"name": "Done"}},
        }
        monkeypatch.setattr(recorder, "_build_jira_client", lambda: client)

        results = recorder.check_jira(record=True, manifest={"jira": {"seed_issue_key": "PFQA-99"}})

        get_issue = next(r for r in results if r.method == "get_issue")
        assert not get_issue.ok
        assert get_issue.raw is None


class TestCheckSalesforce:
    def _client(self, sf: MagicMock) -> SalesforceClient:
        client = SalesforceClient(config={"access_token": "t", "instance_url": "https://my.salesforce.com"})
        client._get_sf = lambda: sf
        return client

    def test_owner_relationship_redacted(self, monkeypatch):
        sf = MagicMock()
        sf.query.return_value = {"records": [{"Id": "r1", "Name": "PrivacyFence QA Report"}]}
        sf.Account.get.return_value = {
            "attributes": {"type": "Account"}, "Id": "001a",
            "Name": "PrivacyFence QA — Acme Test Co [QATEST]",
            "Owner": {"Id": "005xx", "Name": "Real Owner", "Email": "real@company.com"},
        }
        monkeypatch.setattr(recorder, "_build_salesforce_client", lambda: self._client(sf))

        results = recorder.check_salesforce(
            record=True, manifest={"salesforce": {"seed_record_id": "001a"}},
        )

        get_record = next(r for r in results if r.method == "get_record")
        assert get_record.ok
        assert get_record.raw["Owner"] == {"Id": recorder._REDACTED_ACCOUNT_ID, "Name": recorder._REDACTED_NAME}

    def test_untagged_record_is_refused(self, monkeypatch):
        sf = MagicMock()
        sf.Account.get.return_value = {
            "attributes": {"type": "Account"}, "Id": "001x", "Name": "Some real unrelated account",
        }
        monkeypatch.setattr(recorder, "_build_salesforce_client", lambda: self._client(sf))

        results = recorder.check_salesforce(
            record=True, manifest={"salesforce": {"seed_record_id": "001x"}},
        )

        get_record = next(r for r in results if r.method == "get_record")
        assert not get_record.ok
        assert get_record.raw is None


class TestCheckGmail:
    def _service(self, raw_message: dict):
        return _offline_google_service("gmail", "v1", raw_message)

    def test_missing_seed_message_id_fails_without_a_call(self, monkeypatch):
        # No _build_gmail_client patch needed -- this must fail before
        # ever trying to build a client.
        results = recorder.check_gmail(record=True, manifest={})
        assert len(results) == 1
        assert not results[0].ok
        assert "seed_message_id" in results[0].note

    def test_from_and_to_headers_redacted(self, monkeypatch):
        raw_message = {
            "id": "m1", "threadId": "t1",
            "payload": {"headers": [
                {"name": "From", "value": "real@company.com"},
                {"name": "To", "value": "real@company.com"},
                {"name": "Subject", "value": "PrivacyFence QA seed message [QATEST]"},
                {"name": "Date", "value": "Wed, 15 Jul 2026 10:00:00 +0000"},
            ]},
        }
        client = GmailClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = self._service(raw_message)
        monkeypatch.setattr(recorder, "_build_gmail_client", lambda: client)

        results = recorder.check_gmail(record=True, manifest={"gmail": {"seed_message_id": "m1"}})

        get_message = next(r for r in results if r.method == "get_message")
        assert get_message.ok
        headers = {h["name"]: h["value"] for h in get_message.raw["payload"]["headers"]}
        assert headers["From"] == recorder._REDACTED_EMAIL
        assert headers["To"] == recorder._REDACTED_EMAIL
        assert headers["Subject"] == "PrivacyFence QA seed message [QATEST]"

    def test_untagged_message_is_refused(self, monkeypatch):
        raw_message = {
            "id": "m99", "threadId": "t99",
            "payload": {"headers": [
                {"name": "From", "value": "real@company.com"},
                {"name": "Subject", "value": "Some real unrelated email"},
                {"name": "Date", "value": "x"},
            ]},
        }
        client = GmailClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = self._service(raw_message)
        monkeypatch.setattr(recorder, "_build_gmail_client", lambda: client)

        results = recorder.check_gmail(record=True, manifest={"gmail": {"seed_message_id": "m99"}})

        get_message = next(r for r in results if r.method == "get_message")
        assert not get_message.ok
        assert get_message.raw is None


class TestCheckDrive:
    def test_owner_identity_redacted_when_targeted_by_id(self, monkeypatch):
        raw_file = {
            "id": "f1", "name": "PrivacyFence QA Sandbox", "mimeType": "application/vnd.google-apps.folder",
            "owners": [{"emailAddress": "real@company.com", "displayName": "Real Owner", "me": True}],
        }
        client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("drive", "v3", raw_file)
        monkeypatch.setattr(recorder, "_build_drive_client", lambda: client)

        results = recorder.check_drive(record=True, manifest={"drive": {"folder_id": "f1"}})

        get_meta = next(r for r in results if r.method == "get_file_metadata")
        assert get_meta.ok
        owner = get_meta.raw["owners"][0]
        assert owner["emailAddress"] == recorder._REDACTED_EMAIL
        assert owner["displayName"] == recorder._REDACTED_NAME
        assert get_meta.raw["name"] == "PrivacyFence QA Sandbox"  # folder name is content, not identity

    def test_resolves_by_name_when_no_folder_id_configured(self, monkeypatch):
        # Two calls, two different response shapes: list_files() first
        # (wrapped in {"files": [...]}), then get_file_metadata() on the
        # resolved id (a bare file object) -- the fake transport returns
        # each in order.
        raw_file = {"id": "f1", "name": "PrivacyFence QA Sandbox", "mimeType": "application/vnd.google-apps.folder"}
        client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("drive", "v3", [{"files": [raw_file]}, raw_file])
        monkeypatch.setattr(recorder, "_build_drive_client", lambda: client)

        results = recorder.check_drive(record=True, manifest={})

        get_meta = next(r for r in results if r.method == "get_file_metadata")
        assert get_meta.ok

    def test_mismatched_folder_name_is_refused(self, monkeypatch):
        raw_file = {"id": "f1", "name": "Some Real Unrelated Folder", "mimeType": "application/vnd.google-apps.folder"}
        client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("drive", "v3", raw_file)
        monkeypatch.setattr(recorder, "_build_drive_client", lambda: client)

        results = recorder.check_drive(record=True, manifest={"drive": {"folder_id": "f1"}})

        get_meta = next(r for r in results if r.method == "get_file_metadata")
        assert not get_meta.ok
        assert get_meta.raw is None


class TestCheckCalendar:
    def test_organizer_and_attendee_identity_redacted(self, monkeypatch):
        raw_event = {
            "id": "e1", "summary": "PrivacyFence QA seed event [QATEST]",
            "start": {"dateTime": "2026-12-01T10:00:00Z"}, "end": {"dateTime": "2026-12-01T11:00:00Z"},
            "organizer": {"email": "real.user@company.com", "self": True},
            "attendees": [{"email": "real.other@company.com", "displayName": "Real Attendee", "responseStatus": "accepted"}],
        }
        client = CalendarClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("calendar", "v3", raw_event)
        monkeypatch.setattr(recorder, "_build_calendar_client", lambda: client)

        results = recorder.check_calendar(record=True, manifest={"calendar": {"seed_event_id": "e1"}})

        get_event = next(r for r in results if r.method == "get_event")
        assert get_event.ok
        assert get_event.raw["organizer"]["email"] == recorder._REDACTED_EMAIL
        assert get_event.raw["attendees"][0]["email"] == recorder._REDACTED_EMAIL
        assert get_event.raw["attendees"][0]["displayName"] == recorder._REDACTED_NAME
        assert get_event.raw["summary"] == "PrivacyFence QA seed event [QATEST]"  # content, untouched

    def test_untagged_event_is_refused(self, monkeypatch):
        raw_event = {
            "id": "e2", "summary": "Some real unrelated event",
            "start": {"dateTime": "x"}, "end": {"dateTime": "y"},
            "organizer": {"email": "real.user@company.com"},
        }
        client = CalendarClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("calendar", "v3", raw_event)
        monkeypatch.setattr(recorder, "_build_calendar_client", lambda: client)

        results = recorder.check_calendar(record=True, manifest={"calendar": {"seed_event_id": "e2"}})

        get_event = next(r for r in results if r.method == "get_event")
        assert not get_event.ok
        assert get_event.raw is None


class TestCheckContacts:
    """Contacts is the one connector where redaction is deliberately
    skipped -- the seed contact's own name/email/phone are the content
    under test, not someone else's identity leaking into it. These tests
    prove that choice explicitly (the fixture's own fields survive
    unredacted), not just that nothing crashes.
    """

    def test_seed_contact_fields_survive_unredacted(self, monkeypatch):
        raw_contact = {
            "resourceName": "people/c1",
            "names": [{"displayName": "PrivacyFence QA Test Contact [QATEST]"}],
            "emailAddresses": [{"value": "qatest.contact@example.com", "type": "home"}],
            "phoneNumbers": [{"value": "555-0142", "type": "home"}],
        }
        client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("people", "v1", raw_contact)
        monkeypatch.setattr(recorder, "_build_contacts_client", lambda: client)

        results = recorder.check_contacts(
            record=True, manifest={"contacts": {"seed_contact_resource_name": "people/c1"}},
        )

        get_contact = next(r for r in results if r.method == "get_contact")
        assert get_contact.ok
        # Deliberately NOT redacted -- see the comment in check_contacts().
        assert get_contact.raw["names"][0]["displayName"] == "PrivacyFence QA Test Contact [QATEST]"
        assert get_contact.raw["emailAddresses"][0]["value"] == "qatest.contact@example.com"

    def test_untagged_contact_is_refused(self, monkeypatch):
        raw_contact = {"resourceName": "people/c2", "names": [{"displayName": "Some Real Unrelated Person"}]}
        client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("people", "v1", raw_contact)
        monkeypatch.setattr(recorder, "_build_contacts_client", lambda: client)

        results = recorder.check_contacts(
            record=True, manifest={"contacts": {"seed_contact_resource_name": "people/c2"}},
        )

        get_contact = next(r for r in results if r.method == "get_contact")
        assert not get_contact.ok
        assert get_contact.raw is None


class TestCheckTasks:
    def test_missing_ids_fails_without_a_call(self):
        results = recorder.check_tasks(record=True, manifest={})
        assert len(results) == 1
        assert not results[0].ok
        assert "task_list_id" in results[0].note

    def test_tagged_seed_task_records_successfully(self, monkeypatch):
        raw_task = {"id": "t1", "title": "PrivacyFence QA seed task [QATEST]", "status": "needsAction"}
        client = TasksClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("tasks", "v1", raw_task)
        monkeypatch.setattr(recorder, "_build_tasks_client", lambda: client)

        results = recorder.check_tasks(
            record=True, manifest={"tasks": {"task_list_id": "l1", "seed_task_id": "t1"}},
        )

        get_task = next(r for r in results if r.method == "get_task")
        assert get_task.ok
        assert get_task.raw["title"] == "PrivacyFence QA seed task [QATEST]"

    def test_untagged_task_is_refused(self, monkeypatch):
        raw_task = {"id": "t2", "title": "Some real unrelated task", "status": "needsAction"}
        client = TasksClient(client_config={}, token_file="/tmp/unused-token.json")
        client._local.service = _offline_google_service("tasks", "v1", raw_task)
        monkeypatch.setattr(recorder, "_build_tasks_client", lambda: client)

        results = recorder.check_tasks(
            record=True, manifest={"tasks": {"task_list_id": "l1", "seed_task_id": "t2"}},
        )

        get_task = next(r for r in results if r.method == "get_task")
        assert not get_task.ok
        assert get_task.raw is None


class TestCheckSlack:
    SEED_TEXT = "PrivacyFence QA seed message [QATEST]. No real information. Safe to read/reply/delete."

    def _client(self, responses: dict[str, dict]) -> SlackClient:
        client = SlackClient(user_token="xoxp-fake-token")
        client._client = _fake_slack_web_client(responses)
        return client

    def test_resolves_channel_and_thread_when_not_configured(self, monkeypatch):
        client = self._client({
            "conversations.list": {
                "channels": [{"id": "C1", "name": "privacyfence-qa-control", "is_private": False, "num_members": 2}],
            },
            "conversations.history": {
                "messages": [{"ts": "100.1", "user": "U1", "text": self.SEED_TEXT}],
            },
            "conversations.replies": {
                "messages": [
                    {"ts": "100.1", "user": "U1", "text": self.SEED_TEXT},
                    {"ts": "100.2", "user": "U1", "text": "PrivacyFence QA seed reply [QATEST]. No real information.", "thread_ts": "100.1"},
                ],
            },
        })
        monkeypatch.setattr(recorder, "_build_slack_client", lambda: client)

        results = recorder.check_slack(record=True, manifest={})

        get_thread = next(r for r in results if r.method == "get_thread_replies")
        assert get_thread.ok
        assert get_thread.raw["messages"][0]["text"] == self.SEED_TEXT
        assert get_thread.raw["messages"][0]["user"] == recorder._REDACTED_SLACK_USER_ID

    def test_targets_configured_channel_and_thread_directly(self, monkeypatch):
        # channel_id and seed_thread_ts both configured -- no
        # conversations.list/conversations.history resolve calls needed.
        client = self._client({
            "conversations.replies": {
                "messages": [{"ts": "100.1", "user": "U1", "text": self.SEED_TEXT}],
            },
        })
        monkeypatch.setattr(recorder, "_build_slack_client", lambda: client)

        results = recorder.check_slack(
            record=True,
            manifest={"slack": {"channel_id": "C1", "seed_thread_ts": "100.1"}},
        )

        get_thread = next(r for r in results if r.method == "get_thread_replies")
        assert get_thread.ok
        assert get_thread.raw["messages"][0]["user"] == recorder._REDACTED_SLACK_USER_ID

    def test_untagged_thread_is_refused(self, monkeypatch):
        client = self._client({
            "conversations.replies": {
                "messages": [{"ts": "200.1", "user": "U1", "text": "Some real unrelated message"}],
            },
        })
        monkeypatch.setattr(recorder, "_build_slack_client", lambda: client)

        results = recorder.check_slack(
            record=True,
            manifest={"slack": {"channel_id": "C1", "seed_thread_ts": "200.1"}},
        )

        get_thread = next(r for r in results if r.method == "get_thread_replies")
        assert not get_thread.ok
        assert get_thread.raw is None

    def test_no_tagged_message_in_channel_history_is_refused(self, monkeypatch):
        client = self._client({
            "conversations.list": {
                "channels": [{"id": "C1", "name": "privacyfence-qa-control", "is_private": False, "num_members": 2}],
            },
            "conversations.history": {
                "messages": [{"ts": "300.1", "user": "U1", "text": "Some real unrelated message"}],
            },
        })
        monkeypatch.setattr(recorder, "_build_slack_client", lambda: client)

        results = recorder.check_slack(record=True, manifest={})

        get_thread = next(r for r in results if r.method == "get_thread_replies")
        assert not get_thread.ok
        assert get_thread.raw is None
        assert "[QATEST]" in get_thread.note


class TestCheckTelegram:
    """check_telegram() is a sync wrapper around asyncio.run() -- called
    from plain (non-async) test functions here, the same way the script's
    own run() calls it, since asyncio.run() can't be nested inside an
    already-running event loop (which pytest-asyncio's auto mode would
    otherwise put an `async def` test inside).
    """

    SEED_TEXT = "PrivacyFence QA seed message [QATEST]. No real information."

    def _connected_client(self, fake_telethon_client: MagicMock) -> TelegramPrivacyFenceClient:
        fake_telethon_client.disconnect = AsyncMock()
        client = TelegramPrivacyFenceClient(api_id=1, api_hash="h", session_file="/tmp/unused.session")
        client._client = fake_telethon_client
        client._connected = True
        return client

    def test_resolves_saved_messages_via_is_self_and_records_with_redaction(self, monkeypatch):
        from telethon.tl.types import User

        entity = MagicMock(spec=User)
        entity.id = 999
        entity.username = ""
        entity.bot = False
        entity.is_self = True
        dialog = SimpleNamespace(name="Saved Messages", entity=entity, unread_count=0)
        tagged_msg = _FakeTelethonMessage(
            id=1, text=self.SEED_TEXT, date=datetime(2026, 7, 16, tzinfo=timezone.utc), user_id=555,
        )

        fake = MagicMock()
        fake.get_dialogs = AsyncMock(return_value=[dialog])
        fake.get_messages = AsyncMock(return_value=[tagged_msg])
        client = self._connected_client(fake)
        monkeypatch.setattr(recorder, "_build_telegram_client", lambda: client)

        results = recorder.check_telegram(record=True, manifest={})

        get_messages = next(r for r in results if r.method == "get_messages")
        assert get_messages.ok
        assert get_messages.raw[0]["message"] == self.SEED_TEXT
        assert get_messages.raw[0]["peer_id"]["user_id"] == recorder._REDACTED_TELEGRAM_USER_ID
        assert get_messages.raw[0]["from_id"]["user_id"] == recorder._REDACTED_TELEGRAM_USER_ID
        fake.disconnect.assert_awaited_once()

    def test_targets_configured_chat_id_directly(self, monkeypatch):
        # chat_id configured -- no get_dialogs resolve call needed.
        tagged_msg = _FakeTelethonMessage(
            id=2, text=self.SEED_TEXT, date=datetime(2026, 7, 16, tzinfo=timezone.utc), user_id=555,
        )
        fake = MagicMock()
        fake.get_messages = AsyncMock(return_value=[tagged_msg])
        client = self._connected_client(fake)
        monkeypatch.setattr(recorder, "_build_telegram_client", lambda: client)

        results = recorder.check_telegram(record=True, manifest={"telegram": {"chat_id": "999"}})

        get_messages = next(r for r in results if r.method == "get_messages")
        assert get_messages.ok
        fake.get_dialogs.assert_not_called()

    def test_no_tagged_message_in_history_is_refused(self, monkeypatch):
        untagged_msg = _FakeTelethonMessage(
            id=9, text="Some real unrelated note", date=datetime(2026, 7, 16, tzinfo=timezone.utc), user_id=555,
        )
        fake = MagicMock()
        fake.get_messages = AsyncMock(return_value=[untagged_msg])
        client = self._connected_client(fake)
        monkeypatch.setattr(recorder, "_build_telegram_client", lambda: client)

        results = recorder.check_telegram(record=True, manifest={"telegram": {"chat_id": "999"}})

        get_messages = next(r for r in results if r.method == "get_messages")
        assert not get_messages.ok
        assert get_messages.raw is None
        assert "[QATEST]" in get_messages.note

    def test_no_saved_messages_chat_found_is_refused(self, monkeypatch):
        entity = MagicMock()
        entity.id = 111
        entity.is_self = False
        entity.username = ""
        dialog = SimpleNamespace(name="Some Other Chat", entity=entity, unread_count=0)
        fake = MagicMock()
        fake.get_dialogs = AsyncMock(return_value=[dialog])
        client = self._connected_client(fake)
        monkeypatch.setattr(recorder, "_build_telegram_client", lambda: client)

        results = recorder.check_telegram(record=True, manifest={})

        get_messages = next(r for r in results if r.method == "get_messages")
        assert not get_messages.ok
        assert "Saved Messages" in get_messages.note


# ---------------------------------------------------------------------------- #
# Manifest / report
# ---------------------------------------------------------------------------- #

class TestLoadManifest:
    def test_missing_manifest_exits(self, monkeypatch, tmp_path):
        monkeypatch.setattr(recorder, "MANIFEST_PATH", tmp_path / "does-not-exist.yaml")
        with pytest.raises(SystemExit):
            recorder.load_manifest()


class TestRenderReport:
    def test_report_includes_pass_fail_marks_and_notes(self):
        results = [
            recorder.CheckResult("confluence", "get_page", "seed", True, "all present"),
            recorder.CheckResult("jira", "get_issue", "seed", False, "does not carry [QATEST]"),
        ]
        report = recorder.render_report("qa_fixture_recorder.py --check", results)
        assert "✅ pass" in report
        assert "❌ fail" in report
        assert "does not carry [QATEST]" in report
