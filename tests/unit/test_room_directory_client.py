"""Tests for RoomDirectoryClient: the Admin SDK Directory client that lists
Workspace room resources. Deliberately its own client/token/scope, separate
from CalendarClient (see calendar_client.py's module docstring) -- these
tests exist to lock in that the Admin SDK call, its 403 handling, its
CalendarRoom mapping, and the OAuth token lifecycle all still work once moved
out of CalendarClient.
"""
from __future__ import annotations

import stat
from unittest.mock import MagicMock

import pytest

from privacyfence.calendar_client import CalendarRoom
from privacyfence.room_directory_client import (
    SCOPES,
    RoomDirectoryClient,
    RoomDirectoryClientError,
)
from googleapiclient.errors import HttpError


def make_client(service: MagicMock) -> RoomDirectoryClient:
    client = RoomDirectoryClient(client_config={}, token_file="/tmp/unused-room-token.json")
    client._local.service = service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


class TestScopes:
    def test_scope_is_admin_directory_readonly_only(self):
        assert SCOPES == ["https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly"]


class TestAuthorizeInteractive:
    def test_missing_client_config_raises(self, tmp_path):
        client = RoomDirectoryClient(client_config={}, token_file=str(tmp_path / "token.json"))
        with pytest.raises(RoomDirectoryClientError, match="No admin client config given"):
            client.authorize_interactive()

    def test_runs_local_server_flow_and_persists_returned_credentials(self, tmp_path, monkeypatch):
        token_file = tmp_path / "nested" / "token.json"
        client = RoomDirectoryClient(client_config={"installed": {"client_id": "cid"}}, token_file=str(token_file))

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fake_creds
        mock_from_client_config = MagicMock(return_value=fake_flow)
        monkeypatch.setattr(
            "privacyfence.room_directory_client.InstalledAppFlow.from_client_config", mock_from_client_config
        )

        client.authorize_interactive()

        mock_from_client_config.assert_called_once_with({"installed": {"client_id": "cid"}}, SCOPES)
        fake_flow.run_local_server.assert_called_once_with(port=0)
        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'


class TestLoadCredentials:
    def test_missing_token_file_raises(self, tmp_path):
        client = RoomDirectoryClient(client_config={}, token_file=str(tmp_path / "does-not-exist.json"))
        with pytest.raises(RoomDirectoryClientError, match="No OAuth token found"):
            client._load_credentials()

    def test_valid_token_is_returned_without_refresh_or_network(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = True
        monkeypatch.setattr(
            "privacyfence.room_directory_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = RoomDirectoryClient(client_config={}, token_file=str(token_file))

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
            "privacyfence.room_directory_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = RoomDirectoryClient(client_config={}, token_file=str(token_file))

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
            "privacyfence.room_directory_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = RoomDirectoryClient(client_config={}, token_file=str(token_file))

        with pytest.raises(RoomDirectoryClientError, match="Failed to refresh Room Directory OAuth token.*revoked"):
            client._load_credentials()

    def test_expired_token_without_refresh_token_raises_invalid_cached_token(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = ""
        monkeypatch.setattr(
            "privacyfence.room_directory_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = RoomDirectoryClient(client_config={}, token_file=str(token_file))

        with pytest.raises(RoomDirectoryClientError, match="Cached Room Directory OAuth token is invalid"):
            client._load_credentials()


class TestSaveToken:
    def test_writes_credentials_json_with_owner_only_permissions(self, tmp_path):
        token_file = tmp_path / "nested" / "token.json"
        client = RoomDirectoryClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'

        client._save_token(fake_creds)

        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


class TestGetService:
    def test_same_thread_reuses_cached_service(self, monkeypatch):
        client = RoomDirectoryClient(client_config={}, token_file="/tmp/unused-room-token.json")
        mock_build = MagicMock(side_effect=lambda *a, **k: MagicMock())
        monkeypatch.setattr("privacyfence.room_directory_client.build", mock_build)
        monkeypatch.setattr(client, "_load_credentials", lambda: MagicMock())

        assert client._get_service() is client._get_service()
        assert mock_build.call_count == 1


class TestListRooms:
    def test_maps_response(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.return_value = {
            "items": [{
                "resourceId": "r1", "resourceName": "Room A", "resourceEmail": "room-a@x.com",
                "buildingId": "b1", "floorName": "3", "capacity": "10",
                "generatedResourceName": "Room A (3rd floor)",
            }]
        }
        client = make_client(service)

        rooms = client.list_rooms()

        assert rooms == [CalendarRoom(
            resource_id="r1", resource_name="Room A", resource_email="room-a@x.com",
            building_id="b1", floor_name="3", capacity=10, description="Room A (3rd floor)",
        )]

    def test_403_gives_actionable_admin_access_message(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.side_effect = (
            http_error(403)
        )
        client = make_client(service)

        with pytest.raises(RoomDirectoryClientError, match="Workspace admin access"):
            client.list_rooms()

    def test_other_http_error_gives_generic_message(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.side_effect = (
            http_error(500)
        )
        client = make_client(service)

        with pytest.raises(RoomDirectoryClientError, match="list_rooms failed"):
            client.list_rooms()

    def test_query_param_included_only_when_given(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.return_value = {
            "items": []
        }
        client = make_client(service)

        client.list_rooms()
        assert "query" not in service.resources.return_value.calendars.return_value.list.call_args.kwargs

        client.list_rooms(query="floor 3")
        assert service.resources.return_value.calendars.return_value.list.call_args.kwargs["query"] == "floor 3"
