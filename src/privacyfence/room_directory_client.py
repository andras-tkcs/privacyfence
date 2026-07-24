"""Google Workspace Admin SDK client for reading the room/resource directory.

Deliberately separate from CalendarClient (see calendar_client.py): this is the
only PrivacyFence client that ever requests a Workspace-admin-level scope
(`admin.directory.resource.calendar.readonly`), so it gets its own OAuth
client/token rather than riding on the everyday Calendar/Gmail/Drive/Contacts/
Tasks client — an employee's day-to-day token should never be able to read the
Workspace directory. Not used by the daemon at all: it's driven by
scripts/sync_room_directory.py, run by IT against a second Google Cloud
project, to populate org_config.json's "rooms" field (see that script and
docs/google-cloud-setup.md for the full setup).

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .calendar_client import CalendarRoom

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly"]


class RoomDirectoryClientError(Exception):
    """Raised for unrecoverable RoomDirectoryClient problems (auth, config, API)."""


class RoomDirectoryClient:
    """Admin SDK Directory client, read-only, rooms/resources only."""

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        self._local = threading.local()
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        The signed-in Google account must hold Workspace admin / Directory
        Reader privilege — this is enforced by Google, not by PrivacyFence
        (see list_rooms()'s 403 handling below).
        """
        if not self._client_config:
            raise RoomDirectoryClientError(
                "No admin client config given. Pass --admin-client-secret to "
                "scripts/sync_room_directory.py."
            )
        logger.info("Starting Room Directory interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Room Directory OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise RoomDirectoryClientError(
                    f"No OAuth token found at '{self._token_file}'. Run "
                    "scripts/sync_room_directory.py to authorize."
                )
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Room Directory OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    raise RoomDirectoryClientError(
                        f"Failed to refresh Room Directory OAuth token: {exc}. "
                        "Re-run scripts/sync_room_directory.py to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds
            raise RoomDirectoryClientError(
                "Cached Room Directory OAuth token is invalid. Re-run "
                "scripts/sync_room_directory.py."
            )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            logger.debug("Could not chmod room directory token file (non-fatal)")

    def _get_service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("admin", "directory_v1", credentials=creds, cache_discovery=False)
            self._local.service = service
            logger.debug(
                "Room Directory API service initialized for thread %s",
                threading.current_thread().name,
            )
        return service

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_rooms(self, query: str = "") -> list[CalendarRoom]:
        """List meeting rooms/resources from the Google Workspace directory."""
        try:
            kwargs: dict[str, Any] = {"customer": "my_customer", "maxResults": 500}
            if query:
                kwargs["query"] = query
            result = self._get_service().resources().calendars().list(**kwargs).execute()
        except HttpError as exc:
            if exc.resp.status == 403:
                raise RoomDirectoryClientError(
                    "Room directory listing requires Google Workspace admin access. "
                    "Sign in with an account that has the 'Directory Reader' role, "
                    "or ask your Workspace admin to grant it."
                ) from exc
            raise RoomDirectoryClientError(f"list_rooms failed: {exc}") from exc
        rooms = []
        for raw in result.get("items", []):
            rooms.append(CalendarRoom(
                resource_id=raw.get("resourceId", ""),
                resource_name=raw.get("resourceName", ""),
                resource_email=raw.get("resourceEmail", ""),
                building_id=raw.get("buildingId", ""),
                floor_name=raw.get("floorName", ""),
                capacity=int(raw.get("capacity", 0)),
                description=raw.get("generatedResourceName", raw.get("resourceDescription", "")),
            ))
        logger.info("list_rooms returned %d room(s)", len(rooms))
        return rooms
