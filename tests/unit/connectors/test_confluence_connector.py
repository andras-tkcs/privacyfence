"""Unit tests for privacyfence.connectors.confluence.ConfluenceConnector.

Same approach as the other connector tests: ConfluenceClient is mocked
and gate.gated_call is stubbed to capture what's sent into the gate.

One real bug found and fixed while writing these: confluence_get_page and
confluence_get_page_by_title built the "Last modified" preview field from
getattr(page, "last_modified", ""), but ConfluencePage has no
last_modified attribute -- only `updated` -- so that field was always
blank, even though README documents "last modified" as part of the
Cowork preview.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.confluence_client import (
    ConfluenceAttachment,
    ConfluenceClient,
    ConfluenceClientError,
    ConfluencePage,
    ConfluenceSearchResult,
    ConfluenceSpace,
)
from privacyfence.connectors import confluence as confluence_module
from privacyfence.connectors.confluence import ConfluenceConnector
from privacyfence.privacy_filter import init_privacy_filter

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = ConfluenceConnector(client)
    connector.my_email = my_email
    return connector, client


def make_real_client(config: dict | None = None) -> ConfluenceClient:
    """A real ConfluenceClient (real _parse_page_v2 and friends) with only
    the underlying atlassian-python-api object mocked -- same pattern as
    test_confluence_client.py's make_client(). Used by TestFieldCompleteness
    to exercise the real raw-response -> dataclass -> popup-preview path end
    to end, instead of stubbing ConfluencePage directly like every other
    test in this file does.
    """
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = ConfluenceClient(config=base)
    client._client = MagicMock()
    return client


def make_page(**overrides):
    defaults = dict(
        id="p1", title="Runbook", space_key="ENG", space_name="Engineering",
        version=3, author="alice@example.com", created="2026-01-01T00:00:00Z",
        updated="2026-07-01T00:00:00Z", body="<p>Confidential steps here</p>",
    )
    defaults.update(overrides)
    return ConfluencePage(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(confluence_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Confluence tool"):
            await connector.call("confluence_does_not_exist", {})


class TestAutoTools:
    async def test_list_spaces(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_spaces.return_value = [ConfluenceSpace(key="ENG", name="Engineering")]

        result = await connector.call("confluence_list_spaces", {"max_results": 10, "space_type": "global"})

        assert result[0]["key"] == "ENG"
        client.list_spaces.assert_called_once_with(10, "global")
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_list_spaces_default_omits_type_filter(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_spaces.return_value = []

        await connector.call("confluence_list_spaces", {})

        client.list_spaces.assert_called_once_with(50, "")

    async def test_search(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search.return_value = [
            ConfluenceSearchResult(id="s1", title="Runbook", content_type="page", space_key="ENG"),
        ]

        result = await connector.call("confluence_search", {"query": "runbook"})

        assert result[0]["title"] == "Runbook"
        client.search.assert_called_once_with("runbook", 20)

    async def test_cql_search(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.cql_search.return_value = []

        result = await connector.call("confluence_cql_search", {"cql": "space = ENG"})

        assert result == []
        client.cql_search.assert_called_once_with("space = ENG", 20)

    async def test_list_pages(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_pages_in_space.return_value = [make_page()]

        result = await connector.call("confluence_list_pages", {"space_key": "ENG"})

        assert result[0]["id"] == "p1"
        client.list_pages_in_space.assert_called_once_with("ENG", 20)

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_spaces.side_effect = ConfluenceClientError("no access")

        with pytest.raises(RuntimeError, match="no access"):
            await connector.call("confluence_list_spaces", {})


class TestListAttachments:
    """confluence_list_attachments is auto-approved (metadata only, no
    content) -- confluence_download_attachment (below) is the separate,
    approval-gated tool for fetching actual attachment bytes."""

    async def test_attachments_carry_no_content_and_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="diagram.png", media_type="image/png", size=2048, attachment_id="att-x"),
        ]

        result = await connector.call("confluence_list_attachments", {"page_id": "p1"})

        assert result == {
            "page_id": "p1",
            "attachments": [{"name": "diagram.png", "media_type": "image/png", "size": 2048}],
        }
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "confluence_list_attachments"' in entries[0]

    async def test_no_attachments_yields_empty_list(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_attachments.return_value = []

        result = await connector.call("confluence_list_attachments", {"page_id": "p1"})

        assert result == {"page_id": "p1", "attachments": []}

    async def test_honors_attachments_category(self, tmp_path):
        # The default settings.yaml.example ships "attachments: block".
        init_audit_logger(str(tmp_path))
        init_privacy_filter({"confluence_privacy": {"categories": {"attachments": "block"}}})
        connector, client = make_connector()
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="secret.pdf", media_type="application/pdf", size=10),
        ]

        result = await connector.call("confluence_list_attachments", {"page_id": "p1"})

        assert result == {"page_id": "p1", "attachments": []}


class TestExcerptPrivacyFilter:
    """confluence_search/confluence_cql_search are auto-approved, but
    excerpt is a genuine content excerpt (not structural metadata like
    title/space/id) -- the one auto search tool across every connector that
    returns actual page content pre-approval. Filtered through
    confluence_privacy's "search_excerpt" category (see privacy_filter.py)."""

    async def test_search_excerpt_blocked(self, tmp_path):
        init_audit_logger(str(tmp_path))
        init_privacy_filter({"confluence_privacy": {"categories": {"search_excerpt": "block"}}})
        connector, client = make_connector()
        client.search.return_value = [
            ConfluenceSearchResult(
                id="s1", title="Runbook", content_type="page", space_key="ENG",
                excerpt="the confidential matching snippet",
            ),
        ]

        result = await connector.call("confluence_search", {"query": "runbook"})

        assert result[0]["excerpt"] == "[BLOCKED BY PRIVACY FILTER]"
        # title/space/id have no category of their own -- untouched
        assert result[0]["title"] == "Runbook"

    async def test_cql_search_excerpt_blocked(self, tmp_path):
        init_audit_logger(str(tmp_path))
        init_privacy_filter({"confluence_privacy": {"categories": {"search_excerpt": "block"}}})
        connector, client = make_connector()
        client.cql_search.return_value = [
            ConfluenceSearchResult(
                id="s1", title="Runbook", content_type="page", space_key="ENG",
                excerpt="confidential",
            ),
        ]

        result = await connector.call("confluence_cql_search", {"cql": "space = ENG"})

        assert result[0]["excerpt"] == "[BLOCKED BY PRIVACY FILTER]"

    async def test_excerpt_allowed_by_default(self, tmp_path):
        # No init_privacy_filter call -- fails open to "allow".
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search.return_value = [
            ConfluenceSearchResult(
                id="s1", title="Runbook", content_type="page", space_key="ENG",
                excerpt="a normal excerpt",
            ),
        ]

        result = await connector.call("confluence_search", {"query": "runbook"})

        assert result[0]["excerpt"] == "a normal excerpt"


