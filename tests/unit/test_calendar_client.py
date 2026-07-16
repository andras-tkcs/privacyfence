"""Tests for CalendarClient's parsing/normalization logic: event
normalization (all-day detection, attendees, conference links), timezone
handling on create/update, room listing, and the events->free/busy fallback
logic in get_colleagues_schedule. As with the Gmail/Drive client tests, these
call real CalendarClient methods against a MagicMock stand-in for the
googleapiclient service object.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from privacyfence.calendar_client import (
    CalendarAttachment,
    CalendarAttendee,
    CalendarClient,
    CalendarClientError,
    CalendarEvent,
    CalendarListEntry,
    CalendarRoom,
    FreeBusyResult,
    FreeBusySlot,
    _has_timezone,
)
from googleapiclient.errors import HttpError

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "calendar"


def make_client(service: MagicMock) -> CalendarClient:
    client = CalendarClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


# ---------------------------------------------------------------------------- #
# _has_timezone
# ---------------------------------------------------------------------------- #

class TestHasTimezone:
    def test_offset_present(self):
        assert _has_timezone("2024-01-01T10:00:00+02:00") is True

    def test_utc_z_suffix_present(self):
        assert _has_timezone("2024-01-01T10:00:00Z") is True

    def test_naive_datetime_has_no_timezone(self):
        assert _has_timezone("2024-01-01T10:00:00") is False

    def test_date_only_string_has_no_timezone(self):
        assert _has_timezone("2024-01-01") is False

    def test_garbage_string_is_treated_as_no_timezone(self):
        assert _has_timezone("not a date") is False


# ---------------------------------------------------------------------------- #
# _parse_event
# ---------------------------------------------------------------------------- #

class TestParseEvent:
    def test_timed_event_with_attendees_and_organizer(self):
        client = make_client(MagicMock())
        raw = {
            "id": "e1", "summary": "Standup", "description": "daily sync",
            "start": {"dateTime": "2024-01-01T10:00:00Z"},
            "end": {"dateTime": "2024-01-01T10:30:00Z"},
            "organizer": {"email": "boss@x.com"},
            "attendees": [
                {"email": "a@x.com", "displayName": "A", "responseStatus": "accepted"},
                {"email": "boss@x.com", "responseStatus": "accepted", "organizer": True},
            ],
            "location": "Room 1", "status": "confirmed", "htmlLink": "https://cal/e1",
        }
        event = client._parse_event(raw, "primary")
        assert event.all_day is False
        assert event.organizer_email == "boss@x.com"
        assert event.attendees == [
            CalendarAttendee(email="a@x.com", display_name="A", response_status="accepted", organizer=False),
            CalendarAttendee(email="boss@x.com", display_name="", response_status="accepted", organizer=True),
        ]
        assert event.short_summary() == "Standup (2024-01-01T10:00:00Z)"

    def test_all_day_event_detected_via_date_field(self):
        client = make_client(MagicMock())
        raw = {"id": "e1", "summary": "Holiday", "start": {"date": "2024-01-01"}, "end": {"date": "2024-01-02"}}
        event = client._parse_event(raw, "primary")
        assert event.all_day is True
        assert event.start_time == "2024-01-01"
        assert event.end_time == "2024-01-02"

    def test_attendee_missing_response_status_defaults_needs_action(self):
        client = make_client(MagicMock())
        raw = {"id": "e1", "attendees": [{"email": "a@x.com"}]}
        event = client._parse_event(raw, "primary")
        assert event.attendees[0].response_status == "needsAction"

    def test_conference_video_link_extracted_first_matching_entry_point(self):
        client = make_client(MagicMock())
        raw = {
            "id": "e1",
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "phone", "uri": "tel:12345"},
                    {"entryPointType": "video", "uri": "https://meet.google.com/abc"},
                ]
            },
        }
        event = client._parse_event(raw, "primary")
        assert event.conference_link == "https://meet.google.com/abc"

    def test_no_conference_data_yields_empty_link(self):
        client = make_client(MagicMock())
        event = client._parse_event({"id": "e1"}, "primary")
        assert event.conference_link == ""

    def test_missing_status_defaults_to_confirmed(self):
        client = make_client(MagicMock())
        event = client._parse_event({"id": "e1"}, "primary")
        assert event.status == "confirmed"

    def test_missing_visibility_defaults_to_default(self):
        client = make_client(MagicMock())
        event = client._parse_event({"id": "e1"}, "primary")
        assert event.visibility == "default"

    def test_visibility_parsed_from_raw_event(self):
        client = make_client(MagicMock())
        event = client._parse_event({"id": "e1", "visibility": "private"}, "primary")
        assert event.visibility == "private"

    def test_calendar_id_is_carried_from_caller_not_the_payload(self):
        client = make_client(MagicMock())
        event = client._parse_event({"id": "e1"}, "someone@x.com")
        assert event.calendar_id == "someone@x.com"

    def test_no_attachments_yields_empty_list(self):
        client = make_client(MagicMock())
        event = client._parse_event({"id": "e1"}, "primary")
        assert event.attachments == []

    def test_attachments_parsed_from_raw_event(self):
        # This is the shape Google Meet's Gemini note-taker attaches after a
        # meeting ends: a "Notes by Gemini" Doc and a transcript Doc.
        client = make_client(MagicMock())
        raw = {
            "id": "e1",
            "attachments": [
                {
                    "fileId": "doc123",
                    "title": "Notes by Gemini - Q3 Planning - 2026/07/08",
                    "mimeType": "application/vnd.google-apps.document",
                    "fileUrl": "https://docs.google.com/document/d/doc123/edit",
                    "iconLink": "https://icon.example/doc.png",
                },
                {
                    "fileId": "doc456",
                    "title": "Transcript - Q3 Planning - 2026/07/08",
                    "mimeType": "application/vnd.google-apps.document",
                    "fileUrl": "https://docs.google.com/document/d/doc456/edit",
                },
            ],
        }
        event = client._parse_event(raw, "primary")
        assert event.attachments == [
            CalendarAttachment(
                file_id="doc123",
                title="Notes by Gemini - Q3 Planning - 2026/07/08",
                mime_type="application/vnd.google-apps.document",
                file_url="https://docs.google.com/document/d/doc123/edit",
                icon_link="https://icon.example/doc.png",
            ),
            CalendarAttachment(
                file_id="doc456",
                title="Transcript - Q3 Planning - 2026/07/08",
                mime_type="application/vnd.google-apps.document",
                file_url="https://docs.google.com/document/d/doc456/edit",
                icon_link="",
            ),
        ]


# ---------------------------------------------------------------------------- #
# list_calendars / list_events / get_event
# ---------------------------------------------------------------------------- #

class TestListCalendars:
    def test_maps_response(self):
        service = MagicMock()
        service.calendarList.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "primary", "summary": "Me", "primary": True, "accessRole": "owner"}]
        }
        client = make_client(service)
        entries = client.list_calendars()
        assert entries == [
            CalendarListEntry(id="primary", summary="Me", description="", primary=True, access_role="owner")
        ]

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.calendarList.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="list_calendars failed"):
            client.list_calendars()


class TestGetCalendar:
    def test_requires_calendar_id(self):
        client = make_client(MagicMock())
        with pytest.raises(CalendarClientError, match="requires a calendar_id"):
            client.get_calendar("")

    def test_maps_response(self):
        service = MagicMock()
        service.calendarList.return_value.get.return_value.execute.return_value = {
            "id": "c_abc@group.calendar.google.com", "summary": "Team Offsite",
            "primary": False, "accessRole": "reader",
        }
        client = make_client(service)

        result = client.get_calendar("c_abc@group.calendar.google.com")

        assert result == CalendarListEntry(
            id="c_abc@group.calendar.google.com", summary="Team Offsite",
            description="", primary=False, access_role="reader",
        )
        service.calendarList.return_value.get.assert_called_once_with(
            calendarId="c_abc@group.calendar.google.com"
        )

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.calendarList.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="get_calendar\\(c1\\) failed"):
            client.get_calendar("c1")


class TestListEvents:
    def test_clamps_max_results_into_1_to_250(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": []}
        client = make_client(service)
        client.list_events("primary", max_results=10000)
        assert service.events.return_value.list.call_args.kwargs["maxResults"] == 250

    def test_optional_filters_only_included_when_given(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": []}
        client = make_client(service)
        client.list_events("primary")
        kwargs = service.events.return_value.list.call_args.kwargs
        assert "timeMin" not in kwargs
        assert "timeMax" not in kwargs
        assert "q" not in kwargs

    def test_optional_filters_included_when_given(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": []}
        client = make_client(service)
        client.list_events("primary", time_min="a", time_max="b", query="standup")
        kwargs = service.events.return_value.list.call_args.kwargs
        assert kwargs["timeMin"] == "a"
        assert kwargs["timeMax"] == "b"
        assert kwargs["q"] == "standup"

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="list_events"):
            client.list_events("primary")


class TestGetEvent:
    def test_missing_ids_raise(self):
        client = make_client(MagicMock())
        with pytest.raises(CalendarClientError, match="requires calendar_id and event_id"):
            client.get_event("", "e1")
        with pytest.raises(CalendarClientError, match="requires calendar_id and event_id"):
            client.get_event("primary", "")

    def test_fetches_and_normalizes(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1", "summary": "Hi"}
        client = make_client(service)
        event = client.get_event("primary", "e1")
        assert event.title == "Hi"

    def test_does_not_pass_supports_attachments(self):
        # events.get doesn't accept supportsAttachments at all (only
        # insert/update/patch/import do) -- passing it raises a client-side
        # TypeError in the real googleapiclient before any request is sent,
        # since a bare MagicMock silently accepts any kwarg and wouldn't have
        # caught that regression on its own (see test_rejects_unknown_kwargs
        # below for a fake that actually enforces the real API's signature).
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.get_event("primary", "e1")
        assert "supportsAttachments" not in service.events.return_value.get.call_args.kwargs

    def test_rejects_unknown_kwargs(self):
        # A stand-in for events().get() that enforces the real Calendar API
        # v3 signature (calendarId, eventId, alwaysIncludeEmail, maxAttendees,
        # timeZone) -- unlike a MagicMock, it actually raises on an
        # unsupported kwarg like supportsAttachments, the way
        # google-api-python-client's discovery-generated methods do.
        allowed = {"calendarId", "eventId", "alwaysIncludeEmail", "maxAttendees", "timeZone"}

        class StrictEventsGet:
            def __call__(self, **kwargs):
                unknown = set(kwargs) - allowed
                if unknown:
                    raise TypeError(f"Got an unexpected keyword argument {sorted(unknown)!r}")
                return self

            def execute(self):
                return {"id": "e1"}

        service = MagicMock()
        service.events.return_value.get = StrictEventsGet()
        client = make_client(service)
        event = client.get_event("primary", "e1")
        assert event.id == "e1"

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="get_event"):
            client.get_event("primary", "e1")


# ---------------------------------------------------------------------------- #
# get_free_busy
# ---------------------------------------------------------------------------- #

class TestGetFreeBusy:
    def test_empty_emails_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(CalendarClientError, match="non-empty emails"):
            client.get_free_busy([], "a", "b")

    def test_maps_busy_periods_per_email(self):
        service = MagicMock()
        service.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {
                "a@x.com": {"busy": [{"start": "10:00", "end": "11:00"}]},
                "b@x.com": {"busy": []},
            }
        }
        client = make_client(service)
        result = client.get_free_busy(["a@x.com", "b@x.com"], "tmin", "tmax")
        assert result == [
            FreeBusyResult(email="a@x.com", busy=[FreeBusySlot(start="10:00", end="11:00")]),
            FreeBusyResult(email="b@x.com", busy=[]),
        ]

    def test_email_absent_from_response_yields_empty_busy(self):
        service = MagicMock()
        service.freebusy.return_value.query.return_value.execute.return_value = {"calendars": {}}
        client = make_client(service)
        result = client.get_free_busy(["a@x.com"], "tmin", "tmax")
        assert result == [FreeBusyResult(email="a@x.com", busy=[])]

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.freebusy.return_value.query.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="get_free_busy failed"):
            client.get_free_busy(["a@x.com"], "tmin", "tmax")


# ---------------------------------------------------------------------------- #
# get_colleagues_schedule: events -> free/busy fallback
# ---------------------------------------------------------------------------- #

class TestGetColleaguesSchedule:
    def test_uses_events_source_when_calendar_accessible(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "e1", "summary": "Standup", "start": {"dateTime": "t1"}, "end": {"dateTime": "t2"}}]
        }
        client = make_client(service)
        result = client.get_colleagues_schedule(["a@x.com"], "tmin", "tmax")
        assert result == [{
            "email": "a@x.com", "source": "events",
            "events": [{"id": "e1", "title": "Standup", "start_time": "t1", "end_time": "t2",
                        "status": "confirmed", "all_day": False}],
        }]

    def test_falls_back_to_free_busy_when_events_list_fails(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.side_effect = http_error(403)
        service.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {"a@x.com": {"busy": [{"start": "10:00", "end": "11:00"}]}}
        }
        client = make_client(service)
        result = client.get_colleagues_schedule(["a@x.com"], "tmin", "tmax")
        assert result == [{"email": "a@x.com", "source": "free_busy", "busy": [{"start": "10:00", "end": "11:00"}]}]

    def test_free_busy_also_failing_yields_error_entries_per_email(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.side_effect = http_error(403)
        service.freebusy.return_value.query.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        result = client.get_colleagues_schedule(["a@x.com", "b@x.com"], "tmin", "tmax")
        assert len(result) == 2
        assert all(r["source"] == "error" for r in result)
        assert all(r["email"] in {"a@x.com", "b@x.com"} for r in result)

    def test_mixed_accessible_and_inaccessible_calendars(self):
        service = MagicMock()
        def events_side_effect(**kwargs):
            mock = MagicMock()
            if kwargs["calendarId"] == "ok@x.com":
                mock.execute.return_value = {"items": []}
            else:
                mock.execute.side_effect = http_error(403)
            return mock
        service.events.return_value.list.side_effect = events_side_effect
        service.freebusy.return_value.query.return_value.execute.return_value = {
            "calendars": {"blocked@x.com": {"busy": []}}
        }
        client = make_client(service)
        result = client.get_colleagues_schedule(["ok@x.com", "blocked@x.com"], "tmin", "tmax")
        sources = {r["email"]: r["source"] for r in result}
        assert sources == {"ok@x.com": "events", "blocked@x.com": "free_busy"}


# ---------------------------------------------------------------------------- #
# list_rooms
# ---------------------------------------------------------------------------- #

class TestListRooms:
    def test_maps_response(self):
        directory_service = MagicMock()
        directory_service.resources.return_value.calendars.return_value.list.return_value.execute.return_value = {
            "items": [{
                "resourceId": "r1", "resourceName": "Room A", "resourceEmail": "room-a@x.com",
                "buildingId": "b1", "floorName": "3", "capacity": "10",
                "generatedResourceName": "Room A (3rd floor)",
            }]
        }
        client = make_client(MagicMock())
        client._get_directory_service = lambda: directory_service

        rooms = client.list_rooms()

        assert rooms == [CalendarRoom(
            resource_id="r1", resource_name="Room A", resource_email="room-a@x.com",
            building_id="b1", floor_name="3", capacity=10, description="Room A (3rd floor)",
        )]

    def test_403_gives_actionable_admin_access_message(self):
        directory_service = MagicMock()
        directory_service.resources.return_value.calendars.return_value.list.return_value.execute.side_effect = (
            http_error(403)
        )
        client = make_client(MagicMock())
        client._get_directory_service = lambda: directory_service

        with pytest.raises(CalendarClientError, match="Workspace admin access"):
            client.list_rooms()

    def test_other_http_error_gives_generic_message(self):
        directory_service = MagicMock()
        directory_service.resources.return_value.calendars.return_value.list.return_value.execute.side_effect = (
            http_error(500)
        )
        client = make_client(MagicMock())
        client._get_directory_service = lambda: directory_service

        with pytest.raises(CalendarClientError, match="list_rooms failed"):
            client.list_rooms()

    def test_query_param_included_only_when_given(self):
        directory_service = MagicMock()
        directory_service.resources.return_value.calendars.return_value.list.return_value.execute.return_value = {
            "items": []
        }
        client = make_client(MagicMock())
        client._get_directory_service = lambda: directory_service

        client.list_rooms()
        assert "query" not in directory_service.resources.return_value.calendars.return_value.list.call_args.kwargs

        client.list_rooms(query="floor 3")
        assert directory_service.resources.return_value.calendars.return_value.list.call_args.kwargs["query"] == "floor 3"


# ---------------------------------------------------------------------------- #
# create_event: timezone injection + attendees/rooms/conferencing
# ---------------------------------------------------------------------------- #

class TestCreateEvent:
    def test_injects_utc_when_no_timezone_in_datetime(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "Meeting", "2024-01-01T10:00:00", "2024-01-01T11:00:00")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["start"] == {"dateTime": "2024-01-01T10:00:00", "timeZone": "UTC"}
        assert body["end"] == {"dateTime": "2024-01-01T11:00:00", "timeZone": "UTC"}

    def test_preserves_existing_offset_without_injecting_utc(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "Meeting", "2024-01-01T10:00:00+02:00", "2024-01-01T11:00:00+02:00")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert "timeZone" not in body["start"]
        assert "timeZone" not in body["end"]

    def test_attendees_only_no_rooms(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "M", "t1", "t2", attendees=["a@x.com", "b@x.com"])
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@x.com"}]

    def test_room_emails_marked_as_resources(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "M", "t1", "t2", attendees=["a@x.com"], room_emails=["room@x.com"])
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["attendees"] == [{"email": "a@x.com"}, {"email": "room@x.com", "resource": True}]

    def test_no_attendees_or_rooms_omits_attendees_key(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "M", "t1", "t2")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert "attendees" not in body

    def test_google_meet_adds_conference_data_and_version_kwarg(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "M", "t1", "t2", add_google_meet=True)
        call_kwargs = service.events.return_value.insert.call_args.kwargs
        assert call_kwargs["conferenceDataVersion"] == 1
        assert call_kwargs["body"]["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"

    def test_description_and_location_included_only_when_given(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)
        client.create_event("primary", "M", "t1", "t2")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert "description" not in body
        assert "location" not in body

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="create_event"):
            client.create_event("primary", "M", "t1", "t2")


# ---------------------------------------------------------------------------- #
# update_event: partial field updates + room replacement + conferencing
# ---------------------------------------------------------------------------- #

class TestUpdateEvent:
    def test_get_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="update_event get"):
            client.update_event("primary", "e1", title="New")

    def test_only_provided_fields_are_changed(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {
            "id": "e1", "summary": "Old", "description": "Old desc", "location": "Old loc",
        }
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1", "summary": "New"}
        client = make_client(service)

        client.update_event("primary", "e1", title="New")

        updated_body = service.events.return_value.update.call_args.kwargs["body"]
        assert updated_body["summary"] == "New"
        assert updated_body["description"] == "Old desc"
        assert updated_body["location"] == "Old loc"

    def test_start_time_update_defaults_timezone_to_utc(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1"}
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)

        client.update_event("primary", "e1", start_time="2024-01-01T10:00:00")

        body = service.events.return_value.update.call_args.kwargs["body"]
        assert body["start"] == {"dateTime": "2024-01-01T10:00:00", "timeZone": "UTC"}

    def test_room_emails_replace_existing_room_attendees_only(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {
            "id": "e1",
            "attendees": [
                {"email": "person@x.com"},
                {"email": "old-room@x.com", "resource": True},
            ],
        }
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)

        client.update_event("primary", "e1", room_emails=["new-room@x.com"])

        body = service.events.return_value.update.call_args.kwargs["body"]
        assert body["attendees"] == [{"email": "person@x.com"}, {"email": "new-room@x.com", "resource": True}]

    def test_add_google_meet_only_when_not_already_present(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1"}
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)

        client.update_event("primary", "e1", add_google_meet=True)

        call_kwargs = service.events.return_value.update.call_args.kwargs
        assert call_kwargs["conferenceDataVersion"] == 1
        assert "conferenceData" in call_kwargs["body"]

    def test_existing_conference_data_preserves_version_kwarg_without_re_adding(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {
            "id": "e1", "conferenceData": {"already": "there"},
        }
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)

        client.update_event("primary", "e1", add_google_meet=False)

        call_kwargs = service.events.return_value.update.call_args.kwargs
        assert call_kwargs["conferenceDataVersion"] == 1
        assert call_kwargs["body"]["conferenceData"] == {"already": "there"}

    def test_update_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1"}
        service.events.return_value.update.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="update_event\\(e1\\)"):
            client.update_event("primary", "e1", title="x")


# ---------------------------------------------------------------------------- #
# set_event_visibility: only the visibility field changes
# ---------------------------------------------------------------------------- #

class TestSetEventVisibility:
    def test_invalid_visibility_raises_before_any_api_call(self):
        service = MagicMock()
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="visibility must be one of"):
            client.set_event_visibility("primary", "e1", "hidden")
        service.events.return_value.get.assert_not_called()

    def test_value_is_normalized_case_and_whitespace(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1", "summary": "Standup"}
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1", "visibility": "private"}
        client = make_client(service)

        client.set_event_visibility("primary", "e1", "  PRIVATE  ")

        body = service.events.return_value.update.call_args.kwargs["body"]
        assert body["visibility"] == "private"

    def test_only_visibility_field_changes_other_fields_preserved(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {
            "id": "e1", "summary": "Standup", "description": "daily sync", "location": "Room 1",
        }
        service.events.return_value.update.return_value.execute.return_value = {"id": "e1"}
        client = make_client(service)

        client.set_event_visibility("primary", "e1", "public")

        body = service.events.return_value.update.call_args.kwargs["body"]
        assert body["visibility"] == "public"
        assert body["summary"] == "Standup"
        assert body["description"] == "daily sync"
        assert body["location"] == "Room 1"

    def test_returns_parsed_updated_event(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1"}
        service.events.return_value.update.return_value.execute.return_value = {
            "id": "e1", "summary": "Standup", "visibility": "confidential",
        }
        client = make_client(service)

        event = client.set_event_visibility("primary", "e1", "confidential")

        assert event.visibility == "confidential"
        assert event.title == "Standup"

    def test_get_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="set_event_visibility get"):
            client.set_event_visibility("primary", "e1", "private")

    def test_update_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {"id": "e1"}
        service.events.return_value.update.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="set_event_visibility update"):
            client.set_event_visibility("primary", "e1", "private")


# ---------------------------------------------------------------------------- #
# create_out_of_office: always the "new conflicts only" autoDeclineMode,
# always on the primary calendar
# ---------------------------------------------------------------------------- #

class TestCreateOutOfOffice:
    def test_sets_event_type_and_fixed_auto_decline_mode(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "ooo1"}
        client = make_client(service)

        client.create_out_of_office("Vacation", "2026-08-01T00:00:00", "2026-08-05T00:00:00")

        call_kwargs = service.events.return_value.insert.call_args.kwargs
        assert call_kwargs["calendarId"] == "primary"
        body = call_kwargs["body"]
        assert body["eventType"] == "outOfOffice"
        assert body["outOfOfficeProperties"] == {
            "autoDeclineMode": "declineOnlyNewConflictingInvitations",
        }
        assert body["transparency"] == "opaque"
        assert body["summary"] == "Vacation"

    def test_decline_message_included_only_when_given(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "ooo1"}
        client = make_client(service)

        client.create_out_of_office("Vacation", "t0", "t1")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert "declineMessage" not in body["outOfOfficeProperties"]

        client.create_out_of_office("Vacation", "t0", "t1", decline_message="Back Monday")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["outOfOfficeProperties"]["declineMessage"] == "Back Monday"

    def test_injects_utc_when_no_timezone_in_datetime(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "ooo1"}
        client = make_client(service)

        client.create_out_of_office("Vacation", "2026-08-01T00:00:00", "2026-08-05T00:00:00")

        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["start"] == {"dateTime": "2026-08-01T00:00:00", "timeZone": "UTC"}
        assert body["end"] == {"dateTime": "2026-08-05T00:00:00", "timeZone": "UTC"}

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="create_out_of_office failed"):
            client.create_out_of_office("Vacation", "t0", "t1")


# ---------------------------------------------------------------------------- #
# set_working_location: office/home presence, primary calendar only
# ---------------------------------------------------------------------------- #

class TestSetWorkingLocation:
    def test_invalid_location_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(CalendarClientError, match="location must be 'office' or 'home'"):
            client.set_working_location("2026-08-01", "beach")

    def test_home_office_sets_type_and_visibility(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "wl1"}
        client = make_client(service)

        client.set_working_location("2026-08-01", "home")

        call_kwargs = service.events.return_value.insert.call_args.kwargs
        assert call_kwargs["calendarId"] == "primary"
        body = call_kwargs["body"]
        assert body["eventType"] == "workingLocation"
        assert body["visibility"] == "public"
        assert body["transparency"] == "transparent"
        assert body["workingLocationProperties"] == {"type": "homeOffice"}
        assert body["start"] == {"date": "2026-08-01"}
        assert body["end"] == {"date": "2026-08-02"}  # exclusive end: day after start

    def test_office_location_includes_building_and_label_only_when_given(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "wl1"}
        client = make_client(service)

        client.set_working_location("2026-08-01", "office")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["workingLocationProperties"] == {"type": "officeLocation", "officeLocation": {}}

        client.set_working_location("2026-08-01", "office", building_id="b1", label="HQ Floor 3")
        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["workingLocationProperties"]["officeLocation"] == {
            "buildingId": "b1", "label": "HQ Floor 3",
        }

    def test_http_error_becomes_calendar_client_error(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(CalendarClientError, match="set_working_location failed"):
            client.set_working_location("2026-08-01", "home")

    def test_end_date_rolls_over_month_boundary(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "wl1"}
        client = make_client(service)

        client.set_working_location("2026-08-31", "home")

        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["start"] == {"date": "2026-08-31"}
        assert body["end"] == {"date": "2026-09-01"}


# ---------------------------------------------------------------------------- #
# _get_service / _get_directory_service: must not share one service (and its
# underlying httplib2 transport) across threads, since concurrent requests
# dispatched via asyncio.to_thread corrupt a shared connection
# (SSL: WRONG_VERSION_NUMBER).
# ---------------------------------------------------------------------------- #

class TestServiceIsThreadLocal:
    def test_each_thread_gets_its_own_service_instance(self):
        client = CalendarClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.calendar_client.build") as mock_build, \
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

    def test_same_thread_reuses_cached_service(self):
        client = CalendarClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.calendar_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()
            assert client._get_service() is client._get_service()
            assert mock_build.call_count == 1


class TestLiveFixtureParsing:
    """Replays a fixture recorded from a real, [QATEST]-tagged seed event by
    scripts/qa_fixture_recorder.py --record calendar -- real API shape, not
    hand-authored, with organizer/attendee identity already redacted.
    Skipped (not failed) until that fixture exists; see
    tests/fixtures/live/README.md and
    docs/external-api-contract-testing.md's Part A/B. Re-record via that
    script if this ever starts failing after a genuine Calendar API
    change.
    """

    def test_get_event_fixture_still_parses(self):
        path = LIVE_FIXTURES_DIR / "get_event.json"
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record calendar` locally first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        client = make_client(MagicMock())

        event = client._parse_event(raw, "primary")

        assert event.title and event.start_time and event.organizer_email
