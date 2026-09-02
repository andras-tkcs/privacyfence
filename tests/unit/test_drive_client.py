"""Tests for DriveClient's parsing/normalization logic: file metadata
normalization, content download/truncation, the Markdown->Google-Docs-API
converter, and the write/upload validation branches. As with
test_gmail_client.py, these call real DriveClient methods against a
MagicMock stand-in for the googleapiclient service object so the actual
normalization/conversion code runs -- the connector-layer tests mock
DriveClient itself and never touch this file.
"""
from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from privacyfence import drive_client as drive_client_module
from privacyfence.drive_client import (
    DriveClient,
    DriveClientError,
    DriveFile,
    InlineRun,
    _col_letters_to_index,
    _docs_content_elements_to_markdown,
    _docs_list_nesting_is_ordered,
    _docs_paragraph_is_divider,
    _docs_plain_text_with_index_map,
    _docs_run_color_notes,
    _docs_structure_color_sidecar,
    _docs_structure_to_markdown,
    _docs_table_column_alignment,
    _docs_table_to_markdown,
    _docs_text_run_to_markdown,
    _extract_tables,
    _find_text_matches,
    _hex_to_rgb_dict,
    _markdown_to_docs_requests,
    _offset_to_docs_index,
    _parse_a1_range,
    _parse_inline_runs,
    _rgb_dict_to_hex,
    _table_cell_start_indices,
    resolve_download_destination,
    resolve_download_name,
)
from googleapiclient.errors import HttpError

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "drive"


def make_client(service: MagicMock) -> DriveClient:
    client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    return client


def make_client_with_sheets(sheets_service: MagicMock) -> DriveClient:
    client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.sheets_service = sheets_service
    return client


def make_client_with_docs(docs_service: MagicMock) -> DriveClient:
    client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.docs_service = docs_service
    return client


def make_client_with_drive_and_docs(service: MagicMock, docs_service: MagicMock) -> DriveClient:
    """get_file_content's Google-Doc branch needs both: the Drive service
    for get_file_metadata (name/mimeType), the Docs service for the
    structured content fetch itself."""
    client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    client._local.docs_service = docs_service
    return client


def doc_run(text: str, **style: object) -> dict:
    """One Docs API textRun, optionally styled -- e.g. doc_run("x", bold=True)
    or doc_run("x", link={"url": "http://x.com"})."""
    run: dict = {"content": text}
    if style:
        run["textStyle"] = style
    return run


def doc_para(runs: list[dict], heading: str = "", alignment: str = "") -> dict:
    """One Docs API structural element wrapping a paragraph of doc_run()s,
    e.g. doc_para([doc_run("bold", bold=True), doc_run("\\n")], heading="HEADING_1")."""
    paragraph: dict = {"elements": [{"textRun": r} for r in runs]}
    if heading:
        paragraph["paragraphStyle"] = {"namedStyleType": heading}
    if alignment:
        paragraph.setdefault("paragraphStyle", {})["alignment"] = alignment
    return {"paragraph": paragraph}


def doc_horizontal_rule() -> dict:
    """One Docs API structural element for a horizontal-rule divider --
    a paragraph whose sole element is a horizontalRule, no textRun."""
    return {"paragraph": {"elements": [{"horizontalRule": {}}]}}


def doc_list_para(runs: list[dict], list_id: str, nesting_level: int = 0) -> dict:
    """One Docs API structural element for a list-item paragraph, e.g.
    doc_list_para([doc_run("item\\n")], "list1", nesting_level=1)."""
    return {"paragraph": {
        "elements": [{"textRun": r} for r in runs],
        "bullet": {"listId": list_id, "nestingLevel": nesting_level},
    }}


def doc_bullet_list_map(list_id: str = "list1", levels: int = 1) -> dict:
    """A document's top-level `lists` map entry for an unordered list --
    each level's NestingLevel carries a glyphSymbol, no glyphType."""
    return {list_id: {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}] * levels}}}


def doc_numbered_list_map(list_id: str = "list1", levels: int = 1) -> dict:
    """A document's top-level `lists` map entry for a numbered list --
    each level's NestingLevel carries an ordered glyphType."""
    return {list_id: {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}] * levels}}}


def make_doc(*paragraphs: str) -> dict:
    """Build a minimal Docs API document body from plain paragraph strings,
    each paragraph becoming one textRun (no bold/italic/etc structure) with
    correctly contiguous Docs indices, mirroring what documents.get() returns."""
    content = []
    index = 1
    for text in paragraphs:
        run_text = text + "\n"
        start = index
        end = start + len(run_text)
        content.append({
            "startIndex": start,
            "endIndex": end,
            "paragraph": {
                "elements": [
                    {"startIndex": start, "endIndex": end, "textRun": {"content": run_text}}
                ]
            },
        })
        index = end
    return {"body": {"content": content}}


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


def fake_downloader_class(chunks: list[bytes]):
    """Stand-in for googleapiclient.http.MediaIoBaseDownload."""
    class _FakeDownloader:
        def __init__(self, fd, request, chunksize=104857600):
            self._fd = fd
            self._remaining = list(chunks)

        def next_chunk(self):
            if self._remaining:
                self._fd.write(self._remaining.pop(0))
            return (None, not self._remaining)

    return _FakeDownloader


# ---------------------------------------------------------------------------- #
# Pure helpers: _clamp_max_results, _parse_file
# ---------------------------------------------------------------------------- #

class TestClampMaxResults:
    @pytest.mark.parametrize("value,expected", [
        (20, 20), (1, 1), (1000, 1000), (0, 1), (-5, 1), (5000, 1000),
        ("50", 50), ("nope", 20), (None, 20),
    ])
    def test_clamps_into_1_to_1000(self, value, expected):
        assert DriveClient._clamp_max_results(value) == expected


class TestParseFile:
    def test_full_metadata_normalized(self):
        raw = {
            "id": "f1", "name": "doc.txt", "mimeType": "text/plain", "size": "1234",
            "createdTime": "c", "modifiedTime": "m",
            "owners": [{"emailAddress": "a@x.com"}, {"emailAddress": "b@x.com"}],
            "shared": True, "webViewLink": "https://x", "parents": ["p1", "p2"],
            "driveId": "d1", "thumbnailLink": "https://signed.example/thumb",
        }
        f = DriveClient._parse_file(raw)
        assert f == DriveFile(
            id="f1", name="doc.txt", mime_type="text/plain", size=1234,
            created_time="c", modified_time="m", owners=["a@x.com", "b@x.com"],
            shared=True, web_view_link="https://x", parent_ids=["p1", "p2"], drive_id="d1",
            thumbnail_link="https://signed.example/thumb",
        )

    def test_missing_fields_default_sensibly(self):
        f = DriveClient._parse_file({})
        assert f == DriveFile(id="", name="", mime_type="", size=0)
        assert f.short_summary() == "(unnamed) ()"
        assert f.thumbnail_link == ""

    def test_owners_without_email_address_are_dropped(self):
        f = DriveClient._parse_file({"owners": [{"emailAddress": "a@x.com"}, {}]})
        assert f.owners == ["a@x.com"]

    def test_non_numeric_size_defaults_to_zero(self):
        f = DriveClient._parse_file({"size": "not-a-number"})
        assert f.size == 0

    def test_google_docs_report_no_size_as_zero(self):
        f = DriveClient._parse_file({"size": None})
        assert f.size == 0


# ---------------------------------------------------------------------------- #
# _download: truncation semantics
# ---------------------------------------------------------------------------- #

class TestDownload:
    def test_returns_all_data_when_under_cap(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"a" * 100]))
        data = DriveClient._download(request=object(), max_bytes=1000)
        assert data == b"a" * 100

    def test_stops_once_buffer_exceeds_cap(self, monkeypatch):
        monkeypatch.setattr(
            drive_client_module, "MediaIoBaseDownload",
            fake_downloader_class([b"a" * 5000, b"b" * 5000, b"c" * 5000]),
        )
        data = DriveClient._download(request=object(), max_bytes=8000)
        # Loop breaks right after the chunk that pushes it over the cap --
        # exactly 2 chunks (10000 bytes), never reaching the 3rd.
        assert len(data) == 10000


# ---------------------------------------------------------------------------- #
# list_files / get_file_metadata / list_folder
# ---------------------------------------------------------------------------- #

class TestListFiles:
    def test_maps_response_to_drive_files(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "f1", "name": "a.txt", "mimeType": "text/plain"}]
        }
        client = make_client(service)
        files = client.list_files("query")
        assert len(files) == 1
        assert files[0].id == "f1"

    def test_http_error_becomes_drive_client_error(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="list_files failed"):
            client.list_files("q")


class TestGetFileMetadata:
    def test_empty_file_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.get_file_metadata("")

    def test_fetches_and_normalizes(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "a.txt", "mimeType": "text/plain",
        }
        client = make_client(service)
        f = client.get_file_metadata("f1")
        assert f.name == "a.txt"

    def test_http_error_becomes_drive_client_error(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="get_file_metadata"):
            client.get_file_metadata("f1")


class TestListFolder:
    def test_empty_folder_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty folder_id"):
            client.list_folder("")

    def test_query_scopes_to_parent_and_excludes_trashed(self):
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        client = make_client(service)
        client.list_folder("folder-1")
        call_kwargs = service.files.return_value.list.call_args.kwargs
        assert call_kwargs["q"] == "'folder-1' in parents and trashed = false"


# ---------------------------------------------------------------------------- #
# get_file_content: workspace export vs binary vs text, truncation
# ---------------------------------------------------------------------------- #

class TestGetFileContent:
    def test_empty_file_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.get_file_content("")

    def test_google_doc_is_read_via_docs_api_as_markdown(self):
        # Regression test: a Google Doc used to be exported through the
        # Drive API's plain-text export, dropping all formatting -- it now
        # goes through the structured Docs API instead, same call the
        # docs_* write tools already use, and comes back as Markdown.
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [
                doc_para([doc_run("Title\n")], heading="HEADING_1"),
                doc_para([doc_run("bold text", bold=True), doc_run("\n")]),
            ]}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1")

        assert content.content_text == "# Title\n**bold text**"
        assert content.content_bytes == b""
        assert content.truncated is False
        docs_service.documents.return_value.get.assert_called_once_with(documentId="f1")
        service.files.return_value.export_media.assert_not_called()

    def test_google_doc_content_is_truncated_at_a_line_boundary(self):
        # Truncation must land on a complete line, not an arbitrary byte
        # offset, or a delimiter opened on one line and closed on the next
        # could be cut in between.
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [doc_para([doc_run(f"line{i}\n")]) for i in range(50)]}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1", max_bytes=20)

        assert content.truncated is True
        assert content.content_text == "line0\nline1\nline2"
        assert len(content.content_text.encode("utf-8")) <= 20

    def test_google_doc_content_with_no_line_break_falls_back_to_a_raw_cut(self):
        # One huge paragraph with no "\n" inside the cap at all -- there's
        # no line boundary to back off to, so this falls back to the same
        # raw byte cut every other get_file_content branch already uses.
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [doc_para([doc_run("x" * 100 + "\n")])]}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1", max_bytes=20)

        assert content.truncated is True
        assert content.content_text == "x" * 20

    def test_google_doc_docs_api_error_becomes_drive_client_error(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = http_error(500)
        client = make_client_with_drive_and_docs(service, docs_service)

        with pytest.raises(DriveClientError, match="get_file_content"):
            client.get_file_content("f1")

    def test_google_doc_highlight_renders_and_reports_non_default_color(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [doc_para([
                doc_run("Follow-up", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}}),
                doc_run(": ok\n"),
            ])]}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1")

        assert content.content_text == "==Follow-up==: ok"
        assert content.highlights == [{"text": "Follow-up", "hex": "#b6d7a8"}]
        assert content.text_colors == []

    def test_google_doc_default_highlight_color_has_no_sidecar_entry(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        default_hex = drive_client_module._DEFAULT_HIGHLIGHT_COLOR
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [doc_para([
                doc_run("x", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict(default_hex)}}),
                doc_run("\n"),
            ])]}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1")

        assert content.content_text == "==x=="
        assert content.highlights == []

    def test_google_doc_truncation_drops_sidecar_entries_outside_the_cut(self):
        # A sidecar entry for text that got truncated away would describe
        # content the caller can no longer see.
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        paragraphs = [doc_para([doc_run(f"line{i}\n")]) for i in range(20)]
        paragraphs.append(doc_para([
            doc_run("hl", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}}),
            doc_run("\n"),
        ]))
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": paragraphs}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1", max_bytes=15)

        assert content.truncated is True
        assert "hl" not in content.content_text
        assert content.highlights == []

    def test_google_doc_table_reads_as_a_real_gfm_grid(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [{"table": {"tableRows": [
                {"tableCells": [
                    {"content": [doc_para([doc_run("Name", bold=True), doc_run("\n")])]},
                    {"content": [doc_para([doc_run("Qty", bold=True), doc_run("\n")])]},
                ]},
                {"tableCells": [
                    {"content": [doc_para([doc_run("Widget\n")])]},
                    {"content": [doc_para([doc_run("3\n")])]},
                ]},
            ]}}]}
        }
        client = make_client_with_drive_and_docs(service, docs_service)

        content = client.get_file_content("f1")

        assert content.content_text == "| Name | Qty |\n| --- | --- |\n| Widget | 3 |"

    def test_google_sheet_is_exported_as_csv(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"a,b\n1,2"]))
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Sheet", "mimeType": "application/vnd.google-apps.spreadsheet",
        }
        client = make_client(service)
        content = client.get_file_content("f1")
        service.files.return_value.export_media.assert_called_once_with(fileId="f1", mimeType="text/csv")
        assert content.content_text == "a,b\n1,2"

    def test_text_mime_binary_is_decoded_to_text(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"hello world"]))
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "notes.md", "mimeType": "text/markdown",
        }
        client = make_client(service)
        content = client.get_file_content("f1")
        assert content.content_text == "hello world"
        service.files.return_value.get_media.assert_called_once_with(fileId="f1", supportsAllDrives=True)

    def test_non_text_binary_kept_as_raw_bytes(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"\x89PNG\x00\x01"]))
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "img.png", "mimeType": "image/png",
        }
        client = make_client(service)
        content = client.get_file_content("f1")
        assert content.content_bytes == b"\x89PNG\x00\x01"
        assert content.content_text == ""

    def test_content_over_max_bytes_is_truncated(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"x" * 100]))
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "big.txt", "mimeType": "text/plain",
        }
        client = make_client(service)
        content = client.get_file_content("f1", max_bytes=50)
        assert content.truncated is True
        assert len(content.content_text) == 50

    def test_non_positive_max_bytes_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"short"]))
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "f.txt", "mimeType": "text/plain",
        }
        client = make_client(service)
        content = client.get_file_content("f1", max_bytes=0)
        assert content.truncated is False
        assert content.content_text == "short"

    def test_download_http_error_becomes_drive_client_error(self, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "f.txt", "mimeType": "text/plain",
        }
        def raising_downloader(fd, request, chunksize=None):
            raise http_error(500)
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", raising_downloader)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="get_file_content"):
            client.get_file_content("f1")


# ---------------------------------------------------------------------------- #
# upload_file: validation + local_path vs content_base64 branches
# ---------------------------------------------------------------------------- #

