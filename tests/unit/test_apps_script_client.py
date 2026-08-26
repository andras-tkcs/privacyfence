"""Tests for AppsScriptClient's parsing/normalization logic and the OAuth2
token lifecycle (authorize_interactive / _load_credentials / _save_token).

The token lifecycle tests mock at the google-auth library boundary
(``Credentials.from_authorized_user_file``, ``InstalledAppFlow.from_client_config``)
rather than at ``_load_credentials`` itself, so the actual
load/valid/expired/refresh/save branching in ``_load_credentials`` is
exercised for real -- same pattern as test_tasks_client.py/test_drive_client.py.
"""
from __future__ import annotations

import stat
import threading
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from privacyfence.apps_script_client import (
    SCOPES,
    AppsScriptClient,
    AppsScriptClientError,
    ScriptContent,
    ScriptExecution,
    ScriptFile,
    ScriptProject,
)


def make_client(service: MagicMock, drive_service: MagicMock | None = None) -> AppsScriptClient:
    client = AppsScriptClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    if drive_service is not None:
        client._local.drive_service = drive_service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


# ---------------------------------------------------------------------------- #
# authorize_interactive
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractive:
    def test_missing_client_config_raises(self, tmp_path):
        client = AppsScriptClient(client_config={}, token_file=str(tmp_path / "token.json"))
        with pytest.raises(AppsScriptClientError, match="No Google organization config installed"):
            client.authorize_interactive()

    def test_runs_local_server_flow_and_persists_returned_credentials(self, tmp_path, monkeypatch):
        token_file = tmp_path / "nested" / "token.json"
        client = AppsScriptClient(client_config={"installed": {"client_id": "cid"}}, token_file=str(token_file))

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fake_creds
        mock_from_client_config = MagicMock(return_value=fake_flow)
        monkeypatch.setattr(
            "privacyfence.apps_script_client.InstalledAppFlow.from_client_config", mock_from_client_config
        )

        client.authorize_interactive()

        mock_from_client_config.assert_called_once_with({"installed": {"client_id": "cid"}}, SCOPES)
        fake_flow.run_local_server.assert_called_once_with(port=0)
        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'


# ---------------------------------------------------------------------------- #
# _load_credentials: no-token / valid / expired-refresh-succeeds /
# expired-refresh-fails / expired-unrefreshable.
# ---------------------------------------------------------------------------- #

