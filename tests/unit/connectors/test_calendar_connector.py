"""Unit tests for privacyfence.connectors.calendar.CalendarConnector.

Same approach as the Gmail/Drive connector tests: CalendarClient is
mocked, gate.gated_call is stubbed to capture exactly what's sent into
the gate. The data-minimization property under test: calendar_list_events
(auto-approved) must never carry description/attendees -- only the
review-gated calendar_get_event_details is allowed to expose those.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.calendar_client import (
    CalendarAttendee,
    CalendarClientError,
    CalendarEvent,
    CalendarListEntry,
    CalendarRoom,
)
from privacyfence.connectors import calendar as calendar_module
from privacyfence.connectors.calendar import CalendarConnector, _day_of_week


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = CalendarConnector(client)
    connector.my_email = my_email
    return connector, client


def make_event(**overrides):
    defaults = dict(
        id="e1", calendar_id="primary", title="Q3 Planning",
        description="Confidential roadmap discussion.",
        start_time="2026-07-08T10:00:00+00:00", end_time="2026-07-08T11:00:00+00:00",
        all_day=False, organizer_email="alice@example.com",
        attendees=[CalendarAttendee(email="bob@example.com", display_name="Bob", response_status="accepted")],
        location="Room 1", hangout_link="", conference_link="https://meet.example.com/xyz",
        status="confirmed", html_link="https://calendar.google.com/event?eid=e1",
    )
    defaults.update(overrides)
    return CalendarEvent(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(calendar_module, "gated_call", fake_gated_call)
    return calls


class TestDayOfWeek:
    def test_valid_iso_string(self):
        assert _day_of_week("2026-07-06T00:00:00+00:00") == "Monday"

    def test_invalid_string_returns_empty(self):
        assert _day_of_week("not-a-date") == ""

    def test_empty_string_returns_empty(self):
        assert _day_of_week("") == ""


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Calendar tool"):
            await connector.call("calendar_does_not_exist", {})


class TestAutoTools:
    async def test_list_calendars(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_calendars.return_value = [
            CalendarListEntry(id="primary", summary="Me", description="", primary=True, access_role="owner"),
        ]

        result = await connector.call("calendar_list_calendars", {})

        assert result == [{"id": "primary", "summary": "Me", "primary": True, "access_role": "owner"}]
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_list_events_excludes_description_and_attendees(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_events.return_value = [make_event()]

        result = await connector.call("calendar_list_events", {"calendar_id": "primary"})

        assert result == [{
            "id": "e1", "title": "Q3 Planning",
            "start_time": "2026-07-08T10:00:00+00:00", "end_time": "2026-07-08T11:00:00+00:00",
            "day_of_week": "Wednesday", "all_day": False, "status": "confirmed",
        }]
        # Data minimization: the auto-approved list must never carry the
        # description or attendee list -- those require calendar_get_event_details.
        assert "description" not in result[0]
        assert "attendees" not in result[0]
        client.list_events.assert_called_once_with("primary", 20, "", "", "")

    async def test_get_free_busy_summarizes_by_source(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_colleagues_schedule.return_value = [
            {"email": "a@example.com", "source": "events"},
            {"email": "b@example.com", "source": "free_busy"},
        ]

        result = await connector.call(
            "calendar_get_free_busy", {"emails": "a@example.com, b@example.com", "time_min": "t0", "time_max": "t1"}
        )

        assert result == client.get_colleagues_schedule.return_value
        client.get_colleagues_schedule.assert_called_once_with(
            ["a@example.com", "b@example.com"], "t0", "t1"
        )

    async def test_list_rooms(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_rooms.return_value = [
            CalendarRoom(resource_id="r1", resource_name="Boardroom", resource_email="room1@example.com",
                         building_id="HQ", floor_name="3", capacity=10, description="Big room"),
        ]

        result = await connector.call("calendar_list_rooms", {"query": "board"})

        assert result == [{
            "resource_email": "room1@example.com", "resource_name": "Boardroom",
            "building_id": "HQ", "floor_name": "3", "capacity": 10, "description": "Big room",
        }]


class TestGetEventDetails:
    async def test_preview_excludes_description_and_full_attendees(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event()

        await connector.call("calendar_get_event_details", {"calendar_id": "primary", "event_id": "e1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Title": "Q3 Planning", "Time": "2026-07-08T10:00:00+00:00 – 2026-07-08T11:00:00+00:00",
            "Organizer": "alice@example.com", "Attendees": "1",
        }
        assert "Confidential roadmap discussion" not in str(kwargs["preview"])
        assert "bob@example.com" not in str(kwargs["preview"])
        assert "Confidential roadmap discussion" in kwargs["details_text"]
        assert "bob@example.com" in kwargs["details_text"]
        assert kwargs["gate"] == "review"
        assert kwargs["raw_data"] is client.get_event.return_value
        assert kwargs["args"] == {"calendar_id": "primary", "event_id": "e1"}

    async def test_filtered_data_includes_day_of_week_and_full_attendees(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event()

        result = await connector.call("calendar_get_event_details", {"calendar_id": "primary", "event_id": "e1"})

        assert result["day_of_week"] == "Wednesday"
        assert result["attendees"] == [
            {"email": "bob@example.com", "display_name": "Bob", "response_status": "accepted", "organizer": False}
        ]

    async def test_no_attendees_shows_none_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event(attendees=[])

        await connector.call("calendar_get_event_details", {"calendar_id": "primary", "event_id": "e1"})

        assert "  (none)" in gated_call_spy[0]["details_text"]


class TestCreateEvent:
    async def test_preview_omits_absent_optional_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.create_event.return_value = make_event(id="new1")

        await connector.call("calendar_create_event", {
            "calendar_id": "primary", "title": "Sync", "start_time": "t0", "end_time": "t1",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Title": "Sync", "Time": "t0 – t1", "Calendar": "primary"}
        assert kwargs["gate"] == "popup"

    async def test_preview_includes_optional_fields_when_present(self, gated_call_spy):
        connector, client = make_connector()
        client.create_event.return_value = make_event(id="new1")

        await connector.call("calendar_create_event", {
            "calendar_id": "primary", "title": "Sync", "start_time": "t0", "end_time": "t1",
            "location": "HQ", "add_google_meet": True, "rooms": "room1@example.com",
            "attendees": "bob@example.com, eve@external.com",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Location"] == "HQ"
        assert kwargs["preview"]["Conferencing"] == "Google Meet (will be created)"
        assert kwargs["preview"]["Rooms"] == "room1@example.com"
        assert kwargs["preview"]["Attendees"] == "bob@example.com, eve@external.com"
        # raw_data.attendees is the parsed list -- this is what
        # _rule_no_external_attendees actually evaluates against.
        assert kwargs["raw_data"]["attendees"] == ["bob@example.com", "eve@external.com"]

    async def test_result_includes_conference_link_only_when_present(self, gated_call_spy):
        connector, client = make_connector()
        client.create_event.return_value = make_event(id="new1", conference_link="", hangout_link="")

        result = await connector.call("calendar_create_event", {
            "calendar_id": "primary", "title": "Sync", "start_time": "t0", "end_time": "t1",
        })

        assert "conference_link" not in result

        client.create_event.return_value = make_event(id="new2", conference_link="https://meet/xyz")
        result2 = await connector.call("calendar_create_event", {
            "calendar_id": "primary", "title": "Sync2", "start_time": "t0", "end_time": "t1",
        })
        assert result2["conference_link"] == "https://meet/xyz"


class TestUpdateEvent:
    async def test_preview_only_lists_actual_changes(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event(title="Old Title", location="Room A")
        client.update_event.return_value = make_event(id="e1", title="New Title")

        await connector.call("calendar_update_event", {
            "calendar_id": "primary", "event_id": "e1", "title": "New Title",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Event": "Old Title", "Calendar": "primary", "Title": "Old Title → New Title"}
        assert kwargs["gate"] == "popup"

    async def test_no_changes_yields_empty_preview_diff(self, gated_call_spy):
        connector, client = make_connector()
        event = make_event()
        client.get_event.return_value = event
        client.update_event.return_value = event

        await connector.call("calendar_update_event", {"calendar_id": "primary", "event_id": "e1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Event": "Q3 Planning", "Calendar": "primary"}

    async def test_add_google_meet_skipped_if_conferencing_already_exists(self, gated_call_spy):
        connector, client = make_connector()
        event = make_event(conference_link="https://existing")
        client.get_event.return_value = event
        client.update_event.return_value = event

        await connector.call(
            "calendar_update_event",
            {"calendar_id": "primary", "event_id": "e1", "add_google_meet": True},
        )

        assert "Conferencing" not in gated_call_spy[0]["preview"]


class TestFetchErrorMapping:
    async def test_calendar_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_calendars.side_effect = CalendarClientError("auth expired")

        with pytest.raises(RuntimeError, match="auth expired"):
            await connector.call("calendar_list_calendars", {})