class TestUploadFile:
    def test_neither_local_path_nor_content_base64_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="exactly one"):
            client.upload_file()

    def test_both_local_path_and_content_base64_raises(self, tmp_path):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="exactly one"):
            client.upload_file(local_path=str(tmp_path), content_base64="abc")

    def test_local_path_that_does_not_exist_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="no such file"):
            client.upload_file(local_path="/no/such/file.txt")

    def test_content_base64_without_name_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="name is required"):
            client.upload_file(content_base64=base64.b64encode(b"data").decode())

    def test_invalid_base64_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="invalid content_base64"):
            client.upload_file(name="f.txt", content_base64="not valid base64!!!")

    def test_uploads_from_local_path(self, tmp_path, monkeypatch):
        file_path = tmp_path / "report.pdf"
        file_path.write_bytes(b"%PDF-1.4 fake pdf")
        fake_media = MagicMock()
        monkeypatch.setattr("googleapiclient.http.MediaFileUpload", lambda *a, **kw: fake_media)

        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "f1", "name": "report.pdf", "mimeType": "application/pdf",
        }
        client = make_client(service)

        result = client.upload_file(local_path=str(file_path))

        assert result["id"] == "f1"
        assert result["name"] == "report.pdf"
        assert result["size_bytes"] == len(b"%PDF-1.4 fake pdf")
        create_kwargs = service.files.return_value.create.call_args.kwargs
        assert create_kwargs["media_body"] is fake_media
        assert create_kwargs["body"] == {"name": "report.pdf"}

    def test_uploads_from_content_base64(self, monkeypatch):
        fake_media = MagicMock()
        monkeypatch.setattr("googleapiclient.http.MediaIoBaseUpload", lambda *a, **kw: fake_media)

        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {
            "id": "f2", "name": "note.txt", "mimeType": "text/plain",
        }
        client = make_client(service)

        content = base64.b64encode(b"hello").decode()
        result = client.upload_file(name="note.txt", content_base64=content)

        assert result["id"] == "f2"
        assert result["size_bytes"] == len(b"hello")

    def test_parent_folder_included_when_given(self, tmp_path, monkeypatch):
        file_path = tmp_path / "f.txt"
        file_path.write_bytes(b"data")
        monkeypatch.setattr("googleapiclient.http.MediaFileUpload", lambda *a, **kw: MagicMock())

        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {"id": "f1", "name": "f.txt"}
        client = make_client(service)

        client.upload_file(local_path=str(file_path), parent_folder_id="folder-1")

        create_kwargs = service.files.return_value.create.call_args.kwargs
        assert create_kwargs["body"]["parents"] == ["folder-1"]

    def test_http_error_becomes_drive_client_error(self, tmp_path, monkeypatch):
        file_path = tmp_path / "f.txt"
        file_path.write_bytes(b"data")
        monkeypatch.setattr("googleapiclient.http.MediaFileUpload", lambda *a, **kw: MagicMock())

        service = MagicMock()
        service.files.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client(service)

        with pytest.raises(DriveClientError, match="upload_file"):
            client.upload_file(local_path=str(file_path))


# ---------------------------------------------------------------------------- #
# write_file_content / move_file / add_comment / list_shared_drives /
# create_blank_file
# ---------------------------------------------------------------------------- #

class TestWriteFileContent:
    def test_empty_file_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.write_file_content("", "text")

    def test_writes_and_returns_modified_time(self):
        service = MagicMock()
        service.files.return_value.update.return_value.execute.return_value = {
            "id": "f1", "modifiedTime": "2024-01-01",
        }
        client = make_client(service)
        result = client.write_file_content("f1", "new content")
        assert result == {"file_id": "f1", "modified_time": "2024-01-01"}

    def test_http_error_becomes_drive_client_error(self):
        service = MagicMock()
        service.files.return_value.update.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="write_file_content"):
            client.write_file_content("f1", "x")


class TestMoveFile:
    def test_missing_ids_raise(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="requires file_id"):
            client.move_file("", "dest")
        with pytest.raises(DriveClientError, match="requires file_id"):
            client.move_file("f1", "")

    def test_moves_file_removing_current_parents(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {"parents": ["old1", "old2"]}
        service.files.return_value.update.return_value.execute.return_value = {"id": "f1", "parents": ["new1"]}
        client = make_client(service)

        result = client.move_file("f1", "new1")

        assert result == {"file_id": "f1", "new_parent": "new1"}
        update_kwargs = service.files.return_value.update.call_args.kwargs
        assert update_kwargs["addParents"] == "new1"
        assert update_kwargs["removeParents"] == "old1,old2"

    def test_http_error_on_get_parents_becomes_drive_client_error(self):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="move_file get_parents"):
            client.move_file("f1", "dest")


class TestAddComment:
    def test_empty_file_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.add_comment("", "hi")

    def test_adds_comment(self):
        service = MagicMock()
        service.comments.return_value.create.return_value.execute.return_value = {"id": "c1"}
        client = make_client(service)
        result = client.add_comment("f1", "nice work")
        assert result == {"file_id": "f1", "comment_id": "c1", "content": "nice work"}


class TestListSharedDrives:
    def test_maps_response(self):
        service = MagicMock()
        service.drives.return_value.list.return_value.execute.return_value = {
            "drives": [{"id": "d1", "name": "Team Drive"}]
        }
        client = make_client(service)
        assert client.list_shared_drives() == [{"id": "d1", "name": "Team Drive"}]

    def test_http_error_becomes_drive_client_error(self):
        service = MagicMock()
        service.drives.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="list_shared_drives"):
            client.list_shared_drives()


class TestCreateBlankFile:
    def test_creates_with_parent(self):
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = {"id": "f1"}
        client = make_client(service)
        result = client.create_blank_file("New Doc", "application/vnd.google-apps.document", "folder-1")
        assert result == {"id": "f1", "name": "New Doc", "mime_type": "application/vnd.google-apps.document"}
        body = service.files.return_value.create.call_args.kwargs["body"]
        assert body == {"name": "New Doc", "mimeType": "application/vnd.google-apps.document", "parents": ["folder-1"]}

    def test_http_error_becomes_drive_client_error(self):
        service = MagicMock()
        service.files.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(DriveClientError, match="create_blank_file"):
            client.create_blank_file("f", "text/plain")


# ---------------------------------------------------------------------------- #
# _parse_inline_runs / _markdown_to_docs_requests: Markdown -> Docs API
# ---------------------------------------------------------------------------- #

class TestParseInlineRuns:
    def test_plain_text_single_run(self):
        assert _parse_inline_runs("hello world") == [InlineRun("hello world")]

    def test_bold(self):
        assert _parse_inline_runs("**bold**") == [InlineRun("bold", bold=True)]

    def test_italic(self):
        assert _parse_inline_runs("*italic*") == [InlineRun("italic", italic=True)]

    def test_bold_italic(self):
        assert _parse_inline_runs("***both***") == [InlineRun("both", bold=True, italic=True)]

    def test_strikethrough(self):
        assert _parse_inline_runs("~~gone~~") == [InlineRun("gone", strikethrough=True)]

    def test_underline(self):
        assert _parse_inline_runs("__stressed__") == [InlineRun("stressed", underline=True)]

    def test_highlight(self):
        assert _parse_inline_runs("==flagged==") == [InlineRun("flagged", highlight=True)]

    def test_code_gets_monospace_style(self):
        assert _parse_inline_runs("`code`") == [InlineRun("code", code=True)]

    def test_link(self):
        assert _parse_inline_runs("[text](http://x.com)") == [InlineRun("text", url="http://x.com")]

    def test_link_text_with_escaped_brackets(self):
        assert _parse_inline_runs(r"[\[label \]text](http://x.com)") == [
            InlineRun("[label ]text", url="http://x.com")
        ]

    def test_link_text_with_escaped_close_bracket_only(self):
        assert _parse_inline_runs(r"[tag\]suffix](http://x.com)") == [
            InlineRun("tag]suffix", url="http://x.com")
        ]

    def test_link_text_with_unescaped_close_bracket_is_not_a_link(self):
        # An unescaped `]` inside the label closes it early, and the
        # dangling `](http://x.com)` that follows never resolves into a
        # complete link -- the whole string falls back to a single plain run
        # rather than being misread as a link with a truncated label.
        text = r"[[tag] text](http://x.com)"
        assert _parse_inline_runs(text) == [InlineRun(text)]

    def test_mixed_runs_preserve_order_and_plain_gaps(self):
        runs = _parse_inline_runs("hello **bold** world")
        assert runs == [
            InlineRun("hello "),
            InlineRun("bold", bold=True),
            InlineRun(" world"),
        ]

    def test_empty_string_yields_single_empty_run(self):
        assert _parse_inline_runs("") == [InlineRun("")]

    # -- Nested inline styles (regression: previously the outer span's
    # regex alternative swallowed the inner syntax as literal, unparsed
    # text -- e.g. ==**Follow-up**== inserted the literal string
    # "**Follow-up**" with only highlight applied, dropping bold entirely). --

    def test_highlight_wrapping_bold_nests_both_styles(self):
        assert _parse_inline_runs("==**Follow-up**==") == [
            InlineRun("Follow-up", bold=True, highlight=True)
        ]

    def test_bold_wrapping_highlight_nests_both_styles(self):
        assert _parse_inline_runs("**==Follow-up==**") == [
            InlineRun("Follow-up", bold=True, highlight=True)
        ]

    def test_bold_wrapping_italic_nests_both_styles(self):
        assert _parse_inline_runs("**bold *and italic* text**") == [
            InlineRun("bold ", bold=True),
            InlineRun("and italic", bold=True, italic=True),
            InlineRun(" text", bold=True),
        ]

    def test_highlight_wrapping_underline_and_strikethrough(self):
        assert _parse_inline_runs("==__a__ ~~b~~==") == [
            InlineRun("a", underline=True, highlight=True),
            InlineRun(" ", highlight=True),
            InlineRun("b", strikethrough=True, highlight=True),
        ]

    def test_code_inside_highlight_is_a_leaf_not_reparsed(self):
        # Code spans stay literal even when nested -- matches CommonMark's
        # treatment of code spans as never containing further inline syntax.
        assert _parse_inline_runs("==`**not bold**`==") == [
            InlineRun("**not bold**", code=True, highlight=True)
        ]

    def test_non_overlapping_bold_and_highlight_are_unaffected(self):
        # Two separate spans on the same line, not nested -- must keep
        # working exactly as before.
        runs = _parse_inline_runs("**bold** and ==flagged==")
        assert runs == [
            InlineRun("bold", bold=True),
            InlineRun(" and "),
            InlineRun("flagged", highlight=True),
        ]