class TestGetPage:
    async def test_preview_shows_real_last_modified_not_blank(self, gated_call_spy):
        # Regression test for the last_modified bug: preview must reflect
        # page.updated, not silently be blank.
        connector, client = make_connector()
        client.get_page.return_value = make_page(updated="2026-07-01T00:00:00Z")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        assert kwargs["new_info"]["Last modified"] == "2026-07-01T00:00:00Z"

    async def test_last_modified_placeholder_when_missing(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(updated="")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        assert gated_call_spy[0]["new_info"]["Last modified"] == "(unknown)"

    async def test_preview_excludes_body_details_include_it(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(body="<p>Confidential steps here</p>")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        # Title/Space are known via either confluence_list_pages or
        # confluence_search; Author/Last modified only via list_pages (not
        # search), so they're new_info here -- see connectors/confluence.py.
        assert kwargs["preview"] == {"Title": "Runbook", "Space": "ENG"}
        assert "Confidential steps here" not in str(kwargs["preview"])
        assert "Confidential steps here" in kwargs["details_text"]
        assert kwargs["new_info"] == {
            "Author": "alice@example.com", "Last modified": "2026-07-01T00:00:00Z",
            "Page body": "Full page content",
        }
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"page_id": "p1"}
        assert kwargs["raw_data"] is kwargs["filtered_data"]

    async def test_storage_format_markup_is_converted_to_markdown(self, gated_call_spy):
        # Regression test for issue #112: confluence_get_page's details_text/
        # pii_scan_text were fed raw Confluence storage-format XHTML (with
        # unstripped <ac:*> macro tags) straight into the approval popup and
        # the PII scanner. body must be run through html_to_markdown() first
        # so the reviewer sees readable, rendered content (not raw tag soup)
        # and the scanner sees the actual text rather than XML markup. Also
        # covers the "markdown" preview_blocks entry that renders that
        # content richly (see approval_window_html.py).
        connector, client = make_connector()
        client.get_page.return_value = make_page(
            body='<p>Confidential steps here</p>'
            '<ac:structured-macro ac:name="info">'
            '<ac:rich-text-body><p>Rotate the secret</p></ac:rich-text-body>'
            "</ac:structured-macro>"
        )

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        assert "<p>" not in kwargs["details_text"]
        assert "<ac:structured-macro" not in kwargs["details_text"]
        assert "Confidential steps here" in kwargs["details_text"]
        assert "Rotate the secret" in kwargs["details_text"]
        assert kwargs["pii_scan_text"] == kwargs["details_text"]
        assert kwargs["preview_blocks"] == [{"type": "markdown", "text": kwargs["details_text"]}]

    async def test_pii_scan_text_is_body_only_not_author(self, gated_call_spy):
        # author defaults to an email address, present on every page
        # regardless of content -- the PII scan must not see it.
        connector, client = make_connector()
        client.get_page.return_value = make_page(body="nothing sensitive here")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "nothing sensitive here"
        assert kwargs["new_info"]["Author"] == "alice@example.com"  # still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_page.side_effect = ConfluenceClientError("page deleted")

        with pytest.raises(RuntimeError, match="page deleted"):
            await connector.call("confluence_get_page", {"page_id": "p1"})


class TestGetPageByTitle:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page_by_title.return_value = make_page()

        await connector.call("confluence_get_page_by_title", {"space_key": "ENG", "title": "Runbook"})

        kwargs = gated_call_spy[0]
        assert kwargs["new_info"]["Last modified"] == "2026-07-01T00:00:00Z"
        assert kwargs["args"] == {"space_key": "ENG", "title": "Runbook"}
        client.get_page_by_title.assert_called_once_with("ENG", "Runbook")

    async def test_sender_falls_back_to_space_key_when_author_unknown(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page_by_title.return_value = make_page(author="")

        await connector.call("confluence_get_page_by_title", {"space_key": "ENG", "title": "Runbook"})

        assert gated_call_spy[0]["sender"] == "ENG"

    async def test_storage_format_markup_is_converted_to_markdown(self, gated_call_spy):
        # Same regression as TestGetPage's test of the same name (issue #112).
        connector, client = make_connector()
        client.get_page_by_title.return_value = make_page(
            body='<ul><li>Confidential item</li></ul>'
        )

        await connector.call("confluence_get_page_by_title", {"space_key": "ENG", "title": "Runbook"})

        kwargs = gated_call_spy[0]
        assert "<ul>" not in kwargs["details_text"]
        assert "- Confidential item" in kwargs["details_text"]
        assert kwargs["preview_blocks"] == [{"type": "markdown", "text": kwargs["details_text"]}]


class TestDownloadAttachment:
    def _attachment(self, **overrides):
        defaults = dict(
            name="report.pdf", media_type="application/pdf", size=1024, attachment_id="att-1",
        )
        defaults.update(overrides)
        return ConfluenceAttachment(**defaults)

    async def test_preview_and_gate(self, gated_call_spy):
        # application/octet-stream -- a type is_prefetch_worthy() doesn't
        # recognize -- keeps this test's focus on the preview/gate fields
        # themselves; see TestPiiScanWiring below for PDF/text prefetch behavior.
        connector, client = make_connector()
        client.get_page.return_value = make_page(title="Runbook", space_key="ENG", author="alice@example.com")
        client.list_attachments.return_value = [self._attachment(media_type="application/octet-stream")]
        dest_path = os.path.join("/tmp", "report.pdf")
        client.download_attachment.return_value = {"path": dest_path, "name": "report.pdf", "size_bytes": 1024}

        result = await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "report.pdf", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Title"] == "Runbook"
        assert kwargs["preview"]["Space"] == "ENG"
        assert kwargs["preview"]["Attachment"] == "report.pdf"
        assert kwargs["preview"]["Type"] == "application/octet-stream"
        assert kwargs["preview"]["Size"] == "1,024 bytes"
        # Will save to / no-content-returned are new-on-approval facts, not
        # already-known metadata -- see connectors/confluence.py's comment.
        # resolve_attachment_destination() builds this with os.path.join, so
        # the separator is the host OS's native one (backslash on Windows) --
        # match that here rather than hardcoding a POSIX path.
        assert kwargs["new_info"]["Will save to"] == dest_path
        assert "None" in kwargs["new_info"]["Content returned to Claude"]
        assert kwargs["details_text"] == "The attachment above will be downloaded to the destination shown."
        assert kwargs["filtered_data"] is None
        assert kwargs["args"] == {"page_id": "p1", "attachment_name": "report.pdf"}
        # raw_data is the asdict()'d page (a dict, not a ConfluencePage
        # instance) -- same shape confluence_get_page/get_page_by_title
        # already pass, so the i_am_author/approved_space_keys Always-allow
        # candidates (auto_accept._confluence_read_page_candidates) work
        # identically for this tool.
        assert kwargs["raw_data"]["space_key"] == "ENG"
        assert kwargs["raw_data"]["author"] == "alice@example.com"
        assert kwargs["sender"] == "alice@example.com"
        assert result == {"path": "/tmp/report.pdf", "name": "report.pdf", "size_bytes": 1024}
        assert kwargs["pii_scan_text"] == ""  # unrecognized type, nothing prefetched to scan
        assert kwargs["preview_blocks"] == [{"type": "text", "text": kwargs["details_text"]}]
        client.download_attachment.assert_called_once_with("p1", "att-1", "report.pdf", "/tmp")

    async def test_unknown_attachment_name_raises_without_gating(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [self._attachment()]

        with pytest.raises(RuntimeError, match="No attachment named 'nope.pdf' on page p1"):
            await connector.call(
                "confluence_download_attachment", {"page_id": "p1", "attachment_name": "nope.pdf"}
            )
        assert gated_call_spy == []

    async def test_client_error_after_approval_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [self._attachment(media_type="application/octet-stream")]
        client.download_attachment.side_effect = ConfluenceClientError("disk full")

        with pytest.raises(RuntimeError, match="disk full"):
            await connector.call(
                "confluence_download_attachment",
                {"page_id": "p1", "attachment_name": "report.pdf", "destination_dir": "/tmp"},
            )

    async def test_image_attachment_under_size_cap_gets_a_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            self._attachment(name="photo.png", media_type="image/png", size=1024, attachment_id="att-2"),
        ]
        client.fetch_attachment_bytes.return_value = b"\x89PNGfakebytes"
        client.save_attachment_bytes.return_value = {
            "path": "/tmp/photo.png", "name": "photo.png", "size_bytes": 13,
        }

        result = await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "photo.png", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview_bytes"] == b"\x89PNGfakebytes"
        assert kwargs["preview_mime_type"] == "image/png"
        client.fetch_attachment_bytes.assert_called_once_with("p1", "att-2")
        # Already fetched for the preview -- must reuse those bytes, not
        # fetch the same attachment from Confluence a second time.
        client.save_attachment_bytes.assert_called_once_with(b"\x89PNGfakebytes", "photo.png", "/tmp")
        client.download_attachment.assert_not_called()
        assert result == {"path": "/tmp/photo.png", "name": "photo.png", "size_bytes": 13}

    async def test_image_attachment_over_size_cap_gets_no_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            self._attachment(
                name="huge.png", media_type="image/png",
                size=confluence_module._ATTACHMENT_PREFETCH_MAX_BYTES + 1,
            ),
        ]
        client.download_attachment.return_value = {
            "path": "/tmp/huge.png", "name": "huge.png", "size_bytes": 1,
        }

        await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "huge.png", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview_bytes"] == b""
        assert kwargs["preview_mime_type"] == ""
        client.fetch_attachment_bytes.assert_not_called()
        client.download_attachment.assert_called_once()

    async def test_unrecognized_binary_attachment_gets_no_prefetch_at_all(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [self._attachment(media_type="application/octet-stream")]
        client.download_attachment.return_value = {
            "path": "/tmp/report.bin", "name": "report.bin", "size_bytes": 1024,
        }

        await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "report.pdf", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview_bytes"] == b""
        assert kwargs["preview_mime_type"] == ""
        assert kwargs["pii_scan_text"] == ""
        client.fetch_attachment_bytes.assert_not_called()

    async def test_preview_fetch_failure_degrades_gracefully(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            self._attachment(name="photo.png", media_type="image/png", size=1024, attachment_id="att-2"),
        ]
        client.fetch_attachment_bytes.side_effect = ConfluenceClientError("expired token")
        client.download_attachment.return_value = {
            "path": "/tmp/photo.png", "name": "photo.png", "size_bytes": 1024,
        }

        result = await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "photo.png", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview_bytes"] == b""
        assert kwargs["preview_mime_type"] == ""
        # Falls back to the original single-call path since no preview bytes
        # were actually obtained.
        client.download_attachment.assert_called_once_with("p1", "att-2", "photo.png", "/tmp")
        assert result == {"path": "/tmp/photo.png", "name": "photo.png", "size_bytes": 1024}


class TestAttachmentPiiScanWiring:
    """confluence_download_attachment fetches prefetch-worthy attachments
    (text/PDF/DOCX/PPTX, in addition to images) and extracts text via
    text_extraction.extract_text() for the scan -- same wiring as
    gmail_download_attachment's own TestPiiScanWiring."""

    async def test_text_attachment_populates_pii_scan_text(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="notes.txt", media_type="text/plain", size=1024, attachment_id="att-3"),
        ]
        client.fetch_attachment_bytes.return_value = b"Please wire the deposit to DE89370400440532013000."
        client.save_attachment_bytes.return_value = {
            "path": "/tmp/notes.txt", "name": "notes.txt", "size_bytes": 1024,
        }

        await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "notes.txt", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "Please wire the deposit to DE89370400440532013000."
        # Replaces the old QuickLook-thumbnail fallback: the attachment's
        # own extracted content becomes a rich "markdown" preview_blocks
        # entry instead -- see connectors/confluence.py's _download_attachment.
        assert kwargs["preview_blocks"] == [
            {"type": "text", "text": kwargs["details_text"]},
            {"type": "markdown", "text": "Please wire the deposit to DE89370400440532013000."},
        ]

    async def test_reuses_fetched_bytes_for_the_save_even_without_a_preview(self, gated_call_spy):
        # A PDF isn't an image -- no preview -- but the bytes fetched for the
        # PII scan must still be reused for the actual save, not re-fetched.
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="report.pdf", media_type="application/pdf", size=1024, attachment_id="att-4"),
        ]
        client.fetch_attachment_bytes.return_value = b"%PDF-1.4 fake"
        client.save_attachment_bytes.return_value = {
            "path": "/tmp/report.pdf", "name": "report.pdf", "size_bytes": 1024,
        }

        result = await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "report.pdf", "destination_dir": "/tmp"},
        )

        client.fetch_attachment_bytes.assert_called_once_with("p1", "att-4")
        client.save_attachment_bytes.assert_called_once_with(b"%PDF-1.4 fake", "report.pdf", "/tmp")
        client.download_attachment.assert_not_called()
        assert result == {"path": "/tmp/report.pdf", "name": "report.pdf", "size_bytes": 1024}

    async def test_image_attachment_has_no_scannable_text(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="photo.png", media_type="image/png", size=1024, attachment_id="att-photo"),
        ]
        client.fetch_attachment_bytes.return_value = b"\x89PNGfakebytes"
        client.save_attachment_bytes.return_value = {
            "path": "/tmp/photo.png", "name": "photo.png", "size_bytes": 1024,
        }

        await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "photo.png", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == ""

    async def test_prefetch_failure_degrades_scan_text_to_empty(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="report.pdf", media_type="application/pdf", size=1024, attachment_id="att-report"),
        ]
        client.fetch_attachment_bytes.side_effect = ConfluenceClientError("expired token")
        client.download_attachment.return_value = {
            "path": "/tmp/report.pdf", "name": "report.pdf", "size_bytes": 1024,
        }

        await connector.call(
            "confluence_download_attachment",
            {"page_id": "p1", "attachment_name": "report.pdf", "destination_dir": "/tmp"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == ""


class TestCreatePage:
    async def test_preview_omits_parent_id_when_absent(self, gated_call_spy):
        connector, client = make_connector()
        client.create_page.return_value = make_page(id="new1")

        await connector.call("confluence_create_page", {
            "space_key": "ENG", "title": "New Page", "body": "<p>content</p>",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Space": "ENG", "Title": "New Page"}
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "<p>content</p>"

    async def test_preview_includes_parent_id_when_present(self, gated_call_spy):
        connector, client = make_connector()
        client.create_page.return_value = make_page(id="new1")

        await connector.call("confluence_create_page", {
            "space_key": "ENG", "title": "New Page", "body": "<p>x</p>", "parent_id": "p0",
        })

        assert gated_call_spy[0]["preview"]["Parent page ID"] == "p0"
        assert gated_call_spy[0]["args"] == {"space_key": "ENG", "title": "New Page", "parent_id": "p0"}

    async def test_result_is_serialized_page(self, gated_call_spy):
        connector, client = make_connector()
        client.create_page.return_value = make_page(id="new1", title="New Page")

        result = await connector.call("confluence_create_page", {
            "space_key": "ENG", "title": "New Page", "body": "<p>x</p>",
        })

        assert result["id"] == "new1"
        client.create_page.assert_called_once_with("ENG", "New Page", "<p>x</p>", "")


class TestUpdatePage:
    async def test_preview_shows_title_diff_when_changed(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(title="Old Title", space_key="ENG")
        client.update_page.return_value = make_page(title="New Title")

        await connector.call("confluence_update_page", {"page_id": "p1", "title": "New Title", "body": "<p>x</p>"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Title"] == "Old Title → New Title"
        assert kwargs["gate"] == "popup"

    async def test_preview_shows_plain_title_when_unchanged(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(title="Same Title")
        client.update_page.return_value = make_page(title="Same Title")

        await connector.call("confluence_update_page", {"page_id": "p1", "title": "Same Title", "body": "<p>x</p>"})

        assert gated_call_spy[0]["preview"]["Title"] == "Same Title"

    async def test_result_is_serialized_page(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.update_page.return_value = make_page(title="Updated")

        result = await connector.call("confluence_update_page", {"page_id": "p1", "title": "Updated", "body": "<p>x</p>"})

        assert result["title"] == "Updated"
        client.update_page.assert_called_once_with("p1", "Updated", "<p>x</p>")

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_page.side_effect = ConfluenceClientError("locked")

        with pytest.raises(RuntimeError, match="locked"):
            await connector.call("confluence_update_page", {"page_id": "p1", "title": "x", "body": "y"})


class TestFieldCompleteness:
    """End to end: a fully-populated raw v2 API response -> the real
    ConfluenceClient._parse_page_v2 -> the real connector's popup preview
    -- not a hand-built ConfluencePage, unlike every other test in this
    file. This is the shape of check that would have caught the
    last_modified bug (see module docstring) without already knowing it
    existed: assert_no_placeholder_fields fails loudly the moment any
    preview field silently degrades to a fallback.
    """

    async def test_get_page_preview_has_no_placeholder_fields(self, gated_call_spy):
        client = make_real_client()
        # First _client.get() call is get_page's own fetch; the second is
        # _resolve_space_key's follow-up lookup for the human-readable key
        # (v2 pages only carry a numeric spaceId).
        client._client.get.side_effect = [
            {
                "id": "123", "title": "My Page", "spaceId": "999",
                "version": {"number": 3, "createdAt": "2026-07-01T00:00:00Z"},
                "authorId": "acc-1", "createdAt": "2026-01-01T00:00:00Z",
                "_links": {"webui": "/spaces/ENG/pages/123"},
                "body": {"storage": {"value": "<p>content</p>"}},
            },
            {"key": "ENG"},
        ]

        connector = ConfluenceConnector(client)
        connector.my_email = "me@example.com"
        await connector.call("confluence_get_page", {"page_id": "123"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # get_page/get_page_by_title/create_page/update_page results are
        # asdict()'d unconditionally -- need real ConfluencePage instances,
        # not a bare MagicMock.
        client.get_page.return_value = make_page()
        client.get_page_by_title.return_value = make_page()
        client.create_page.return_value = make_page()
        client.update_page.return_value = make_page()
        # confluence_download_attachment looks up the attachment by name on
        # the fetched page's attachment list before ever reaching the gate,
        # so the generic "stub" arg needs a matching attachment on the
        # mocked client -- same reasoning as gmail_download_attachment's
        # equivalent fixup in test_gmail_connector.py.
        client.list_attachments.return_value = [
            ConfluenceAttachment(name="stub", media_type="application/octet-stream", size=1, attachment_id="att-x"),
        ]

        await assert_all_tools_leave_an_audit_trail(connector, confluence_module, monkeypatch, tmp_path)
