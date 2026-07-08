"""Google Calendar API client.

Handles OAuth2 authorization and full read/write access to Google Calendar.
All event data is normalized into simple dataclasses so the rest of the
application never has to deal with the raw Calendar API payload shape.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly",
]


class CalendarClientError(Exception):
    """Raised for unrecoverable Calendar client problems (auth, config, API)."""


@dataclass
class CalendarListEntry:
    id: str
    summary: str   # display name
    description: str
    primary: bool
    access_role: str


@dataclass
class CalendarAttendee:
    email: str
    display_name: str
    response_status: str  # "accepted" | "declined" | "tentative" | "needsAction"
    organizer: bool = False


@dataclass
class CalendarEvent:
    id: str
    calendar_id: str
    title: str
    description: str
    start_time: str   # ISO 8601
    end_time: str     # ISO 8601
    all_day: bool
    organizer_email: str
    attendees: list[CalendarAttendee]
    location: str
    hangout_link: str
    conference_link: str
    status: str       # "confirmed" | "tentative" | "cancelled"
    html_link: str

    def short_summary(self) -> str:
        return f"{self.title} ({self.start_time})"


@dataclass
class CalendarRoom:
    resource_id: str
    resource_name: str
    resource_email: str
    building_id: str
    floor_name: str
    capacity: int
    description: str


@dataclass
class FreeBusySlot:
    start: str
    end: str


@dataclass
class FreeBusyResult:
    email: str
    busy: list[FreeBusySlot]


def _has_timezone(iso_str: str) -> bool:
    """Return True if the ISO 8601 string already carries timezone information."""
    try:
        return datetime.fromisoformat(iso_str).tzinfo is not None
    except (ValueError, TypeError):
        return False


class CalendarClient:
    """Google Calendar client with OAuth2 token caching."""

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        # googleapiclient service objects (and the httplib2 transport they
        # wrap) are not thread-safe. Requests are dispatched to a thread per
        # call (see connectors/*.py._fetch), so a single shared service can
        # have two threads read/write the same socket concurrently,
        # corrupting the connection (observed as SSL: WRONG_VERSION_NUMBER
        # on a later, unrelated request reusing the same connection). Keep
        # one service per thread instead of one shared instance.
        self._local = threading.local()
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        ``client_config`` comes from the organization config bundle (installed
        via the menu bar), not a file on disk.
        """
        if not self._client_config:
            raise CalendarClientError(
                "No Google organization config installed. Install/Update "
                "Organization Config from the PrivacyFence menu bar first."
            )
        logger.info("Starting Calendar interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Calendar OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        # Guards concurrent refresh/save of the shared token file when
        # multiple threads hit an expired token at the same time.
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise CalendarClientError(
                    f"No OAuth token found at '{self._token_file}'. "
                    "Run with '--calendar-oauth' to authorize."
                )
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Calendar OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    raise CalendarClientError(
                        f"Failed to refresh Calendar OAuth token: {exc}. "
                        "Re-run with '--calendar-oauth' to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds
            raise CalendarClientError(
                "Cached Calendar OAuth token is invalid. Re-run with '--calendar-oauth'."
            )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            logger.debug("Could not chmod calendar token file (non-fatal)")

    def _get_service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            self._local.service = service
            logger.debug("Calendar API service initialized for thread %s", threading.current_thread().name)
        return service

    def _get_directory_service(self):
        service = getattr(self._local, "directory_service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("admin", "directory_v1", credentials=creds, cache_discovery=False)
            self._local.directory_service = service
        return service

    # ------------------------------------------------------------------ #
    # Connection check
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials. Returns the primary calendar email."""
        try:
            primary = self._get_service().calendars().get(calendarId="primary").execute()
        except HttpError as exc:
            raise CalendarClientError(f"Calendar connection check failed: {exc}") from exc
        email = primary.get("id", "unknown")
        logger.info("Connected to Calendar as %s", email)
        return email

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_calendars(self) -> list[CalendarListEntry]:
        """List all calendars for the authenticated user."""
        try:
            result = self._get_service().calendarList().list().execute()
        except HttpError as exc:
            raise CalendarClientError(f"list_calendars failed: {exc}") from exc
        entries = []
        for raw in result.get("items", []):
            entries.append(CalendarListEntry(
                id=raw.get("id", ""),
                summary=raw.get("summary", ""),
                description=raw.get("description", ""),
                primary=bool(raw.get("primary", False)),
                access_role=raw.get("accessRole", ""),
            ))
        logger.info("list_calendars returned %d calendar(s)", len(entries))
        return entries

    def list_events(
        self,
        calendar_id: str,
        max_results: int = 20,
        time_min: str = "",
        time_max: str = "",
        query: str = "",
    ) -> list[CalendarEvent]:
        """List events from a calendar."""
        max_results = max(1, min(int(max_results), 250))
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query
        try:
            result = self._get_service().events().list(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"list_events({calendar_id}) failed: {exc}") from exc
        events = [self._parse_event(raw, calendar_id) for raw in result.get("items", [])]
        logger.info("list_events %s returned %d event(s)", calendar_id, len(events))
        return events

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        """Fetch a single event by id."""
        if not calendar_id or not event_id:
            raise CalendarClientError("get_event requires calendar_id and event_id")
        try:
            raw = self._get_service().events().get(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            raise CalendarClientError(f"get_event({calendar_id}, {event_id}) failed: {exc}") from exc
        event = self._parse_event(raw, calendar_id)
        logger.info("get_event %s: %s", event_id, event.short_summary())
        return event

    def get_free_busy(
        self, emails: list[str], time_min: str, time_max: str
    ) -> list[FreeBusyResult]:
        """Query free/busy status for a list of emails."""
        if not emails:
            raise CalendarClientError("get_free_busy requires a non-empty emails list")
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": e} for e in emails],
        }
        try:
            result = self._get_service().freebusy().query(body=body).execute()
        except HttpError as exc:
            raise CalendarClientError(f"get_free_busy failed: {exc}") from exc
        calendars = result.get("calendars", {})
        results = []
        for email in emails:
            cal_data = calendars.get(email, {})
            busy_periods = [
                FreeBusySlot(start=b.get("start", ""), end=b.get("end", ""))
                for b in cal_data.get("busy", [])
            ]
            results.append(FreeBusyResult(email=email, busy=busy_periods))
        logger.info("get_free_busy: %d email(s) queried", len(emails))
        return results

    def get_colleagues_schedule(
        self, emails: list[str], time_min: str, time_max: str
    ) -> list[dict]:
        """Try events.list per calendar; fall back to free/busy for inaccessible calendars.

        Returns a list of per-email dicts with source="events" (full event titles) or
        source="free_busy" (only busy slots) depending on what the authenticated user
        can see.
        """
        results = []
        fallback_emails: list[str] = []

        for email in emails:
            try:
                events = self.list_events(
                    email, max_results=50, time_min=time_min, time_max=time_max
                )
                results.append({
                    "email": email,
                    "source": "events",
                    "events": [
                        {
                            "id": e.id,
                            "title": e.title,
                            "start_time": e.start_time,
                            "end_time": e.end_time,
                            "status": e.status,
                            "all_day": e.all_day,
                        }
                        for e in events
                    ],
                })
            except CalendarClientError:
                fallback_emails.append(email)

        if fallback_emails:
            try:
                fb = self.get_free_busy(fallback_emails, time_min, time_max)
                for r in fb:
                    results.append({
                        "email": r.email,
                        "source": "free_busy",
                        "busy": [{"start": s.start, "end": s.end} for s in r.busy],
                    })
            except CalendarClientError as exc:
                for email in fallback_emails:
                    results.append({"email": email, "source": "error", "error": str(exc)})

        return results

    def list_rooms(self, query: str = "") -> list[CalendarRoom]:
        """List meeting rooms/resources from the Google Workspace directory.

        Requires the authenticated user to have admin directory read access
        (https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly).
        Raises CalendarClientError with a clear message if access is denied.
        """
        try:
            kwargs: dict[str, Any] = {"customer": "my_customer", "maxResults": 500}
            if query:
                kwargs["query"] = query
            result = self._get_directory_service().resources().calendars().list(**kwargs).execute()
        except HttpError as exc:
            if exc.resp.status == 403:
                raise CalendarClientError(
                    "Room listing requires Google Workspace admin access. "
                    "Ask your Workspace admin to grant you the 'Directory Reader' role, "
                    "or provide room email addresses directly."
                ) from exc
            raise CalendarClientError(f"list_rooms failed: {exc}") from exc
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

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #

    def create_event(
        self,
        calendar_id: str,
        title: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: list[str] | None = None,
        location: str = "",
        add_google_meet: bool = False,
        room_emails: list[str] | None = None,
    ) -> CalendarEvent:
        """Create a new event and return the created CalendarEvent."""
        start_entry: dict[str, str] = {"dateTime": start_time}
        end_entry: dict[str, str] = {"dateTime": end_time}
        # Only inject a UTC fallback when the ISO string has no embedded offset.
        # If the caller already includes one (e.g. "+02:00"), preserve it.
        if not _has_timezone(start_time):
            start_entry["timeZone"] = "UTC"
        if not _has_timezone(end_time):
            end_entry["timeZone"] = "UTC"
        body: dict[str, Any] = {
            "summary": title,
            "start": start_entry,
            "end": end_entry,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        all_attendees = list(attendees or [])
        if room_emails:
            body["attendees"] = (
                [{"email": e} for e in all_attendees]
                + [{"email": r, "resource": True} for r in room_emails]
            )
        elif all_attendees:
            body["attendees"] = [{"email": e} for e in all_attendees]
        kwargs: dict[str, Any] = {"calendarId": calendar_id, "body": body}
        if add_google_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    "requestId": str(uuid.uuid4()),
                }
            }
            kwargs["conferenceDataVersion"] = 1
        try:
            raw = self._get_service().events().insert(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"create_event({calendar_id}) failed: {exc}") from exc
        event = self._parse_event(raw, calendar_id)
        logger.info("create_event: %s", event.short_summary())
        return event

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        title: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
        add_google_meet: bool = False,
        room_emails: list[str] | None = None,
    ) -> CalendarEvent:
        """Update fields on an existing event and return the updated CalendarEvent."""
        try:
            raw = (
                self._get_service()
                .events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as exc:
            raise CalendarClientError(f"update_event get({event_id}) failed: {exc}") from exc

        if title is not None:
            raw["summary"] = title
        if description is not None:
            raw["description"] = description
        if location is not None:
            raw["location"] = location
        if start_time is not None:
            raw.setdefault("start", {})["dateTime"] = start_time
            raw["start"].setdefault("timeZone", "UTC")
        if end_time is not None:
            raw.setdefault("end", {})["dateTime"] = end_time
            raw["end"].setdefault("timeZone", "UTC")
        if room_emails:
            existing = [a for a in raw.get("attendees", []) if not a.get("resource")]
            raw["attendees"] = existing + [{"email": r, "resource": True} for r in room_emails]

        kwargs: dict[str, Any] = {"calendarId": calendar_id, "eventId": event_id, "body": raw}
        if add_google_meet and not raw.get("conferenceData"):
            raw["conferenceData"] = {
                "createRequest": {
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    "requestId": str(uuid.uuid4()),
                }
            }
            kwargs["conferenceDataVersion"] = 1
        elif raw.get("conferenceData"):
            kwargs["conferenceDataVersion"] = 1

        try:
            updated = self._get_service().events().update(**kwargs).execute()
        except HttpError as exc:
            raise CalendarClientError(f"update_event({event_id}) failed: {exc}") from exc
        event = self._parse_event(updated, calendar_id)
        logger.info("update_event: %s", event.short_summary())
        return event

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _parse_event(self, raw: dict[str, Any], calendar_id: str) -> CalendarEvent:
        start = raw.get("start", {})
        end = raw.get("end", {})
        all_day = "date" in start and "dateTime" not in start
        start_time = start.get("dateTime") or start.get("date", "")
        end_time = end.get("dateTime") or end.get("date", "")

        organizer = raw.get("organizer", {})
        organizer_email = organizer.get("email", "")

        raw_attendees = raw.get("attendees", []) or []
        attendees = [
            CalendarAttendee(
                email=a.get("email", ""),
                display_name=a.get("displayName", ""),
                response_status=a.get("responseStatus", "needsAction"),
                organizer=bool(a.get("organizer", False)),
            )
            for a in raw_attendees
        ]

        conference_data = raw.get("conferenceData") or {}
        conference_link = ""
        for ep in conference_data.get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                conference_link = ep.get("uri", "")
                break

        return CalendarEvent(
            id=raw.get("id", ""),
            calendar_id=calendar_id,
            title=raw.get("summary", ""),
            description=raw.get("description", ""),
            start_time=start_time,
            end_time=end_time,
            all_day=all_day,
            organizer_email=organizer_email,
            attendees=attendees,
            location=raw.get("location", ""),
            hangout_link=raw.get("hangoutLink", ""),
            conference_link=conference_link,
            status=raw.get("status", "confirmed"),
            html_link=raw.get("htmlLink", ""),
        )