class TestMarkdownToDocsRequests:
    def test_empty_markdown_yields_no_requests(self):
        assert _markdown_to_docs_requests("") == []
        assert _markdown_to_docs_requests("\n\n") == []

    def test_plain_paragraph_only_inserts_text(self):
        requests = _markdown_to_docs_requests("just text")
        assert requests == [{"insertText": {"location": {"index": 1}, "text": "just text\n"}}]

    def test_heading_levels_map_to_named_styles(self):
        for prefix, style in [
            ("# ", "HEADING_1"), ("## ", "HEADING_2"), ("### ", "HEADING_3"),
            ("#### ", "HEADING_4"), ("##### ", "HEADING_5"), ("###### ", "HEADING_6"),
        ]:
            requests = _markdown_to_docs_requests(f"{prefix}Title")
            style_reqs = [r for r in requests if "updateParagraphStyle" in r]
            assert len(style_reqs) == 1
            assert style_reqs[0]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == style
            # heading prefix must be stripped from the inserted text
            assert requests[0]["insertText"]["text"] == "Title\n"

    def test_bullet_list_item_gets_bullet_preset_and_prefix_stripped(self):
        requests = _markdown_to_docs_requests("- item one")
        assert requests[0]["insertText"]["text"] == "item one\n"
        bullet_reqs = [r for r in requests if "createParagraphBullets" in r]
        assert len(bullet_reqs) == 1
        assert bullet_reqs[0]["createParagraphBullets"]["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE"

    def test_numbered_list_item_gets_numbered_preset(self):
        requests = _markdown_to_docs_requests("1. first")
        assert requests[0]["insertText"]["text"] == "first\n"
        bullet_reqs = [r for r in requests if "createParagraphBullets" in r]
        assert bullet_reqs[0]["createParagraphBullets"]["bulletPreset"] == "NUMBERED_DECIMAL_ALPHA_ROMAN"

    def test_bold_run_produces_update_text_style_request(self):
        requests = _markdown_to_docs_requests("**bold**")
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert len(style_reqs) == 1
        style = style_reqs[0]["updateTextStyle"]
        assert style["textStyle"] == {"bold": True}
        assert style["fields"] == "bold"
        assert style["range"] == {"startIndex": 1, "endIndex": 5}  # "bold" is 4 chars

    def test_link_run_produces_link_field(self):
        requests = _markdown_to_docs_requests("[click](http://x.com)")
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert style_reqs[0]["updateTextStyle"]["textStyle"] == {"link": {"url": "http://x.com"}}
        assert style_reqs[0]["updateTextStyle"]["fields"] == "link"

    def test_highlight_run_produces_background_color_field(self):
        requests = _markdown_to_docs_requests("==flagged==")
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert len(style_reqs) == 1
        style = style_reqs[0]["updateTextStyle"]
        assert style["fields"] == "backgroundColor"
        assert style["textStyle"]["backgroundColor"]["color"]["rgbColor"] == _hex_to_rgb_dict(
            drive_client_module._DEFAULT_HIGHLIGHT_COLOR
        )

    def test_strikethrough_run_produces_strikethrough_field(self):
        requests = _markdown_to_docs_requests("~~gone~~")
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert style_reqs[0]["updateTextStyle"]["textStyle"] == {"strikethrough": True}
        assert style_reqs[0]["updateTextStyle"]["fields"] == "strikethrough"

    def test_underline_run_produces_underline_field(self):
        requests = _markdown_to_docs_requests("__stressed__")
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert style_reqs[0]["updateTextStyle"]["textStyle"] == {"underline": True}
        assert style_reqs[0]["updateTextStyle"]["fields"] == "underline"

    def test_code_run_produces_monospace_font(self):
        requests = _markdown_to_docs_requests("`foo()`")
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert len(style_reqs) == 1
        style = style_reqs[0]["updateTextStyle"]
        assert style["fields"] == "weightedFontFamily"
        assert style["textStyle"]["weightedFontFamily"] == {
            "fontFamily": drive_client_module._CODE_FONT_FAMILY
        }

    def test_flat_list_items_get_no_leading_tabs(self):
        requests = _markdown_to_docs_requests("- top level")
        assert requests[0]["insertText"]["text"] == "top level\n"
        bullet_reqs = [r for r in requests if "createParagraphBullets" in r]
        assert bullet_reqs[0]["createParagraphBullets"]["range"] == {"startIndex": 1, "endIndex": 11}

    def test_nested_bullet_gets_leading_tab_and_shifted_style_range(self):
        # 2 leading spaces = one nesting level = one leading tab character,
        # which the Docs API uses to infer nesting depth for createParagraphBullets.
        requests = _markdown_to_docs_requests("  - nested **bold**")
        assert requests[0]["insertText"]["text"] == "\tnested bold\n"
        bullet_reqs = [r for r in requests if "createParagraphBullets" in r]
        # bullet range spans the leading tab too, per the Docs API contract
        assert bullet_reqs[0]["createParagraphBullets"]["range"] == {"startIndex": 1, "endIndex": 14}
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        # By the time this request runs, createParagraphBullets has already
        # stripped the leading tab it counted, so "bold" (after "nested ",
        # 7 chars) starts at the line's original start index, not after it.
        assert style_reqs[0]["updateTextStyle"]["range"] == {"startIndex": 8, "endIndex": 12}

    def test_consecutive_nested_bullets_both_get_correctly_shifted_ranges(self):
        # Regression test: each createParagraphBullets request strips its own
        # line's leading tabs as a side effect, shrinking the document that
        # every later request in the same batchUpdate is interpreted against.
        # A second (or third...) consecutive nested item must have its ranges
        # shifted by every earlier list line's already-stripped tab count, or
        # it silently lands on the wrong paragraph -- previously only the
        # first nested line in a run got fixed up correctly.
        requests = _markdown_to_docs_requests(
            "- Top level item one\n  - Nested item one-a\n  - Nested item one-b"
        )
        assert requests[0]["insertText"]["text"] == (
            "Top level item one\n\tNested item one-a\n\tNested item one-b\n"
        )
        bullet_reqs = [r["createParagraphBullets"]["range"] for r in requests if "createParagraphBullets" in r]

        # Simulate the Docs API applying these requests in order against the
        # inserted text, stripping each paragraph's leading tabs as it goes,
        # to prove the final ranges land on paragraph boundaries.
        text = requests[0]["insertText"]["text"]
        for rng in bullet_reqs:
            start, end = rng["startIndex"] - 1, rng["endIndex"] - 1
            para = text[start:end]
            stripped = para.lstrip("\t")
            ntabs = len(para) - len(stripped)
            assert para.startswith("\t" * ntabs), f"range {rng} does not start on a paragraph boundary: {para!r}"
            text = text[:start] + stripped + text[end:]

        assert text == "Top level item one\nNested item one-a\nNested item one-b\n"

    def test_deeply_nested_list_caps_at_max_nesting(self):
        huge_indent = " " * 40  # far beyond _MAX_LIST_NESTING levels
        requests = _markdown_to_docs_requests(f"{huge_indent}- deep")
        assert requests[0]["insertText"]["text"] == ("\t" * drive_client_module._MAX_LIST_NESTING) + "deep\n"

    def test_indented_plain_paragraph_keeps_its_whitespace(self):
        # Indentation only triggers nesting for recognized list markers --
        # an indented non-list line must be left untouched.
        requests = _markdown_to_docs_requests("    not a list")
        assert requests[0]["insertText"]["text"] == "    not a list\n"
        assert not any("createParagraphBullets" in r for r in requests)

    def test_start_index_offsets_every_range(self):
        requests = _markdown_to_docs_requests("**bold**", start_index=10)
        assert requests[0]["insertText"]["location"]["index"] == 10
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert style_reqs[0]["updateTextStyle"]["range"] == {"startIndex": 10, "endIndex": 14}

    def test_multiple_lines_accumulate_correct_positions(self):
        requests = _markdown_to_docs_requests("# Title\nplain line")
        assert requests[0]["insertText"]["text"] == "Title\nplain line\n"
        heading_reqs = [r for r in requests if "updateParagraphStyle" in r]
        assert heading_reqs[0]["updateParagraphStyle"]["range"] == {"startIndex": 1, "endIndex": 7}

    def test_plain_run_produces_no_style_request(self):
        requests = _markdown_to_docs_requests("plain text only")
        assert not any("updateTextStyle" in r for r in requests)

    # -- Thematic-break dividers (regression: previously `---` on its own
    # line had no recognized meaning at all and was inserted as the literal
    # 3-character string, not a divider and not an error either). --

    def test_thematic_break_becomes_bordered_paragraph(self):
        requests = _markdown_to_docs_requests("---")
        # No literal "-" text lands in the document.
        assert requests[0]["insertText"]["text"] == "\n"
        style_reqs = [r for r in requests if "updateParagraphStyle" in r]
        assert len(style_reqs) == 1
        style = style_reqs[0]["updateParagraphStyle"]
        assert style["fields"] == "borderBottom"
        assert style["paragraphStyle"]["borderBottom"]["dashStyle"] == "SOLID"
        assert style["range"] == {"startIndex": 1, "endIndex": 2}

    @pytest.mark.parametrize("marker", ["---", "***", "___", "- - -"])
    def test_asterisk_and_underscore_variants_also_become_dividers(self, marker):
        requests = _markdown_to_docs_requests(marker)
        assert requests[0]["insertText"]["text"] == "\n"
        assert any(
            "updateParagraphStyle" in r and r["updateParagraphStyle"]["fields"] == "borderBottom"
            for r in requests
        )

    def test_lone_divider_is_not_treated_as_blank_markdown(self):
        # The "nothing but blank lines" short-circuit that makes
        # _markdown_to_docs_requests("\n\n") == [] must not also eat a
        # divider-only document, even though a divider line has no text.
        assert _markdown_to_docs_requests("---") != []

    def test_divider_between_paragraphs_does_not_disturb_their_text(self):
        requests = _markdown_to_docs_requests("before\n---\nafter")
        assert requests[0]["insertText"]["text"] == "before\n\nafter\n"
        border_reqs = [
            r for r in requests
            if "updateParagraphStyle" in r and r["updateParagraphStyle"]["fields"] == "borderBottom"
        ]
        assert len(border_reqs) == 1
        # "before\n" is 7 chars -> divider paragraph spans [8, 9).
        assert border_reqs[0]["updateParagraphStyle"]["range"] == {"startIndex": 8, "endIndex": 9}

    def test_double_asterisk_is_not_mistaken_for_a_divider(self):
        # Two asterisks alone don't meet the 3-or-more thematic-break rule,
        # so `**` (an otherwise-degenerate bold marker) is left alone.
        requests = _markdown_to_docs_requests("**")
        assert not any(
            "updateParagraphStyle" in r and r["updateParagraphStyle"]["fields"] == "borderBottom"
            for r in requests
        )
        assert not any("updateParagraphStyle" in r for r in requests)
        assert not any("createParagraphBullets" in r for r in requests)


# ---------------------------------------------------------------------------- #
# _docs_structure_to_markdown / _docs_text_run_to_markdown /
# _docs_content_elements_to_markdown: Docs API -> Markdown (the read-side
# mirror of _markdown_to_docs_requests)
# ---------------------------------------------------------------------------- #

class TestDocsTextRunToMarkdown:
    def test_plain_run(self):
        assert _docs_text_run_to_markdown(doc_run("hello\n")) == "hello"

    def test_bold(self):
        assert _docs_text_run_to_markdown(doc_run("x", bold=True)) == "**x**"

    def test_italic(self):
        assert _docs_text_run_to_markdown(doc_run("x", italic=True)) == "*x*"

    def test_bold_and_italic_together(self):
        assert _docs_text_run_to_markdown(doc_run("x", bold=True, italic=True)) == "***x***"

    def test_strikethrough(self):
        assert _docs_text_run_to_markdown(doc_run("x", strikethrough=True)) == "~~x~~"

    def test_underline(self):
        assert _docs_text_run_to_markdown(doc_run("x", underline=True)) == "__x__"

    def test_code(self):
        run = doc_run("x", weightedFontFamily={"fontFamily": drive_client_module._CODE_FONT_FAMILY})
        assert _docs_text_run_to_markdown(run) == "`x`"

    def test_non_code_font_is_not_treated_as_code(self):
        run = doc_run("x", weightedFontFamily={"fontFamily": "Arial"})
        assert _docs_text_run_to_markdown(run) == "x"

    def test_link(self):
        assert _docs_text_run_to_markdown(doc_run("x", link={"url": "http://x.com"})) == "[x](http://x.com)"

    def test_bold_link_nests_link_innermost(self):
        # Round-trip check: _parse_inline_runs only finds the link if the
        # outer bold delimiter is what wraps it, not the other way around
        # (see _docs_text_run_to_markdown's docstring).
        run = doc_run("x", bold=True, link={"url": "http://x.com"})
        rendered = _docs_text_run_to_markdown(run)
        assert rendered == "**[x](http://x.com)**"
        assert _parse_inline_runs(rendered) == [InlineRun("x", bold=True, url="http://x.com")]

    def test_code_and_link_together_prefers_link(self):
        # No representation for "code AND link" in this dialect -- the link
        # wins, monospace styling is dropped (see the docstring).
        run = doc_run("x", link={"url": "http://x.com"},
                      weightedFontFamily={"fontFamily": drive_client_module._CODE_FONT_FAMILY})
        assert _docs_text_run_to_markdown(run) == "[x](http://x.com)"

    def test_bold_code_nests_code_innermost(self):
        run = doc_run("x", bold=True, weightedFontFamily={"fontFamily": drive_client_module._CODE_FONT_FAMILY})
        rendered = _docs_text_run_to_markdown(run)
        assert rendered == "**`x`**"
        assert _parse_inline_runs(rendered) == [InlineRun("x", bold=True, code=True)]

    def test_all_four_wrap_styles_together_round_trip(self):
        run = doc_run("x", bold=True, italic=True, underline=True, strikethrough=True)
        rendered = _docs_text_run_to_markdown(run)
        assert _parse_inline_runs(rendered) == [
            InlineRun("x", bold=True, italic=True, underline=True, strikethrough=True)
        ]

    def test_highlight_wraps_the_run(self):
        run = doc_run("x", backgroundColor={"color": {"rgbColor": {}}})
        assert _docs_text_run_to_markdown(run) == "==x=="

    def test_highlight_renders_regardless_of_actual_color(self):
        # Binary presence only in the Markdown body -- the exact hex isn't
        # representable here; see _docs_run_color_notes for where it goes.
        run = doc_run("x", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}})
        assert _docs_text_run_to_markdown(run) == "==x=="

    def test_highlight_combines_with_bold_round_trip(self):
        run = doc_run("x", bold=True, backgroundColor={"color": {"rgbColor": {}}})
        rendered = _docs_text_run_to_markdown(run)
        assert rendered == "==**x**=="
        assert _parse_inline_runs(rendered) == [InlineRun("x", bold=True, highlight=True)]

    def test_highlight_is_outermost_of_all_five_wrap_styles(self):
        run = doc_run(
            "x", bold=True, italic=True, underline=True, strikethrough=True,
            backgroundColor={"color": {"rgbColor": {}}},
        )
        rendered = _docs_text_run_to_markdown(run)
        assert _parse_inline_runs(rendered) == [
            InlineRun("x", bold=True, italic=True, underline=True, strikethrough=True, highlight=True)
        ]

    def test_trailing_paragraph_newline_is_stripped_before_wrapping(self):
        # Regression guard: wrapping the paragraph's own trailing "\n"
        # along with real text would leave a raw newline *inside* a
        # delimited span (e.g. "**bold text\n**"), corrupting every line
        # boundary downstream of it.
        assert _docs_text_run_to_markdown(doc_run("bold text\n", bold=True)) == "**bold text**"

    def test_soft_line_break_becomes_a_space(self):
        assert _docs_text_run_to_markdown(doc_run("line1\x0bline2\n")) == "line1 line2"

    def test_empty_run_yields_empty_string(self):
        assert _docs_text_run_to_markdown(doc_run("\n")) == ""
        assert _docs_text_run_to_markdown(doc_run("")) == ""

    def test_suppress_bold_drops_the_bold_wrap(self):
        assert _docs_text_run_to_markdown(doc_run("x", bold=True), suppress_bold=True) == "x"

    def test_suppress_bold_has_no_effect_without_bold(self):
        assert _docs_text_run_to_markdown(doc_run("x", italic=True), suppress_bold=True) == "*x*"

    def test_suppress_bold_keeps_every_other_style(self):
        run = doc_run("x", bold=True, italic=True, underline=True, strikethrough=True)
        rendered = _docs_text_run_to_markdown(run, suppress_bold=True)
        assert _parse_inline_runs(rendered) == [
            InlineRun("x", italic=True, underline=True, strikethrough=True)
        ]


