"""Tests for GmailClient's parsing/normalization logic: MIME-part walking,
address splitting, header extraction, and the reply-draft address-dedup
logic. These call real GmailClient methods against a MagicMock stand-in for
the googleapiclient service object (the same chained-call shape Google's
client library produces), so the actual normalization code runs -- unlike
the connector-layer tests, which mock GmailClient itself and never touch
this file.

GmailClient.__init__ does no I/O, so we construct it normally and set
the thread-local service directly to skip the OAuth/credential-loading
path entirely.

The OAuth2 token lifecycle itself (authorize_interactive / _load_credentials
/ _save_token) is exercised separately below, mocking at the google-auth
library boundary (Credentials.from_authorized_user_file /
InstalledAppFlow.from_client_config) rather than at _load_credentials
itself -- see test_tasks_client.py's module docstring for why.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from privacyfence.gmail_client import (
    SCOPES,
    Attachment,
    GmailClient,
    GmailClientError,
    GmailMessage,
    GmailThread,
    resolve_attachment_destination,
)
from googleapiclient.errors import HttpError

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "gmail"


def make_client(service: MagicMock) -> GmailClient:
    client = GmailClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    return client


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


def header_list(**kv) -> list[dict]:
    return [{"name": k, "value": v} for k, v in kv.items()]


# ---------------------------------------------------------------------------- #
# authorize_interactive
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractive:
    def test_missing_client_config_raises(self, tmp_path):
        client = GmailClient(client_config={}, token_file=str(tmp_path / "token.json"))
        with pytest.raises(GmailClientError, match="No Google organization config installed"):
            client.authorize_interactive()

    def test_runs_local_server_flow_and_persists_returned_credentials(self, tmp_path, monkeypatch):
        token_file = tmp_path / "nested" / "token.json"
        client = GmailClient(client_config={"installed": {"client_id": "cid"}}, token_file=str(token_file))

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fake_creds
        mock_from_client_config = MagicMock(return_value=fake_flow)
        monkeypatch.setattr(
            "privacyfence.gmail_client.InstalledAppFlow.from_client_config", mock_from_client_config
        )

        client.authorize_interactive()

        mock_from_client_config.assert_called_once_with({"installed": {"client_id": "cid"}}, SCOPES)
        fake_flow.run_local_server.assert_called_once_with(port=0)
        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'


# ---------------------------------------------------------------------------- #
# _load_credentials: no-token / valid / expired-refresh-succeeds /
# expired-refresh-fails / expired-unrefreshable. Mocks
# Credentials.from_authorized_user_file (the google-auth library boundary),
# not _load_credentials itself.
# ---------------------------------------------------------------------------- #

class TestLoadCredentials:
    def test_missing_token_file_raises(self, tmp_path):
        client = GmailClient(client_config={}, token_file=str(tmp_path / "does-not-exist.json"))
        with pytest.raises(GmailClientError, match="No OAuth token found"):
            client._load_credentials()

    def test_valid_token_is_returned_without_refresh_or_network(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = True
        monkeypatch.setattr(
            "privacyfence.gmail_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = GmailClient(client_config={}, token_file=str(token_file))

        result = client._load_credentials()

        assert result is fake_creds
        fake_creds.refresh.assert_not_called()

    def test_expired_token_with_refresh_token_is_refreshed_and_saved_back(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-me"
        fake_creds.to_json.return_value = '{"token": "refreshed"}'
        monkeypatch.setattr(
            "privacyfence.gmail_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = GmailClient(client_config={}, token_file=str(token_file))

        result = client._load_credentials()

        assert result is fake_creds
        fake_creds.refresh.assert_called_once()
        assert token_file.read_text(encoding="utf-8") == '{"token": "refreshed"}'

    def test_expired_token_refresh_failure_raises_clear_error(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-me"
        fake_creds.refresh.side_effect = Exception("token has been revoked")
        monkeypatch.setattr(
            "privacyfence.gmail_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = GmailClient(client_config={}, token_file=str(token_file))

        with pytest.raises(GmailClientError, match="Failed to refresh OAuth token.*revoked"):
            client._load_credentials()

    def test_expired_token_without_refresh_token_raises_invalid_cached_token(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = ""
        monkeypatch.setattr(
            "privacyfence.gmail_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = GmailClient(client_config={}, token_file=str(token_file))

        with pytest.raises(GmailClientError, match="Cached OAuth token is invalid"):
            client._load_credentials()


# ---------------------------------------------------------------------------- #
# _save_token: file permissions
# ---------------------------------------------------------------------------- #

class TestSaveToken:
    def test_writes_credentials_json_with_owner_only_permissions(self, tmp_path):
        token_file = tmp_path / "nested" / "token.json"
        client = GmailClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'

        client._save_token(fake_creds)

        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_chmod_failure_is_non_fatal(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        client = GmailClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = "{}"
        monkeypatch.setattr("os.chmod", MagicMock(side_effect=OSError("read-only filesystem")))

        client._save_token(fake_creds)  # must not raise

        assert token_file.exists()


# ---------------------------------------------------------------------------- #
# check_connection
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_authorized_email_address(self):
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "me@example.com"
        }
        client = make_client(service)
        assert client.check_connection() == "me@example.com"

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.getProfile.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="Gmail connection check failed"):
            client.check_connection()


# ---------------------------------------------------------------------------- #
# Small pure helpers
# ---------------------------------------------------------------------------- #

class TestClampMaxResults:
    @pytest.mark.parametrize("value,expected", [
        (10, 10), (1, 1), (100, 100), (0, 1), (-5, 1), (500, 100),
        ("20", 20), ("not a number", 10), (None, 10),
    ])
    def test_clamps_into_1_to_100(self, value, expected):
        assert GmailClient._clamp_max_results(value) == expected


class TestHeadersToDict:
    def test_lowercases_names_and_maps_values(self):
        message = {"payload": {"headers": header_list(Subject="Hi", From="a@x.com")}}
        assert GmailClient._headers_to_dict(message) == {"subject": "Hi", "from": "a@x.com"}

    def test_missing_headers_yields_empty_dict(self):
        assert GmailClient._headers_to_dict({"payload": {}}) == {}
        assert GmailClient._headers_to_dict({}) == {}


class TestSplitAddresses:
    def test_splits_on_comma_and_strips_whitespace(self):
        assert GmailClient._split_addresses("a@x.com, b@x.com,  c@x.com") == ["a@x.com", "b@x.com", "c@x.com"]

    def test_empty_string_yields_empty_list(self):
        assert GmailClient._split_addresses("") == []

    def test_drops_empty_entries_from_trailing_comma(self):
        assert GmailClient._split_addresses("a@x.com,,") == ["a@x.com"]


# ---------------------------------------------------------------------------- #
# _parse_message / _walk_parts: MIME tree walking
# ---------------------------------------------------------------------------- #

class TestParseMessage:
    def test_flat_text_plain_message(self):
        client = make_client(MagicMock())
        raw = {
            "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": header_list(Subject="Hello", From="a@x.com", To="b@x.com", Date="today"),
                "mimeType": "text/plain",
                "body": {"data": b64("plain body")},
            },
        }
        msg = client._parse_message(raw)
        assert msg == GmailMessage(
            id="m1", thread_id="t1", subject="Hello", sender="a@x.com",
            recipients=["b@x.com"], date="today", body_text="plain body", body_html="",
            attachments=[], labels=["INBOX", "UNREAD"],
        )

    def test_multipart_alternative_collects_both_text_and_html(self):
        client = make_client(MagicMock())
        raw = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("plain part")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>html part</p>")}},
                ],
            },
        }
        msg = client._parse_message(raw)
        assert msg.body_text == "plain part"
        assert msg.body_html == "<p>html part</p>"

    def test_nested_multipart_mixed_with_attachment_and_body(self):
        client = make_client(MagicMock())
        raw = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": b64("body text")}},
                        ],
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "report.pdf",
                        "body": {"size": 4096, "attachmentId": "att-1"},
                    },
                ],
            },
        }
        msg = client._parse_message(raw)
        assert msg.body_text == "body text"
        assert msg.attachments == [
            Attachment(name="report.pdf", mime_type="application/pdf", size=4096, attachment_id="att-1")
        ]

    def test_attachment_body_never_fetched_or_decoded_into_text(self):
        # An attachment part carries a filename; even if it also has a data
        # blob, that data must never be decoded into body_text/body_html --
        # attachment *content* must never leak into the normalized message.
        client = make_client(MagicMock())
        raw = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "filename": "notes.txt",
                        "body": {"data": b64("secret file contents"), "size": 20},
                    },
                ],
            },
        }
        msg = client._parse_message(raw)
        assert msg.body_text == ""
        assert msg.attachments == [Attachment(name="notes.txt", mime_type="text/plain", size=20)]

    def test_large_body_part_without_filename_is_fetched_via_attachment_id(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": b64("fetched large body")
        }
        client = make_client(service)
        raw = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "text/plain",
                "body": {"attachmentId": "att-body-1"},
            },
        }
        msg = client._parse_message(raw)
        assert msg.body_text == "fetched large body"
        service.users.return_value.messages.return_value.attachments.return_value.get.assert_called_once_with(
            userId="me", messageId="m1", id="att-body-1"
        )

    def test_attachment_fetch_failure_is_swallowed_not_fatal(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.side_effect = (
            RuntimeError("network blip")
        )
        client = make_client(service)
        raw = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "text/plain",
                "body": {"attachmentId": "att-body-1"},
            },
        }
        msg = client._parse_message(raw)  # must not raise
        assert msg.body_text == ""

    def test_malformed_base64_body_decodes_to_empty_string_not_fatal(self):
        client = make_client(MagicMock())
        raw = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "text/plain",
                "body": {"data": "%%%not-base64%%%"},
            },
        }
        msg = client._parse_message(raw)
        assert msg.body_text == ""

    def test_missing_optional_fields_default_sensibly(self):
        client = make_client(MagicMock())
        msg = client._parse_message({"payload": {}})
        assert msg.id == ""
        assert msg.thread_id == ""
        assert msg.subject == ""
        assert msg.sender == ""
        assert msg.recipients == []
        assert msg.labels == []
        assert msg.short_summary() == "(no subject) - from (unknown sender)"

    def test_concurrent_parses_never_fetch_attachment_against_wrong_message_id(self):
        # message_id used to be stashed on self during _parse_message and
        # read back deeper in the recursive _walk_parts -- two threads
        # parsing different messages at once could race on that shared
        # attribute and fetch an attachment against the wrong message id.
        # It's threaded through as a parameter now instead, which removes
        # the shared state outright (the race window on the old attribute
        # was narrow enough that GIL scheduling rarely hit it even before
        # the fix, so this asserts correctness going forward rather than
        # reliably reproducing the old bug on demand).
        seen_message_ids: dict[str, str] = {}

        def get_side_effect(**kwargs):
            # Force interleaving: message "1" pauses mid-fetch so message
            # "2"'s parse runs (and would clobber shared state, if any).
            if kwargs["messageId"] == "m1":
                time.sleep(0.05)
            seen_message_ids[kwargs["messageId"]] = kwargs["id"]
            mock = MagicMock()
            mock.execute.return_value = {"data": b64(f"body-for-{kwargs['messageId']}")}
            return mock

        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.side_effect = get_side_effect
        client = make_client(service)
        # _get_service() caches per-thread (see TestServiceIsThreadLocal below),
        # so worker threads need the mock service patched in directly rather
        # than relying on the main thread's cached instance.
        client._get_service = lambda: service

        def make_raw(message_id: str, attachment_id: str) -> dict:
            return {
                "id": message_id, "threadId": "t1",
                "payload": {
                    "headers": header_list(Subject="Hi", From="a@x.com"),
                    "mimeType": "text/plain",
                    "body": {"attachmentId": attachment_id},
                },
            }

        results: dict[str, GmailMessage] = {}

        def worker(message_id: str, attachment_id: str) -> None:
            results[message_id] = client._parse_message(make_raw(message_id, attachment_id))

        threads = [
            threading.Thread(target=worker, args=("m1", "att-1")),
            threading.Thread(target=worker, args=("m2", "att-2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert seen_message_ids == {"m1": "att-1", "m2": "att-2"}
        assert results["m1"].body_text == "body-for-m1"
        assert results["m2"].body_text == "body-for-m2"


# ---------------------------------------------------------------------------- #
# list_messages / get_message / list_threads / get_thread
# ---------------------------------------------------------------------------- #

class TestListMessages:
    def test_builds_summaries_from_metadata(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "1"}, {"id": "2"}]
        }
        def get_side_effect(**kwargs):
            mock = MagicMock()
            if kwargs["id"] == "1":
                mock.execute.return_value = {
                    "id": "1", "threadId": "t1",
                    "payload": {"headers": header_list(Subject="One", From="a@x.com", Date="d1")},
                }
            else:
                mock.execute.return_value = {
                    "id": "2", "threadId": "t2",
                    "payload": {"headers": header_list(Subject="Two", From="b@x.com", Date="d2")},
                }
            return mock
        service.users.return_value.messages.return_value.get.side_effect = get_side_effect

        client = make_client(service)
        summaries = client.list_messages("is:unread", max_results=5)

        assert summaries == [
            {"id": "1", "thread_id": "t1", "subject": "One", "sender": "a@x.com", "date": "d1"},
            {"id": "2", "thread_id": "t2", "subject": "Two", "sender": "b@x.com", "date": "d2"},
        ]

    def test_message_that_fails_to_fetch_is_skipped_not_fatal(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "1"}, {"id": "2"}]
        }
        def get_side_effect(**kwargs):
            mock = MagicMock()
            if kwargs["id"] == "1":
                mock.execute.side_effect = http_error(404)
            else:
                mock.execute.return_value = {
                    "id": "2", "threadId": "t2",
                    "payload": {"headers": header_list(Subject="Two", From="b@x.com")},
                }
            return mock
        service.users.return_value.messages.return_value.get.side_effect = get_side_effect

        client = make_client(service)
        summaries = client.list_messages("q")

        assert len(summaries) == 1
        assert summaries[0]["id"] == "2"

    def test_list_call_failure_raises_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="list_messages failed"):
            client.list_messages("q")


class TestGetMessage:
    def test_empty_message_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="non-empty message_id"):
            client.get_message("")

    def test_fetches_and_normalizes(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1",
            "payload": {
                "headers": header_list(Subject="Hi", From="a@x.com"),
                "mimeType": "text/plain",
                "body": {"data": b64("hello")},
            },
        }
        client = make_client(service)
        msg = client.get_message("m1")
        assert msg.subject == "Hi"
        assert msg.body_text == "hello"

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="get_message"):
            client.get_message("m1")


class TestListThreads:
    def test_builds_id_and_snippet_summaries(self):
        service = MagicMock()
        service.users.return_value.threads.return_value.list.return_value.execute.return_value = {
            "threads": [{"id": "t1", "snippet": "snip1"}, {"id": "t2", "snippet": "snip2"}]
        }
        client = make_client(service)
        assert client.list_threads("q") == [
            {"id": "t1", "snippet": "snip1"}, {"id": "t2", "snippet": "snip2"},
        ]

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.threads.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="list_threads failed"):
            client.list_threads("q")


class TestGetThread:
    def test_empty_thread_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="non-empty thread_id"):
            client.get_thread("")

    def test_subject_taken_from_first_message(self):
        service = MagicMock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "t1",
            "messages": [
                {"id": "m1", "threadId": "t1", "payload": {"headers": header_list(Subject="First", From="a@x.com")}},
                {"id": "m2", "threadId": "t1", "payload": {"headers": header_list(Subject="Second", From="b@x.com")}},
            ],
        }
        client = make_client(service)
        thread = client.get_thread("t1")
        assert thread.subject == "First"
        assert len(thread.messages) == 2
        assert thread.short_summary() == "First (2 messages)"

    def test_empty_thread_yields_empty_subject(self):
        service = MagicMock()
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "t1", "messages": [],
        }
        client = make_client(service)
        thread = client.get_thread("t1")
        assert thread.subject == ""
        assert thread.messages == []


# ---------------------------------------------------------------------------- #
# create_draft
# ---------------------------------------------------------------------------- #

class TestCreateDraft:
    def test_builds_mime_message_with_to_subject_body(self):
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d1"}
        client = make_client(service)

        result = client.create_draft(to="a@x.com", subject="Hi", body="body text")

        assert result == {"draft_id": "d1", "to": "a@x.com", "subject": "Hi"}
        call_kwargs = service.users.return_value.drafts.return_value.create.call_args.kwargs
        raw = call_kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        assert "to: a@x.com" in decoded
        assert "subject: Hi" in decoded
        assert "body text" in decoded

    def test_cc_and_bcc_included_when_provided(self):
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d1"}
        client = make_client(service)

        client.create_draft(to="a@x.com", subject="Hi", body="b", cc="c@x.com", bcc="d@x.com")

        raw = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        assert "cc: c@x.com" in decoded
        assert "bcc: d@x.com" in decoded

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.drafts.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="create_draft failed"):
            client.create_draft(to="a@x.com", subject="s", body="b")


# ---------------------------------------------------------------------------- #
# create_reply_draft: threading headers + reply-all address dedup
# ---------------------------------------------------------------------------- #

def make_reply_service(headers: dict, thread_id: str = "t1") -> MagicMock:
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "threadId": thread_id,
        "payload": {"headers": header_list(**headers)},
    }
    service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d1"}
    return service


class TestCreateReplyDraft:
    def test_empty_message_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="non-empty message_id"):
            client.create_reply_draft("", body="b")

    def test_reply_to_original_sender_only_by_default(self):
        service = make_reply_service({
            "Subject": "Original", "From": "sender@x.com", "To": "me@x.com",
            "Message-ID": "<orig@x.com>",
        })
        client = make_client(service)
        result = client.create_reply_draft("m1", body="reply body", my_email="me@x.com")

        assert result["to"] == "sender@x.com"
        assert result["cc"] == ""
        assert result["subject"] == "Re: Original"

    def test_reply_to_sender_with_non_ascii_display_name_keeps_address_plain(self):
        # Regression: assigning "Kázmér Kovács <kazmer@x.com>" straight to a
        # Message header RFC-2047-encodes the *whole* value (name, brackets,
        # and address) as one opaque blob once it contains non-ASCII text.
        # Gmail's own header parser then rejects the draft with "Invalid To
        # header", since an encoded-word can't stand in for a whole addr-spec.
        service = make_reply_service({
            "Subject": "Original", "From": "Kázmér Kovács <kazmer@x.com>", "To": "me@x.com",
        })
        client = make_client(service)
        client.create_reply_draft("m1", body="b", my_email="me@x.com")

        raw = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        to_header = next(line for line in decoded.splitlines() if line.lower().startswith("to:"))
        assert "<kazmer@x.com>" in to_header
        assert "kazmer@x.com" not in to_header.split("<")[0]

    def test_subject_already_prefixed_with_re_is_not_doubled(self):
        service = make_reply_service({"Subject": "Re: Original", "From": "s@x.com", "To": "me@x.com"})
        client = make_client(service)
        result = client.create_reply_draft("m1", body="b", my_email="me@x.com")
        assert result["subject"] == "Re: Original"

    def test_reply_all_includes_to_and_cc_excluding_self_and_sender(self):
        service = make_reply_service({
            "Subject": "Original", "From": "sender@x.com",
            "To": "me@x.com, other1@x.com", "Cc": "other2@x.com",
        })
        client = make_client(service)
        result = client.create_reply_draft("m1", body="b", reply_all=True, my_email="me@x.com")

        assert result["to"] == "sender@x.com"
        assert set(a.strip() for a in result["cc"].split(",")) == {"other1@x.com", "other2@x.com"}

    def test_reply_all_dedup_is_case_and_display_name_insensitive(self):
        service = make_reply_service({
            "Subject": "Original", "From": "Sender <sender@X.com>",
            "To": "Me <ME@x.com>, Other <OTHER@x.com>", "Cc": "other@X.COM",
        })
        client = make_client(service)
        result = client.create_reply_draft("m1", body="b", reply_all=True, my_email="me@x.com")

        # "me" excluded (self), "other" appears once despite case/display-name
        # variation between To and Cc.
        assert result["cc"].lower().count("other@x.com") == 1
        assert "me@x.com" not in result["cc"].lower()

    def test_explicit_cc_param_merged_with_reply_all_and_deduped(self):
        service = make_reply_service({
            "Subject": "Original", "From": "sender@x.com", "To": "me@x.com, other@x.com",
        })
        client = make_client(service)
        result = client.create_reply_draft(
            "m1", body="b", reply_all=True, my_email="me@x.com", cc="other@x.com, extra@x.com",
        )
        addrs = {a.strip() for a in result["cc"].split(",")}
        assert addrs == {"other@x.com", "extra@x.com"}

    def test_threading_headers_chain_off_original_message_id(self):
        service = make_reply_service({
            "Subject": "Original", "From": "sender@x.com",
            "Message-ID": "<orig-id@x.com>", "References": "<earlier@x.com>",
        }, thread_id="t99")
        client = make_client(service)
        client.create_reply_draft("m1", body="b", my_email="me@x.com")

        raw = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        assert "In-Reply-To: <orig-id@x.com>" in decoded
        assert "References: <earlier@x.com> <orig-id@x.com>" in decoded

        body_dict = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]
        assert body_dict["threadId"] == "t99"

    def test_no_references_header_falls_back_to_message_id_only(self):
        service = make_reply_service({
            "Subject": "Original", "From": "sender@x.com", "Message-ID": "<orig-id@x.com>",
        })
        client = make_client(service)
        client.create_reply_draft("m1", body="b", my_email="me@x.com")

        raw = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        assert "References: <orig-id@x.com>" in decoded

    def test_bcc_included_when_provided(self):
        service = make_reply_service({"Subject": "Original", "From": "sender@x.com"})
        client = make_client(service)
        client.create_reply_draft("m1", body="b", my_email="me@x.com", bcc="hidden@x.com")

        raw = service.users.return_value.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        assert "bcc: hidden@x.com" in decoded

    def test_http_error_on_headers_fetch_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="get_message"):
            client.create_reply_draft("m1", body="b", my_email="me@x.com")


# ---------------------------------------------------------------------------- #
# Label operations
# ---------------------------------------------------------------------------- #

class TestAddLabel:
    def test_reuses_existing_label_id(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "Important"}]
        }
        client = make_client(service)

        result = client.add_label("m1", "Important")

        assert result == {"message_id": "m1", "label_added": "Important"}
        service.users.return_value.labels.return_value.create.assert_not_called()
        service.users.return_value.messages.return_value.modify.assert_called_once_with(
            userId="me", id="m1", body={"addLabelIds": ["Label_1"]}
        )

    def test_creates_label_when_not_found(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        service.users.return_value.labels.return_value.create.return_value.execute.return_value = {"id": "Label_new"}
        client = make_client(service)

        client.add_label("m1", "NewLabel")

        service.users.return_value.labels.return_value.create.assert_called_once_with(
            userId="me", body={"name": "NewLabel"}
        )
        service.users.return_value.messages.return_value.modify.assert_called_once_with(
            userId="me", id="m1", body={"addLabelIds": ["Label_new"]}
        )

    def test_label_lookup_is_case_insensitive(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "important"}]
        }
        client = make_client(service)
        client.add_label("m1", "IMPORTANT")
        service.users.return_value.labels.return_value.create.assert_not_called()

    def test_http_error_on_modify_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "Important"}]
        }
        service.users.return_value.messages.return_value.modify.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="add_label"):
            client.add_label("m1", "Important")


class TestRemoveLabel:
    def test_removes_existing_label(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "Important"}]
        }
        client = make_client(service)

        result = client.remove_label("m1", "Important")

        assert result == {"message_id": "m1", "label_removed": "Important"}
        service.users.return_value.messages.return_value.modify.assert_called_once_with(
            userId="me", id="m1", body={"removeLabelIds": ["Label_1"]}
        )

    def test_returns_note_when_label_not_found_no_api_call(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        client = make_client(service)

        result = client.remove_label("m1", "Nonexistent")

        assert result == {"message_id": "m1", "label_removed": "Nonexistent", "note": "label not found"}
        service.users.return_value.messages.return_value.modify.assert_not_called()


class TestArchiveMessage:
    def test_removes_inbox_label(self):
        service = MagicMock()
        client = make_client(service)

        result = client.archive_message("m1")

        assert result == {"message_id": "m1", "archived": True}
        service.users.return_value.messages.return_value.modify.assert_called_once_with(
            userId="me", id="m1", body={"removeLabelIds": ["INBOX"]}
        )

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.modify.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="archive_message"):
            client.archive_message("m1")


# ---------------------------------------------------------------------------- #
# Labels: list / create (incl. nested)
# ---------------------------------------------------------------------------- #

class TestListLabels:
    def test_returns_id_name_type(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "Label_1", "name": "Work/Projects", "type": "user"},
            ]
        }
        client = make_client(service)

        result = client.list_labels()

        assert result == [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_1", "name": "Work/Projects", "type": "user"},
        ]

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="list_labels"):
            client.list_labels()


class TestCreateLabel:
    def test_creates_simple_label(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        service.users.return_value.labels.return_value.create.return_value.execute.return_value = {
            "id": "Label_1", "name": "Receipts", "type": "user"
        }
        client = make_client(service)

        result = client.create_label("Receipts")

        assert result == {"id": "Label_1", "name": "Receipts", "type": "user"}
        service.users.return_value.labels.return_value.create.assert_called_once_with(
            userId="me", body={"name": "Receipts"}
        )

    def test_fails_if_exact_label_already_exists(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "Receipts", "type": "user"}]
        }
        client = make_client(service)

        with pytest.raises(GmailClientError, match="already exists"):
            client.create_label("Receipts")
        service.users.return_value.labels.return_value.create.assert_not_called()

    def test_existing_check_is_case_insensitive(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "receipts", "type": "user"}]
        }
        client = make_client(service)

        with pytest.raises(GmailClientError, match="already exists"):
            client.create_label("Receipts")

    def test_nested_name_creates_missing_parent_segment_first(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        service.users.return_value.labels.return_value.create.return_value.execute.side_effect = [
            {"id": "Label_parent", "name": "Work", "type": "user"},
            {"id": "Label_child", "name": "Work/Projects", "type": "user"},
        ]
        client = make_client(service)

        result = client.create_label("Work/Projects")

        assert result == {"id": "Label_child", "name": "Work/Projects", "type": "user"}
        create_calls = service.users.return_value.labels.return_value.create.call_args_list
        assert create_calls[0].kwargs == {"userId": "me", "body": {"name": "Work"}}
        assert create_calls[1].kwargs == {"userId": "me", "body": {"name": "Work/Projects"}}

    def test_nested_name_reuses_existing_parent_segment(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_parent", "name": "Work", "type": "user"}]
        }
        service.users.return_value.labels.return_value.create.return_value.execute.return_value = {
            "id": "Label_child", "name": "Work/Projects", "type": "user"
        }
        client = make_client(service)

        client.create_label("Work/Projects")

        service.users.return_value.labels.return_value.create.assert_called_once_with(
            userId="me", body={"name": "Work/Projects"}
        )

    def test_double_slash_normalizes_before_exists_check(self):
        # Regression: the exists-check and the segment-creation loop must
        # agree on the collapsed name, or "Work//Projects" (a stray double
        # slash) slips past an existing "Work/Projects" label undetected
        # and creates a spurious standalone "Work" label as a side effect.
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
            "labels": [{"id": "Label_1", "name": "Work/Projects", "type": "user"}]
        }
        client = make_client(service)

        with pytest.raises(GmailClientError, match="already exists"):
            client.create_label("Work//Projects")
        service.users.return_value.labels.return_value.create.assert_not_called()

    def test_empty_name_raises_without_api_call(self):
        service = MagicMock()
        client = make_client(service)

        with pytest.raises(GmailClientError, match="non-empty label_name"):
            client.create_label("   ")
        service.users.return_value.labels.return_value.list.assert_not_called()

    def test_http_error_on_create_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        service.users.return_value.labels.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="create_label"):
            client.create_label("Receipts")


# ---------------------------------------------------------------------------- #
# Filters: list / create / update (delete+recreate)
# ---------------------------------------------------------------------------- #

class TestListFilters:
    def test_returns_id_criteria_action(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.list.return_value.execute.return_value = {
            "filter": [
                {"id": "f1", "criteria": {"from": "boss@example.com"}, "action": {"addLabelIds": ["Label_1"]}},
            ]
        }
        client = make_client(service)

        result = client.list_filters()

        assert result == [
            {"id": "f1", "criteria": {"from": "boss@example.com"}, "action": {"addLabelIds": ["Label_1"]}},
        ]

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="list_filters"):
            client.list_filters()


class TestCreateFilter:
    def test_builds_criteria_and_action_and_calls_api(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.create.return_value.execute.return_value = {
            "id": "f1",
            "criteria": {"from": "boss@example.com", "hasAttachment": True},
            "action": {"removeLabelIds": ["INBOX"]},
        }
        client = make_client(service)

        result = client.create_filter(from_address="boss@example.com", has_attachment=True, archive=True)

        assert result["id"] == "f1"
        service.users.return_value.settings.return_value.filters.return_value.create.assert_called_once_with(
            userId="me",
            body={
                "criteria": {"from": "boss@example.com", "hasAttachment": True},
                "action": {"removeLabelIds": ["INBOX"]},
            },
        )

    def test_add_label_names_resolved_via_get_or_create_label(self):
        service = MagicMock()
        service.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        service.users.return_value.labels.return_value.create.return_value.execute.return_value = {"id": "Label_new"}
        service.users.return_value.settings.return_value.filters.return_value.create.return_value.execute.return_value = {
            "id": "f1", "criteria": {"subject": "x"}, "action": {"addLabelIds": ["Label_new"]}
        }
        client = make_client(service)

        client.create_filter(subject="x", add_label_names="NewLabel")

        service.users.return_value.labels.return_value.create.assert_called_once_with(
            userId="me", body={"name": "NewLabel"}
        )
        service.users.return_value.settings.return_value.filters.return_value.create.assert_called_once_with(
            userId="me",
            body={"criteria": {"subject": "x"}, "action": {"addLabelIds": ["Label_new"]}},
        )

    def test_requires_at_least_one_criteria_field(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="at least one criteria field"):
            client.create_filter(archive=True)

    def test_requires_at_least_one_action(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="at least one action"):
            client.create_filter(subject="x")

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="create_filter"):
            client.create_filter(subject="x", archive=True)


class TestUpdateFilter:
    def test_deletes_old_and_creates_new(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.create.return_value.execute.return_value = {
            "id": "f2", "criteria": {"subject": "y"}, "action": {"removeLabelIds": ["INBOX"]}
        }
        client = make_client(service)

        result = client.update_filter("f1", subject="y", archive=True)

        assert result == {
            "old_id": "f1", "id": "f2",
            "criteria": {"subject": "y"}, "action": {"removeLabelIds": ["INBOX"]},
        }
        service.users.return_value.settings.return_value.filters.return_value.delete.assert_called_once_with(
            userId="me", id="f1"
        )
        service.users.return_value.settings.return_value.filters.return_value.create.assert_called_once_with(
            userId="me", body={"criteria": {"subject": "y"}, "action": {"removeLabelIds": ["INBOX"]}}
        )

    def test_requires_filter_id(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="non-empty filter_id"):
            client.update_filter("", subject="y", archive=True)

    def test_validates_criteria_before_deleting_anything(self):
        service = MagicMock()
        client = make_client(service)

        with pytest.raises(GmailClientError, match="at least one criteria field"):
            client.update_filter("f1", archive=True)
        service.users.return_value.settings.return_value.filters.return_value.delete.assert_not_called()

    def test_validates_action_before_deleting_anything(self):
        service = MagicMock()
        client = make_client(service)

        with pytest.raises(GmailClientError, match="at least one action"):
            client.update_filter("f1", subject="y")
        service.users.return_value.settings.return_value.filters.return_value.delete.assert_not_called()

    def test_delete_http_error_becomes_gmail_client_error_and_skips_create(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.delete.return_value.execute.side_effect = http_error(404)
        client = make_client(service)

        with pytest.raises(GmailClientError, match="failed to delete existing filter"):
            client.update_filter("f1", subject="y", archive=True)
        service.users.return_value.settings.return_value.filters.return_value.create.assert_not_called()

    def test_create_http_error_after_delete_reports_original_filter_is_gone(self):
        service = MagicMock()
        service.users.return_value.settings.return_value.filters.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client(service)

        with pytest.raises(GmailClientError, match="original filter is gone"):
            client.update_filter("f1", subject="y", archive=True)
        service.users.return_value.settings.return_value.filters.return_value.delete.assert_called_once_with(
            userId="me", id="f1"
        )


# ---------------------------------------------------------------------------- #
# resolve_attachment_destination: path-traversal sanitization
# ---------------------------------------------------------------------------- #

class TestResolveAttachmentDestination:
    def test_joins_basename_with_destination_dir(self, tmp_path):
        result = resolve_attachment_destination("report.pdf", str(tmp_path))
        assert result == str(tmp_path / "report.pdf")

    def test_strips_directory_traversal_from_filename(self, tmp_path):
        # A crafted filename from the sender's MIME headers must never be
        # able to write outside destination_dir.
        result = resolve_attachment_destination("../../.ssh/authorized_keys", str(tmp_path))
        assert result == str(tmp_path / "authorized_keys")

    def test_strips_absolute_path_prefix_from_filename(self, tmp_path):
        result = resolve_attachment_destination("/etc/passwd", str(tmp_path))
        assert result == str(tmp_path / "passwd")

    def test_empty_filename_falls_back_to_generic_name(self, tmp_path):
        assert resolve_attachment_destination("", str(tmp_path)) == str(tmp_path / "attachment")

    def test_empty_destination_dir_defaults_to_downloads(self, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: "/home/user/Downloads" if p == "~/Downloads" else p)
        assert resolve_attachment_destination("report.pdf", "") == "/home/user/Downloads/report.pdf"

    def test_whitespace_only_destination_dir_defaults_to_downloads(self, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: "/home/user/Downloads" if p == "~/Downloads" else p)
        assert resolve_attachment_destination("report.pdf", "   ") == "/home/user/Downloads/report.pdf"


# ---------------------------------------------------------------------------- #
# download_attachment
# ---------------------------------------------------------------------------- #

class TestDownloadAttachment:
    def test_requires_message_id_and_attachment_id(self):
        client = make_client(MagicMock())
        with pytest.raises(GmailClientError, match="non-empty message_id and attachment_id"):
            client.download_attachment("", "att1", "file.pdf")
        with pytest.raises(GmailClientError, match="non-empty message_id and attachment_id"):
            client.download_attachment("m1", "", "file.pdf")

    def test_downloads_decodes_and_saves_content(self, tmp_path):
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": b64("file contents")
        }
        client = make_client(service)

        result = client.download_attachment("m1", "att1", "report.pdf", str(tmp_path))

        dest = tmp_path / "report.pdf"
        assert dest.read_bytes() == b"file contents"
        assert result == {"path": str(dest), "name": "report.pdf", "size_bytes": len(b"file contents")}
        service.users.return_value.messages.return_value.attachments.return_value.get.assert_called_once_with(
            userId="me", messageId="m1", id="att1"
        )

    def test_sanitizes_filename_before_writing(self, tmp_path):
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": b64("data")
        }
        client = make_client(service)

        result = client.download_attachment("m1", "att1", "../../evil.txt", str(tmp_path))

        assert result == {"path": str(tmp_path / "evil.txt"), "name": "evil.txt", "size_bytes": 4}

    def test_empty_filename_falls_back_to_attachment_id(self, tmp_path):
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": b64("data")
        }
        client = make_client(service)

        result = client.download_attachment("m1", "att1", "", str(tmp_path))

        assert result["name"] == "att1"

    def test_creates_destination_directory_if_missing(self, tmp_path):
        nested = tmp_path / "nested" / "dir"
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": b64("data")
        }
        client = make_client(service)

        client.download_attachment("m1", "att1", "f.txt", str(nested))

        assert (nested / "f.txt").read_bytes() == b"data"

    def test_http_error_becomes_gmail_client_error(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(GmailClientError, match="download_attachment"):
            client.download_attachment("m1", "att1", "f.txt")


# ---------------------------------------------------------------------------- #
# _get_service: must not share one service (and its underlying httplib2
# transport) across threads, since concurrent requests dispatched via
# asyncio.to_thread corrupt a shared connection (SSL: WRONG_VERSION_NUMBER).
# ---------------------------------------------------------------------------- #

class TestServiceIsThreadLocal:
    def test_each_thread_gets_its_own_service_instance(self):
        client = GmailClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.gmail_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()

            services: dict[int, object] = {}

            def worker(idx: int) -> None:
                services[idx] = client._get_service()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len({id(s) for s in services.values()}) == 5

    def test_same_thread_reuses_cached_service(self):
        client = GmailClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.gmail_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()
            assert client._get_service() is client._get_service()
            assert mock_build.call_count == 1


class TestLiveFixtureParsing:
    """Replays a fixture recorded from a real, [QATEST]-tagged seed message
    by scripts/qa_fixture_recorder.py --record gmail -- real API shape, not
    hand-authored, with From/To headers already redacted. Skipped (not
    failed) until that fixture exists; see tests/fixtures/live/README.md
    and docs/testing-policy.md. Re-record via
    that script if this ever starts failing after a genuine Gmail API
    change.
    """

    def test_get_message_fixture_still_parses(self):
        path = LIVE_FIXTURES_DIR / "get_message.json"
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record gmail` locally first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        client = make_client(MagicMock())

        message = client._parse_message(raw)

        assert message.sender and message.date and message.subject
