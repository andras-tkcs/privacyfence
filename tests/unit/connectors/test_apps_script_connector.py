"""Unit tests for privacyfence.connectors.apps_script.AppsScriptConnector.

apps_script_list_projects is unconditionally auto-approved (metadata only,
mirrors drive_list_shared_drives) and writes its own audit entry directly --
no gate.gated_call involvement.

apps_script_get_content and apps_script_get_execution_log are review-gated
reads; apps_script_write_content is a popup-gated write. gated_call itself is
stubbed here (never spawn a real osascript dialog from a unit test); these
tests instead assert that each gated tool sends a minimal, non-body-carrying
`preview` dict into the gate (the full source/log goes in details_text/
preview_tables, the WIDE right pane), and that a denial genuinely blocks the
underlying client call from ever happening.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from privacyfence.apps_script_client import (
    AppsScriptClient,
    AppsScriptClientError,
    ScriptExecution,
    ScriptFile,
    ScriptProject,
)
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import apps_script as apps_script_module
from privacyfence.connectors.apps_script import AppsScriptConnector

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields


def make_connector():
    client = MagicMock()
    return AppsScriptConnector(client), client


def make_project(**overrides):
    defaults = dict(id="s1", name="My Script", created_time="c", modified_time="m")
    defaults.update(overrides)
    return ScriptProject(**defaults)


_VALID_FILES_JSON = json.dumps([{"name": "Code", "type": "SERVER_JS", "source": "function foo() {}"}])


@pytest.fixture
def gated_call_spy(monkeypatch):
    """Stub gated_call to record its kwargs and act as if the user approved."""
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(apps_script_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Apps Script tool"):
            await connector.call("apps_script_does_not_exist", {})

    def test_name_and_client_properties(self):
        connector, client = make_connector()
        assert connector.name == "apps_script"
        assert connector.client is client


class TestListProjects:
    async def test_serializes_dataclasses_and_writes_audit_entry(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_projects.return_value = [make_project()]

        result = await connector.call("apps_script_list_projects", {})

        assert result == [{"id": "s1", "name": "My Script", "created_time": "c", "modified_time": "m"}]
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_max_results_passed_through(self):
        connector, client = make_connector()
        client.list_projects.return_value = []

        await connector.call("apps_script_list_projects", {"max_results": 5})

        client.list_projects.assert_called_once_with(5)


class TestGetContent:
    async def test_gates_with_metadata_only_preview_and_full_source_in_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project(name="My Script")
        client.get_content.return_value = MagicMock(
            files=[ScriptFile(name="Code", type="SERVER_JS", source="function secretApiKey() { return 'abc123'; }")]
        )

        result = await connector.call("apps_script_get_content", {"script_id": "s1"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"] == {"Project": "My Script", "Files": "1 file(s)"}
        # The full source is only in details_text (the WIDE right pane), never the preview dict.
        assert "secretApiKey" not in kwargs["preview"].values()
        assert "secretApiKey" in kwargs["details_text"]
        assert kwargs["details_text"].startswith("=== Code (SERVER_JS) ===")
        assert result == {
            "script_id": "s1",
            "files": [{"name": "Code", "type": "SERVER_JS", "source": "function secretApiKey() { return 'abc123'; }"}],
        }

    async def test_multiple_files_all_rendered(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.get_content.return_value = MagicMock(
            files=[
                ScriptFile(name="Code", type="SERVER_JS", source="function foo() {}"),
                ScriptFile(name="appsscript", type="JSON", source="{}"),
            ]
        )

        await connector.call("apps_script_get_content", {"script_id": "s1"})

        details = gated_call_spy[0]["details_text"]
        assert "=== Code (SERVER_JS) ===" in details
        assert "=== appsscript (JSON) ===" in details
        assert gated_call_spy[0]["preview"]["Files"] == "2 file(s)"


class TestGetExecutionLog:
    async def test_gates_with_table_and_metadata_only_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.get_execution_log.return_value = [
            ScriptExecution(
                function_name="doStuff", status="COMPLETED",
                start_time="2026-01-01T00:00:00Z", duration="1.234s", process_type="TRIGGER",
            )
        ]

        result = await connector.call("apps_script_get_execution_log", {"script_id": "s1"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"] == {"Project": "My Script", "Executions": "1 run(s)"}
        assert kwargs["table_only"] is True
        assert kwargs["preview_tables"] == [{
            "headers": ["Function", "Status", "Start time", "Duration"],
            "rows": [["doStuff", "COMPLETED", "2026-01-01T00:00:00Z", "1.234s"]],
        }]
        assert result == [{
            "function_name": "doStuff", "status": "COMPLETED",
            "start_time": "2026-01-01T00:00:00Z", "duration": "1.234s", "process_type": "TRIGGER",
        }]

    async def test_no_executions_omits_table(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.get_execution_log.return_value = []

        await connector.call("apps_script_get_execution_log", {"script_id": "s1"})

        assert gated_call_spy[0]["preview_tables"] == []
        assert gated_call_spy[0]["details_text"] == "No executions found."

    async def test_max_results_passed_through(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.get_execution_log.return_value = []

        await connector.call("apps_script_get_execution_log", {"script_id": "s1", "max_results": 3})

        client.get_execution_log.assert_called_once_with("s1", 3)


class TestWriteContent:
    async def test_gates_before_writing_with_metadata_only_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.write_content.return_value = {"script_id": "s1", "file_count": 1}

        result = await connector.call(
            "apps_script_write_content",
            {"script_id": "s1", "files": _VALID_FILES_JSON},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"] == {"Project": "My Script", "Files": "Code (SERVER_JS)"}
        assert "function foo() {}" not in kwargs["preview"].values()
        assert "function foo() {}" in kwargs["details_text"]
        client.write_content.assert_called_once_with(
            "s1", [{"name": "Code", "type": "SERVER_JS", "source": "function foo() {}"}]
        )
        assert result == {"script_id": "s1", "file_count": 1}

    async def test_invalid_json_raises_before_gating(self, gated_call_spy):
        connector, _client = make_connector()

        with pytest.raises(ValueError, match="must be a JSON array"):
            await connector.call("apps_script_write_content", {"script_id": "s1", "files": "not json"})

        assert gated_call_spy == []

    async def test_non_list_json_raises_before_gating(self, gated_call_spy):
        connector, _client = make_connector()

        with pytest.raises(ValueError, match="must be a JSON array"):
            await connector.call("apps_script_write_content", {"script_id": "s1", "files": "{}"})

        assert gated_call_spy == []

    async def test_empty_list_raises_before_gating(self, gated_call_spy):
        connector, _client = make_connector()

        with pytest.raises(ValueError, match="must be a JSON array"):
            await connector.call("apps_script_write_content", {"script_id": "s1", "files": "[]"})

        assert gated_call_spy == []

    async def test_missing_name_or_type_raises_before_gating(self, gated_call_spy):
        connector, _client = make_connector()
        bad_files = json.dumps([{"name": "Code"}])

        with pytest.raises(ValueError, match="must be a JSON array"):
            await connector.call("apps_script_write_content", {"script_id": "s1", "files": bad_files})

        assert gated_call_spy == []

    async def test_multiple_files_shown_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.write_content.return_value = {}
        files = json.dumps([
            {"name": "Code", "type": "SERVER_JS", "source": "function foo() {}"},
            {"name": "index", "type": "HTML", "source": "<p>hi</p>"},
        ])

        await connector.call("apps_script_write_content", {"script_id": "s1", "files": files})

        assert gated_call_spy[0]["preview"]["Files"] == "Code (SERVER_JS), index (HTML)"


class TestGatedCallsBlockOnDenial:
    """The point of gating these tools at all: a denial must stop the write
    from ever reaching the client, not just get logged after the fact."""

    async def test_denied_write_raises_and_client_is_never_called(self, monkeypatch):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()

        async def deny(**kwargs):
            raise RuntimeError("Request denied by user")

        monkeypatch.setattr(apps_script_module, "gated_call", deny)

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call(
                "apps_script_write_content", {"script_id": "s1", "files": _VALID_FILES_JSON}
            )

        client.write_content.assert_not_called()


class TestFieldCompleteness:
    """End to end: a fully-populated raw listScriptProcesses response -> the
    real AppsScriptClient.get_execution_log -> the real connector method --
    not a hand-built ScriptExecution, unlike every other test in this file.
    Mirrors test_confluence_connector.py's TestFieldCompleteness -- the shape
    of check that would catch a field mapping silently degrading to a
    fallback value before it ships."""

    async def test_execution_log_result_has_no_placeholder_fields(self, gated_call_spy):
        raw = {
            "functionName": "syncDriveInventory",
            "processStatus": "COMPLETED",
            "startTime": "2026-06-01T12:00:00.000Z",
            "duration": "4.567s",
            "processType": "TRIGGER",
        }
        service = MagicMock()
        service.processes.return_value.listScriptProcesses.return_value.execute.return_value = {
            "processes": [raw]
        }
        client = AppsScriptClient(client_config={}, token_file="/tmp/unused-token.json")
        # get_execution_log() runs inside a worker thread (connector._fetch
        # uses asyncio.to_thread), so client._local.service -- thread-local
        # -- wouldn't be visible there; overriding _get_service directly is
        # the thread-agnostic equivalent of test_apps_script_client.py's
        # make_client().
        client._get_service = lambda: service
        client.get_project_metadata = lambda script_id: make_project()

        connector = AppsScriptConnector(client)
        result = await connector.call("apps_script_get_execution_log", {"script_id": "s1"})

        assert_no_placeholder_fields(result[0], placeholders=("", "(unknown)", None))


class TestErrorMapping:
    async def test_apps_script_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_projects.side_effect = AppsScriptClientError("token expired")

        with pytest.raises(RuntimeError, match="token expired"):
            await connector.call("apps_script_list_projects", {})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.get_project_metadata.return_value = make_project()
        client.get_content.return_value = MagicMock(
            files=[ScriptFile(name="Code", type="SERVER_JS", source="function foo() {}")]
        )
        client.get_execution_log.return_value = []
        await assert_all_tools_leave_an_audit_trail(
            connector, apps_script_module, monkeypatch, tmp_path,
            arg_overrides={"apps_script_write_content": {"files": _VALID_FILES_JSON}},
        )