class TestDocsStructureToMarkdown:
    def test_plain_paragraph(self):
        doc = {"body": {"content": [doc_para([doc_run("hello world\n")])]}}
        assert _docs_structure_to_markdown(doc) == "hello world"

    def test_heading_levels_map_to_markdown_prefixes(self):
        for style, prefix in [
            ("HEADING_1", "# "), ("HEADING_2", "## "), ("HEADING_3", "### "),
            ("HEADING_4", "#### "), ("HEADING_5", "##### "), ("HEADING_6", "###### "),
        ]:
            doc = {"body": {"content": [doc_para([doc_run("Title\n")], heading=style)]}}
            assert _docs_structure_to_markdown(doc) == f"{prefix}Title"

    def test_normal_text_style_gets_no_prefix(self):
        doc = {"body": {"content": [doc_para([doc_run("x\n")], heading="NORMAL_TEXT")]}}
        assert _docs_structure_to_markdown(doc) == "x"

    def test_multiple_runs_in_one_paragraph_concatenate(self):
        doc = {"body": {"content": [doc_para([
            doc_run("plain "), doc_run("bold", bold=True), doc_run(" and "),
            doc_run("italic", italic=True), doc_run(".\n"),
        ])]}}
        assert _docs_structure_to_markdown(doc) == "plain **bold** and *italic*."

    def test_multiple_paragraphs_join_with_newline(self):
        doc = {"body": {"content": [
            doc_para([doc_run("Title\n")], heading="HEADING_1"),
            doc_para([doc_run("plain\n")]),
        ]}}
        assert _docs_structure_to_markdown(doc) == "# Title\nplain"

    def test_non_text_paragraph_element_contributes_no_text(self):
        # An image or footnote reference is a paragraph element with no
        # textRun and no dedicated handling (unlike a horizontal rule,
        # phase 2 below) -- for now it's simply skipped, same as the old
        # plain-text export already lost anything that wasn't a textRun.
        doc = {"body": {"content": [
            doc_para([doc_run("above\n")]),
            {"paragraph": {"elements": [{"inlineObjectElement": {"inlineObjectId": "kix.abc"}}]}},
            doc_para([doc_run("below\n")]),
        ]}}
        assert _docs_structure_to_markdown(doc) == "above\n\nbelow"

    def test_horizontal_rule_renders_as_a_bare_divider_line(self):
        # Regression test: exact reverse of the write side's
        # _THEMATIC_BREAK_RE handling -- a horizontalRule element used to
        # fall into the "no text to render" bucket above (contributing
        # nothing at all) rather than becoming a real "---" divider line.
        # This is the "a human inserted one through the Docs UI" shape --
        # see TestDocsParagraphIsDivider for the write side's own
        # borderBottom shape, which read the same way.
        doc = {"body": {"content": [
            doc_para([doc_run("above\n")]),
            doc_horizontal_rule(),
            doc_para([doc_run("below\n")]),
        ]}}
        assert _docs_structure_to_markdown(doc) == "above\n---\nbelow"

    def test_horizontal_rule_ignores_any_heading_style_on_its_paragraph(self):
        # A rule's own paragraph never legitimately carries a
        # namedStyleType, but the check for it comes first regardless --
        # confirm a rule never gets a heading prefix even if one turned up.
        doc = {"body": {"content": [
            {"paragraph": {
                "elements": [{"horizontalRule": {}}],
                "paragraphStyle": {"namedStyleType": "HEADING_1"},
            }},
        ]}}
        assert _docs_structure_to_markdown(doc) == "---"

    def test_consecutive_horizontal_rules(self):
        doc = {"body": {"content": [doc_horizontal_rule(), doc_horizontal_rule()]}}
        assert _docs_structure_to_markdown(doc) == "---\n---"

    def test_horizontal_rule_round_trips_through_markdown_to_docs_requests(self):
        # write_doc_rich_content has no native "insert horizontal rule"
        # request to round-trip through -- it renders a divider as a
        # bottom-bordered empty paragraph instead (see
        # TestMarkdownToDocsRequests.test_thematic_break_becomes_bordered_
        # paragraph), so the round trip is checked against that shape.
        doc = {"body": {"content": [
            doc_para([doc_run("above\n")]),
            doc_horizontal_rule(),
            doc_para([doc_run("below\n")]),
        ]}}
        markdown = _docs_structure_to_markdown(doc)
        requests = _markdown_to_docs_requests(markdown)
        assert requests[0]["insertText"]["text"] == "above\n\nbelow\n"
        border_reqs = [
            r for r in requests
            if "updateParagraphStyle" in r and r["updateParagraphStyle"]["fields"] == "borderBottom"
        ]
        assert len(border_reqs) == 1

    def test_horizontal_rule_inside_a_table_cell_does_not_crash(self):
        # Edge case with no real-world precedent worth optimizing for --
        # just confirm the recursive cell renderer doesn't choke on it. As
        # of phase 5 a one-row table's row is the GFM header, so the
        # literal "---" text lands in both the header and separator rows.
        doc = {"body": {"content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [doc_horizontal_rule()]}]},
        ]}}]}}
        assert _docs_structure_to_markdown(doc) == "| --- |\n| --- |"

    def test_table_renders_as_a_real_gfm_grid(self):
        # A table renders as a real GFM pipe table, not flat tab/newline
        # text -- see TestDocsTableToMarkdown for the dedicated
        # per-construct coverage (alignment, <br>-joined
        # multi-paragraph cells, pipe escaping, header-bold suppression).
        # The header row's bold is suppressed here too (H2 renders
        # unwrapped) -- GFM's own header row is already visually bold, and
        # re-emitting "**H2**" would double-bold it on the next write.
        doc = {"body": {"content": [{"table": {"tableRows": [
            {"tableCells": [
                {"content": [doc_para([doc_run("H1\n")])]},
                {"content": [doc_para([doc_run("H2", bold=True), doc_run("\n")])]},
            ]},
            {"tableCells": [
                {"content": [doc_para([doc_run("a\n")])]},
                {"content": [doc_para([doc_run("b\n")])]},
            ]},
        ]}}]}}
        assert _docs_structure_to_markdown(doc) == "| H1 | H2 |\n| --- | --- |\n| a | b |"

    def test_empty_document_yields_empty_string(self):
        assert _docs_structure_to_markdown({"body": {"content": []}}) == ""
        assert _docs_structure_to_markdown({}) == ""

    def test_round_trips_through_markdown_to_docs_requests_insert_text(self):
        # The strongest test this feature can have: build a Docs structure,
        # render it to Markdown, feed that straight into the write side,
        # and confirm the plain text _markdown_to_docs_requests would
        # insert matches what a plain-text reading of the same structure
        # would show -- i.e. formatting markers round-trip losslessly
        # around the same underlying words.
        doc = {"body": {"content": [
            doc_para([doc_run("Title\n")], heading="HEADING_1"),
            doc_para([
                doc_run("hello "), doc_run("world", bold=True, italic=True), doc_run("!\n"),
            ]),
        ]}}
        markdown = _docs_structure_to_markdown(doc)
        requests = _markdown_to_docs_requests(markdown)
        assert requests[0]["insertText"]["text"] == "Title\nhello world!\n"
        style_reqs = [r for r in requests if "updateTextStyle" in r]
        assert style_reqs[0]["updateTextStyle"]["textStyle"] == {"bold": True, "italic": True}


class TestDocsParagraphIsDivider:
    def test_horizontal_rule_element_is_a_divider(self):
        assert _docs_paragraph_is_divider(doc_horizontal_rule()["paragraph"]) is True

    def test_empty_border_bottom_paragraph_is_a_divider(self):
        # The shape write_doc_rich_content's own thematic-break handling
        # actually produces (see TestMarkdownToDocsRequests) -- an empty
        # paragraph with paragraphStyle.borderBottom set, not a
        # horizontalRule element at all (the Docs API has no native
        # "insert horizontal rule" request).
        paragraph = {
            "elements": [{"textRun": doc_run("\n")}],
            "paragraphStyle": {"borderBottom": {"dashStyle": "SOLID"}},
        }
        assert _docs_paragraph_is_divider(paragraph) is True

    def test_bordered_paragraph_with_real_text_is_not_a_divider(self):
        # A bottom border on a paragraph that still has real prose in it
        # must not be mistaken for a divider and lose its text.
        paragraph = {
            "elements": [{"textRun": doc_run("real text\n")}],
            "paragraphStyle": {"borderBottom": {"dashStyle": "SOLID"}},
        }
        assert _docs_paragraph_is_divider(paragraph) is False

    def test_plain_paragraph_is_not_a_divider(self):
        assert _docs_paragraph_is_divider(doc_para([doc_run("x\n")])["paragraph"]) is False

    def test_border_bottom_on_paragraph_with_no_text_runs_at_all(self):
        paragraph = {"elements": [], "paragraphStyle": {"borderBottom": {"dashStyle": "SOLID"}}}
        assert _docs_paragraph_is_divider(paragraph) is True


class TestDocsContentElementsToMarkdown:
    def test_delegates_the_same_way_as_top_level_content(self):
        # _docs_structure_to_markdown is a thin wrapper over this -- proves
        # the two stay in sync rather than duplicating every case above.
        elements = [doc_para([doc_run("x\n")])]
        assert _docs_content_elements_to_markdown(elements) == _docs_structure_to_markdown(
            {"body": {"content": elements}}
        )


# ---------------------------------------------------------------------------- #
# _docs_run_color_notes / _docs_structure_color_sidecar: the color sidecar
# -- exact highlight/text colors Markdown alone can't carry.
# ---------------------------------------------------------------------------- #

class TestDocsRunColorNotes:
    def test_no_style_yields_no_notes(self):
        assert _docs_run_color_notes(doc_run("x")) == ("", "")

    def test_default_highlight_color_yields_no_note(self):
        # The plain ==x== _docs_text_run_to_markdown already emits
        # represents this losslessly -- no sidecar entry needed.
        run = doc_run("x", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict(drive_client_module._DEFAULT_HIGHLIGHT_COLOR)}})
        assert _docs_run_color_notes(run) == ("", "")

    def test_default_highlight_color_comparison_is_case_insensitive(self):
        run = doc_run("x", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict(
            drive_client_module._DEFAULT_HIGHLIGHT_COLOR.upper()
        )}})
        assert _docs_run_color_notes(run) == ("", "")

    def test_non_default_highlight_color_is_reported(self):
        run = doc_run("x", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}})
        assert _docs_run_color_notes(run) == ("#b6d7a8", "")

    def test_any_text_color_is_reported_default_or_not(self):
        # Markdown has no text-color syntax at all -- unlike highlight,
        # there's no "already represented" case to skip.
        run = doc_run("x", foregroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#000000")}})
        assert _docs_run_color_notes(run) == ("", "#000000")

    def test_both_at_once(self):
        run = doc_run(
            "x",
            backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}},
            foregroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#ff0000")}},
        )
        assert _docs_run_color_notes(run) == ("#b6d7a8", "#ff0000")

    def test_empty_run_yields_no_notes_even_with_style(self):
        run = doc_run("\n", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}})
        assert _docs_run_color_notes(run) == ("", "")


class TestDocsStructureColorSidecar:
    def test_no_colors_yields_empty_lists(self):
        doc = {"body": {"content": [doc_para([doc_run("plain\n")])]}}
        assert _docs_structure_color_sidecar(doc) == ([], [])

    def test_default_highlight_is_not_in_the_sidecar(self):
        run = doc_run("x", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict(
            drive_client_module._DEFAULT_HIGHLIGHT_COLOR
        )}})
        doc = {"body": {"content": [doc_para([run, doc_run("\n")])]}}
        assert _docs_structure_color_sidecar(doc) == ([], [])

    def test_non_default_highlight_is_collected(self):
        run = doc_run("Follow-up", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}})
        doc = {"body": {"content": [doc_para([run, doc_run("\n")])]}}
        highlights, text_colors = _docs_structure_color_sidecar(doc)
        assert highlights == [{"text": "Follow-up", "hex": "#b6d7a8"}]
        assert text_colors == []

    def test_text_color_is_collected(self):
        run = doc_run("red", foregroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#ff0000")}})
        doc = {"body": {"content": [doc_para([run, doc_run("\n")])]}}
        highlights, text_colors = _docs_structure_color_sidecar(doc)
        assert highlights == []
        assert text_colors == [{"text": "red", "hex": "#ff0000"}]

    def test_multiple_runs_across_multiple_paragraphs(self):
        hl = doc_run("a", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}})
        fg = doc_run("b", foregroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#00ff00")}})
        doc = {"body": {"content": [
            doc_para([hl, doc_run("\n")]),
            doc_para([fg, doc_run("\n")]),
        ]}}
        highlights, text_colors = _docs_structure_color_sidecar(doc)
        assert highlights == [{"text": "a", "hex": "#b6d7a8"}]
        assert text_colors == [{"text": "b", "hex": "#00ff00"}]

    def test_collects_from_inside_table_cells(self):
        hl = doc_run("cell", backgroundColor={"color": {"rgbColor": _hex_to_rgb_dict("#b6d7a8")}})
        doc = {"body": {"content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [doc_para([hl, doc_run("\n")])]}]},
        ]}}]}}
        highlights, _text_colors = _docs_structure_color_sidecar(doc)
        assert highlights == [{"text": "cell", "hex": "#b6d7a8"}]

    def test_horizontal_rule_paragraph_does_not_crash(self):
        # A horizontalRule element has no textRun/textStyle at all --
        # confirm the walk skips it cleanly rather than erroring.
        doc = {"body": {"content": [doc_horizontal_rule()]}}
        assert _docs_structure_color_sidecar(doc) == ([], [])


# ---------------------------------------------------------------------------- #
# _docs_list_nesting_is_ordered / list rendering in _docs_structure_to_markdown
# ---------------------------------------------------------------------------- #

class TestDocsListNestingIsOrdered:
    def test_bullet_list_is_not_ordered(self):
        assert _docs_list_nesting_is_ordered(doc_bullet_list_map(), "list1", 0) is False

    def test_numbered_list_is_ordered(self):
        assert _docs_list_nesting_is_ordered(doc_numbered_list_map(), "list1", 0) is True

    def test_unknown_list_id_defaults_to_unordered(self):
        assert _docs_list_nesting_is_ordered({}, "no-such-list", 0) is False

    def test_nesting_level_beyond_the_list_defaults_to_unordered(self):
        assert _docs_list_nesting_is_ordered(doc_numbered_list_map(levels=1), "list1", 5) is False

    def test_each_ordered_glyph_type_is_recognized(self):
        for glyph_type in ["DECIMAL", "ZERO_DECIMAL", "UPPER_ALPHA", "ALPHA", "UPPER_ROMAN", "ROMAN"]:
            doc_lists = {"list1": {"listProperties": {"nestingLevels": [{"glyphType": glyph_type}]}}}
            assert _docs_list_nesting_is_ordered(doc_lists, "list1", 0) is True

    def test_nesting_levels_are_independent(self):
        # A list can be bulleted at one level and numbered at another --
        # each NestingLevel entry is its own glyph type.
        doc_lists = {"list1": {"listProperties": {"nestingLevels": [
            {"glyphSymbol": "●"}, {"glyphType": "DECIMAL"},
        ]}}}
        assert _docs_list_nesting_is_ordered(doc_lists, "list1", 0) is False
        assert _docs_list_nesting_is_ordered(doc_lists, "list1", 1) is True


