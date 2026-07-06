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
import os
from unittest.mock import MagicMock

import pytest

from privacyfence import drive_client as drive_client_module
from privacyfence.drive_client import (
    DriveClient,
    DriveClientError,
    DriveFile,
    _markdown_to_docs_requests,
    _parse_inline_runs,
)
from googleapiclient.errors import HttpError


def make_client(service: MagicMock) -> DriveClient:
    client = DriveClient(client_config={}, token_file="/tmp/unused-token.json")
    client._service = service
    return client


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
        assert _parse_inline_runs("hello world") == [("hello world", False, False, "")]

    def test_bold(self):
        assert _parse_inline_runs("**bold**") == [("bold", True, False, "")]

    def test_italic(self):
        assert _parse_inline_runs("*italic*") == [("italic", False, True, "")]

    def test_bold_italic(self):
        assert _parse_inline_runs("***both***") == [("both", True, True, "")]

    def test_code_has_no_style(self):
        assert _parse_inline_runs("`code`") == [("code", False, False, "")]

    def test_link(self):
        assert _parse_inline_runs("[text](http://x.com)") == [("text", False, False, "http://x.com")]

    def test_mixed_runs_preserve_order_and_plain_gaps(self):
        runs = _parse_inline_runs("hello **bold** world")
        assert runs == [
            ("hello ", False, False, ""),
            ("bold", True, False, ""),
            (" world", False, False, ""),
        ]

    def test_empty_string_yields_single_empty_run(self):
        assert _parse_inline_runs("") == [("", False, False, "")]


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
