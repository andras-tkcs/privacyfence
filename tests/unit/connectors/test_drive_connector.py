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

import json
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import drive as drive_module
from privacyfence.connectors.drive import DriveConnector
from privacyfence.drive_client import DriveClientError, DriveFile, DriveFileContent
from privacyfence.privacy_filter import init_privacy_filter

from ...helpers import assert_all_tools_leave_an_audit_trail


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

    async def test_pii_scan_text_is_content_only_not_owner(self, gated_call_spy):
        # owner defaults to an email address, present on every file
        # regardless of content -- the PII scan must not see it.
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(), content_text="nothing sensitive")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "nothing sensitive"
        assert kwargs["preview"]["Owner"] == "alice@example.com"  # still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]


class TestDrivePrivacyFilter:
    """drive_privacy.categories, enforced -- see privacy_filter.py. Without
    calling init_privacy_filter (every other test class here), every
    category resolves to "allow" and behaves exactly as before this
    existed; these tests are the ones that actually turn a policy on."""

    async def test_file_content_blocked_replaces_details_and_filtered_data(self, gated_call_spy):
        init_privacy_filter({"drive_privacy": {"categories": {"file_content": "block"}}})
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(), content_text="the actual confidential text")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        kwargs = gated_call_spy[0]
        assert "the actual confidential text" not in kwargs["details_text"]
        assert kwargs["filtered_data"] == {"file_id": "f1", "content": "[BLOCKED BY PRIVACY FILTER]"}

    async def test_file_metadata_blocked_replaces_preview_and_get_file_metadata_result(self, gated_call_spy, tmp_path):
        init_audit_logger(str(tmp_path))
        init_privacy_filter({"drive_privacy": {"categories": {"file_metadata": "block"}}})
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(), content_text="fine to read")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["File"] == "[BLOCKED BY PRIVACY FILTER]"
        assert kwargs["filtered_data"]["content"] == "fine to read"  # file_content is a separate category

        client.get_file_metadata.return_value = make_file()
        result = await connector.call("drive_get_file_metadata", {"file_id": "f1"})
        assert result == {"id": "f1"}

    async def test_file_list_blocked_empties_auto_accepted_result(self, tmp_path):
        init_audit_logger(str(tmp_path))
        init_privacy_filter({"drive_privacy": {"categories": {"file_list": "block"}}})
        connector, client = make_connector()
        client.list_files.return_value = [make_file()]

        result = await connector.call("drive_list_files", {"query": "report", "max_results": 5})

        assert result == []

    async def test_folder_structure_blocked_empties_auto_accepted_result(self, tmp_path):
        init_audit_logger(str(tmp_path))
        init_privacy_filter({"drive_privacy": {"categories": {"folder_structure": "block"}}})
        connector, client = make_connector()
        client.list_folder.return_value = [make_file()]

        result = await connector.call("drive_list_folder", {"folder_id": "folder1"})

        assert result == []

    async def test_sheets_file_content_blocked_empties_values(self, gated_call_spy):
        init_privacy_filter({"drive_privacy": {"categories": {"file_content": "block"}}})
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget.gsheet")
        client.get_sheet_values.return_value = [["salary", "100000"]]

        result = await connector.call(
            "drive_sheets_get_values", {"spreadsheet_id": "f1", "range_a1": "A1:B1"}
        )

        assert result == []
        assert "100000" not in gated_call_spy[0]["details_text"]

    async def test_allow_is_the_default_when_unconfigured(self, gated_call_spy):
        # No init_privacy_filter call in this test -- conftest's autouse
        # reset leaves _GROUPS empty, which must resolve to "allow", not
        # "block" -- this module must never fail closed on missing config.
        connector, client = make_connector()
        content = DriveFileContent(file=make_file(), content_text="business as usual")
        client.get_file_content.return_value = content

        await connector.call("drive_get_file_content", {"file_id": "f1"})

        assert gated_call_spy[0]["filtered_data"]["content"] == "business as usual"


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
        assert kwargs["pii_scan_text"] == ""  # no content involved, nothing to scan


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

    async def test_move_file_preview_shows_destination_folder_name(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.side_effect = [
            make_file(),  # source file
            make_file(id="folderB", name="Archive", mime_type="application/vnd.google-apps.folder"),
        ]
        client.move_file.return_value = {"ok": True}

        await connector.call("drive_move_file", {"file_id": "f1", "destination_folder_id": "folderB"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["Move to folder"] == "Archive"
        assert kwargs["details_text"] == "File will be moved to the new folder; its content is unchanged."
        assert kwargs["args"] == {"file_id": "f1", "destination_folder_id": "folderB"}

    async def test_move_file_falls_back_to_raw_folder_id_when_lookup_fails(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.side_effect = [make_file(), DriveClientError("not found")]
        client.move_file.return_value = {"ok": True}

        await connector.call("drive_move_file", {"file_id": "f1", "destination_folder_id": "folderB"})

        assert gated_call_spy[0]["preview"]["Move to folder"] == "folderB"

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


class TestParseJsonHelpers:
    def test_parse_json_str_list_valid(self):
        assert drive_module._parse_json_str_list('["a", "b"]') == ["a", "b"]

    def test_parse_json_str_list_empty_string_yields_none(self):
        assert drive_module._parse_json_str_list("") is None
        assert drive_module._parse_json_str_list("   ") is None

    def test_parse_json_str_list_invalid_json_yields_none(self):
        assert drive_module._parse_json_str_list("not json") is None

    def test_parse_json_str_list_non_list_yields_none(self):
        assert drive_module._parse_json_str_list('{"a": 1}') is None

    def test_parse_json_str_list_non_string_elements_yield_none(self):
        assert drive_module._parse_json_str_list("[1, 2]") is None

    def test_parse_json_2d_list_valid(self):
        assert drive_module._parse_json_2d_list('[["a", "b"], ["c", "d"]]') == [["a", "b"], ["c", "d"]]

    def test_parse_json_2d_list_invalid_json_yields_none(self):
        assert drive_module._parse_json_2d_list("not json") is None

    def test_parse_json_2d_list_non_list_yields_none(self):
        assert drive_module._parse_json_2d_list('{"a": 1}') is None


class TestSheetsAutoTools:
    async def test_sheets_create_tracks_session_id_and_parses_titles(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.create_spreadsheet.return_value = {"id": "sheet1", "name": "Budget"}

        result = await connector.call(
            "drive_sheets_create", {"name": "Budget", "sheet_titles": '["Q1", "Q2"]'}
        )

        client.create_spreadsheet.assert_called_once_with("Budget", ["Q1", "Q2"], "")
        assert result == {"id": "sheet1", "name": "Budget"}
        assert "sheet1" in connector.session_created_ids
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_sheets_create_no_titles_passes_none(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.create_spreadsheet.return_value = {"id": "sheet1"}

        await connector.call("drive_sheets_create", {"name": "Budget"})

        client.create_spreadsheet.assert_called_once_with("Budget", None, "")

    async def test_sheets_create_no_id_in_result_not_tracked(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.create_spreadsheet.return_value = {}

        await connector.call("drive_sheets_create", {"name": "Budget"})

        assert connector.session_created_ids == set()

    async def test_sheets_get_metadata_auto_accepts(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_sheets.return_value = [{"sheet_id": 0, "title": "Sheet1"}]

        result = await connector.call("drive_sheets_get_metadata", {"spreadsheet_id": "sheet1"})

        assert result == [{"sheet_id": 0, "title": "Sheet1"}]
        client.list_sheets.assert_called_once_with("sheet1")
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]


class TestSheetsGatedTools:
    async def test_get_values_gate_is_review_with_metadata_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.get_sheet_values.return_value = [["a", "b"], ["1", "2"]]

        result = await connector.call(
            "drive_sheets_get_values", {"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"] == {"Spreadsheet": "Budget", "Owner": "alice@example.com", "Range": "Sheet1!A1:B2"}
        assert kwargs["filtered_data"] == [["a", "b"], ["1", "2"]]
        assert result == [["a", "b"], ["1", "2"]]
        assert kwargs["pii_scan_text"] == "a, b\n1, 2"  # rows only, not the "Owner: alice@example.com" header

    async def test_get_values_no_owner_shows_unknown(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(owners=[])
        client.get_sheet_values.return_value = []

        await connector.call("drive_sheets_get_values", {"spreadsheet_id": "sheet1", "range_a1": "A1:B2"})

        assert gated_call_spy[0]["preview"]["Owner"] == "(unknown)"

    async def test_write_range_gate_popup_and_valid_json(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.write_sheet_values.return_value = {"updated_cells": 4}

        result = await connector.call(
            "drive_sheets_write_range",
            {"spreadsheet_id": "sheet1", "range_a1": "A1:B2", "values": '[["a","b"],["1","2"]]'},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"] == {"Spreadsheet": "Budget", "Owner": "alice@example.com", "Range": "A1:B2"}
        # Details show the parsed values formatted like the read path does
        # (comma-joined per row), not the raw unparsed JSON string argument.
        # Spreadsheet/Range are already in preview, not repeated here.
        assert kwargs["details_text"] == "a, b\n1, 2"
        client.write_sheet_values.assert_called_once_with("sheet1", "A1:B2", [["a", "b"], ["1", "2"]])
        assert result == {"updated_cells": 4}

    async def test_write_range_details_truncates_long_row_lists(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.write_sheet_values.return_value = {"updated_cells": 51}
        values = [[str(i)] for i in range(51)]

        await connector.call(
            "drive_sheets_write_range",
            {"spreadsheet_id": "sheet1", "range_a1": "A1:A51", "values": json.dumps(values)},
        )

        details = gated_call_spy[0]["details_text"]
        assert "… and 1 more row(s)" in details
        assert "49" in details  # last of the 50 shown rows (index 49)
        assert "50" not in details.split("… and")[0]  # the 51st row (index 50) is truncated away

    async def test_write_range_invalid_json_raises_before_gating(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()

        with pytest.raises(ValueError, match="JSON 2D array"):
            await connector.call(
                "drive_sheets_write_range",
                {"spreadsheet_id": "sheet1", "range_a1": "A1:B2", "values": "not json"},
            )

    async def test_add_sheet_gate_popup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.add_sheet.return_value = {"sheet_id": 5, "title": "Q3"}

        result = await connector.call(
            "drive_sheets_add_sheet", {"spreadsheet_id": "sheet1", "title": "Q3", "rows": 10, "cols": 5}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["New tab"] == "Q3"
        client.add_sheet.assert_called_once_with("sheet1", "Q3", 10, 5)
        assert result == {"sheet_id": 5, "title": "Q3"}

    async def test_rename_sheet_gate_popup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.rename_sheet.return_value = {"sheet_id": 5, "title": "Renamed"}

        result = await connector.call(
            "drive_sheets_rename_sheet", {"spreadsheet_id": "sheet1", "sheet_id": 5, "new_title": "Renamed"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["Tab id"] == 5
        assert kwargs["preview"]["New title"] == "Renamed"
        client.rename_sheet.assert_called_once_with("sheet1", 5, "Renamed")
        assert result == {"sheet_id": 5, "title": "Renamed"}

    async def test_format_range_gate_popup_summarizes_applied_changes(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.format_sheet_range.return_value = {"requests_applied": 2}

        result = await connector.call(
            "drive_sheets_format_range",
            {
                "spreadsheet_id": "sheet1", "sheet_id": 0, "range_a1": "A1:B2",
                "bold": "true", "background_color": "#ffcc00",
            },
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert "bold=true" in kwargs["preview"]["Format"]
        assert "background=#ffcc00" in kwargs["preview"]["Format"]
        client.format_sheet_range.assert_called_once_with(
            "sheet1", 0, "A1:B2", "true", "", "#ffcc00", "", "", "", -1, -1, -1, "KEEP"
        )
        assert result == {"requests_applied": 2}

    async def test_format_range_no_changes_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.format_sheet_range.return_value = {"requests_applied": 0}

        await connector.call(
            "drive_sheets_format_range", {"spreadsheet_id": "sheet1", "sheet_id": 0, "range_a1": "A1:B2"}
        )

        assert gated_call_spy[0]["preview"]["Format"] == "(no changes)"

    async def test_format_range_summary_covers_every_remaining_option(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.format_sheet_range.return_value = {"requests_applied": 8}

        await connector.call(
            "drive_sheets_format_range",
            {
                "spreadsheet_id": "sheet1", "sheet_id": 0, "range_a1": "A1:B2",
                "italic": "true", "text_color": "#000000", "number_format": "0.00%",
                "horizontal_alignment": "center", "freeze_rows": 1, "freeze_cols": 2,
                "column_width": 100, "merge_type": "MERGE_ALL",
            },
        )

        summary = gated_call_spy[0]["preview"]["Format"]
        for expected in (
            "italic=true", "text_color=#000000", "number_format=0.00%", "align=center",
            "freeze_rows=1", "freeze_cols=2", "column_width=100px", "merge=MERGE_ALL",
        ):
            assert expected in summary

    async def test_format_range_bad_syntax_rejected_before_gate(self, gated_call_spy):
        # format_sheet_range() only discovers bad A1 syntax once it's already
        # past the approval popup -- this must be caught earlier so a doomed
        # call never costs the user an approval decision.
        connector, client = make_connector()

        with pytest.raises(RuntimeError, match="Unsupported range syntax"):
            await connector.call(
                "drive_sheets_format_range",
                {"spreadsheet_id": "sheet1", "sheet_id": 0, "range_a1": "not-a-range"},
            )

        assert gated_call_spy == []  # popup never shown
        client.get_file_metadata.assert_not_called()
        client.format_sheet_range.assert_not_called()


class TestSheetsDimensionTools:
    async def test_insert_dimensions_gate_popup(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.insert_dimensions.return_value = {"inserted": 2}

        result = await connector.call(
            "drive_sheets_insert_dimensions",
            {"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "rows", "start_index": 5, "count": 2},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["Action"] == "Insert 2 ROWS before index 5"
        assert kwargs["args"] == {
            "spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "ROWS", "start_index": 5, "count": 2,
        }
        client.insert_dimensions.assert_called_once_with("sheet1", 0, "ROWS", 5, 2, True)
        assert result == {"inserted": 2}

    async def test_insert_dimensions_normalizes_dimension_case(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.insert_dimensions.return_value = {"inserted": 1}

        await connector.call(
            "drive_sheets_insert_dimensions",
            {"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "columns", "start_index": 0},
        )

        client.insert_dimensions.assert_called_once_with("sheet1", 0, "COLUMNS", 0, 1, True)

    async def test_insert_dimensions_invalid_dimension_rejected_before_gate(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="ROWS.*COLUMNS"):
            await connector.call(
                "drive_sheets_insert_dimensions",
                {"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "cells", "start_index": 0},
            )

        assert gated_call_spy == []
        client.get_file_metadata.assert_not_called()
        client.insert_dimensions.assert_not_called()

    async def test_delete_dimensions_gate_popup_and_warns_of_data_loss(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Budget")
        client.delete_dimensions.return_value = {"deleted": 3}

        result = await connector.call(
            "drive_sheets_delete_dimensions",
            {"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "COLUMNS", "start_index": 1, "count": 3},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["Action"] == "Delete 3 COLUMNS starting at index 1"
        assert "not recoverable" in kwargs["details_text"]
        client.delete_dimensions.assert_called_once_with("sheet1", 0, "COLUMNS", 1, 3)
        assert result == {"deleted": 3}

    async def test_delete_dimensions_invalid_dimension_rejected_before_gate(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="ROWS.*COLUMNS"):
            await connector.call(
                "drive_sheets_delete_dimensions",
                {"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "cells", "start_index": 0},
            )

        assert gated_call_spy == []
        client.get_file_metadata.assert_not_called()


class TestDocsEditAndFormatContent:
    async def test_edit_content_gate_popup_and_preview_is_metadata_only(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Notes")
        client.edit_doc_content.return_value = {"occurrences_replaced": 1}

        result = await connector.call(
            "drive_docs_edit_content",
            {"file_id": "f1", "find_text": "old sentence", "replace_markdown": "new sentence"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"] == {
            "File": "Notes", "Owner": "alice@example.com", "Match": "the one matching occurrence",
        }
        assert "old sentence" not in str(kwargs["preview"])
        assert "old sentence" in kwargs["details_text"]
        assert "new sentence" in kwargs["details_text"]
        assert kwargs["args"] == {"file_id": "f1"}
        client.edit_doc_content.assert_called_once_with("f1", "old sentence", "new sentence", False)
        assert result == {"occurrences_replaced": 1}

    async def test_edit_content_replace_all_reflected_in_preview_and_call(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.edit_doc_content.return_value = {"occurrences_replaced": 3}

        await connector.call(
            "drive_docs_edit_content",
            {"file_id": "f1", "find_text": "cat", "replace_markdown": "dog", "replace_all": True},
        )

        assert gated_call_spy[0]["preview"]["Match"] == "every occurrence"
        client.edit_doc_content.assert_called_once_with("f1", "cat", "dog", True)

    async def test_format_content_gate_popup_summarizes_applied_changes(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file(name="Notes")
        client.format_doc_content.return_value = {"occurrences_formatted": 1}

        result = await connector.call(
            "drive_docs_format_content",
            {"file_id": "f1", "find_text": "important", "bold": "true", "highlight_color": "#fff59d"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert "bold=true" in kwargs["preview"]["Format"]
        assert "highlight=#fff59d" in kwargs["preview"]["Format"]
        assert "important" not in str(kwargs["preview"])
        assert "important" in kwargs["details_text"]
        client.format_doc_content.assert_called_once_with(
            "f1", "important", "true", "", "#fff59d", "", False
        )
        assert result == {"occurrences_formatted": 1}

    async def test_format_content_summary_covers_every_remaining_option(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.format_doc_content.return_value = {"occurrences_formatted": 1}

        await connector.call(
            "drive_docs_format_content",
            {"file_id": "f1", "find_text": "x", "italic": "true", "text_color": "#000000"},
        )

        summary = gated_call_spy[0]["preview"]["Format"]
        assert "italic=true" in summary
        assert "text_color=#000000" in summary

    async def test_format_content_no_changes_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_file_metadata.return_value = make_file()
        client.format_doc_content.return_value = {"occurrences_formatted": 0}

        await connector.call("drive_docs_format_content", {"file_id": "f1", "find_text": "x"})

        assert gated_call_spy[0]["preview"]["Format"] == "(no changes)"


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # download_file's size is read via result.get("size_bytes", 0) and then
        # formatted with ":," -- a bare MagicMock has no meaningful __format__
        # for that spec, so it needs a real dict back.
        client.download_file.return_value = {"name": "f.txt", "path": "/tmp/f.txt", "size_bytes": 100}

        await assert_all_tools_leave_an_audit_trail(
            connector, drive_module, monkeypatch, tmp_path,
            arg_overrides={
                # Must supply exactly one of local_path/content_base64.
                "drive_upload_file": {"content_base64": "aGVsbG8=", "name": "greeting.txt"},
                # values must be a JSON 2D array for _parse_json_2d_list to accept.
                "drive_sheets_write_range": {"values": '[["a", "b"]]'},
                # range_a1 must be a fully-bounded A1 range for _parse_a1_range
                # to accept -- it's now validated before gating.
                "drive_sheets_format_range": {"range_a1": "A1:B2"},
                # dimension must be 'ROWS' or 'COLUMNS' -- validated before gating.
                "drive_sheets_insert_dimensions": {"dimension": "ROWS"},
                "drive_sheets_delete_dimensions": {"dimension": "ROWS"},
            },
        )