class TestDocsListRendering:
    def test_flat_bullet_list(self):
        doc = {"body": {"content": [
            doc_list_para([doc_run("one\n")], "list1"),
            doc_list_para([doc_run("two\n")], "list1"),
        ]}, "lists": doc_bullet_list_map()}
        assert _docs_structure_to_markdown(doc) == "- one\n- two"

    def test_flat_numbered_list(self):
        # The literal digit doesn't matter -- Docs auto-numbers by list
        # position, not by what's in the Markdown source, and
        # _markdown_to_docs_requests' own regex (^\d+\. ) accepts any
        # digit(s) -- so every line renders "1. ", never "2. ", "3. ", ....
        doc = {"body": {"content": [
            doc_list_para([doc_run("one\n")], "list1"),
            doc_list_para([doc_run("two\n")], "list1"),
        ]}, "lists": doc_numbered_list_map()}
        assert _docs_structure_to_markdown(doc) == "1. one\n1. two"

    def test_nested_bullet_list_gets_two_spaces_per_level(self):
        doc = {"body": {"content": [
            doc_list_para([doc_run("top\n")], "list1", nesting_level=0),
            doc_list_para([doc_run("nested\n")], "list1", nesting_level=1),
        ]}, "lists": doc_bullet_list_map(levels=2)}
        assert _docs_structure_to_markdown(doc) == "- top\n  - nested"

    def test_missing_lists_map_defaults_every_list_paragraph_to_unordered(self):
        doc = {"body": {"content": [doc_list_para([doc_run("x\n")], "list1")]}}
        assert _docs_structure_to_markdown(doc) == "- x"

    def test_list_item_gets_inline_styles_applied(self):
        doc = {"body": {"content": [
            doc_list_para([doc_run("bold", bold=True), doc_run(" item\n")], "list1"),
        ]}, "lists": doc_bullet_list_map()}
        assert _docs_structure_to_markdown(doc) == "- **bold** item"

    def test_list_paragraph_ignores_any_heading_style(self):
        # A list paragraph never legitimately carries a namedStyleType, but
        # the bullet check comes first regardless of what's there.
        doc = {"body": {"content": [
            {"paragraph": {
                "elements": [{"textRun": doc_run("x\n")}],
                "bullet": {"listId": "list1", "nestingLevel": 0},
                "paragraphStyle": {"namedStyleType": "HEADING_1"},
            }},
        ]}, "lists": doc_bullet_list_map()}
        assert _docs_structure_to_markdown(doc) == "- x"

    def test_list_mixed_with_heading_and_plain_paragraphs(self):
        doc = {"body": {"content": [
            doc_para([doc_run("Title\n")], heading="HEADING_1"),
            doc_list_para([doc_run("item\n")], "list1"),
            doc_para([doc_run("after\n")]),
        ]}, "lists": doc_bullet_list_map()}
        assert _docs_structure_to_markdown(doc) == "# Title\n- item\nafter"

    def test_list_inside_a_table_cell_gets_the_doc_lists_map_too(self):
        # Regression guard: the table-cell recursion must thread doc_lists
        # through, or a list inside a cell would silently default to
        # unordered even when it's really numbered. As of phase 5 a
        # one-row table's row is the GFM header (see TestDocsTableToMarkdown
        # for dedicated multi-row table coverage).
        doc = {"body": {"content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [doc_list_para([doc_run("cell item\n")], "list1")]}]},
        ]}}]}, "lists": doc_numbered_list_map()}
        assert _docs_structure_to_markdown(doc) == "| 1. cell item |\n| --- |"

    def test_round_trips_through_markdown_to_docs_requests(self):
        doc = {"body": {"content": [
            doc_list_para([doc_run("top\n")], "list1", nesting_level=0),
            doc_list_para([doc_run("nested\n")], "list1", nesting_level=1),
        ]}, "lists": doc_bullet_list_map(levels=2)}
        markdown = _docs_structure_to_markdown(doc)
        requests = _markdown_to_docs_requests(markdown)
        assert requests[0]["insertText"]["text"] == "top\n\tnested\n"
        bullet_reqs = [r for r in requests if "createParagraphBullets" in r]
        assert len(bullet_reqs) == 2
        assert all(
            r["createParagraphBullets"]["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE"
            for r in bullet_reqs
        )


# ---------------------------------------------------------------------------- #
# _docs_table_column_alignment / _docs_table_to_markdown: Docs API tables ->
# GFM pipe tables
# ---------------------------------------------------------------------------- #

def doc_table_cell(runs: list[dict], alignment: str = "") -> dict:
    """One Docs API table cell, its content a single paragraph of
    doc_run()s, optionally with paragraph alignment -- e.g.
    doc_table_cell([doc_run("x\\n")], alignment="CENTER")."""
    return {"content": [doc_para(runs, alignment=alignment) if alignment else doc_para(runs)]}


def doc_table(rows: list[list[list[dict]]]) -> dict:
    """A Docs API table's inner ``{"tableRows": [...]}`` dict, from a
    3-level nested list -- rows, each a list of cells, each cell a list of
    doc_run()s for that cell's single paragraph. E.g.
    doc_table([[[doc_run("A\\n")], [doc_run("B\\n")]], [[doc_run("1\\n")], [doc_run("2\\n")]]])
    for a 2-row, 2-column table."""
    return {"tableRows": [
        {"tableCells": [doc_table_cell(cell_runs) for cell_runs in row]}
        for row in rows
    ]}


class TestDocsTableColumnAlignment:
    def test_defaults_to_start_with_no_alignment_set(self):
        table = doc_table([[[doc_run("x\n")]]])
        assert _docs_table_column_alignment(table, 0) == "START"

    def test_reads_center_and_end_from_the_header_row(self):
        table = {"tableRows": [
            {"tableCells": [doc_table_cell([doc_run("x\n")], "CENTER"), doc_table_cell([doc_run("y\n")], "END")]},
        ]}
        assert _docs_table_column_alignment(table, 0) == "CENTER"
        assert _docs_table_column_alignment(table, 1) == "END"

    def test_unsupported_alignment_defaults_to_start(self):
        table = {"tableRows": [{"tableCells": [doc_table_cell([doc_run("x\n")], "JUSTIFIED")]}]}
        assert _docs_table_column_alignment(table, 0) == "START"

    def test_no_rows_defaults_to_start(self):
        assert _docs_table_column_alignment({"tableRows": []}, 0) == "START"

    def test_column_index_beyond_the_row_defaults_to_start(self):
        table = doc_table([[[doc_run("x\n")]]])
        assert _docs_table_column_alignment(table, 5) == "START"


class TestDocsTableToMarkdown:
    def test_basic_grid(self):
        table = doc_table([
            [[doc_run("A\n")], [doc_run("B\n")]],
            [[doc_run("1\n")], [doc_run("2\n")]],
        ])
        assert _docs_table_to_markdown(table, {}) == "| A | B |\n| --- | --- |\n| 1 | 2 |"

    def test_header_row_bold_is_suppressed(self):
        # write_doc_rich_content always bolds row 0 on write
        # (_insert_table_at_placeholder) -- reading that bold back as
        # literal **...** would double it up on the next write.
        table = doc_table([[[doc_run("Head", bold=True), doc_run("\n")]]])
        assert _docs_table_to_markdown(table, {}) == "| Head |\n| --- |"

    def test_non_header_row_keeps_bold(self):
        table = doc_table([
            [[doc_run("Head\n")]],
            [[doc_run("bold", bold=True), doc_run("\n")]],
        ])
        assert _docs_table_to_markdown(table, {}) == "| Head |\n| --- |\n| **bold** |"

    def test_column_alignment_renders_as_separator_syntax(self):
        table = {"tableRows": [
            {"tableCells": [
                doc_table_cell([doc_run("L\n")], "START"),
                doc_table_cell([doc_run("C\n")], "CENTER"),
                doc_table_cell([doc_run("R\n")], "END"),
            ]},
        ]}
        assert _docs_table_to_markdown(table, {}) == "| L | C | R |\n| --- | :---: | ---: |"

    def test_literal_pipe_in_a_cell_is_escaped(self):
        table = doc_table([[[doc_run("a|b\n")]]])
        assert _docs_table_to_markdown(table, {}) == "| a\\|b |\n| --- |"

    def test_multi_paragraph_cell_joins_with_br(self):
        table = {"tableRows": [
            {"tableCells": [{"content": [doc_para([doc_run("line1\n")]), doc_para([doc_run("line2\n")])]}]},
        ]}
        assert _docs_table_to_markdown(table, {}) == "| line1<br>line2 |\n| --- |"

    def test_empty_cell_renders_as_blank(self):
        table = {"tableRows": [{"tableCells": [{"content": []}, doc_table_cell([doc_run("x\n")])]}]}
        assert _docs_table_to_markdown(table, {}) == "|  | x |\n| --- | --- |"

    def test_no_rows_renders_as_empty_string(self):
        assert _docs_table_to_markdown({"tableRows": []}, {}) == ""

    def test_list_inside_a_non_header_cell_uses_the_doc_lists_map(self):
        table = {"tableRows": [
            {"tableCells": [doc_table_cell([doc_run("Head\n")])]},
            {"tableCells": [{"content": [doc_list_para([doc_run("item\n")], "list1")]}]},
        ]}
        assert _docs_table_to_markdown(table, doc_numbered_list_map()) == "| Head |\n| --- |\n| 1. item |"

    def test_round_trips_through_extract_tables(self):
        table = doc_table([
            [[doc_run("A", bold=True), doc_run("\n")], [doc_run("B", bold=True), doc_run("\n")]],
            [[doc_run("1\n")], [doc_run("2\n")]],
        ])
        markdown = _docs_table_to_markdown(table, {})
        text, tables = _extract_tables(markdown)
        assert text == tables[0].placeholder
        assert len(tables) == 1
        # Header bold suppressed on read, so it round-trips as plain text
        # here -- write_doc_rich_content re-bolds row 0 fresh on its own.
        assert tables[0].rows == [["A", "B"], ["1", "2"]]


# ---------------------------------------------------------------------------- #
# _extract_tables / _table_cell_start_indices: Markdown tables -> Docs API
# ---------------------------------------------------------------------------- #

class TestExtractTables:
    def test_basic_table_replaced_with_placeholder(self):
        text, tables = _extract_tables("| A | B |\n| --- | --- |\n| 1 | 2 |")
        assert len(tables) == 1
        assert tables[0].rows == [["A", "B"], ["1", "2"]]
        assert tables[0].alignments == ["START", "START"]
        assert text == tables[0].placeholder

    def test_alignment_markers(self):
        _, tables = _extract_tables("| A | B | C |\n|:---|:---:|---:|\n| 1 | 2 | 3 |")
        assert tables[0].alignments == ["START", "CENTER", "END"]

    def test_short_and_long_rows_are_padded_and_truncated(self):
        _, tables = _extract_tables("| A | B |\n| --- | --- |\n| 1 |\n| 1 | 2 | 3 |")
        assert tables[0].rows == [["A", "B"], ["1", ""], ["1", "2"]]

    def test_escaped_pipe_in_cell_is_unescaped(self):
        _, tables = _extract_tables("| A |\n| --- |\n| a\\|b |")
        assert tables[0].rows == [["A"], ["a|b"]]

    def test_non_table_text_is_preserved_around_a_table(self):
        md = "Intro\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nOutro"
        text, tables = _extract_tables(md)
        assert text == f"Intro\n\n{tables[0].placeholder}\n\nOutro"

    def test_pipe_without_separator_row_is_not_a_table(self):
        md = "a | b\nnot a separator"
        text, tables = _extract_tables(md)
        assert tables == []
        assert text == md

    def test_multiple_tables_get_distinct_placeholders(self):
        md = "| A |\n| --- |\n| 1 |\n\ntext\n\n| B |\n| --- |\n| 2 |"
        text, tables = _extract_tables(md)
        assert len(tables) == 2
        assert tables[0].placeholder != tables[1].placeholder
        assert text.count(tables[0].placeholder) == 1
        assert text.count(tables[1].placeholder) == 1

    def test_no_table_returns_markdown_unchanged(self):
        assert _extract_tables("just some\nplain text") == ("just some\nplain text", [])


class TestTableCellStartIndices:
    def test_extracts_grid_of_cell_start_indices(self):
        doc = {
            "body": {
                "content": [
                    {
                        # Docs always inserts a newline immediately before a table, so a
                        # table requested at location.index=5 actually starts at 6 -- see
                        # _table_cell_start_indices' own docstring.
                        "startIndex": 6,
                        "table": {
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {"content": [{"startIndex": 7}]},
                                        {"content": [{"startIndex": 10}]},
                                    ]
                                }
                            ]
                        },
                    }
                ]
            }
        }
        assert _table_cell_start_indices(doc, 5) == [[7, 10]]

    def test_raises_when_table_not_found_at_index(self):
        with pytest.raises(DriveClientError, match="could not locate"):
            _table_cell_start_indices({"body": {"content": []}}, 5)

    def test_raises_when_cell_has_no_content(self):
        doc = {
            "body": {
                "content": [
                    {"startIndex": 6, "table": {"tableRows": [{"tableCells": [{"content": []}]}]}}
                ]
            }
        }
        with pytest.raises(DriveClientError, match="no content"):
            _table_cell_start_indices(doc, 5)


# ---------------------------------------------------------------------------- #
# write_doc_rich_content: end-index / delete-range calculation
# ---------------------------------------------------------------------------- #

class TestWriteDocRichContent:
    def test_empty_file_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.write_doc_rich_content("", "text")

    def test_empty_document_skips_delete_range(self, monkeypatch):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": []}
        }
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        client.write_doc_rich_content("f1", "hello")

        batch_kwargs = docs_service.documents.return_value.batchUpdate.call_args.kwargs
        requests = batch_kwargs["body"]["requests"]
        assert not any("deleteContentRange" in r for r in requests)

    def test_existing_document_content_is_deleted_first(self, monkeypatch):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": [{"endIndex": 42}]}
        }
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        client.write_doc_rich_content("f1", "hello")

        batch_kwargs = docs_service.documents.return_value.batchUpdate.call_args.kwargs
        requests = batch_kwargs["body"]["requests"]
        assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 41}}}
        # The leftover empty paragraph must have any residual bullet
        # cleared, or createParagraphBullets could silently merge the new
        # document's first list item into whatever list was there before.
        assert requests[1] == {"deleteParagraphBullets": {"range": {"startIndex": 1, "endIndex": 2}}}

    def test_empty_document_skips_bullet_clear_too(self, monkeypatch):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {
            "body": {"content": []}
        }
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        client.write_doc_rich_content("f1", "hello")

        batch_kwargs = docs_service.documents.return_value.batchUpdate.call_args.kwargs
        requests = batch_kwargs["body"]["requests"]
        assert not any("deleteParagraphBullets" in r for r in requests)

    def test_get_http_error_becomes_drive_client_error(self, monkeypatch):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = http_error(404)
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="write_doc_rich_content get"):
            client.write_doc_rich_content("f1", "hello")

    def test_batch_update_http_error_becomes_drive_client_error(self, monkeypatch):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {"body": {"content": []}}
        docs_service.documents.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="write_doc_rich_content batchUpdate"):
            client.write_doc_rich_content("f1", "hello")

    def test_markdown_without_a_table_makes_exactly_one_batch_update(self, monkeypatch):
        # Regression guard: the table code path must add zero extra
        # get()/batchUpdate() round trips when there's nothing to insert.
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = {"body": {"content": []}}
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        client.write_doc_rich_content("f1", "just text, no table here")

        assert docs_service.documents.return_value.get.return_value.execute.call_count == 1
        assert docs_service.documents.return_value.batchUpdate.call_count == 1

    def test_table_round_trips_through_placeholder_then_cells(self, monkeypatch):
        markdown = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        _, tables = _extract_tables(markdown)
        placeholder = tables[0].placeholder

        empty_doc = {"body": {"content": []}}
        doc_with_placeholder = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [
                        {"startIndex": 1, "textRun": {"content": placeholder + "\n"}}
                    ]}}
                ]
            }
        }
        doc_with_table = {
            "body": {
                "content": [
                    {
                        # Docs always inserts a newline immediately before a table, so a
                        # table requested at location.index=1 actually starts at 2.
                        "startIndex": 2,
                        "table": {
                            "tableRows": [
                                {"tableCells": [
                                    {"content": [{"startIndex": 3}]},
                                    {"content": [{"startIndex": 6}]},
                                ]},
                                {"tableCells": [
                                    {"content": [{"startIndex": 9}]},
                                    {"content": [{"startIndex": 12}]},
                                ]},
                            ]
                        },
                    }
                ]
            }
        }

        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            empty_doc, doc_with_placeholder, doc_with_table,
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        client.write_doc_rich_content("f1", markdown)

        calls = docs_service.documents.return_value.batchUpdate.call_args_list
        assert len(calls) == 3

        skeleton_requests = calls[0].kwargs["body"]["requests"]
        assert skeleton_requests == [
            {"insertText": {"location": {"index": 1}, "text": placeholder + "\n"}}
        ]

        structure_requests = calls[1].kwargs["body"]["requests"]
        assert structure_requests[0] == {
            "deleteContentRange": {"range": {"startIndex": 1, "endIndex": 1 + len(placeholder)}}
        }
        assert structure_requests[1] == {
            "insertTable": {"rows": 2, "columns": 2, "location": {"index": 1}}
        }

        fill_requests = calls[2].kwargs["body"]["requests"]
        insert_texts = [r["insertText"] for r in fill_requests if "insertText" in r]
        # filled from the last cell back to the first
        assert [it["location"]["index"] for it in insert_texts] == [12, 9, 6, 3]
        assert [it["text"] for it in insert_texts] == ["2\n", "1\n", "B\n", "A\n"]
        # header row (A, B) is bolded; body row (1, 2) is not
        bold_ranges = {
            (r["updateTextStyle"]["range"]["startIndex"])
            for r in fill_requests
            if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"].get("bold")
        }
        assert bold_ranges == {3, 6}

    def test_missing_placeholder_raises(self, monkeypatch):
        markdown = "| A |\n| --- |\n| 1 |"
        empty_doc = {"body": {"content": []}}
        doc_without_placeholder = {"body": {"content": []}}  # placeholder text never made it in
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            empty_doc, doc_without_placeholder,
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="expected exactly one placeholder"):
            client.write_doc_rich_content("f1", markdown)

    def test_table_shape_mismatch_raises(self, monkeypatch):
        markdown = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        _, tables = _extract_tables(markdown)
        placeholder = tables[0].placeholder

        empty_doc = {"body": {"content": []}}
        doc_with_placeholder = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [
                        {"startIndex": 1, "textRun": {"content": placeholder + "\n"}}
                    ]}}
                ]
            }
        }
        # Docs somehow returned a 1x1 table instead of the requested 2x2 grid.
        wrong_shape_doc = {
            "body": {
                "content": [
                    {"startIndex": 2, "table": {"tableRows": [
                        {"tableCells": [{"content": [{"startIndex": 3}]}]},
                    ]}},
                ]
            }
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            empty_doc, doc_with_placeholder, wrong_shape_doc,
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="did not match"):
            client.write_doc_rich_content("f1", markdown)

    def test_table_placeholder_lookup_http_error(self, monkeypatch):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            {"body": {"content": []}}, http_error(500),
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="write_doc_rich_content table lookup"):
            client.write_doc_rich_content("f1", "| A |\n| --- |\n| 1 |")

    def test_table_structure_batch_update_http_error(self, monkeypatch):
        markdown = "| A |\n| --- |\n| 1 |"
        _, tables = _extract_tables(markdown)
        placeholder = tables[0].placeholder
        doc_with_placeholder = {
            "body": {"content": [
                {"paragraph": {"elements": [
                    {"startIndex": 1, "textRun": {"content": placeholder + "\n"}}
                ]}}
            ]}
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            {"body": {"content": []}}, doc_with_placeholder,
        ]
        docs_service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            None, http_error(500),
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="write_doc_rich_content table insert"):
            client.write_doc_rich_content("f1", markdown)

    def test_table_second_lookup_http_error(self, monkeypatch):
        markdown = "| A |\n| --- |\n| 1 |"
        _, tables = _extract_tables(markdown)
        placeholder = tables[0].placeholder
        doc_with_placeholder = {
            "body": {"content": [
                {"paragraph": {"elements": [
                    {"startIndex": 1, "textRun": {"content": placeholder + "\n"}}
                ]}}
            ]}
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            {"body": {"content": []}}, doc_with_placeholder, http_error(500),
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="write_doc_rich_content table lookup"):
            client.write_doc_rich_content("f1", markdown)

    def test_table_fill_batch_update_http_error(self, monkeypatch):
        markdown = "| A |\n| --- |\n| 1 |"
        _, tables = _extract_tables(markdown)
        placeholder = tables[0].placeholder
        doc_with_placeholder = {
            "body": {"content": [
                {"paragraph": {"elements": [
                    {"startIndex": 1, "textRun": {"content": placeholder + "\n"}}
                ]}}
            ]}
        }
        doc_with_table = {
            "body": {"content": [
                {"startIndex": 2, "table": {"tableRows": [
                    {"tableCells": [{"content": [{"startIndex": 3}]}]},
                    {"tableCells": [{"content": [{"startIndex": 6}]}]},
                ]}},
            ]}
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            {"body": {"content": []}}, doc_with_placeholder, doc_with_table,
        ]
        docs_service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            None, None, http_error(500),
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="write_doc_rich_content table fill"):
            client.write_doc_rich_content("f1", markdown)

    def test_table_alignment_produces_paragraph_style_request(self, monkeypatch):
        markdown = "| A | B |\n| :--- | ---: |"  # header row only -> a 1x2 grid
        _, tables = _extract_tables(markdown)
        assert tables[0].alignments == ["START", "END"]
        placeholder = tables[0].placeholder
        doc_with_placeholder = {
            "body": {"content": [
                {"paragraph": {"elements": [
                    {"startIndex": 1, "textRun": {"content": placeholder + "\n"}}
                ]}}
            ]}
        }
        doc_with_table = {
            "body": {"content": [
                {"startIndex": 2, "table": {"tableRows": [
                    {"tableCells": [
                        {"content": [{"startIndex": 3}]},
                        {"content": [{"startIndex": 6}]},
                    ]},
                ]}},
            ]}
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            {"body": {"content": []}}, doc_with_placeholder, doc_with_table,
        ]
        monkeypatch.setattr(drive_client_module, "build", lambda *a, **kw: docs_service)
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        client.write_doc_rich_content("f1", markdown)

        fill_requests = docs_service.documents.return_value.batchUpdate.call_args_list[-1].kwargs["body"]["requests"]
        alignment_reqs = [r["updateParagraphStyle"] for r in fill_requests if "updateParagraphStyle" in r]
        assert len(alignment_reqs) == 1
        assert alignment_reqs[0]["paragraphStyle"] == {"alignment": "END"}
        assert alignment_reqs[0]["range"] == {"startIndex": 6, "endIndex": 7}


