"""Google Calendar connector: wraps CalendarClient + gated_call."""

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
                description=(
                    "List all Google Calendars for the authenticated user. Auto-approved."
                ),
                params=[],
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
            ),
            ToolSpec(
                name="calendar_get_free_busy",
                description=(
                    "Query free/busy status for a list of email addresses. Auto-approved."
                ),
                params=[
                    ToolParam("emails", "str", description="Comma-separated list of email addresses"),
                    ToolParam("time_min", "str"),
                    ToolParam("time_max", "str"),
                ],
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
            ),
            ToolSpec(
                name="calendar_create_event",
                description=(
                    "Create a new calendar event. Requires user approval."
                ),
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
                description=(
                    "Update an existing calendar event. Requires user approval."
                ),
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
    # Always-allowed
    # ------------------------------------------------------------------ #

    async def _list_calendars(self) -> Any:
        t0 = time.time()
        entries = await self._fetch(self._calendar.list_calendars)
        result = [
            {"id": e.id, "summary": e.summary, "primary": e.primary, "access_role": e.access_role}
            for e in entries
        ]
        self._auto_audit("calendar_list_calendars", "List Calendars", "List all calendars", f"{len(entries)} calendar(s)", t0)
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
        # Only return safe summary fields (no attendees/description/links)
        result = [
            {
                "id": e.id,
                "title": e.title,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "all_day": e.all_day,
                "status": e.status,
            }
            for e in events
        ]
        self._auto_audit(
            "calendar_list_events", "List Calendar Events",
            f"List events: calendar={calendar_id}", f"{len(events)} event(s)", t0,
        )
        return result

    async def _get_free_busy(self, emails: str, time_min: str, time_max: str) -> Any:
        t0 = time.time()
        email_list = [e.strip() for e in emails.split(",") if e.strip()]
        results = await self._fetch(self._calendar.get_free_busy, email_list, time_min, time_max)
        data = [
            {
                "email": r.email,
                "busy": [{"start": s.start, "end": s.end} for s in r.busy],
            }
            for r in results
        ]
        self._auto_audit(
            "calendar_get_free_busy", "Get Free/Busy",
            f"Free/busy: {emails}", f"{len(results)} result(s)", t0,
        )
        return data

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_event_details(self, calendar_id: str, event_id: str) -> Any:
        event = await self._fetch(self._calendar.get_event, calendar_id, event_id)
        filtered_data = {
            "id": event.id,
            "calendar_id": event.calendar_id,
            "title": event.title,
            "description": event.description,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "all_day": event.all_day,
            "organizer_email": event.organizer_email,
            "attendees": [
                {
                    "email": a.email,
                    "display_name": a.display_name,
                    "response_status": a.response_status,
                    "organizer": a.organizer,
                }
                for a in event.attendees
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
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id},
        )

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
        attendee_list = [e.strip() for e in attendees.split(",") if e.strip()] if attendees else None
        # We gate before creating — pass the intent as raw_data
        raw_data = {
            "calendar_id": calendar_id,
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "description": description,
            "attendees": attendee_list or [],
            "location": location,
        }

        async def _execute():
            event = await self._fetch(
                self._calendar.create_event,
                calendar_id, title, start_time, end_time, description, attendee_list, location,
            )
            return {
                "id": event.id,
                "title": event.title,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "html_link": event.html_link,
            }

        return await gated_call(
            connector=self.name,
            tool="calendar_create_event",
            tool_name="Create Calendar Event",
            summary=f"Create \"{title}\" on {start_time}",
            sender=calendar_id,
            raw_data=raw_data,
            filtered_data=raw_data,
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "attendees": attendees},
        )

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
        raw_data = {
            "calendar_id": calendar_id,
            "event_id": event_id,
            "current_title": event.title,
            "new_title": title,
            "start_time": start_time,
            "end_time": end_time,
            "description": description,
            "location": location,
        }
        return await gated_call(
            connector=self.name,
            tool="calendar_update_event",
            tool_name="Update Calendar Event",
            summary=f"Update \"{event.title}\"",
            sender=event.organizer_email or calendar_id,
            raw_data=raw_data,
            filtered_data=raw_data,
            my_email=self.my_email,
            args={"calendar_id": calendar_id, "event_id": event_id},
        )

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
                auto_accept_rule="always_allowed",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
