"""Unit tests for privacyfence.connectors.drive.DriveConnector.

Same approach as test_gmail_connector.py: DriveClient is mocked and
gate.gated_call is stubbed to capture what's sent into the gate. Also
covers a real bug found while writing these tests: _get_file_content read
`content.text` (an attribute that doesn't exist on DriveFileContent --
only `.content_text`/`.content_bytes` do), so `getattr(..., "text", None)`
always returned None and every call fell through to `str(content)`, the
dataclass repr, both in the details popup and in the data actually
returned to Claude on approval. Fixed in connectors/drive.py; the
regression tests below pin the corrected behavior.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import drive as drive_module
from privacyfence.connectors.drive import DriveConnector
from privacyfence.drive_client import DriveClientError, DriveFile, DriveFileContent


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = DriveConnector(client)
    connector.my_email = my_email
    return connector, client


def make_file(**overrides):
    defaults = dict(
        id="f1", name="Q3 Report.gdoc", mime_type="application/vnd.google-apps.document",
        size=2048, modified_time="2026-07-01T00:00:00Z", owners=["alice@example.com"],
    )
    defaults.update(overrides)
    return DriveFile(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(drive_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Drive tool"):
            await connector.call("drive_does_not_exist", {})


class TestAutoTools:
    async def test_list_files_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_files.return_value = [make_file()]

        result = await connector.call("drive_list_files", {"query": "report", "max_results": 5})

        assert result == [make_file()]
        client.list_files.assert_called_once_with("report", 5)
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_get_file_metadata_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()

        result = await connector.call("drive_get_file_metadata", {"file_id": "f1"})

        assert result.id == "f1"

    async def test_list_folder_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_folder.return_value = [make_file()]

        result = await connector.call("drive_list_folder", {"folder_id": "folder1"})

        assert result == [make_file()]
        client.list_folder.assert_called_once_with("folder1", 50)

    async def test_list_shared_drives_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_shared_drives.return_value = [{"id": "d1", "name": "Team Drive"}]

        result = await connector.call("drive_list_shared_drives", {})

        assert result == [{"id": "d1", "name": "Team Drive"}]

    async def test_create_blank_file_tracks_session_created_id(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.create_blank_file.return_value = {"id": "newfile1"}

        result = await connector.call(
            "drive_create_blank_file", {"name": "notes", "mime_type": "text/plain"}
        )

        assert result == {"id": "newfile1"}
        assert "newfile1" in connector.session_created_ids


class TestGetFileContentBugFix:
    async def test_text_content_extracted_correctly(self, gated_call_spy):
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(), content_text="The actual report contents.")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        kwargs = gated_call_spy[0]
        assert "The actual report contents." in kwargs["details_text"]
        assert kwargs["filtered_data"] == {"file_id": "f1", "content": "The actual report contents."}
        # Must not fall back to the dataclass repr.
        assert "DriveFileContent(" not in kwargs["details_text"]
        assert "DriveFileContent(" not in str(kwargs["filtered_data"])

    async def test_binary_content_gets_placeholder_not_repr(self, gated_call_spy):
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(mime_type="image/png"), content_bytes=b"\x89PNG\r\n...")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        kwargs = gated_call_spy[0]
        assert "binary content" in kwargs["details_text"]
        assert "drive_download_file" in kwargs["details_text"]
        assert "DriveFileContent(" not in kwargs["details_text"]

    async def test_empty_content_gets_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        content = DriveFileContent(file=make_file())
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        assert "(no content)" in gated_call_spy[0]["details_text"]

    async def test_preview_contains_only_metadata(self, gated_call_spy):
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(), content_text="secret content body")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "File": "Q3 Report.gdoc", "Owner": "alice@example.com",
            "Size": "2048", "Modified": "2026-07-01T00:00:00Z",
        }
        assert "secret content body" not in str(kwargs["preview"])
        assert kwargs["gate"] == "review"
        assert kwargs["raw_data"] is content
        assert kwargs["args"] == {"file_id": "f1"}
        # The auto-accept i_am_owner rule reads raw_data.file.owners -- the
        # wrapper shape must be preserved, not unwrapped.
        assert kwargs["raw_data"].file.owners == ["alice@example.com"]


class TestDownloadFile:
    async def test_download_file_preview_and_args(self, gated_call_spy):
        connector, client = make_connector()
        client.download_file.return_value = {"name": "Q3 Report.pdf", "path": "/tmp/Q3 Report.pdf", "size_bytes": 4096}
        client.get_file_metadata.return_value = make_file(name="Q3 Report.pdf")

        result = await connector.call("drive_download_file", {"file_id": "f1", "destination_dir": "/tmp"})

        assert result == {"name": "Q3 Report.pdf", "path": "/tmp/Q3 Report.pdf", "size_bytes": 4096}
        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Saved to"] == "/tmp/Q3 Report.pdf"
        assert kwargs["args"] == {"file_id": "f1", "destination_dir": "/tmp"}


class TestWriteToolsGateAndPreview:
    async def test_write_file_content_preview_excludes_full_content(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="notes.txt")
        client.write_file_content.return_value = {"ok": True}

        long_content = "line one\n" * 500
        await connector.call("drive_write_file_content", {"file_id": "f1", "content": long_content})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"] == {"File": "notes.txt", "Owner": "alice@example.com"}
        assert kwargs["details_text"] == long_content
        assert kwargs["args"] == {"file_id": "f1"}

    async def test_write_doc_content_gate_popup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.write_doc_rich_content.return_value = {"ok": True}

        await connector.call("drive_write_doc_content", {"file_id": "f1", "markdown": "# Title\n\nBody"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "# Title\n\nBody"

    async def test_move_file_preview_shows_destination(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.move_file.return_value = {"ok": True}

        await connector.call("drive_move_file", {"file_id": "f1", "destination_folder_id": "folderB"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["Move to folder"] == "folderB"
        assert kwargs["args"] == {"file_id": "f1", "destination_folder_id": "folderB"}

    async def test_add_comment_gate_popup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.add_comment.return_value = {"ok": True}

        await connector.call("drive_add_comment", {"file_id": "f1", "comment": "Looks good"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "Looks good"


class TestUploadFile:
    async def test_requires_exactly_one_of_local_path_or_content_base64(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="exactly one"):
            await connector.call("drive_upload_file", {})
        with pytest.raises(ValueError, match="exactly one"):
            await connector.call(
                "drive_upload_file", {"local_path": "/tmp/x.txt", "content_base64": "aGk="}
            )

    async def test_local_path_upload_computes_size_and_tracks_session_id(self, tmp_path, gated_call_spy):
        connector, client = make_connector()
        f = tmp_path / "photo.png"
        f.write_bytes(b"x" * 1234)
        client.upload_file.return_value = {"id": "uploaded1"}

        result = await connector.call("drive_upload_file", {"local_path": str(f)})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["File"] == "photo.png"
        assert kwargs["preview"]["Size"] == "1,234 bytes"
        assert result == {"id": "uploaded1"}
        assert "uploaded1" in connector.session_created_ids

    async def test_content_base64_upload_computes_decoded_size(self, gated_call_spy):
        import base64
        connector, client = make_connector()
        client.upload_file.return_value = {"id": "uploaded2"}
        payload = base64.b64encode(b"hello world").decode()

        await connector.call(
            "drive_upload_file", {"content_base64": payload, "name": "greeting.txt"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Size"] == f"{len(b'hello world'):,} bytes"

    async def test_invalid_base64_does_not_crash_size_becomes_zero(self, gated_call_spy):
        connector, client = make_connector()
        client.upload_file.return_value = {"id": "uploaded3"}

        await connector.call(
            "drive_upload_file", {"content_base64": "not valid base64!!", "name": "x"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Size"] == "0 bytes"


class TestFetchErrorMapping:
    async def test_drive_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_files.side_effect = DriveClientError("quota exceeded")

        with pytest.raises(RuntimeError, match="quota exceeded"):
            await connector.call("drive_list_files", {"query": "q"})