class TestLoadCredentials:
    def test_missing_token_file_raises(self, tmp_path):
        client = AppsScriptClient(client_config={}, token_file=str(tmp_path / "does-not-exist.json"))
        with pytest.raises(AppsScriptClientError, match="No OAuth token found"):
            client._load_credentials()

    def test_valid_token_is_returned_without_refresh_or_network(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = True
        monkeypatch.setattr(
            "privacyfence.apps_script_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = AppsScriptClient(client_config={}, token_file=str(token_file))

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
            "privacyfence.apps_script_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = AppsScriptClient(client_config={}, token_file=str(token_file))

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
            "privacyfence.apps_script_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = AppsScriptClient(client_config={}, token_file=str(token_file))

        with pytest.raises(AppsScriptClientError, match="Failed to refresh OAuth token.*revoked"):
            client._load_credentials()

    def test_expired_token_without_refresh_token_raises_invalid_cached_token(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = ""
        monkeypatch.setattr(
            "privacyfence.apps_script_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = AppsScriptClient(client_config={}, token_file=str(token_file))

        with pytest.raises(AppsScriptClientError, match="Cached OAuth token is invalid"):
            client._load_credentials()


# ---------------------------------------------------------------------------- #
# _save_token: file permissions
# ---------------------------------------------------------------------------- #

class TestSaveToken:
    def test_writes_credentials_json_with_owner_only_permissions(self, tmp_path):
        token_file = tmp_path / "nested" / "token.json"
        client = AppsScriptClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'

        client._save_token(fake_creds)

        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_chmod_failure_is_non_fatal(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        client = AppsScriptClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = "{}"
        monkeypatch.setattr("os.chmod", MagicMock(side_effect=OSError("read-only filesystem")))

        client._save_token(fake_creds)  # must not raise

        assert token_file.exists()


# ---------------------------------------------------------------------------- #
# check_connection
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_authorized_email(self):
        drive_service = MagicMock()
        drive_service.about.return_value.get.return_value.execute.return_value = {
            "user": {"emailAddress": "me@example.com"}
        }
        client = make_client(MagicMock(), drive_service)
        assert client.check_connection() == "me@example.com"

    def test_http_error_becomes_apps_script_client_error(self):
        drive_service = MagicMock()
        drive_service.about.return_value.get.return_value.execute.side_effect = http_error(500)
        client = make_client(MagicMock(), drive_service)
        with pytest.raises(AppsScriptClientError, match="Apps Script connection check failed"):
            client.check_connection()


# ---------------------------------------------------------------------------- #
# list_projects: goes through the Drive API, not the Apps Script API
# ---------------------------------------------------------------------------- #

class TestListProjects:
    def test_maps_response(self):
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "s1", "name": "My Script", "createdTime": "c", "modifiedTime": "m"}]
        }
        client = make_client(MagicMock(), drive_service)

        result = client.list_projects()

        assert result == [ScriptProject(id="s1", name="My Script", created_time="c", modified_time="m")]

    def test_query_filters_to_script_mime_type_and_excludes_trashed(self):
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
        client = make_client(MagicMock(), drive_service)

        client.list_projects()

        query = drive_service.files.return_value.list.call_args.kwargs["q"]
        assert "application/vnd.google-apps.script" in query
        assert "trashed=false" in query

    def test_max_results_clamped_to_at_least_one(self):
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
        client = make_client(MagicMock(), drive_service)

        client.list_projects(max_results=0)

        assert drive_service.files.return_value.list.call_args.kwargs["pageSize"] == 1

    def test_http_error_becomes_apps_script_client_error(self):
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(MagicMock(), drive_service)
        with pytest.raises(AppsScriptClientError, match="list_projects failed"):
            client.list_projects()


# ---------------------------------------------------------------------------- #
# get_project_metadata
# ---------------------------------------------------------------------------- #

class TestGetProjectMetadata:
    def test_requires_script_id(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="requires a non-empty script_id"):
            client.get_project_metadata("")

    def test_maps_response(self):
        service = MagicMock()
        service.projects.return_value.get.return_value.execute.return_value = {
            "scriptId": "s1", "title": "My Script", "createTime": "c", "updateTime": "u",
        }
        client = make_client(service)

        result = client.get_project_metadata("s1")

        assert result == ScriptProject(id="s1", name="My Script", created_time="c", modified_time="u")
        service.projects.return_value.get.assert_called_once_with(scriptId="s1")

    def test_missing_title_falls_back_to_script_id(self):
        service = MagicMock()
        service.projects.return_value.get.return_value.execute.return_value = {"scriptId": "s1"}
        client = make_client(service)

        result = client.get_project_metadata("s1")

        assert result.name == "s1"

    def test_short_summary_prefers_name_falls_back_to_id(self):
        assert ScriptProject(id="s1", name="My Script").short_summary() == "My Script"
        assert ScriptProject(id="s1", name="").short_summary() == "s1"

    def test_http_error_becomes_apps_script_client_error(self):
        service = MagicMock()
        service.projects.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(AppsScriptClientError, match="get_project_metadata\\(s1\\) failed"):
            client.get_project_metadata("s1")


# ---------------------------------------------------------------------------- #
# get_content
# ---------------------------------------------------------------------------- #

class TestGetContent:
    def test_requires_script_id(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="requires a non-empty script_id"):
            client.get_content("")

    def test_maps_response(self):
        service = MagicMock()
        service.projects.return_value.getContent.return_value.execute.return_value = {
            "scriptId": "s1",
            "files": [
                {"name": "Code", "type": "SERVER_JS", "source": "function foo() {}"},
                {"name": "appsscript", "type": "JSON", "source": "{}"},
            ],
        }
        client = make_client(service)

        result = client.get_content("s1")

        assert result == ScriptContent(
            script_id="s1",
            files=[
                ScriptFile(name="Code", type="SERVER_JS", source="function foo() {}"),
                ScriptFile(name="appsscript", type="JSON", source="{}"),
            ],
        )

    def test_http_error_becomes_apps_script_client_error(self):
        service = MagicMock()
        service.projects.return_value.getContent.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(AppsScriptClientError, match="get_content\\(s1\\) failed"):
            client.get_content("s1")


# ---------------------------------------------------------------------------- #
# get_execution_log
# ---------------------------------------------------------------------------- #

class TestGetExecutionLog:
    def test_requires_script_id(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="requires a non-empty script_id"):
            client.get_execution_log("")

    def test_maps_response(self):
        service = MagicMock()
        service.processes.return_value.listScriptProcesses.return_value.execute.return_value = {
            "processes": [
                {
                    "functionName": "doStuff", "processStatus": "COMPLETED",
                    "startTime": "2026-01-01T00:00:00Z", "duration": "1.234s",
                    "processType": "TRIGGER",
                }
            ]
        }
        client = make_client(service)

        result = client.get_execution_log("s1")

        assert result == [
            ScriptExecution(
                function_name="doStuff", status="COMPLETED",
                start_time="2026-01-01T00:00:00Z", duration="1.234s", process_type="TRIGGER",
            )
        ]

    def test_missing_fields_default_sensibly(self):
        service = MagicMock()
        service.processes.return_value.listScriptProcesses.return_value.execute.return_value = {
            "processes": [{}]
        }
        client = make_client(service)

        result = client.get_execution_log("s1")

        assert result[0].function_name == "(unknown)"
        assert result[0].status == "(unknown)"
        assert result[0].duration == "(unknown)"

    def test_max_results_clamped(self):
        service = MagicMock()
        service.processes.return_value.listScriptProcesses.return_value.execute.return_value = {"processes": []}
        client = make_client(service)

        client.get_execution_log("s1", max_results=1000)

        assert (
            service.processes.return_value.listScriptProcesses.call_args.kwargs["pageSize"] == 50
        )

    def test_http_error_becomes_apps_script_client_error(self):
        service = MagicMock()
        service.processes.return_value.listScriptProcesses.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(AppsScriptClientError, match="get_execution_log\\(s1\\) failed"):
            client.get_execution_log("s1")


# ---------------------------------------------------------------------------- #
# write_content
# ---------------------------------------------------------------------------- #

class TestWriteContent:
    def test_requires_script_id(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="requires a non-empty script_id"):
            client.write_content("", [{"name": "Code", "type": "SERVER_JS", "source": ""}])

    def test_requires_at_least_one_file(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="requires at least one file"):
            client.write_content("s1", [])

    def test_rejects_invalid_file_type(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="'type' of SERVER_JS/HTML/JSON"):
            client.write_content("s1", [{"name": "Code", "type": "PYTHON", "source": ""}])

    def test_rejects_missing_name(self):
        client = make_client(MagicMock())
        with pytest.raises(AppsScriptClientError, match="non-empty 'name'"):
            client.write_content("s1", [{"name": "", "type": "SERVER_JS", "source": ""}])

    def test_normalizes_and_sends_files(self):
        service = MagicMock()
        service.projects.return_value.updateContent.return_value.execute.return_value = {}
        client = make_client(service)

        result = client.write_content(
            "s1", [{"name": "Code", "type": "server_js", "source": "function foo() {}"}]
        )

        body = service.projects.return_value.updateContent.call_args.kwargs["body"]
        assert body == {"files": [{"name": "Code", "type": "SERVER_JS", "source": "function foo() {}"}]}
        assert result == {"script_id": "s1", "file_count": 1}

    def test_http_error_becomes_apps_script_client_error(self):
        service = MagicMock()
        service.projects.return_value.updateContent.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(AppsScriptClientError, match="write_content\\(s1\\) failed"):
            client.write_content("s1", [{"name": "Code", "type": "SERVER_JS", "source": ""}])


# ---------------------------------------------------------------------------- #
# _get_service / _get_drive_service: must not share one service (and its
# underlying httplib2 transport) across threads.
# ---------------------------------------------------------------------------- #

class TestServiceIsThreadLocal:
    def test_each_thread_gets_its_own_service_instance(self):
        client = AppsScriptClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.apps_script_client.build") as mock_build, \
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
            assert mock_build.call_count == 5

    def test_same_thread_reuses_cached_service(self):
        client = AppsScriptClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.apps_script_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()

            first = client._get_service()
            second = client._get_service()

            assert first is second
            assert mock_build.call_count == 1

    def test_get_service_and_get_drive_service_are_independent(self):
        client = AppsScriptClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.apps_script_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda name, *a, **k: MagicMock(name=name)

            script_service = client._get_service()
            drive_service = client._get_drive_service()

            assert script_service is not drive_service
            calls = [c.args[0] for c in mock_build.call_args_list]
            assert calls == ["script", "drive"]