# ---------------------------------------------------------------------------- #
# resolve_download_name / resolve_download_destination: path-traversal
# sanitization + export-extension resolution
# ---------------------------------------------------------------------------- #

def make_file(**overrides) -> DriveFile:
    defaults = dict(id="f1", name="report.pdf", mime_type="application/pdf", size=100)
    defaults.update(overrides)
    return DriveFile(**defaults)


class TestResolveDownloadName:
    def test_non_workspace_file_keeps_its_name(self):
        assert resolve_download_name(make_file(name="report.pdf", mime_type="application/pdf")) == "report.pdf"

    def test_google_doc_gets_txt_extension(self):
        f = make_file(name="MyDoc", mime_type="application/vnd.google-apps.document")
        assert resolve_download_name(f) == "MyDoc.txt"

    def test_google_sheet_gets_csv_extension(self):
        f = make_file(name="Budget", mime_type="application/vnd.google-apps.spreadsheet")
        assert resolve_download_name(f) == "Budget.csv"

    def test_does_not_double_up_an_existing_extension(self):
        f = make_file(name="MyDoc.txt", mime_type="application/vnd.google-apps.document")
        assert resolve_download_name(f) == "MyDoc.txt"

    def test_falls_back_to_file_id_when_name_is_empty(self):
        assert resolve_download_name(make_file(name="", mime_type="application/pdf")) == "f1"


class TestResolveDownloadDestination:
    def test_joins_basename_with_destination_dir(self, tmp_path):
        result = resolve_download_destination(make_file(name="report.pdf"), str(tmp_path))
        assert result == str(tmp_path / "report.pdf")

    def test_strips_directory_traversal_from_file_name(self, tmp_path):
        # A Drive file can be renamed to anything, including path separators --
        # this must never be able to write outside destination_dir.
        result = resolve_download_destination(make_file(name="../../.ssh/authorized_keys"), str(tmp_path))
        assert result == str(tmp_path / "authorized_keys")

    def test_strips_absolute_path_prefix_from_file_name(self, tmp_path):
        result = resolve_download_destination(make_file(name="/etc/passwd"), str(tmp_path))
        assert result == str(tmp_path / "passwd")

    def test_empty_destination_dir_raises(self):
        with pytest.raises(DriveClientError, match="destination_dir"):
            resolve_download_destination(make_file(name="report.pdf"), "")

    def test_whitespace_only_destination_dir_raises(self):
        with pytest.raises(DriveClientError, match="destination_dir"):
            resolve_download_destination(make_file(name="report.pdf"), "   ")


# ---------------------------------------------------------------------------- #
# download_file: URL selection (export vs raw media) + streaming
# ---------------------------------------------------------------------------- #

class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], headers: dict | None = None):
        self._chunks = chunks
        self.headers = headers or {}
    def raise_for_status(self):
        pass
    def iter_content(self, chunk_size):
        yield from self._chunks
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class TestDownloadFile:
    def test_empty_file_id_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.download_file("")

    def test_downloads_binary_file_via_raw_media_url(self, tmp_path, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "image.png", "mimeType": "image/png",
        }
        client = make_client(service)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        captured_urls = []
        fake_session = MagicMock()
        fake_session.get.side_effect = lambda url, stream: (captured_urls.append(url), _FakeStreamResponse([b"data"]))[1]
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        result = client.download_file("f1", destination_dir=str(tmp_path))

        assert result["name"] == "image.png"
        assert result["size_bytes"] == 4
        assert os.path.exists(os.path.join(str(tmp_path), "image.png"))
        assert "alt=media" in captured_urls[0]

    def test_google_doc_downloads_via_export_url_and_gets_txt_extension(self, tmp_path, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "MyDoc", "mimeType": "application/vnd.google-apps.document",
        }
        client = make_client(service)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        captured_urls = []
        fake_session = MagicMock()
        fake_session.get.side_effect = lambda url, stream: (captured_urls.append(url), _FakeStreamResponse([b"exported"]))[1]
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        result = client.download_file("f1", destination_dir=str(tmp_path))

        assert result["name"] == "MyDoc.txt"
        assert "export" in captured_urls[0]
        assert os.path.exists(os.path.join(str(tmp_path), "MyDoc.txt"))

    def test_raises_when_destination_dir_omitted(self, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "f.bin", "mimeType": "application/octet-stream",
        }
        client = make_client(service)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        with pytest.raises(DriveClientError, match="destination_dir"):
            client.download_file("f1")

    def test_streaming_failure_becomes_drive_client_error(self, tmp_path, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "f.bin", "mimeType": "application/octet-stream",
        }
        client = make_client(service)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("connection reset")
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        with pytest.raises(DriveClientError, match="download_file"):
            client.download_file("f1", destination_dir=str(tmp_path))

    def test_sanitizes_file_name_before_writing(self, tmp_path, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "../../evil.bin", "mimeType": "application/octet-stream",
        }
        client = make_client(service)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        fake_session = MagicMock()
        fake_session.get.return_value = _FakeStreamResponse([b"data"])
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        result = client.download_file("f1", destination_dir=str(tmp_path))

        assert result["path"] == str(tmp_path / "evil.bin")
        assert result["name"] == "evil.bin"
        assert os.path.exists(tmp_path / "evil.bin")


# ---------------------------------------------------------------------------- #
# fetch_thumbnail
# ---------------------------------------------------------------------------- #

class TestFetchThumbnail:
    def test_empty_thumbnail_link_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty thumbnail_link"):
            client.fetch_thumbnail("")

    def test_fetches_and_returns_bytes_and_mime_type(self, monkeypatch):
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        captured = {}
        fake_session = MagicMock()

        def fake_get(url, stream):
            captured["url"] = url
            captured["stream"] = stream
            return _FakeStreamResponse(
                [b"\x89PNG", b"", b"restofimage"],
                headers={"Content-Type": "image/png; charset=binary"},
            )

        fake_session.get.side_effect = fake_get
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        result = client.fetch_thumbnail("https://signed.example/thumb")

        assert result == {"data": b"\x89PNGrestofimage", "mime_type": "image/png"}
        assert captured["url"] == "https://signed.example/thumb"
        assert captured["stream"] is True

    def test_defaults_mime_type_when_no_content_type_header(self, monkeypatch):
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        fake_session = MagicMock()
        fake_session.get.return_value = _FakeStreamResponse([b"data"])
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        result = client.fetch_thumbnail("https://signed.example/thumb")

        assert result["mime_type"] == "image/jpeg"

    def test_raises_when_response_exceeds_max_bytes(self, monkeypatch):
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        fake_session = MagicMock()
        fake_session.get.return_value = _FakeStreamResponse([b"x" * 10])
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        with pytest.raises(DriveClientError, match="exceeded"):
            client.fetch_thumbnail("https://signed.example/thumb", max_bytes=5)

    def test_network_failure_becomes_drive_client_error(self, monkeypatch):
        client = make_client(MagicMock())
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("connection reset")
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        with pytest.raises(DriveClientError, match="fetch_thumbnail failed"):
            client.fetch_thumbnail("https://signed.example/thumb")


# ---------------------------------------------------------------------------- #
# Sheets API helpers: _col_letters_to_index / _parse_a1_range / _hex_to_rgb_dict
# ---------------------------------------------------------------------------- #

class TestColLettersToIndex:
    @pytest.mark.parametrize("letters,expected", [
        ("A", 0), ("B", 1), ("Z", 25), ("AA", 26), ("AB", 27), ("AZ", 51), ("BA", 52),
    ])
    def test_converts_a1_column_letters_to_zero_based_index(self, letters, expected):
        assert _col_letters_to_index(letters) == expected

    def test_lowercase_letters_accepted(self):
        assert _col_letters_to_index("a") == 0


class TestParseA1Range:
    def test_parses_fully_bounded_range(self):
        assert _parse_a1_range("A1:C10") == {
            "startRowIndex": 0, "endRowIndex": 10, "startColumnIndex": 0, "endColumnIndex": 3,
        }

    def test_out_of_order_corners_are_normalized(self):
        assert _parse_a1_range("C10:A1") == {
            "startRowIndex": 0, "endRowIndex": 10, "startColumnIndex": 0, "endColumnIndex": 3,
        }

    def test_single_cell_range(self):
        assert _parse_a1_range("B2:B2") == {
            "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 1, "endColumnIndex": 2,
        }

    def test_whitespace_stripped(self):
        assert _parse_a1_range("  A1:C10  ") == _parse_a1_range("A1:C10")

    def test_sheet_name_prefix_rejected(self):
        with pytest.raises(DriveClientError, match="Unsupported range syntax"):
            _parse_a1_range("Sheet1!A1:C10")

    def test_open_ended_range_rejected(self):
        with pytest.raises(DriveClientError, match="Unsupported range syntax"):
            _parse_a1_range("A:C")

    def test_single_cell_no_colon_rejected(self):
        with pytest.raises(DriveClientError, match="Unsupported range syntax"):
            _parse_a1_range("A1")


class TestHexToRgbDict:
    def test_converts_with_hash_prefix(self):
        assert _hex_to_rgb_dict("#ffcc00") == pytest.approx({"red": 1.0, "green": 0.8, "blue": 0.0}, abs=1e-6)

    def test_converts_without_hash_prefix(self):
        assert _hex_to_rgb_dict("000000") == {"red": 0.0, "green": 0.0, "blue": 0.0}

    def test_white(self):
        assert _hex_to_rgb_dict("#ffffff") == {"red": 1.0, "green": 1.0, "blue": 1.0}

    def test_wrong_length_raises(self):
        with pytest.raises(DriveClientError, match="Invalid hex color"):
            _hex_to_rgb_dict("#fff")

    def test_non_hex_characters_raise(self):
        with pytest.raises(DriveClientError, match="Invalid hex color"):
            _hex_to_rgb_dict("#zzzzzz")


class TestRgbDictToHex:
    def test_round_trips_through_hex_to_rgb_dict(self):
        for hex_color in ["#ffcc00", "#000000", "#ffffff", "#b6d7a8", "#123456"]:
            assert _rgb_dict_to_hex(_hex_to_rgb_dict(hex_color)) == hex_color

    def test_missing_channel_keys_default_to_zero(self):
        # Docs/Sheets API JSON (proto3) omits a zero-valued channel key
        # entirely rather than sending 0.0 -- {} means black, not an error.
        assert _rgb_dict_to_hex({}) == "#000000"
        assert _rgb_dict_to_hex({"red": 1.0}) == "#ff0000"


# ---------------------------------------------------------------------------- #
# get_credentials
# ---------------------------------------------------------------------------- #

class TestGetCredentials:
    def test_exposes_loaded_credentials(self):
        client = make_client(MagicMock())
        sentinel_creds = object()
        client._load_credentials = lambda: sentinel_creds
        assert client.get_credentials() is sentinel_creds


# ---------------------------------------------------------------------------- #
# create_spreadsheet
# ---------------------------------------------------------------------------- #

