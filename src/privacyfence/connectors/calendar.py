"""Google Calendar connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..calendar_client import VALID_VISIBILITIES, CalendarClient, CalendarClientError
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call

logger = logging.getLogger(__name__)


def _day_of_week(iso_str: str) -> str:
    """Return the full weekday name for an ISO 8601 datetime string."""
    try:
        return datetime.fromisoformat(iso_str).strftime("%A")
    except (ValueError, TypeError):
        return ""


def _downgrade_to_busy_only(entry: dict) -> dict:
    """Collapse a colleague's 'events'-sourced free/busy entry (full event
    title/status) down to the same busy-slot-only shape a 'free_busy'-
    sourced entry already has, for calendar.free_busy_full_event_details:
    false. Titles/status never reach Claude this way, regardless of whether
    the authenticated account happens to have full calendar access to that
    colleague. Entries already source="free_busy" (or "error") pass
    through unchanged -- there's nothing to downgrade."""
    if entry.get("source") != "events":
        return entry
    return {
        "email": entry.get("email", ""),
        "source": "free_busy",
        "busy": [
            {"start": e.get("start_time", ""), "end": e.get("end_time", "")}
            for e in (entry.get("events") or [])
        ],
    }


class CalendarConnector(Connector):
    def __init__(self, client: CalendarClient) -> None:
        self._calendar = client
        self.my_email: str = ""
        self._calendar_name_cache: dict[str, str] = {}
        # settings.yaml's calendar.free_busy_full_event_details (default
        # true, preserving prior behavior). When false,
        # calendar_get_free_busy always returns busy/free blocks only, even
        # for colleagues the authenticated account has full calendar access
        # to -- see _get_free_busy.
        self.free_busy_full_details: bool = True

    @property
    def client(self) -> CalendarClient:
        return self._calendar

    @property
    def name(self) -> str:
        return "calendar"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="calendar_list_calendars",
                description="List all Google Calendars for the authenticated user. Auto-approved.",
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_list_events",
                description=(
                    "List events from a calendar (id, title, start_time, end_time, all_day, status). "
                    "No attendees, description, or links returned. Auto-approved."
                ),
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                    ToolParam("time_min", "str", required=False, default=""),
                    ToolParam("time_max", "str", required=False, default=""),
                    ToolParam("query", "str", required=False, default=""),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_free_busy",
                description=(
                    "Query colleagues' schedules for a time range. "
                    "For each email, tries to fetch full event details (title, time, status) "
                    "when the authenticated user has calendar access; "
                    "falls back to free/busy slots only when access is unavailable. "
                    "Use this for meeting scheduling. Auto-approved."
                ),
                params=[
                    ToolParam("emails", "str", description="Comma-separated list of email addresses"),
                    ToolParam("time_min", "str", description="ISO 8601 datetime"),
                    ToolParam("time_max", "str", description="ISO 8601 datetime"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_event_details",
                description=(
                    "Fetch full details of a calendar event including attendees, description, "
                    "conferencing links, and file attachments (e.g. the \"Notes by Gemini\" and "
                    "transcript docs Google Meet attaches after a meeting ends). Each attachment's "
                    "file_id can be passed to drive_get_file_content to read its content. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("event_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_event_visibility",
                description=(
                    "Get a calendar event's visibility setting (default, public, private, "
                    "or confidential) without fetching its full details (attendees, "
                    "description, etc.) the way calendar_get_event_details does. Auto-approved."
                ),
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("event_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_set_event_visibility",
                description=(
                    "Set a calendar event's visibility. 'default' follows the calendar's own "
                    "sharing settings; 'public' makes it visible to anyone who can see the "
                    "calendar; 'private' hides its details from viewers who aren't invited; "
                    "'confidential' is a legacy synonym the Calendar API still accepts for "
                    "'private'. Only visibility changes — no other fields are affected. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("event_id", "str"),
                    ToolParam("visibility", "str", description="'default', 'public', 'private', or 'confidential'"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_list_rooms",
                description=(
                    "List meeting rooms and resource calendars from the Google Workspace directory. "
                    "Returns room name, email, building, floor, and capacity. "
                    "Use the room email with calendar_create_event or calendar_update_event to book. "
                    "Requires Google Workspace admin directory access. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str", required=False, default="",
                              description="Optional search query to filter rooms by name or building"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_create_event",
                description="Create a new calendar event. Requires user approval.",
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("title", "str"),
                    ToolParam("start_time", "str", description="ISO 8601 datetime"),
                    ToolParam("end_time", "str", description="ISO 8601 datetime"),
                    ToolParam("description", "str", required=False, default=""),
                    ToolParam("attendees", "str", required=False, default="",
                              description="Comma-separated email addresses"),
                    ToolParam("location", "str", required=False, default=""),
                    ToolParam("add_google_meet", "bool", required=False, default=False,
                              description="Set to true to add a Google Meet video conference link"),
                    ToolParam("rooms", "str", required=False, default="",
                              description="Comma-separated room resource email addresses to book"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_update_event",
                description="Update an existing calendar event. Requires user approval.",
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("event_id", "str"),
                    ToolParam("title", "str", required=False, default=""),
                    ToolParam("start_time", "str", required=False, default=""),
                    ToolParam("end_time", "str", required=False, default=""),
                    ToolParam("description", "str", required=False, default=""),
                    ToolParam("location", "str", required=False, default=""),
                    ToolParam("add_google_meet", "bool", required=False, default=False,
                              description="Set to true to add a Google Meet link (skipped if one already exists)"),
                    ToolParam("rooms", "str", required=False, default="",
                              description="Comma-separated room resource email addresses to book"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_create_out_of_office",
                description=(
                    "Create an out-of-office event on the primary calendar. Always auto-declines "
                    "new conflicting meeting invitations that arrive while it's in effect — existing "
                    "invitations already on the calendar are left alone. Requires user approval."
                ),
                params=[
                    ToolParam("start_time", "str", description="ISO 8601 datetime"),
                    ToolParam("end_time", "str", description="ISO 8601 datetime"),
                    ToolParam("title", "str", required=False, default="Out of Office"),
                    ToolParam("decline_message", "str", required=False, default="",
                              description="Message sent to organizers of auto-declined invitations"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="calendar_set_working_location",
                description=(
                    "Set your working-location presence (office or home) for a single day on the "
                    "primary calendar — the same picker Google Calendar's web UI exposes. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("date", "str", description="ISO 8601 date, e.g. 2026-07-10"),
                    ToolParam("location", "str", description="\"office\" or \"home\""),
                    ToolParam("building_id", "str", required=False, default="",
                              description="Workspace building id (office only; see calendar_list_rooms)"),
                    ToolParam("label", "str", required=False, default="",
                              description="Office label shown on Calendar (office only)"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "calendar_list_calendars":
            return await self._list_calendars()
        if tool == "calendar_list_events":
            return await self._list_events(**args)
        if tool == "calendar_get_free_busy":
            return await self._get_free_busy(**args)
        if tool == "calendar_get_event_details":
            return await self._get_event_details(**args)
        if tool == "calendar_get_event_visibility":
            return await self._get_event_visibility(**args)
        if tool == "calendar_set_event_visibility":
            return await self._set_event_visibility(**args)
        if tool == "calendar_list_rooms":
            return await self._list_rooms(**args)
        if tool == "calendar_create_event":
            return await self._create_event(**args)
        if tool == "calendar_update_event":
            return await self._update_event(**args)
        if tool == "calendar_create_out_of_office":
            return await self._create_out_of_office(**args)
        if tool == "calendar_set_working_location":
            return await self._set_working_location(**args)
        raise ValueError(f"Unknown Calendar tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_calendars(self) -> Any:
        t0 = time.time()
        entries = await self._fetch(self._calendar.list_calendars)
        result = [
            {"id": e.id, "summary": e.summary, "primary": e.primary, "access_role": e.access_role}
            for e in entries
        ]
        self._auto_audit("calendar_list_calendars", "List Calendars",
                         "List all calendars", f"{len(entries)} calendar(s)", t0)
        return result

    async def _list_events(
        self,
        calendar_id: str,
        max_results: int = 20,
        time_min: str = "",
        time_max: str = "",
        query: str = "",
    ) -> Any:
        t0 = time.time()
        events = await self._fetch(
            self._calendar.list_events, calendar_id, max_results, time_min, time_max, query
        )
        result = [
            {
                "id": e.id,
                "title": e.title,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "day_of_week": _day_of_week(e.start_time),
                "all_day": e.all_day,
                "status": e.status,
            }
            for e in events
        ]
        self._auto_audit("calendar_list_events", "List Calendar Events",
                         f"List events: {calendar_id}", f"{len(events)} event(s)", t0)
        return result

    async def _get_free_busy(self, emails: str, time_min: str, time_max: str) -> Any:
        t0 = time.time()
        email_list = [e.strip() for e in emails.split(",") if e.strip()]
        data = await self._fetch(
            self._calendar.get_colleagues_schedule, email_list, time_min, time_max
        )
        if not self.free_busy_full_details:
            data = [_downgrade_to_busy_only(r) for r in data]
        events_count = sum(1 for r in data if r.get("source") == "events")
        fb_count = sum(1 for r in data if r.get("source") == "free_busy")
        summary_note = (
            f"{events_count} with full events, {fb_count} free/busy only"
            if (events_count or fb_count)
            else f"{len(data)} result(s)"
        )
        self._auto_audit("calendar_get_free_busy", "Get Schedule",
                         f"Schedule: {emails}", summary_note, t0)
        return data

    async def _get_event_visibility(self, calendar_id: str, event_id: str) -> Any:
        t0 = time.time()
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        self._auto_audit("calendar_get_event_visibility", "Get Event Visibility",
                         f"Get visibility: {event.title or event_id}", event.visibility, t0)
        return {"visibility": event.visibility}

    async def _list_rooms(self, query: str = "") -> Any:
        t0 = time.time()
        rooms = await self._fetch(self._calendar.list_rooms, query)
        result = [
            {
                "resource_email": r.resource_email,
                "resource_name": r.resource_name,
                "building_id": r.building_id,
                "floor_name": r.floor_name,
                "capacity": r.capacity,
                "description": r.description,
            }
            for r in rooms
        ]
        self._auto_audit("calendar_list_rooms", "List Meeting Rooms",
                         f"List rooms{': ' + query if query else ''}", f"{len(rooms)} room(s)", t0)
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_event_details(self, calendar_id: str, event_id: str) -> Any:
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        attendee_count = len(event.attendees) if hasattr(event, "attendees") else 0
        attachments = event.attachments or []
        preview = {
            "Title": event.title or "(untitled)",
            "Time": f"{event.start_time} – {event.end_time}",
            "Organizer": event.organizer_email or "(unknown)",
            "Attendees": str(attendee_count),
        }
        if attachments:
            preview["Attachments"] = str(len(attachments))
        attendee_lines = [
            f"  {a.display_name or a.email} <{a.email}> [{a.response_status}]"
            for a in (event.attendees or [])
        ]
        attachment_lines = [
            f"  {a.title or '(untitled)'} [{a.mime_type or 'unknown type'}] file_id={a.file_id}"
            for a in attachments
        ]
        details_lines = [
            f"Location: {event.location or '(none)'}",
            f"Conferencing: {event.conference_link or event.hangout_link or '(none)'}",
            "",
            f"Description:\n{event.description or '(none)'}",
            "",
            "Attendees:",
        ] + (attendee_lines or ["  (none)"]) + [
            "",
            "Attachments (use drive_get_file_content with file_id to read):",
        ] + (attachment_lines or ["  (none)"])
        filtered_data = {
            "id": event.id,
            "calendar_id": event.calendar_id,
            "title": event.title,
            "description": event.description,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "day_of_week": _day_of_week(event.start_time),
            "all_day": event.all_day,
            "organizer_email": event.organizer_email,
            "attendees": [
                {"email": a.email, "display_name": a.display_name,
                 "response_status": a.response_status, "organizer": a.organizer}
                for a in (event.attendees or [])
            ],
            "location": event.location,
            "hangout_link": event.hangout_link,
            "conference_link": event.conference_link,
            "status": event.status,
            "html_link": event.html_link,
            "attachments": [
                {"file_id": a.file_id, "title": a.title,
                 "mime_type": a.mime_type, "file_url": a.file_url}
                for a in attachments
            ],
        }
        return await gated_call(
            connector=self.name,
            tool="calendar_get_event_details",
            tool_name="Read Calendar Event",
            summary=f"Read \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=event,
            filtered_data=filtered_data,
            gate="review",
            preview=preview,
            details_text="\n".join(details_lines),
            pii_scan_text=event.description or "",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_event(
        self,
        calendar_id: str,
        title: str,
        start_time: str,
        end_time: str,
        description: str = "",
        attendees: str = "",
        location: str = "",
        add_google_meet: bool = False,
        rooms: str = "",
    ) -> Any:
        attendee_list = [e.strip() for e in attendees.split(",") if e.strip()] if attendees else []
        room_list = [r.strip() for r in rooms.split(",") if r.strip()] if rooms else []
        preview = {
            "Title": title,
            "Time": f"{start_time} – {end_time}",
            "Calendar": await self._calendar_name_for(calendar_id),
        }
        if location:
            preview["Location"] = location
        if add_google_meet:
            preview["Conferencing"] = "Google Meet (will be created)"
        if room_list:
            preview["Rooms"] = ", ".join(room_list)
        if attendee_list:
            preview["Attendees"] = ", ".join(attendee_list)
        raw_data = {
            "calendar_id": calendar_id, "title": title,
            "start_time": start_time, "end_time": end_time,
            "description": description, "attendees": attendee_list,
            "location": location, "add_google_meet": add_google_meet, "rooms": room_list,
        }
        await gated_call(
            connector=self.name,
            tool="calendar_create_event",
            tool_name="Create Calendar Event",
            summary=f"Create \"{title}\" on {start_time}",
            sender=calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=description or "No description provided; see preview for event details.",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "attendees": attendees},
        )
        event = await self._fetch(
            self._calendar.create_event,
            calendar_id, title, start_time, end_time, description,
            attendee_list or None, location, add_google_meet, room_list or None,
        )
        result = {"id": event.id, "title": event.title, "start_time": event.start_time,
                  "end_time": event.end_time, "html_link": event.html_link}
        if event.conference_link or event.hangout_link:
            result["conference_link"] = event.conference_link or event.hangout_link
        return result

    async def _update_event(
        self,
        calendar_id: str,
        event_id: str,
        title: str = "",
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        location: str = "",
        add_google_meet: bool = False,
        rooms: str = "",
    ) -> Any:
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        room_list = [r.strip() for r in rooms.split(",") if r.strip()] if rooms else []
        changes = {}
        if title and title != event.title:
            changes["Title"] = f"{event.title} → {title}"
        if start_time and start_time != event.start_time:
            changes["Start"] = f"{event.start_time} → {start_time}"
        if end_time and end_time != event.end_time:
            changes["End"] = f"{event.end_time} → {end_time}"
        if description and description != event.description:
            changes["Description"] = "(changed)"
        if location and location != event.location:
            changes["Location"] = f"{event.location or '(none)'} → {location}"
        if add_google_meet and not (event.conference_link or event.hangout_link):
            changes["Conferencing"] = "Add Google Meet"
        if room_list:
            changes["Rooms"] = f"Book: {', '.join(room_list)}"
        preview = {"Event": event.title, "Calendar": await self._calendar_name_for(calendar_id), **changes}
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id,
            "current_title": event.title, "new_title": title,
            "start_time": start_time, "end_time": end_time,
            "description": description, "location": location,
            "add_google_meet": add_google_meet, "rooms": room_list,
            "organizer_email": event.organizer_email,
            "attendees": [a.email for a in (event.attendees or [])],
        }
        if description and description != event.description:
            details_text = description
        else:
            changed_fields = ", ".join(changes.keys()) or "no fields"
            details_text = f"{changed_fields} will be updated; description is unchanged."
        await gated_call(
            connector=self.name,
            tool="calendar_update_event",
            tool_name="Update Calendar Event",
            summary=f"Update \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id},
        )
        updated = await self._fetch(
            self._calendar.update_event,
            calendar_id, event_id, title or None, start_time or None,
            end_time or None, description or None, location or None,
            add_google_meet, room_list or None,
        )
        result = {"id": updated.id, "title": updated.title, "start_time": updated.start_time,
                  "end_time": updated.end_time, "html_link": updated.html_link}
        if updated.conference_link or updated.hangout_link:
            result["conference_link"] = updated.conference_link or updated.hangout_link
        return result

    async def _create_out_of_office(
        self,
        start_time: str,
        end_time: str,
        title: str = "Out of Office",
        decline_message: str = "",
    ) -> Any:
        preview = {
            "Title": title,
            "Time": f"{start_time} – {end_time}",
            "Auto-decline": "New conflicting invitations only",
        }
        raw_data = {
            "title": title, "start_time": start_time, "end_time": end_time,
            "decline_message": decline_message,
        }
        await gated_call(
            connector=self.name,
            tool="calendar_create_out_of_office",
            tool_name="Create Out of Office",
            summary=f"Out of Office \"{title}\" {start_time} – {end_time}",
            sender="primary",
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=decline_message or "(no custom decline message)",
            my_email=self.my_email,
            args={},
        )
        event = await self._fetch(
            self._calendar.create_out_of_office, title, start_time, end_time, decline_message,
        )
        return {"id": event.id, "title": event.title, "start_time": event.start_time,
                "end_time": event.end_time, "html_link": event.html_link}

    async def _set_working_location(
        self,
        date: str,
        location: str,
        building_id: str = "",
        label: str = "",
    ) -> Any:
        location_display = {"office": "Office", "home": "Home"}.get(location, location)
        preview = {"Date": date, "Location": location_display}
        if building_id:
            preview["Building"] = building_id
        if label:
            preview["Label"] = label
        raw_data = {"date": date, "location": location, "building_id": building_id, "label": label}
        await gated_call(
            connector=self.name,
            tool="calendar_set_working_location",
            tool_name="Set Working Location",
            summary=f"Set {location_display} on {date}",
            sender="primary",
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Working location will be set as shown above; no other calendar changes.",
            my_email=self.my_email,
            args={},
        )
        event = await self._fetch(
            self._calendar.set_working_location, date, location, building_id, label,
        )
        return {"id": event.id, "start_time": event.start_time, "end_time": event.end_time,
                "html_link": event.html_link}

    async def _set_event_visibility(self, calendar_id: str, event_id: str, visibility: str) -> Any:
        # Validate before gating, not after -- same reasoning as
        # drive_sheets_insert_dimensions's early dimension check: a doomed
        # call shouldn't cost the user an unnecessary approval decision.
        visibility = visibility.strip().lower()
        if visibility not in VALID_VISIBILITIES:
            raise ValueError(
                f"calendar_set_event_visibility: visibility must be one of "
                f"{sorted(VALID_VISIBILITIES)}, got {visibility!r}"
            )
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        preview = {
            "Event": event.title or "(untitled)",
            "Calendar": await self._calendar_name_for(calendar_id),
            "Visibility": f"{event.visibility} → {visibility}",
        }
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id, "visibility": visibility,
            "organizer_email": event.organizer_email,
            "attendees": [a.email for a in (event.attendees or [])],
        }
        await gated_call(
            connector=self.name,
            tool="calendar_set_event_visibility",
            tool_name="Set Event Visibility",
            summary=f"Set visibility of \"{event.title}\" to {visibility}",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Only the event's visibility will change; no other fields are affected.",
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id, "visibility": visibility},
        )
        updated = await self._fetch(self._calendar.set_event_visibility, calendar_id, event_id, visibility)
        return {"id": updated.id, "title": updated.title, "visibility": updated.visibility}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except CalendarClientError as exc:
            logger.error("Calendar fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    async def _calendar_name_for(self, calendar_id: str) -> str:
        """Best-effort, cached calendar display-name lookup; falls back to
        the raw id (e.g. a room resource calendar_list can't resolve this
        way) rather than blocking the popup on a lookup that can't succeed."""
        if calendar_id in self._calendar_name_cache:
            return self._calendar_name_cache[calendar_id]
        try:
            entry = await self._fetch(self._calendar.get_calendar, calendar_id)
            name = entry.summary or calendar_id
        except RuntimeError:
            name = calendar_id
        self._calendar_name_cache[calendar_id] = name
        return name

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
