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
    _col_letters_to_index,
    _docs_plain_text_with_index_map,
    _find_text_matches,
    _hex_to_rgb_dict,
    _markdown_to_docs_requests,
    _offset_to_docs_index,
    _parse_a1_range,
    _parse_inline_runs,
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
            "driveId": "d1",
        }
        f = DriveClient._parse_file(raw)
        assert f == DriveFile(
            id="f1", name="doc.txt", mime_type="text/plain", size=1234,
            created_time="c", modified_time="m", owners=["a@x.com", "b@x.com"],
            shared=True, web_view_link="https://x", parent_ids=["p1", "p2"], drive_id="d1",
        )

    def test_missing_fields_default_sensibly(self):
        f = DriveClient._parse_file({})
        assert f == DriveFile(id="", name="", mime_type="", size=0)
        assert f.short_summary() == "(unnamed) ()"

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

    def test_google_doc_is_exported_as_text_plain(self, monkeypatch):
        monkeypatch.setattr(drive_client_module, "MediaIoBaseDownload", fake_downloader_class([b"exported text"]))
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
        }
        client = make_client(service)

        content = client.get_file_content("f1")

        assert content.content_text == "exported text"
        assert content.content_bytes == b""
        assert content.truncated is False
        service.files.return_value.export_media.assert_called_once_with(fileId="f1", mimeType="text/plain")

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
        assert _parse_inline_runs("hello world") == [("hello world", False, False, "", False)]

    def test_bold(self):
        assert _parse_inline_runs("**bold**") == [("bold", True, False, "", False)]

    def test_italic(self):
        assert _parse_inline_runs("*italic*") == [("italic", False, True, "", False)]

    def test_bold_italic(self):
        assert _parse_inline_runs("***both***") == [("both", True, True, "", False)]

    def test_highlight(self):
        assert _parse_inline_runs("==flagged==") == [("flagged", False, False, "", True)]

    def test_code_has_no_style(self):
        assert _parse_inline_runs("`code`") == [("code", False, False, "", False)]

    def test_link(self):
        assert _parse_inline_runs("[text](http://x.com)") == [("text", False, False, "http://x.com", False)]

    def test_mixed_runs_preserve_order_and_plain_gaps(self):
        runs = _parse_inline_runs("hello **bold** world")
        assert runs == [
            ("hello ", False, False, "", False),
            ("bold", True, False, "", False),
            (" world", False, False, "", False),
        ]

    def test_empty_string_yields_single_empty_run(self):
        assert _parse_inline_runs("") == [("", False, False, "", False)]


class TestMarkdownToDocsRequests:
    def test_empty_markdown_yields_no_requests(self):
        assert _markdown_to_docs_requests("") == []
        assert _markdown_to_docs_requests("\n\n") == []

    def test_plain_paragraph_only_inserts_text(self):
        requests = _markdown_to_docs_requests("just text")
        assert requests == [{"insertText": {"location": {"index": 1}, "text": "just text\n"}}]

    def test_heading_levels_map_to_named_styles(self):
        for prefix, style in [("# ", "HEADING_1"), ("## ", "HEADING_2"), ("### ", "HEADING_3"), ("#### ", "HEADING_4")]:
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
        assert not any("updateParagraphStyle" in r for r in requests)
        assert not any("createParagraphBullets" in r for r in requests)


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


# ---------------------------------------------------------------------------- #
# download_file: URL selection (export vs raw media) + streaming
# ---------------------------------------------------------------------------- #

class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
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

    def test_defaults_to_downloads_directory_when_no_destination_given(self, tmp_path, monkeypatch):
        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "f1", "name": "f.bin", "mimeType": "application/octet-stream",
        }
        client = make_client(service)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~/Downloads" else p)

        fake_session = MagicMock()
        fake_session.get.return_value = _FakeStreamResponse([b"x"])
        monkeypatch.setattr(drive_client_module, "AuthorizedSession", lambda creds: fake_session)

        result = client.download_file("f1")
        assert result["path"] == os.path.join(str(tmp_path), "f.bin")

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
    and docs/external-api-contract-testing.md's Part A/B. Re-record via
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
