"""Unit tests for privacyfence.connectors.gmail.GmailConnector.

The underlying GmailClient (real network calls) is replaced with a
MagicMock; privacyfence.gate.gated_call is stubbed to capture exactly what
each tool sends into the gate (preview/details/raw_data/args/gate) without
spawning a real approval popup. Two things matter most here:

1. Data minimization: the "preview" dict shown pre-approval must never
   contain full body/content, only metadata -- full content only reaches
   details_text (shown only after "Show Details").
2. Auto-accept wiring: gated_call's `args` must carry exactly what the
   to_is_myself/approved_recipient_domain rules need, including the
   reply-all recipient expansion (a prior real bug: reply-all only checked
   the original sender, letting an external Cc slip an auto-accept rule
   scoped to a trusted domain).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import gmail as gmail_module
from privacyfence.connectors.gmail import GmailConnector
from privacyfence.gmail_client import Attachment, GmailClientError, GmailMessage, GmailThread

from ...helpers import assert_all_tools_leave_an_audit_trail


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = GmailConnector(client)
    connector.my_email = my_email
    return connector, client


@pytest.fixture
def gated_call_spy(monkeypatch):
    """Stub gated_call to record its kwargs and act as if the user approved."""
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(gmail_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Gmail tool"):
            await connector.call("gmail_does_not_exist", {})


class TestAutoTools:
    async def test_list_messages_auto_accepts_without_gate(self, monkeypatch, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_messages.return_value = [{"id": "1"}, {"id": "2"}]

        result = await connector.call("gmail_list_messages", {"query": "from:alice", "max_results": 5})

        assert result == [{"id": "1"}, {"id": "2"}]
        client.list_messages.assert_called_once_with("from:alice", 5)

        week_file = tmp_path / f"{current_week()}.jsonl"
        entries = week_file.read_text(encoding="utf-8").splitlines()
        assert len(entries) == 1
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"auto_accept_rule": "auto"' in entries[0]

    async def test_list_threads_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_threads.return_value = [{"id": "t1"}]

        result = await connector.call("gmail_list_threads", {"query": "q"})

        assert result == [{"id": "t1"}]


class TestGetMessagePreviewMinimization:
    async def test_preview_contains_only_metadata_no_body(self, gated_call_spy):
        connector, client = make_connector()
        message = GmailMessage(
            id="m1", thread_id="t1", subject="Confidential Q3 numbers",
            sender="alice@example.com", recipients=["me@example.com"],
            date="Mon, 01 Jul 2026 10:00:00 +0000",
            body_text="Secret body content that must not appear in the preview.",
        )
        client.get_message.return_value = message

        await connector.call("gmail_get_message", {"message_id": "m1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "From": "alice@example.com",
            "To": "me@example.com",
            "Date": "Mon, 01 Jul 2026 10:00:00 +0000",
            "Subject": "Confidential Q3 numbers",
        }
        assert "Secret body content" not in str(kwargs["preview"])
        assert "Secret body content" in kwargs["details_text"]  # full content still reachable via details
        assert kwargs["gate"] == "review"
        assert kwargs["raw_data"] is message
        assert kwargs["args"] == {"message_id": "m1"}
        assert kwargs["my_email"] == "me@example.com"

    async def test_filtered_data_returned_on_approval(self, gated_call_spy):
        connector, client = make_connector()
        message = GmailMessage(id="m1", thread_id="t1", subject="s", sender="a@b.com")
        client.get_message.return_value = message

        result = await connector.call("gmail_get_message", {"message_id": "m1"})

        assert result["subject"] == "s"
        assert result["id"] == "m1"


class TestGetThread:
    async def test_preview_aggregates_participants_without_bodies(self, gated_call_spy):
        connector, client = make_connector()
        m1 = GmailMessage(
            id="m1", thread_id="t1", subject="Re: budget", sender="alice@example.com",
            recipients=["bob@example.com"], date="d1", body_text="body one secret",
        )
        m2 = GmailMessage(
            id="m2", thread_id="t1", subject="Re: budget", sender="bob@example.com",
            recipients=["alice@example.com"], date="d2", body_text="body two secret",
        )
        thread = GmailThread(id="t1", subject="Re: budget", messages=[m1, m2])
        client.get_thread.return_value = thread

        await connector.call("gmail_get_thread", {"thread_id": "t1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Subject"] == "Re: budget"
        assert kwargs["preview"]["Messages"] == "2"
        assert set(kwargs["preview"]["Participants"].split(", ")) == {"alice@example.com", "bob@example.com"}
        assert "secret" not in str(kwargs["preview"])
        assert "body one secret" in kwargs["details_text"]
        assert "body two secret" in kwargs["details_text"]
        assert kwargs["gate"] == "review"
        assert kwargs["raw_data"] is thread


class TestListMessageAttachments:
    async def test_attachments_carry_no_content(self, gated_call_spy):
        connector, client = make_connector()
        message = GmailMessage(
            id="m1", thread_id="t1", subject="s", sender="a@b.com",
            attachments=[Attachment(name="report.pdf", mime_type="application/pdf", size=1024)],
        )
        client.get_message.return_value = message

        result = await connector.call("gmail_list_message_attachments", {"message_id": "m1"})

        assert result == {
            "message_id": "m1",
            "attachments": [{"name": "report.pdf", "mime_type": "application/pdf", "size": 1024}],
        }
        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"


class TestWriteToolsGateAndPreview:
    async def test_create_draft_preview_excludes_body(self, gated_call_spy):
        connector, client = make_connector()
        client.create_draft.return_value = {"draft_id": "d1"}

        await connector.call(
            "gmail_create_draft",
            {"to": "alice@example.com", "subject": "Hi", "body": "Secret plan details", "cc": "bob@example.com"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert "Secret plan details" not in str(kwargs["preview"])
        assert kwargs["details_text"] == "Secret plan details"
        assert kwargs["preview"] == {"To": "alice@example.com", "Cc": "bob@example.com", "Subject": "Hi"}
        client.create_draft.assert_called_once_with("alice@example.com", "Hi", "Secret plan details", "bob@example.com", "")

    async def test_reply_draft_args_to_is_original_sender_only(self, gated_call_spy):
        connector, client = make_connector()
        client.get_message.return_value = GmailMessage(
            id="m1", thread_id="t1", subject="Re: hi", sender="alice@example.com",
        )
        client.create_reply_draft.return_value = {"draft_id": "d2"}

        await connector.call("gmail_reply_draft", {"message_id": "m1", "body": "ok"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["args"] == {"message_id": "m1", "to": "alice@example.com"}

    async def test_reply_all_draft_expands_recipients_excluding_self(self, gated_call_spy):
        # Regression coverage for the reply-all auto-accept fix: the gate
        # must see every recipient the reply will actually reach (sender +
        # original To + extra Cc), minus the authenticated user, so a rule
        # scoped to a trusted domain can't be satisfied by the sender alone
        # while an external participant slips through unauthorized.
        connector, client = make_connector(my_email="me@example.com")
        client.get_message.return_value = GmailMessage(
            id="m1", thread_id="t1", subject="Re: hi", sender="alice@example.com",
            recipients=["me@example.com", "bob@example.com"],
        )
        client.create_reply_draft.return_value = {"draft_id": "d3"}

        await connector.call(
            "gmail_reply_all_draft", {"message_id": "m1", "body": "ok", "cc": "eve@example.com"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert set(kwargs["args"]["to"]) == {"alice@example.com", "bob@example.com", "eve@example.com"}
        assert "me@example.com" not in kwargs["args"]["to"]
        assert "Also to" in kwargs["preview"]

    async def test_add_label_and_remove_label_gate_popup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_message.return_value = GmailMessage(id="m1", thread_id="t1", subject="s", sender="a@b.com")
        client.add_label.return_value = None
        client.remove_label.return_value = None

        await connector.call("gmail_add_label", {"message_id": "m1", "label_name": "Important"})
        await connector.call("gmail_remove_label", {"message_id": "m1", "label_name": "Important"})

        assert gated_call_spy[0]["gate"] == "popup"
        assert gated_call_spy[0]["args"] == {"message_id": "m1", "label_name": "Important"}
        assert gated_call_spy[1]["args"] == {"message_id": "m1", "label_name": "Important"}

    async def test_archive_message_gate_popup_and_reassuring_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_message.return_value = GmailMessage(id="m1", thread_id="t1", subject="s", sender="a@b.com")
        client.archive_message.return_value = None

        await connector.call("gmail_archive_message", {"message_id": "m1"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert "not deleted" in kwargs["details_text"]


class TestFetchErrorMapping:
    async def test_gmail_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_messages.side_effect = GmailClientError("token expired")

        with pytest.raises(RuntimeError, match="token expired"):
            await connector.call("gmail_list_messages", {"query": "q"})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        await assert_all_tools_leave_an_audit_trail(connector, gmail_module, monkeypatch, tmp_path)