class TestCreateSpreadsheet:
    def test_requires_non_empty_name(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty name"):
            client.create_spreadsheet("   ")

    def test_creates_with_default_single_sheet(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet1", "properties": {"title": "Budget"}, "spreadsheetUrl": "https://x",
        }
        client = make_client_with_sheets(sheets_service)

        result = client.create_spreadsheet("Budget")

        body = sheets_service.spreadsheets.return_value.create.call_args.kwargs["body"]
        assert body == {"properties": {"title": "Budget"}}
        assert result == {"id": "sheet1", "name": "Budget", "web_view_link": "https://x"}

    def test_creates_with_named_tabs(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet1", "properties": {"title": "Budget"},
        }
        client = make_client_with_sheets(sheets_service)

        client.create_spreadsheet("Budget", sheet_titles=["Q1", "Q2"])

        body = sheets_service.spreadsheets.return_value.create.call_args.kwargs["body"]
        assert body["sheets"] == [{"properties": {"title": "Q1"}}, {"properties": {"title": "Q2"}}]

    def test_moves_to_parent_folder_when_given(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet1", "properties": {"title": "Budget"},
        }
        drive_service = MagicMock()
        drive_service.files.return_value.get.return_value.execute.return_value = {"parents": ["old"]}
        drive_service.files.return_value.update.return_value.execute.return_value = {"id": "sheet1"}

        client = make_client_with_sheets(sheets_service)
        client._local.service = drive_service

        client.create_spreadsheet("Budget", parent_folder_id="folder1")

        update_kwargs = drive_service.files.return_value.update.call_args.kwargs
        assert update_kwargs["addParents"] == "folder1"

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.create.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="create_spreadsheet"):
            client.create_spreadsheet("Budget")


# ---------------------------------------------------------------------------- #
# list_sheets
# ---------------------------------------------------------------------------- #

class TestListSheets:
    def test_requires_spreadsheet_id(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty spreadsheet_id"):
            client.list_sheets("")

    def test_maps_tab_metadata(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{
                "properties": {
                    "sheetId": 0, "title": "Sheet1", "index": 0, "hidden": False,
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                }
            }]
        }
        client = make_client_with_sheets(sheets_service)

        sheets = client.list_sheets("sheet1")

        assert sheets == [{
            "sheet_id": 0, "title": "Sheet1", "index": 0,
            "row_count": 1000, "column_count": 26, "hidden": False,
        }]

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="list_sheets"):
            client.list_sheets("sheet1")


# ---------------------------------------------------------------------------- #
# get_sheet_values / write_sheet_values
# ---------------------------------------------------------------------------- #

class TestGetSheetValues:
    def test_requires_spreadsheet_id_and_range(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and range"):
            client.get_sheet_values("", "A1:B2")
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and range"):
            client.get_sheet_values("sheet1", "")

    def test_returns_values(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["a", "b"], ["1", "2"]]
        }
        client = make_client_with_sheets(sheets_service)

        values = client.get_sheet_values("sheet1", "Sheet1!A1:B2")

        assert values == [["a", "b"], ["1", "2"]]

    def test_no_values_key_yields_empty_list(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)
        assert client.get_sheet_values("sheet1", "A1:B2") == []

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="get_sheet_values"):
            client.get_sheet_values("sheet1", "A1:B2")

    def test_defaults_to_formatted_value_render_option(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["$1.00"]]
        }
        client = make_client_with_sheets(sheets_service)

        client.get_sheet_values("sheet1", "Sheet1!A1")

        sheets_service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet1", range="Sheet1!A1", valueRenderOption="FORMATTED_VALUE"
        )

    def test_formula_render_option_returns_formula_text(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["=A1+A2"]]
        }
        client = make_client_with_sheets(sheets_service)

        values = client.get_sheet_values("sheet1", "Sheet1!A1", value_render_option="formula")

        assert values == [["=A1+A2"]]
        sheets_service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet1", range="Sheet1!A1", valueRenderOption="FORMULA"
        )

    def test_rejects_invalid_render_option(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="Invalid value_render_option"):
            client.get_sheet_values("sheet1", "A1:B2", value_render_option="RAW")


class TestGetSheetFormatting:
    def test_requires_spreadsheet_id_and_range(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and range"):
            client.get_sheet_formatting("", "A1:B2")
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and range"):
            client.get_sheet_formatting("sheet1", "")

    def test_summarizes_only_non_default_formatting(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{
                "data": [{
                    "rowData": [
                        {"values": [
                            {"userEnteredFormat": {
                                "textFormat": {"bold": True, "foregroundColor": {"red": 1}},
                                "backgroundColor": {"green": 1},
                                "numberFormat": {"type": "NUMBER", "pattern": "0.00%"},
                                "horizontalAlignment": "CENTER",
                                "verticalAlignment": "MIDDLE",
                                "wrapStrategy": "WRAP",
                            }},
                            {"userEnteredFormat": {}},
                        ]},
                    ]
                }]
            }]
        }
        client = make_client_with_sheets(sheets_service)

        grid = client.get_sheet_formatting("sheet1", "Sheet1!A1:B1")

        assert grid == [[
            {
                "bold": True,
                "text_color": "#ff0000",
                "background_color": "#00ff00",
                "number_format": "0.00%",
                "horizontal_alignment": "CENTER",
                "vertical_alignment": "MIDDLE",
                "wrap_strategy": "WRAP",
            },
            {},
        ]]
        sheets_service.spreadsheets.return_value.get.assert_called_once_with(
            spreadsheetId="sheet1",
            ranges=["Sheet1!A1:B1"],
            fields=(
                "sheets.data.rowData.values.userEnteredFormat"
                "(textFormat(bold,italic,foregroundColor),backgroundColor,numberFormat,"
                "horizontalAlignment,verticalAlignment,wrapStrategy)"
            ),
        )

    def test_no_sheets_key_yields_empty_grid(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)
        assert client.get_sheet_formatting("sheet1", "A1:B2") == []

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="get_sheet_formatting"):
            client.get_sheet_formatting("sheet1", "A1:B2")


class TestWriteSheetValues:
    def test_requires_spreadsheet_id_and_range(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and range"):
            client.write_sheet_values("", "A1:B2", [["a"]])

    def test_writes_with_default_user_entered_option(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {
            "updatedRange": "Sheet1!A1:B2", "updatedCells": 4,
        }
        client = make_client_with_sheets(sheets_service)

        result = client.write_sheet_values("sheet1", "A1:B2", [["a", "b"], ["1", "2"]])

        call_kwargs = sheets_service.spreadsheets.return_value.values.return_value.update.call_args.kwargs
        assert call_kwargs["valueInputOption"] == "USER_ENTERED"
        assert call_kwargs["body"] == {"values": [["a", "b"], ["1", "2"]]}
        assert result == {"spreadsheet_id": "sheet1", "updated_range": "Sheet1!A1:B2", "updated_cells": 4}

    def test_custom_value_input_option_passed_through(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.write_sheet_values("sheet1", "A1:B2", [["a"]], value_input_option="RAW")

        call_kwargs = sheets_service.spreadsheets.return_value.values.return_value.update.call_args.kwargs
        assert call_kwargs["valueInputOption"] == "RAW"

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.update.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="write_sheet_values"):
            client.write_sheet_values("sheet1", "A1:B2", [["a"]])


# ---------------------------------------------------------------------------- #
# add_sheet / rename_sheet
# ---------------------------------------------------------------------------- #

class TestAddSheet:
    def test_requires_spreadsheet_id_and_title(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and a non-empty title"):
            client.add_sheet("", "Q3")
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and a non-empty title"):
            client.add_sheet("sheet1", "   ")

    def test_adds_tab_with_grid_properties(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 5, "title": "Q3", "index": 1}}}]
        }
        client = make_client_with_sheets(sheets_service)

        result = client.add_sheet("sheet1", "Q3", rows=100, cols=10)

        batch_kwargs = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs
        request = batch_kwargs["body"]["requests"][0]["addSheet"]
        assert request["properties"]["gridProperties"] == {"rowCount": 100, "columnCount": 10}
        assert result == {"sheet_id": 5, "title": "Q3", "index": 1}

    def test_non_positive_rows_cols_clamped_to_one(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 5, "title": "Q3"}}}]
        }
        client = make_client_with_sheets(sheets_service)

        client.add_sheet("sheet1", "Q3", rows=0, cols=-5)

        request = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
        assert request["addSheet"]["properties"]["gridProperties"] == {"rowCount": 1, "columnCount": 1}

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="add_sheet"):
            client.add_sheet("sheet1", "Q3")


