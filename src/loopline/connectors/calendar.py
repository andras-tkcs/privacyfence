"""Google Calendar connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..calendar_client import CalendarClient, CalendarClientError
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import gated_call

logger = logging.getLogger(__name__)


def _day_of_week(iso_str: str) -> str:
    """Return the full weekday name for an ISO 8601 datetime string."""
    try:
        return datetime.fromisoformat(iso_str).strftime("%A")
    except (ValueError, TypeError):
        return ""


class CalendarConnector(Connector):
    def __init__(self, client: CalendarClient) -> None:
        self._calendar = client
        self.my_email: str = ""

    @property
    def name(self) -> str:
        return "calendar"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="calendar_list_calendars",
                description="List all Google Calendars for the authenticated user. Auto-approved.",
                params=[],
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
                ],
                read_only=True,
            ),
            ToolSpec(
                name="calendar_get_event_details",
                description=(
                    "Fetch full details of a calendar event including attendees, description, "
                    "and conferencing links. Requires user approval."
                ),
                params=[
                    ToolParam("calendar_id", "str"),
                    ToolParam("event_id", "str"),
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
        if tool == "calendar_create_event":
            return await self._create_event(**args)
        if tool == "calendar_update_event":
            return await self._update_event(**args)
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

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_event_details(self, calendar_id: str, event_id: str) -> Any:
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        attendee_count = len(event.attendees) if hasattr(event, "attendees") else 0
        preview = {
            "Title": event.title or "(untitled)",
            "Time": f"{event.start_time} – {event.end_time}",
            "Organizer": event.organizer_email or "(unknown)",
            "Attendees": str(attendee_count),
        }
        attendee_lines = [
            f"  {a.display_name or a.email} <{a.email}> [{a.response_status}]"
            for a in (event.attendees or [])
        ]
        details_lines = [
            f"Title: {event.title}",
            f"Time: {event.start_time} – {event.end_time}",
            f"Organizer: {event.organizer_email}",
            f"Location: {event.location or '(none)'}",
            f"Conferencing: {event.conference_link or event.hangout_link or '(none)'}",
            "",
            f"Description:\n{event.description or '(none)'}",
            "",
            "Attendees:",
        ] + (attendee_lines or ["  (none)"])
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
    ) -> Any:
        attendee_list = [e.strip() for e in attendees.split(",") if e.strip()] if attendees else []
        details_lines = [
            f"Title: {title}",
            f"Time: {start_time} – {end_time}",
            f"Calendar: {calendar_id}",
        ]
        if location:
            details_lines.append(f"Location: {location}")
        if attendee_list:
            details_lines.append(f"Attendees: {', '.join(attendee_list)}")
        if description:
            details_lines += ["", f"Description:\n{description}"]
        raw_data = {
            "calendar_id": calendar_id, "title": title,
            "start_time": start_time, "end_time": end_time,
            "description": description, "attendees": attendee_list, "location": location,
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
            details_text="\n".join(details_lines),
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "attendees": attendees},
        )
        event = await self._fetch(
            self._calendar.create_event,
            calendar_id, title, start_time, end_time, description,
            attendee_list or None, location,
        )
        return {"id": event.id, "title": event.title, "start_time": event.start_time,
                "end_time": event.end_time, "html_link": event.html_link}

    async def _update_event(
        self,
        calendar_id: str,
        event_id: str,
        title: str = "",
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        location: str = "",
    ) -> Any:
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
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
        changes_text = "\n".join(f"  {k}: {v}" for k, v in changes.items()) or "  (no changes)"
        details = f"Event: {event.title}\nCalendar: {calendar_id}\n\nChanges:\n{changes_text}"
        raw_data = {
            "calendar_id": calendar_id, "event_id": event_id,
            "current_title": event.title, "new_title": title,
            "start_time": start_time, "end_time": end_time,
            "description": description, "location": location,
        }
        await gated_call(
            connector=self.name,
            tool="calendar_update_event",
            tool_name="Update Calendar Event",
            summary=f"Update \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id},
        )
        updated = await self._fetch(
            self._calendar.update_event,
            calendar_id, event_id, title or None, start_time or None,
            end_time or None, description or None, location or None,
        )
        return {"id": updated.id, "title": updated.title, "start_time": updated.start_time,
                "end_time": updated.end_time, "html_link": updated.html_link}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except CalendarClientError as exc:
            logger.error("Calendar fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

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
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
