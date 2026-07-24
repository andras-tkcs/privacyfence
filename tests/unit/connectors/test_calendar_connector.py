"""Unit tests for privacyfence.connectors.calendar.CalendarConnector.

Same approach as the Gmail/Drive connector tests: CalendarClient is
mocked, gate.gated_call is stubbed to capture exactly what's sent into
the gate. The data-minimization property under test: calendar_list_events
(auto-approved) must never carry description/attendees -- only the
review-gated calendar_get_event_details is allowed to expose those.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.calendar_client import (
    CalendarAttachment,
    CalendarAttendee,
    CalendarClient,
    CalendarClientError,
    CalendarEvent,
    CalendarListEntry,
)
from privacyfence.connectors import calendar as calendar_module
from privacyfence.connectors.calendar import CalendarConnector, _day_of_week

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields

LIVE_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "live" / "calendar"


def make_connector(my_email="me@example.com", rooms=None):
    client = MagicMock()
    # Default to "not resolvable" so tests that don't care about calendar-name
    # resolution keep seeing the raw calendar id, same as before this was added.
    client.get_calendar.side_effect = CalendarClientError("no such calendar")
    connector = CalendarConnector(client, rooms=rooms)
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

    async def test_list_rooms_filters_the_static_org_config_directory(self, tmp_path):
        """No network call -- calendar_list_rooms now serves org_config.json's
        synced "rooms" list (see daemon_main.build_connectors), so the client
        is never touched here."""
        init_audit_logger(str(tmp_path))
        rooms = [
            {"resource_email": "room1@example.com", "resource_name": "Boardroom",
             "building_id": "HQ", "floor_name": "3", "capacity": 10, "description": "Big room"},
            {"resource_email": "room2@example.com", "resource_name": "Focus Room",
             "building_id": "Annex", "floor_name": "1", "capacity": 2, "description": ""},
        ]
        connector, client = make_connector(rooms=rooms)

        result = await connector.call("calendar_list_rooms", {"query": "board"})

        assert result == [rooms[0]]
        client.list_rooms.assert_not_called()

    async def test_list_rooms_query_matches_building(self, tmp_path):
        init_audit_logger(str(tmp_path))
        rooms = [
            {"resource_email": "room1@example.com", "resource_name": "Boardroom",
             "building_id": "HQ", "floor_name": "3", "capacity": 10, "description": "Big room"},
            {"resource_email": "room2@example.com", "resource_name": "Focus Room",
             "building_id": "Annex", "floor_name": "1", "capacity": 2, "description": ""},
        ]
        connector, client = make_connector(rooms=rooms)

        result = await connector.call("calendar_list_rooms", {"query": "annex"})

        assert result == [rooms[1]]

    async def test_list_rooms_empty_when_org_config_has_no_rooms_synced_yet(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()

        result = await connector.call("calendar_list_rooms", {"query": ""})

        assert result == []

    async def test_get_event_visibility_auto_accepts_and_returns_only_visibility(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_event.return_value = make_event(visibility="private")

        result = await connector.call(
            "calendar_get_event_visibility", {"calendar_id": "primary", "event_id": "e1"}
        )

        assert result == {"visibility": "private"}
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]
        client.get_event.assert_called_once_with("primary", "e1")


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

    async def test_pii_scan_text_is_description_only_not_organizer_or_attendees(self, gated_call_spy):
        # organizer_email/attendee emails are present on every event
        # regardless of content -- the PII scan must not see them, only
        # the description.
        connector, client = make_connector()
        client.get_event.return_value = make_event(description="nothing sensitive")

        await connector.call("calendar_get_event_details", {"calendar_id": "primary", "event_id": "e1"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "nothing sensitive"
        assert kwargs["preview"]["Organizer"] == "alice@example.com"  # still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]
        assert "bob@example.com" not in kwargs["pii_scan_text"]  # attendee

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

    async def test_no_attachments_omits_preview_field_and_shows_none_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event(attachments=[])

        await connector.call("calendar_get_event_details", {"calendar_id": "primary", "event_id": "e1"})

        kwargs = gated_call_spy[0]
        assert "Attachments" not in kwargs["preview"]
        assert "Attachments (use drive_get_file_content with file_id to read):" in kwargs["details_text"]
        assert kwargs["filtered_data"]["attachments"] == []

    async def test_attachments_surfaced_in_preview_details_and_filtered_data(self, gated_call_spy):
        # This is what lets an agent get at the "Notes by Gemini" / transcript
        # docs Google Meet attaches to an event after a meeting ends: the
        # file_id here can be handed straight to drive_get_file_content.
        connector, client = make_connector()
        client.get_event.return_value = make_event(attachments=[
            CalendarAttachment(
                file_id="doc123",
                title="Notes by Gemini - Q3 Planning",
                mime_type="application/vnd.google-apps.document",
                file_url="https://docs.google.com/document/d/doc123/edit",
            ),
            CalendarAttachment(
                file_id="doc456",
                title="Transcript - Q3 Planning",
                mime_type="application/vnd.google-apps.document",
                file_url="https://docs.google.com/document/d/doc456/edit",
            ),
        ])

        result = await connector.call(
            "calendar_get_event_details", {"calendar_id": "primary", "event_id": "e1"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Attachments"] == "2"
        assert "Notes by Gemini - Q3 Planning" in kwargs["details_text"]
        assert "file_id=doc123" in kwargs["details_text"]
        assert "Transcript - Q3 Planning" in kwargs["details_text"]
        assert "file_id=doc456" in kwargs["details_text"]
        assert result["attachments"] == [
            {
                "file_id": "doc123", "title": "Notes by Gemini - Q3 Planning",
                "mime_type": "application/vnd.google-apps.document",
                "file_url": "https://docs.google.com/document/d/doc123/edit",
            },
            {
                "file_id": "doc456", "title": "Transcript - Q3 Planning",
                "mime_type": "application/vnd.google-apps.document",
                "file_url": "https://docs.google.com/document/d/doc456/edit",
            },
        ]


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

    async def test_calendar_name_resolved_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_calendar.side_effect = None
        client.get_calendar.return_value = CalendarListEntry(
            id="c_abc@group.calendar.google.com", summary="Team Offsite",
            description="", primary=False, access_role="reader",
        )
        client.create_event.return_value = make_event(id="new1")

        await connector.call("calendar_create_event", {
            "calendar_id": "c_abc@group.calendar.google.com", "title": "Sync", "start_time": "t0", "end_time": "t1",
        })

        assert gated_call_spy[0]["preview"]["Calendar"] == "Team Offsite"

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

    async def test_calendar_name_resolved_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_calendar.side_effect = None
        client.get_calendar.return_value = CalendarListEntry(
            id="c_abc@group.calendar.google.com", summary="Team Offsite",
            description="", primary=False, access_role="reader",
        )
        client.get_event.return_value = make_event()
        client.update_event.return_value = make_event()

        await connector.call("calendar_update_event", {
            "calendar_id": "c_abc@group.calendar.google.com", "event_id": "e1",
        })

        assert gated_call_spy[0]["preview"]["Calendar"] == "Team Offsite"

    async def test_details_text_shows_description_when_description_changed(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event(description="Old description")
        client.update_event.return_value = make_event()

        await connector.call("calendar_update_event", {
            "calendar_id": "primary", "event_id": "e1", "description": "New description",
        })

        assert gated_call_spy[0]["details_text"] == "New description"

    async def test_details_text_is_a_literal_when_description_unchanged(self, gated_call_spy):
        # Regression: details_text used to fall back to "" here, which
        # gate.py's fallback turns into a raw JSON dump of the update payload.
        connector, client = make_connector()
        client.get_event.return_value = make_event(title="Old Title", location="Room A")
        client.update_event.return_value = make_event()

        await connector.call("calendar_update_event", {
            "calendar_id": "primary", "event_id": "e1", "title": "New Title",
        })

        assert gated_call_spy[0]["details_text"] == "Title will be updated; description is unchanged."
        assert "{" not in gated_call_spy[0]["details_text"]

    async def test_details_text_when_nothing_changed(self, gated_call_spy):
        connector, client = make_connector()
        event = make_event()
        client.get_event.return_value = event
        client.update_event.return_value = event

        await connector.call("calendar_update_event", {"calendar_id": "primary", "event_id": "e1"})

        assert gated_call_spy[0]["details_text"] == "no fields will be updated; description is unchanged."


class TestCreateOutOfOffice:
    async def test_preview_omits_decline_message_when_absent(self, gated_call_spy):
        connector, client = make_connector()
        client.create_out_of_office.return_value = make_event(id="ooo1")

        await connector.call("calendar_create_out_of_office", {
            "start_time": "t0", "end_time": "t1", "title": "Vacation",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Title": "Vacation", "Time": "t0 – t1",
            "Auto-decline": "New conflicting invitations only",
        }
        assert kwargs["gate"] == "popup"
        assert "Decline message" not in kwargs["preview"]

    async def test_details_shows_decline_message_when_given(self, gated_call_spy):
        # Decline message is content, not metadata -- it belongs only in
        # details_text, never duplicated into the preview dict.
        connector, client = make_connector()
        client.create_out_of_office.return_value = make_event(id="ooo1")

        await connector.call("calendar_create_out_of_office", {
            "start_time": "t0", "end_time": "t1", "decline_message": "Back Monday",
        })

        assert "Decline message" not in gated_call_spy[0]["preview"]
        assert gated_call_spy[0]["details_text"] == "Back Monday"

    async def test_default_title_used_when_not_given(self, gated_call_spy):
        connector, client = make_connector()
        client.create_out_of_office.return_value = make_event(id="ooo1")

        await connector.call("calendar_create_out_of_office", {"start_time": "t0", "end_time": "t1"})

        assert gated_call_spy[0]["preview"]["Title"] == "Out of Office"
        client.create_out_of_office.assert_called_once_with("Out of Office", "t0", "t1", "")

    async def test_result_shape(self, gated_call_spy):
        connector, client = make_connector()
        client.create_out_of_office.return_value = make_event(id="ooo1", title="Vacation")

        result = await connector.call("calendar_create_out_of_office", {"start_time": "t0", "end_time": "t1"})

        assert result["id"] == "ooo1"
        assert result["title"] == "Vacation"


class TestSetWorkingLocation:
    async def test_home_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.set_working_location.return_value = make_event(id="wl1")

        await connector.call("calendar_set_working_location", {"date": "2026-08-01", "location": "home"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Date": "2026-08-01", "Location": "Home"}
        assert kwargs["gate"] == "popup"
        client.set_working_location.assert_called_once_with("2026-08-01", "home", "", "")

    async def test_office_preview_includes_building_and_label_only_when_given(self, gated_call_spy):
        connector, client = make_connector()
        client.set_working_location.return_value = make_event(id="wl1")

        await connector.call("calendar_set_working_location", {
            "date": "2026-08-01", "location": "office", "building_id": "b1", "label": "HQ Floor 3",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Location"] == "Office"
        assert kwargs["preview"]["Building"] == "b1"
        assert kwargs["preview"]["Label"] == "HQ Floor 3"

    async def test_result_shape(self, gated_call_spy):
        connector, client = make_connector()
        client.set_working_location.return_value = make_event(id="wl1")

        result = await connector.call("calendar_set_working_location", {"date": "2026-08-01", "location": "home"})

        assert result["id"] == "wl1"
        assert "title" not in result


class TestSetEventVisibility:
    async def test_preview_shows_visibility_transition(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event(visibility="default")
        client.set_event_visibility.return_value = make_event(visibility="private")

        await connector.call(
            "calendar_set_event_visibility",
            {"calendar_id": "primary", "event_id": "e1", "visibility": "private"},
        )

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "popup"
        assert kwargs["preview"]["Visibility"] == "default → private"
        assert kwargs["preview"]["Event"] == "Q3 Planning"
        client.set_event_visibility.assert_called_once_with("primary", "e1", "private")

    async def test_value_normalized_before_gating(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event()
        client.set_event_visibility.return_value = make_event(visibility="public")

        await connector.call(
            "calendar_set_event_visibility",
            {"calendar_id": "primary", "event_id": "e1", "visibility": "  PUBLIC  "},
        )

        assert gated_call_spy[0]["args"]["visibility"] == "public"
        client.set_event_visibility.assert_called_once_with("primary", "e1", "public")

    async def test_invalid_visibility_rejected_before_gate(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="visibility must be one of"):
            await connector.call(
                "calendar_set_event_visibility",
                {"calendar_id": "primary", "event_id": "e1", "visibility": "hidden"},
            )

        assert gated_call_spy == []
        client.get_event.assert_not_called()
        client.set_event_visibility.assert_not_called()

    async def test_result_shape(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event()
        client.set_event_visibility.return_value = make_event(id="e1", visibility="private")

        result = await connector.call(
            "calendar_set_event_visibility",
            {"calendar_id": "primary", "event_id": "e1", "visibility": "private"},
        )

        assert result == {"id": "e1", "title": "Q3 Planning", "visibility": "private"}

    async def test_raw_data_carries_organizer_for_auto_accept_rules(self, gated_call_spy):
        connector, client = make_connector()
        client.get_event.return_value = make_event(organizer_email="alice@example.com")
        client.set_event_visibility.return_value = make_event()

        await connector.call(
            "calendar_set_event_visibility",
            {"calendar_id": "primary", "event_id": "e1", "visibility": "private"},
        )

        assert gated_call_spy[0]["raw_data"]["organizer_email"] == "alice@example.com"
        assert gated_call_spy[0]["args"] == {
            "calendar_id": "primary", "event_id": "e1", "visibility": "private",
        }

    async def test_raw_data_carries_attendees_for_auto_accept_rules(self, gated_call_spy):
        # calendar.set_visibility is auto-accept-gated like any other event
        # update (i_am_organizer, no_external_attendees, personal_calendar) --
        # raw_data.attendees is what _rule_no_external_attendees evaluates
        # against, same as calendar_update_event's raw_data.
        connector, client = make_connector()
        client.get_event.return_value = make_event(attendees=[
            CalendarAttendee(email="bob@example.com", display_name="Bob", response_status="accepted"),
        ])
        client.set_event_visibility.return_value = make_event()

        await connector.call(
            "calendar_set_event_visibility",
            {"calendar_id": "primary", "event_id": "e1", "visibility": "private"},
        )

        assert gated_call_spy[0]["raw_data"]["attendees"] == ["bob@example.com"]


class TestFieldCompleteness:
    """End to end: a fully-populated raw Calendar API event -> the real
    CalendarClient._parse_event -> the real connector's popup preview -- not
    a hand-built CalendarEvent, unlike every other test in this file. Mirrors
    test_confluence_connector.py's TestFieldCompleteness -- the shape of
    check that would catch a _parse_event field mapping silently degrading
    to a fallback before it ships, not after.
    """

    async def test_get_event_details_preview_has_no_placeholder_fields(self, gated_call_spy):
        path = LIVE_FIXTURES_DIR / "get_event.json"
        if not path.exists():
            pytest.skip(f"{path} not recorded yet -- run `python3 scripts/qa_fixture_recorder.py --record calendar` locally first")
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The recorded fixture has no attendees -- add one so the Attendees
        # preview field carries a real (non-zero) value too.
        raw = dict(raw, attendees=[
            {"email": "bob@example.com", "displayName": "Bob", "responseStatus": "accepted"},
        ])

        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = raw
        client = CalendarClient(client_config={}, token_file="/tmp/unused-token.json")
        # get_event() runs inside a worker thread (connector._fetch uses
        # asyncio.to_thread), so client._local.service -- thread-local --
        # wouldn't be visible there; overriding _get_service directly is the
        # thread-agnostic equivalent of test_calendar_client.py's make_client().
        client._get_service = lambda: service

        connector = CalendarConnector(client)
        connector.my_email = "me@example.com"
        await connector.call("calendar_get_event_details", {"calendar_id": "primary", "event_id": raw["id"]})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestFetchErrorMapping:
    async def test_calendar_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_calendars.side_effect = CalendarClientError("auth expired")

        with pytest.raises(RuntimeError, match="auth expired"):
            await connector.call("calendar_list_calendars", {})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # calendar_get_event_visibility is auto-audited (real JSON
        # serialization, unlike the gated_call stub) and reads event.visibility
        # into the audit "sender" field -- a bare MagicMock isn't serializable.
        client.get_event.return_value = make_event()

        await assert_all_tools_leave_an_audit_trail(
            connector, calendar_module, monkeypatch, tmp_path,
            arg_overrides={
                # visibility must be one of VALID_VISIBILITIES -- validated
                # before gating.
                "calendar_set_event_visibility": {"visibility": "private"},
            },
        )