class TestRenameSheet:
    def test_requires_spreadsheet_id_and_new_title(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and a non-empty new_title"):
            client.rename_sheet("", 0, "New")
        with pytest.raises(DriveClientError, match="requires spreadsheet_id and a non-empty new_title"):
            client.rename_sheet("sheet1", 0, "  ")

    def test_renames_via_batch_update(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        result = client.rename_sheet("sheet1", 5, "Renamed")

        request = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
        assert request == {
            "updateSheetProperties": {"properties": {"sheetId": 5, "title": "Renamed"}, "fields": "title"}
        }
        assert result == {"spreadsheet_id": "sheet1", "sheet_id": 5, "title": "Renamed"}

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="rename_sheet"):
            client.rename_sheet("sheet1", 5, "Renamed")


# ---------------------------------------------------------------------------- #
# format_sheet_range: every parameter is opt-in
# ---------------------------------------------------------------------------- #

class TestFormatSheetRange:
    def test_requires_spreadsheet_id(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty spreadsheet_id"):
            client.format_sheet_range("", 0, "A1:B2")

    def test_no_options_given_sends_no_requests(self):
        sheets_service = MagicMock()
        client = make_client_with_sheets(sheets_service)

        result = client.format_sheet_range("sheet1", 0, "A1:B2")

        sheets_service.spreadsheets.return_value.batchUpdate.assert_not_called()
        assert result == {"spreadsheet_id": "sheet1", "sheet_id": 0, "requests_applied": 0}

    def test_bold_and_italic_only_touch_text_format_fields(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", bold="true", italic="false")

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        repeat_cell = requests[0]["repeatCell"]
        assert repeat_cell["cell"]["userEnteredFormat"]["textFormat"] == {"bold": True, "italic": False}
        assert "userEnteredFormat.textFormat(bold,italic)" in repeat_cell["fields"]

    def test_background_and_text_color_converted_to_rgb(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", background_color="#ffcc00", text_color="#000000")

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        cell_format = requests[0]["repeatCell"]["cell"]["userEnteredFormat"]
        assert cell_format["backgroundColor"] == _hex_to_rgb_dict("#ffcc00")
        assert cell_format["textFormat"]["foregroundColor"] == _hex_to_rgb_dict("#000000")

    def test_number_format_and_alignment(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", number_format="0.00%", horizontal_alignment="center")

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        cell_format = requests[0]["repeatCell"]["cell"]["userEnteredFormat"]
        assert cell_format["numberFormat"] == {"type": "NUMBER", "pattern": "0.00%"}
        assert cell_format["horizontalAlignment"] == "CENTER"

    def test_vertical_alignment_and_wrap_strategy(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", vertical_alignment="middle", wrap_strategy="wrap")

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        repeat_cell = requests[0]["repeatCell"]
        cell_format = repeat_cell["cell"]["userEnteredFormat"]
        assert cell_format["verticalAlignment"] == "MIDDLE"
        assert cell_format["wrapStrategy"] == "WRAP"
        assert "userEnteredFormat.verticalAlignment" in repeat_cell["fields"]
        assert "userEnteredFormat.wrapStrategy" in repeat_cell["fields"]

    @pytest.mark.parametrize(
        "wrap_strategy,expected",
        [("overflow_cell", "OVERFLOW_CELL"), ("clip", "CLIP"), ("wrap", "WRAP")],
    )
    def test_wrap_strategy_variants(self, wrap_strategy, expected):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", wrap_strategy=wrap_strategy)

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        cell_format = requests[0]["repeatCell"]["cell"]["userEnteredFormat"]
        assert cell_format["wrapStrategy"] == expected

    @pytest.mark.parametrize(
        "vertical_alignment,expected",
        [("top", "TOP"), ("middle", "MIDDLE"), ("bottom", "BOTTOM")],
    )
    def test_vertical_alignment_variants(self, vertical_alignment, expected):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", vertical_alignment=vertical_alignment)

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        cell_format = requests[0]["repeatCell"]["cell"]["userEnteredFormat"]
        assert cell_format["verticalAlignment"] == expected

    def test_column_width_produces_update_dimension_request(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:C10", column_width=120)

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        dim_request = next(r["updateDimensionProperties"] for r in requests if "updateDimensionProperties" in r)
        assert dim_request["range"] == {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 3}
        assert dim_request["properties"] == {"pixelSize": 120}

    def test_freeze_rows_and_cols_produce_update_sheet_properties_request(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", freeze_rows=1, freeze_cols=2)

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        freeze_request = next(r["updateSheetProperties"] for r in requests if "updateSheetProperties" in r)
        assert freeze_request["properties"]["gridProperties"] == {"frozenRowCount": 1, "frozenColumnCount": 2}
        assert set(freeze_request["fields"].split(",")) == {"gridProperties.frozenRowCount", "gridProperties.frozenColumnCount"}

    def test_freeze_zero_unfreezes(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", freeze_rows=0)

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        freeze_request = next(r["updateSheetProperties"] for r in requests if "updateSheetProperties" in r)
        assert freeze_request["properties"]["gridProperties"] == {"frozenRowCount": 0}

    def test_merge_none_unmerges(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", merge_type="none")

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        assert requests[0] == {"unmergeCells": {"range": {"sheetId": 0, **_parse_a1_range("A1:B2")}}}

    @pytest.mark.parametrize("merge_type", ["MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"])
    def test_merge_variants(self, merge_type):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.format_sheet_range("sheet1", 0, "A1:B2", merge_type=merge_type)

        requests = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        assert requests[0]["mergeCells"]["mergeType"] == merge_type

    def test_merge_keep_default_produces_no_merge_request(self):
        sheets_service = MagicMock()
        client = make_client_with_sheets(sheets_service)

        result = client.format_sheet_range("sheet1", 0, "A1:B2")

        assert result["requests_applied"] == 0

    def test_invalid_merge_type_raises(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="invalid merge_type"):
            client.format_sheet_range("sheet1", 0, "A1:B2", merge_type="BOGUS")

    def test_multiple_options_combine_into_multiple_requests(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        result = client.format_sheet_range(
            "sheet1", 0, "A1:B2", bold="true", column_width=100, freeze_rows=1, merge_type="MERGE_ALL",
        )

        assert result["requests_applied"] == 4

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="format_sheet_range"):
            client.format_sheet_range("sheet1", 0, "A1:B2", bold="true")


# ---------------------------------------------------------------------------- #
# insert_dimensions / delete_dimensions
# ---------------------------------------------------------------------------- #

class TestInsertDimensions:
    def test_requires_spreadsheet_id(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty spreadsheet_id"):
            client.insert_dimensions("", 0, "ROWS", 0, 1)

    def test_invalid_dimension_raises(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="ROWS.*COLUMNS"):
            client.insert_dimensions("sheet1", 0, "CELLS", 0, 1)

    def test_count_below_one_raises(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="count >= 1"):
            client.insert_dimensions("sheet1", 0, "ROWS", 0, 0)

    def test_inserts_via_batch_update(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        result = client.insert_dimensions("sheet1", 5, "ROWS", 2, 3, inherit_from_before=False)

        request = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
        assert request == {
            "insertDimension": {
                "range": {"sheetId": 5, "dimension": "ROWS", "startIndex": 2, "endIndex": 5},
                "inheritFromBefore": False,
            }
        }
        assert result == {"spreadsheet_id": "sheet1", "sheet_id": 5, "dimension": "ROWS", "inserted": 3}

    def test_inherit_from_before_defaults_true(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        client.insert_dimensions("sheet1", 0, "COLUMNS", 0, 1)

        request = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
        assert request["insertDimension"]["inheritFromBefore"] is True

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="insert_dimensions"):
            client.insert_dimensions("sheet1", 0, "ROWS", 0, 1)


class TestDeleteDimensions:
    def test_requires_spreadsheet_id(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty spreadsheet_id"):
            client.delete_dimensions("", 0, "ROWS", 0, 1)

    def test_invalid_dimension_raises(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="ROWS.*COLUMNS"):
            client.delete_dimensions("sheet1", 0, "CELLS", 0, 1)

    def test_count_below_one_raises(self):
        client = make_client_with_sheets(MagicMock())
        with pytest.raises(DriveClientError, match="count >= 1"):
            client.delete_dimensions("sheet1", 0, "COLUMNS", 0, 0)

    def test_deletes_via_batch_update(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_sheets(sheets_service)

        result = client.delete_dimensions("sheet1", 5, "COLUMNS", 1, 2)

        request = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
        assert request == {
            "deleteDimension": {
                "range": {"sheetId": 5, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 3},
            }
        }
        assert result == {"spreadsheet_id": "sheet1", "sheet_id": 5, "dimension": "COLUMNS", "deleted": 2}

    def test_http_error_becomes_drive_client_error(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_sheets(sheets_service)
        with pytest.raises(DriveClientError, match="delete_dimensions"):
            client.delete_dimensions("sheet1", 0, "ROWS", 0, 1)


# ---------------------------------------------------------------------------- #
# _docs_plain_text_with_index_map / _offset_to_docs_index / _find_text_matches
# ---------------------------------------------------------------------------- #

class TestDocsPlainTextIndexMap:
    def test_single_paragraph_maps_contiguously(self):
        doc = make_doc("hello world")
        plain_text, runs = _docs_plain_text_with_index_map(doc)

        assert plain_text == "hello world\n"
        assert runs == [(0, 12, 1)]

    def test_multiple_paragraphs_offsets_accumulate(self):
        doc = make_doc("first", "second")
        plain_text, runs = _docs_plain_text_with_index_map(doc)

        assert plain_text == "first\nsecond\n"
        # "first\n" is 6 chars at docs index 1; "second\n" starts right after
        assert runs == [(0, 6, 1), (6, 13, 7)]

    def test_elements_without_text_run_are_skipped(self):
        doc = {"body": {"content": [{"startIndex": 1, "endIndex": 5, "table": {}}]}}
        plain_text, runs = _docs_plain_text_with_index_map(doc)
        assert plain_text == ""
        assert runs == []


class TestOffsetToDocsIndex:
    def test_maps_offset_within_a_run(self):
        runs = [(0, 6, 1), (6, 13, 7)]
        assert _offset_to_docs_index(0, runs) == 1
        assert _offset_to_docs_index(3, runs) == 4
        assert _offset_to_docs_index(6, runs) == 7  # boundary: start of next run
        assert _offset_to_docs_index(13, runs) == 14  # end of last run

    def test_unmappable_offset_raises(self):
        with pytest.raises(DriveClientError, match="Could not map text offset"):
            _offset_to_docs_index(99, [(0, 6, 1)])


class TestFindTextMatches:
    def test_no_match_returns_empty(self):
        assert _find_text_matches("hello world", "xyz") == []

    def test_single_match(self):
        assert _find_text_matches("hello world", "world") == [(6, 11)]

    def test_multiple_non_overlapping_matches(self):
        assert _find_text_matches("aXaXa", "aX") == [(0, 2), (2, 4)]

    def test_overlapping_pattern_only_matches_non_overlapping(self):
        # "aaa" contains "aa" at [0,2) and then continues searching from
        # index 2, so the overlapping match at [1,3) is never reported --
        # same behavior as str.find in a loop, not a lookahead regex.
        assert _find_text_matches("aaa", "aa") == [(0, 2)]


# ---------------------------------------------------------------------------- #
# edit_doc_content: find/replace with uniqueness-or-replace_all semantics
# ---------------------------------------------------------------------------- #

class TestEditDocContent:
    def test_requires_file_id(self):
        client = make_client_with_docs(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.edit_doc_content("", "x", "y")

    def test_requires_find_text(self):
        client = make_client_with_docs(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty find_text"):
            client.edit_doc_content("f1", "", "y")

    def test_not_found_raises(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="not found"):
            client.edit_doc_content("f1", "missing", "new")

    def test_ambiguous_match_without_replace_all_raises(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("cat cat cat")
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="matches 3 locations"):
            client.edit_doc_content("f1", "cat", "dog")

    def test_unique_match_replaces_span_only(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        result = client.edit_doc_content("f1", "world", "there")

        requests = docs_service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        # "hello world\n" -> "world" spans docs indices [7, 12)
        assert requests[0] == {"deleteContentRange": {"range": {"startIndex": 7, "endIndex": 12}}}
        assert requests[1]["insertText"]["location"]["index"] == 7
        assert requests[1]["insertText"]["text"] == "there\n"
        assert result == {"file_id": "f1", "occurrences_replaced": 1}

    def test_replace_all_processes_matches_from_last_to_first(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("cat cat")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        result = client.edit_doc_content("f1", "cat", "dog", replace_all=True)

        requests = docs_service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        delete_starts = [r["deleteContentRange"]["range"]["startIndex"] for r in requests if "deleteContentRange" in r]
        # Second occurrence ("cat" at offset 4, docs index 5) is deleted
        # before the first ("cat" at offset 0, docs index 1) -- otherwise the
        # first edit would shift the second match's precomputed indices.
        assert delete_starts == [5, 1]
        assert result == {"file_id": "f1", "occurrences_replaced": 2}

    def test_get_http_error_becomes_drive_client_error(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="edit_doc_content get"):
            client.edit_doc_content("f1", "x", "y")

    def test_batch_update_http_error_becomes_drive_client_error(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        docs_service.documents.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="edit_doc_content batchUpdate"):
            client.edit_doc_content("f1", "world", "there")

    def test_replace_markdown_without_a_table_makes_exactly_one_batch_update(self):
        # Regression guard mirroring write_doc_rich_content's own: the table
        # code path must add zero extra get()/batchUpdate() round trips when
        # replace_markdown has nothing to insert as a table.
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        client.edit_doc_content("f1", "world", "there")

        assert docs_service.documents.return_value.get.return_value.execute.call_count == 1
        assert docs_service.documents.return_value.batchUpdate.call_count == 1

    def test_replace_markdown_table_round_trips_through_placeholder_then_cells(self):
        # A GFM table in replace_markdown must land as a real Docs table, not
        # as the raw pipe-table text -- this is the bug being fixed: previously
        # edit_doc_content's replace_markdown skipped table extraction
        # entirely and the Markdown source ended up literally in the document.
        markdown = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        _, tables = _extract_tables(markdown, placeholder_prefix="PRIVACYFENCE_TABLE_PLACEHOLDER_M0_")
        placeholder = tables[0].placeholder

        doc = make_doc("before PLACEHOLDER after")  # "PLACEHOLDER" -> docs indices [8, 19)
        doc_with_placeholder = {
            "body": {
                "content": [
                    {"paragraph": {"elements": [
                        {"startIndex": 8, "textRun": {"content": placeholder + "\n"}}
                    ]}}
                ]
            }
        }
        doc_with_table = {
            "body": {
                "content": [
                    {
                        # Docs inserts a newline immediately before a table, so a
                        # table requested at location.index=8 actually starts at 9.
                        "startIndex": 9,
                        "table": {
                            "tableRows": [
                                {"tableCells": [
                                    {"content": [{"startIndex": 10}]},
                                    {"content": [{"startIndex": 13}]},
                                ]},
                                {"tableCells": [
                                    {"content": [{"startIndex": 16}]},
                                    {"content": [{"startIndex": 19}]},
                                ]},
                            ]
                        },
                    }
                ]
            }
        }
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            doc, doc_with_placeholder, doc_with_table,
        ]
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        result = client.edit_doc_content("f1", "PLACEHOLDER", markdown)

        calls = docs_service.documents.return_value.batchUpdate.call_args_list
        assert len(calls) == 3

        edit_requests = calls[0].kwargs["body"]["requests"]
        assert edit_requests == [
            {"deleteContentRange": {"range": {"startIndex": 8, "endIndex": 19}}},
            {"insertText": {"location": {"index": 8}, "text": placeholder + "\n"}},
        ]

        structure_requests = calls[1].kwargs["body"]["requests"]
        assert structure_requests[0] == {
            "deleteContentRange": {"range": {"startIndex": 8, "endIndex": 8 + len(placeholder)}}
        }
        assert structure_requests[1] == {
            "insertTable": {"rows": 2, "columns": 2, "location": {"index": 8}}
        }

        fill_requests = calls[2].kwargs["body"]["requests"]
        insert_texts = [r["insertText"] for r in fill_requests if "insertText" in r]
        assert [it["location"]["index"] for it in insert_texts] == [19, 16, 13, 10]
        assert [it["text"] for it in insert_texts] == ["2\n", "1\n", "B\n", "A\n"]

        assert result == {"file_id": "f1", "occurrences_replaced": 1}

    def test_replace_all_table_gets_unique_placeholder_per_occurrence(self, monkeypatch):
        # Under replace_all, every match gets the same replace_markdown --
        # without a per-occurrence placeholder prefix, two inserted tables'
        # placeholders would be identical text at two document locations, and
        # _insert_table_at_placeholder's single-match lookup couldn't tell
        # them apart.
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("cat", "cat")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        inserted = []
        monkeypatch.setattr(
            client,
            "_insert_table_at_placeholder",
            lambda docs_svc, file_id, table, caller="write_doc_rich_content": inserted.append(
                (table.placeholder, caller)
            ),
        )

        client.edit_doc_content("f1", "cat", "| A |\n| --- |\n| 1 |", replace_all=True)

        assert sorted(p for p, _ in inserted) == [
            "PRIVACYFENCE_TABLE_PLACEHOLDER_M0_0",
            "PRIVACYFENCE_TABLE_PLACEHOLDER_M1_0",
        ]
        assert all(caller == "edit_doc_content" for _, caller in inserted)

    def test_table_error_messages_use_edit_doc_content_prefix(self):
        # _insert_table_at_placeholder's errors are shared with
        # write_doc_rich_content; edit_doc_content must pass its own name
        # through so a failure is traceable to the tool call that caused it.
        markdown = "| A |\n| --- |\n| 1 |"
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = [
            make_doc("before PLACEHOLDER after"),
            {"body": {"content": []}},  # placeholder never made it into the document
        ]
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="edit_doc_content: expected exactly one placeholder"):
            client.edit_doc_content("f1", "PLACEHOLDER", markdown)


# ---------------------------------------------------------------------------- #
# format_doc_content: opt-in styling located by find_text
# ---------------------------------------------------------------------------- #

class TestFormatDocContent:
    def test_requires_file_id(self):
        client = make_client_with_docs(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty file_id"):
            client.format_doc_content("", "x")

    def test_requires_find_text(self):
        client = make_client_with_docs(MagicMock())
        with pytest.raises(DriveClientError, match="non-empty find_text"):
            client.format_doc_content("f1", "")

    def test_no_options_given_skips_document_fetch(self):
        docs_service = MagicMock()
        client = make_client_with_docs(docs_service)

        result = client.format_doc_content("f1", "world")

        docs_service.documents.return_value.get.assert_not_called()
        assert result == {"file_id": "f1", "occurrences_formatted": 0}

    def test_not_found_raises(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="not found"):
            client.format_doc_content("f1", "missing", bold="true")

    def test_ambiguous_match_without_replace_all_raises(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("cat cat cat")
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="matches 3 locations"):
            client.format_doc_content("f1", "cat", bold="true")

    def test_bold_and_italic_only_touch_text_style_fields(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        client.format_doc_content("f1", "world", bold="true", italic="false")

        requests = docs_service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        style = requests[0]["updateTextStyle"]
        assert style["textStyle"] == {"bold": True, "italic": False}
        assert set(style["fields"].split(",")) == {"bold", "italic"}
        assert style["range"] == {"startIndex": 7, "endIndex": 12}

    def test_highlight_color_and_text_color_converted_to_rgb(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        client.format_doc_content("f1", "world", highlight_color="#fff59d", text_color="#000000")

        requests = docs_service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        text_style = requests[0]["updateTextStyle"]["textStyle"]
        assert text_style["backgroundColor"]["color"]["rgbColor"] == _hex_to_rgb_dict("#fff59d")
        assert text_style["foregroundColor"]["color"]["rgbColor"] == _hex_to_rgb_dict("#000000")

    def test_replace_all_formats_every_occurrence(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("cat cat")
        docs_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
        client = make_client_with_docs(docs_service)

        result = client.format_doc_content("f1", "cat", bold="true", replace_all=True)

        requests = docs_service.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        assert len(requests) == 2
        assert result == {"file_id": "f1", "occurrences_formatted": 2}

    def test_get_http_error_becomes_drive_client_error(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="format_doc_content get"):
            client.format_doc_content("f1", "x", bold="true")

    def test_batch_update_http_error_becomes_drive_client_error(self):
        docs_service = MagicMock()
        docs_service.documents.return_value.get.return_value.execute.return_value = make_doc("hello world")
        docs_service.documents.return_value.batchUpdate.return_value.execute.side_effect = http_error(400)
        client = make_client_with_docs(docs_service)

        with pytest.raises(DriveClientError, match="format_doc_content batchUpdate"):
            client.format_doc_content("f1", "world", bold="true")


# ---------------------------------------------------------------------------- #
# _get_service / _get_sheets_service: must not share one service (and its
# underlying httplib2 transport) across threads, since concurrent requests
# dispatched via asyncio.to_thread corrupt a shared connection
# (SSL: WRONG_VERSION_NUMBER).
# ---------------------------------------------------------------------------- #

class TestServiceIsThreadLocal:
    def test_each_thread_gets_its_own_service_instance(self):
        client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.drive_client.build") as mock_build, \
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
        client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.drive_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()
            assert client._get_service() is client._get_service()
            assert mock_build.call_count == 1

    def test_sheets_service_is_also_thread_local(self):
        client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.drive_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()

            services: dict[int, object] = {}

            def worker(idx: int) -> None:
                services[idx] = client._get_sheets_service()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len({id(s) for s in services.values()}) == 5


class TestLiveFixtureParsing:
    """Replays a fixture recorded from the real QA Sandbox folder by
    scripts/qa_fixture_recorder.py --record drive -- real API shape, not
    hand-authored, with owner identity already redacted. Skipped (not
    failed) until that fixture exists; see tests/fixtures/live/README.md
    and docs/testing-policy.md. Re-record via
    that script if this ever starts failing after a genuine Drive API
    change.
    """

    def test_get_file_metadata_fixture_still_parses(self):
        path = LIVE_FIXTURES_DIR / "get_file_metadata.json"
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record drive` locally first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))

        drive_file = DriveClient._parse_file(raw)

        assert drive_file.id and drive_file.name
