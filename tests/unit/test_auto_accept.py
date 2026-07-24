"""Unit tests for the auto-accept rule engine (privacyfence.auto_accept).

This module is the core privacy control of PrivacyFence: every _rule_*
function decides whether a request skips human review. Each rule gets a
positive and a negative case, plus the malformed/missing-data edge cases
the implementation explicitly guards against.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
import yaml
from freezegun import freeze_time

from privacyfence import auto_accept
from privacyfence.auto_accept import (
    ARGS_ONLY_RULES,
    DATA_DEPENDENT_RULES,
    TOOL_TO_GATE,
    TOOL_TO_OPERATION,
    AutoAcceptEvaluator,
    TEMP_ACCEPT_ELIGIBLE_OPERATIONS,
    add_auto_accept_rule,
    describe_rule,
    describe_rule_change,
    get_auto_accept_evaluator,
    get_current_config,
    init_auto_accept_evaluator,
    init_config_path,
    mutate_grants,
    reload_rules,
    remove_auto_accept_rule,
    set_rules_changed_listener,
    suggest_rule,
    temp_accept_key,
)

from ..helpers import make_ctx


# --------------------------------------------------------------------------- #
# TOOL_TO_OPERATION: every popup/review-gated write tool needs an entry here,
# or a rule configured under its "natural" dotted name in settings.yaml
# silently never matches -- gate.py falls back to the raw f"{connector}.{tool}"
# key instead (see test_gate.py::test_operation_key_falls_back_to_connector_dot_tool).
# --------------------------------------------------------------------------- #

class TestToolToOperationMapping:
    def test_jira_transition_issue_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["jira_transition_issue"] == "jira.transition_issue"

    def test_calendar_create_out_of_office_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["calendar_create_out_of_office"] == "calendar.out_of_office"

    def test_calendar_set_working_location_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["calendar_set_working_location"] == "calendar.working_location"

    def test_jira_transition_issue_gets_approved_project_keys_via_issue_key(self):
        # jira_transition_issue reuses the same generic rule jira_update_issue/
        # jira_get_issue already rely on -- no new rule code needed, just the
        # operation-key mapping above so a configured rule actually gets looked up.
        ev = AutoAcceptEvaluator({"jira.transition_issue": [{"rule": "approved_project_keys", "value": ["ENG"]}]})
        ctx = make_ctx(tool="jira_transition_issue", args={"issue_key": "ENG-42", "transition_name": "Done"})
        ok, matched = ev.should_auto_accept(TOOL_TO_OPERATION["jira_transition_issue"], ctx)
        assert ok is True
        assert matched == "approved_project_keys"

    def test_sheets_dimension_tools_map_to_clean_operation_keys(self):
        assert TOOL_TO_OPERATION["drive_sheets_insert_dimensions"] == "sheets.insert_dimensions"
        assert TOOL_TO_OPERATION["drive_sheets_delete_dimensions"] == "sheets.delete_dimensions"

    def test_docs_edit_and_format_tools_map_to_clean_operation_keys(self):
        assert TOOL_TO_OPERATION["drive_docs_edit_content"] == "docs.edit_content"
        assert TOOL_TO_OPERATION["drive_docs_format_content"] == "docs.format_content"

    def test_salesforce_search_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["salesforce_search"] == "salesforce.search"

    def test_calendar_set_event_visibility_maps_to_its_own_operation_key(self):
        assert TOOL_TO_OPERATION["calendar_set_event_visibility"] == "calendar.set_visibility"


# --------------------------------------------------------------------------- #
# Gmail rules
# --------------------------------------------------------------------------- #

class TestGmailRules:
    def test_i_am_sender_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(sender="Me <me@example.com>"),
        )
        assert ev._rule_i_am_sender(None, ctx) is True

    def test_i_am_sender_no_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(sender="someone-else@example.com"),
        )
        assert ev._rule_i_am_sender(None, ctx) is False

    def test_i_am_sender_requires_my_email(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="", raw_data=SimpleNamespace(sender=""))
        assert ev._rule_i_am_sender(None, ctx) is False

    def test_i_am_sole_recipient_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(recipients=["Me <me@example.com>"]),
        )
        assert ev._rule_i_am_sole_recipient(None, ctx) is True

    def test_i_am_sole_recipient_multiple_recipients(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(recipients=["me@example.com", "other@example.com"]),
        )
        assert ev._rule_i_am_sole_recipient(None, ctx) is False

    @pytest.mark.parametrize(
        "sender,allowlist,expected",
        [
            ("Alice <alice@trusted.com>", ["trusted.com"], True),
            ("alice@trusted.com", ["trusted.com"], True),
            ("Alice <alice@untrusted.com>", ["trusted.com"], False),
            ("Alice <alice@Trusted.COM>", ["trusted.com"], True),
            ("Alice <alice@trusted.com>", [], False),
            ("Netflix <info@members.netflix.com>", ["netflix.com"], True),
            ("Alice <alice@mail.members.trusted.com>", ["trusted.com"], True),
            ("Alice <alice@eviltrusted.com>", ["trusted.com"], False),
        ],
    )
    def test_trusted_sender_domain(self, sender, allowlist, expected):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(raw_data=SimpleNamespace(sender=sender))
        assert ev._rule_trusted_sender_domain(allowlist, ctx) is expected

    def test_label_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(raw_data=SimpleNamespace(labels=["INBOX", "Newsletter"]))
        assert ev._rule_label_match(["newsletter"], ctx) is True
        assert ev._rule_label_match(["promotions"], ctx) is False
        assert ev._rule_label_match(None, ctx) is False

    @freeze_time("2026-07-06 12:00:00", tz_offset=0)
    def test_age_threshold_days(self):
        ev = AutoAcceptEvaluator({})
        old = make_ctx(raw_data=SimpleNamespace(date="Mon, 01 Jan 2024 12:00:00 +0000"))
        recent = make_ctx(raw_data=SimpleNamespace(date="Mon, 01 Jul 2026 12:00:00 +0000"))
        missing = make_ctx(raw_data=SimpleNamespace(date=""))
        malformed = make_ctx(raw_data=SimpleNamespace(date="not-a-date"))

        assert ev._rule_age_threshold_days(30, old) is True
        assert ev._rule_age_threshold_days(30, recent) is False
        assert ev._rule_age_threshold_days(30, missing) is False
        assert ev._rule_age_threshold_days(30, malformed) is False
        assert ev._rule_age_threshold_days(0, old) is False  # falsy value short-circuits

    def test_no_attachments(self):
        ev = AutoAcceptEvaluator({})
        assert ev._rule_no_attachments(None, make_ctx(raw_data=SimpleNamespace(attachments=[]))) is True
        assert ev._rule_no_attachments(None, make_ctx(raw_data=SimpleNamespace(attachments=["a.pdf"]))) is False
        assert ev._rule_no_attachments(None, make_ctx(raw_data=SimpleNamespace())) is True


# --------------------------------------------------------------------------- #
# Drive rules
# --------------------------------------------------------------------------- #

class TestDriveRules:
    def test_i_am_owner_and_created_by_me_alias(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(owners=["me@example.com"]),
        )
        assert ev._rule_i_am_owner(None, ctx) is True
        assert ev._rule_created_by_me(None, ctx) is True

    def test_i_am_owner_unwraps_raw_from_file_attr(self):
        # Some callers pass a wrapper object with a `.file` attribute instead
        # of the file object directly (_file_from handles both).
        ev = AutoAcceptEvaluator({})
        wrapped = SimpleNamespace(file=SimpleNamespace(owners=["me@example.com"]))
        ctx = make_ctx(my_email="me@example.com", raw_data=wrapped)
        assert ev._rule_i_am_owner(None, ctx) is True

    def test_approved_folder_variants(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(raw_data=SimpleNamespace(parent_ids=["folder1", "folder2"]))
        assert ev._rule_approved_folder(["folder1"], ctx) is True
        assert ev._rule_approved_folder(["folder9"], ctx) is False
        assert ev._rule_approved_folder([], ctx) is False
        # aliases evaluate identically
        assert ev._rule_approved_sandbox_folder(["folder1"], ctx) is True
        assert ev._rule_move_within_approved_folders(["folder1"], ctx) is True

    def test_file_type_allowlist(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(raw_data=SimpleNamespace(mime_type="application/pdf"))
        assert ev._rule_file_type_allowlist(["application/pdf"], ctx) is True
        assert ev._rule_file_type_allowlist(["text/plain"], ctx) is False

    def test_created_this_session(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            raw_data=SimpleNamespace(id="file123"),
            session_created_ids={"file123"},
        )
        assert ev._rule_created_this_session(None, ctx) is True
        ctx2 = make_ctx(raw_data=SimpleNamespace(id="other"), session_created_ids={"file123"})
        assert ev._rule_created_this_session(None, ctx2) is False

    def test_shared_drive_exclusion(self):
        ev = AutoAcceptEvaluator({})
        assert ev._rule_shared_drive_exclusion(None, make_ctx(raw_data=SimpleNamespace(shared=False))) is True
        assert ev._rule_shared_drive_exclusion(None, make_ctx(raw_data=SimpleNamespace(shared=True))) is False
        # Missing `shared` attribute defaults closed (not shared -> auto-accept allowed)
        assert ev._rule_shared_drive_exclusion(None, make_ctx(raw_data=SimpleNamespace())) is True


# --------------------------------------------------------------------------- #
# Slack rules
# --------------------------------------------------------------------------- #

class TestSlackRules:
    def test_dm_with_myself_and_alias(self):
        ev = AutoAcceptEvaluator({})
        dm_ctx = make_ctx(args={"channel_id": "D12345"})
        channel_ctx = make_ctx(args={"channel_id": "C12345"})
        assert ev._rule_dm_with_myself(None, dm_ctx) is True
        assert ev._rule_dm_with_myself(None, channel_ctx) is False
        assert ev._rule_send_to_myself(None, dm_ctx) is True

    def test_group_dm(self):
        ev = AutoAcceptEvaluator({})
        group_ctx = make_ctx(args={"channel_id": "G123", "is_group_dm": True})
        channel_ctx = make_ctx(args={"channel_id": "C123", "is_group_dm": False})
        no_flag_ctx = make_ctx(args={"channel_id": "G123"})
        assert ev._rule_group_dm(None, group_ctx) is True
        assert ev._rule_group_dm(None, channel_ctx) is False
        assert ev._rule_group_dm(None, no_flag_ctx) is False

    def test_approved_channel_and_alias(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"channel_id": "C123"})
        assert ev._rule_approved_channel(["C123"], ctx) is True
        assert ev._rule_approved_channel(["C999"], ctx) is False
        assert ev._rule_approved_recipient(["C123"], ctx) is True
        # falls back to `channel` key when `channel_id` absent
        ctx2 = make_ctx(args={"channel": "C123"})
        assert ev._rule_approved_channel(["C123"], ctx2) is True

    def test_public_channels_only(self):
        ev = AutoAcceptEvaluator({})
        public = make_ctx(raw_data=[SimpleNamespace(is_private=False), SimpleNamespace(is_private=False)])
        mixed = make_ctx(raw_data=[SimpleNamespace(is_private=False), SimpleNamespace(is_private=True)])
        single = make_ctx(raw_data=SimpleNamespace(is_private=False))
        assert ev._rule_public_channels_only(None, public) is True
        assert ev._rule_public_channels_only(None, mixed) is False
        assert ev._rule_public_channels_only(None, single) is True

    def test_no_file_attachments(self):
        ev = AutoAcceptEvaluator({})
        clean = make_ctx(raw_data=[SimpleNamespace(files=None), SimpleNamespace(files=[])])
        dirty = make_ctx(raw_data=[SimpleNamespace(files=["img.png"])])
        assert ev._rule_no_file_attachments(None, clean) is True
        assert ev._rule_no_file_attachments(None, dirty) is False

    def test_reply_in_existing_thread(self):
        ev = AutoAcceptEvaluator({})
        assert ev._rule_reply_in_existing_thread(None, make_ctx(args={"thread_ts": "123.45"})) is True
        assert ev._rule_reply_in_existing_thread(None, make_ctx(args={})) is False

    def test_approved_channel_all_results(self):
        ev = AutoAcceptEvaluator({})
        all_approved = make_ctx(raw_data=[SimpleNamespace(channel_id="C1"), SimpleNamespace(channel_id="C2")])
        one_unapproved = make_ctx(raw_data=[SimpleNamespace(channel_id="C1"), SimpleNamespace(channel_id="C9")])
        empty = make_ctx(raw_data=[])
        assert ev._rule_approved_channel_all_results(["C1", "C2"], all_approved) is True
        assert ev._rule_approved_channel_all_results(["C1", "C2"], one_unapproved) is False
        assert ev._rule_approved_channel_all_results(["C1", "C2"], empty) is False
        assert ev._rule_approved_channel_all_results([], all_approved) is False

    def test_approved_channel_all_results_single_item_not_a_list(self):
        # A non-list raw_data (a single message, not search results) is
        # treated as a one-item list, same as the other all()-over-list rules.
        ev = AutoAcceptEvaluator({})
        single = make_ctx(raw_data=SimpleNamespace(channel_id="C1"))
        assert ev._rule_approved_channel_all_results(["C1"], single) is True


# --------------------------------------------------------------------------- #
# Calendar rules
# --------------------------------------------------------------------------- #

class TestCalendarRules:
    def test_i_am_organizer(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(organizer_email="me@example.com"))
        other = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(organizer_email="other@example.com"))
        assert ev._rule_i_am_organizer(None, ctx) is True
        assert ev._rule_i_am_organizer(None, other) is False

    def test_no_external_attendees_dict_and_object_forms(self):
        ev = AutoAcceptEvaluator({})
        ctx_dict = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(attendees=[{"email": "a@example.com"}, {"email": "b@example.com"}]),
        )
        ctx_obj = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(attendees=[SimpleNamespace(email="a@example.com")]),
        )
        ctx_external = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(attendees=[{"email": "a@external.com"}]),
        )
        assert ev._rule_no_external_attendees(None, ctx_dict) is True
        assert ev._rule_no_external_attendees(None, ctx_obj) is True
        assert ev._rule_no_external_attendees(None, ctx_external) is False

    def test_no_external_attendees_requires_my_domain(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="", raw_data=SimpleNamespace(attendees=[]))
        assert ev._rule_no_external_attendees(None, ctx) is False

    def test_personal_calendar(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"calendar_id": "primary"})
        assert ev._rule_personal_calendar(["primary"], ctx) is True
        assert ev._rule_personal_calendar(["work"], ctx) is False

    @freeze_time("2026-07-06 12:00:00", tz_offset=0)
    def test_past_event(self):
        ev = AutoAcceptEvaluator({})
        past = make_ctx(raw_data=SimpleNamespace(end_time="2020-01-01T00:00:00Z"))
        future = make_ctx(raw_data=SimpleNamespace(end_time="2030-01-01T00:00:00Z"))
        missing = make_ctx(raw_data=SimpleNamespace(end_time=""))
        assert ev._rule_past_event(None, past) is True
        assert ev._rule_past_event(None, future) is False
        assert ev._rule_past_event(None, missing) is False

    @freeze_time("2026-07-06 12:00:00", tz_offset=0)
    def test_time_window_days(self):
        ev = AutoAcceptEvaluator({})
        soon = make_ctx(raw_data=SimpleNamespace(start_time="2026-07-08T12:00:00Z"))
        far = make_ctx(raw_data=SimpleNamespace(start_time="2026-08-08T12:00:00Z"))
        assert ev._rule_time_window_days(7, soon) is True
        assert ev._rule_time_window_days(7, far) is False
        assert ev._rule_time_window_days(0, soon) is False  # falsy value short-circuits

    def test_no_conferencing_link(self):
        ev = AutoAcceptEvaluator({})
        assert ev._rule_no_conferencing_link(None, make_ctx(raw_data=SimpleNamespace(conference_link=""))) is True
        assert ev._rule_no_conferencing_link(None, make_ctx(raw_data=SimpleNamespace(hangout_link="https://x"))) is False

    def test_non_private_event_reads_current_visibility_from_raw_data(self):
        # calendar_get_event_details (and any other read) has no "visibility"
        # arg -- falls back to the event's current visibility.
        ev = AutoAcceptEvaluator({})
        private = make_ctx(args={}, raw_data=SimpleNamespace(visibility="private"))
        public = make_ctx(args={}, raw_data=SimpleNamespace(visibility="public"))
        default = make_ctx(args={}, raw_data=SimpleNamespace(visibility="default"))
        assert ev._rule_non_private_event(None, private) is False
        assert ev._rule_non_private_event(None, public) is True
        assert ev._rule_non_private_event(None, default) is True

    def test_non_private_event_missing_visibility_attribute_defaults_non_private(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={}, raw_data=SimpleNamespace())
        assert ev._rule_non_private_event(None, ctx) is True


# --------------------------------------------------------------------------- #
# Salesforce rules
# --------------------------------------------------------------------------- #

class TestSalesforceRules:
    def test_approved_object_types(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"object_type": "Account"})
        assert ev._rule_approved_object_types(["account"], ctx) is True
        assert ev._rule_approved_object_types(["contact"], ctx) is False
        assert ev._rule_approved_object_types([], ctx) is False

    def test_approved_object_types_for_search_requires_every_requested_type_approved(self):
        # salesforce.search carries a comma-separated object_types arg
        # instead of get_record's singular object_type -- a partial match
        # (some but not all requested types approved) must not auto-accept,
        # since that would silently leak an unapproved object type's results.
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"object_types": "Opportunity,Contact"})
        assert ev._rule_approved_object_types(["opportunity", "contact"], ctx) is True
        assert ev._rule_approved_object_types(["opportunity"], ctx) is False
        assert ev._rule_approved_object_types([], ctx) is False

    def test_approved_object_types_for_search_empty_object_types_never_matches(self):
        # An unscoped search (no object_types given) reaches Salesforce's
        # whole default set of globally-searchable objects -- never
        # auto-accepted by an object-type allowlist.
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"object_types": ""})
        assert ev._rule_approved_object_types(["opportunity"], ctx) is False

    def test_approved_report_ids(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"report_id": "00O123"})
        assert ev._rule_approved_report_ids(["00O123"], ctx) is True
        assert ev._rule_approved_report_ids(["00O999"], ctx) is False


# --------------------------------------------------------------------------- #
# Gmail write rules
# --------------------------------------------------------------------------- #

class TestGmailWriteRules:
    def test_to_is_myself_single_string(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", args={"to": "Me <me@example.com>"})
        assert ev._rule_to_is_myself(None, ctx) is True

    def test_to_is_myself_list_all_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", args={"to": ["me@example.com", "Me <me@example.com>"]})
        assert ev._rule_to_is_myself(None, ctx) is True

    def test_to_is_myself_list_one_external_fails(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", args={"to": ["me@example.com", "other@example.com"]})
        assert ev._rule_to_is_myself(None, ctx) is False

    def test_to_is_myself_empty_recipients_fails(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", args={"to": ""})
        assert ev._rule_to_is_myself(None, ctx) is False

    def test_approved_recipient_domain_all_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"to": ["Alice <alice@trusted.com>", "bob@trusted.com"]})
        assert ev._rule_approved_recipient_domain(["trusted.com"], ctx) is True

    def test_approved_recipient_domain_one_external_fails(self):
        # This is the reply-all safety property: a trusted sender being CC'd
        # doesn't authorize an unrelated external Cc that slips through.
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"to": ["alice@trusted.com", "eve@external.com"]})
        assert ev._rule_approved_recipient_domain(["trusted.com"], ctx) is False

    def test_approved_recipient_domain_no_recipients_fails(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"to": []})
        assert ev._rule_approved_recipient_domain(["trusted.com"], ctx) is False

    def test_label_name_allowlist(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"label_name": "Newsletter"})
        assert ev._rule_label_name_allowlist(["newsletter"], ctx) is True
        assert ev._rule_label_name_allowlist(["promotions"], ctx) is False
        assert ev._rule_label_name_allowlist([], ctx) is False


# --------------------------------------------------------------------------- #
# Drive write rules
# --------------------------------------------------------------------------- #

class TestDriveWriteRules:
    def test_parent_folder_allowlist(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"parent_folder_id": "folderA"})
        assert ev._rule_parent_folder_allowlist(["folderA"], ctx) is True
        assert ev._rule_parent_folder_allowlist(["folderB"], ctx) is False
        assert ev._rule_parent_folder_allowlist([], ctx) is False


# --------------------------------------------------------------------------- #
# Sheets rules: approved_spreadsheet + the _sheet_tab_of helper it shares
# with suggest_rule
# --------------------------------------------------------------------------- #

class TestSheetTabOf:
    def test_sheet_id_arg_takes_priority(self):
        # format_range carries both sheet_id and a range_a1 with no "!"
        # prefix, so sheet_id must be checked first.
        ctx = make_ctx(args={"sheet_id": 0, "range_a1": "A1:C10"})
        assert auto_accept._sheet_tab_of(ctx) == "0"

    def test_range_a1_unquoted_sheet_name_prefix(self):
        ctx = make_ctx(args={"range_a1": "Sheet1!A1:C10"})
        assert auto_accept._sheet_tab_of(ctx) == "Sheet1"

    def test_range_a1_quoted_sheet_name_prefix(self):
        ctx = make_ctx(args={"range_a1": "'My Tab'!A1:C10"})
        assert auto_accept._sheet_tab_of(ctx) == "My Tab"

    def test_range_a1_without_prefix_yields_empty(self):
        ctx = make_ctx(args={"range_a1": "A1:C10"})
        assert auto_accept._sheet_tab_of(ctx) == ""

    def test_no_relevant_args_yields_empty(self):
        # add_sheet has no existing tab to identify.
        ctx = make_ctx(args={"title": "New Tab"})
        assert auto_accept._sheet_tab_of(ctx) == ""


class TestSheetsRules:
    def test_matches_by_spreadsheet_id_alone_when_entry_has_no_tab(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet([{"spreadsheet_id": "sheet1"}], ctx) is True

    def test_no_match_for_a_different_spreadsheet_id(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"spreadsheet_id": "sheet2", "range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet([{"spreadsheet_id": "sheet1"}], ctx) is False

    def test_tab_scoped_entry_matches_only_that_tab(self):
        ev = AutoAcceptEvaluator({})
        allowed = [{"spreadsheet_id": "sheet1", "tab": "Sheet1"}]
        matching_ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        other_tab_ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet2!A1:B2"})
        assert ev._rule_approved_spreadsheet(allowed, matching_ctx) is True
        assert ev._rule_approved_spreadsheet(allowed, other_tab_ctx) is False

    def test_tab_match_is_case_insensitive(self):
        ev = AutoAcceptEvaluator({})
        allowed = [{"spreadsheet_id": "sheet1", "tab": "sheet1"}]
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "SHEET1!A1:B2"})
        assert ev._rule_approved_spreadsheet(allowed, ctx) is True

    def test_tab_scoped_entry_does_not_match_when_current_tab_unknown(self):
        ev = AutoAcceptEvaluator({})
        allowed = [{"spreadsheet_id": "sheet1", "tab": "Sheet1"}]
        # add_sheet has no range_a1/sheet_id at all -- current_tab is "".
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "title": "New Tab"})
        assert ev._rule_approved_spreadsheet(allowed, ctx) is False

    def test_multiple_entries_any_match_wins(self):
        ev = AutoAcceptEvaluator({})
        allowed = [{"spreadsheet_id": "other"}, {"spreadsheet_id": "sheet1", "tab": "Sheet1"}]
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet(allowed, ctx) is True

    def test_empty_value_never_matches(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet([], ctx) is False
        assert ev._rule_approved_spreadsheet(None, ctx) is False

    def test_missing_spreadsheet_id_in_args_never_matches(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet([{"spreadsheet_id": "sheet1"}], ctx) is False

    def test_single_dict_value_not_wrapped_in_a_list_is_accepted(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet({"spreadsheet_id": "sheet1"}, ctx) is True

    def test_malformed_entry_is_ignored_not_fatal(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        assert ev._rule_approved_spreadsheet(["not-a-dict"], ctx) is False


class TestSheetsFolderScopedRules:
    """rename_sheet/format_range raw_data is {"file": drive_file, ...} — the
    same shape write_range/add_sheet already use approved_sandbox_folder
    against (via _file_from) -- confirms the rule actually resolves the
    spreadsheet's parent folder end-to-end for these two operations too, not
    just that the generic rule function works in isolation (see
    test_approved_folder_variants above for that).
    """

    def test_rename_sheet_matches_folder_via_should_auto_accept(self):
        ev = AutoAcceptEvaluator({
            "sheets.rename_sheet": [{"rule": "approved_sandbox_folder", "value": ["folder1"]}],
        })
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 5, "new_title": "Renamed"},
            raw_data={"file": SimpleNamespace(parent_ids=["folder1"]), "sheet_id": 5, "new_title": "Renamed"},
        )
        assert ev.should_auto_accept("sheets.rename_sheet", ctx) == (True, "approved_sandbox_folder")

    def test_rename_sheet_does_not_match_a_different_folder(self):
        ev = AutoAcceptEvaluator({
            "sheets.rename_sheet": [{"rule": "approved_sandbox_folder", "value": ["folder1"]}],
        })
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 5, "new_title": "Renamed"},
            raw_data={"file": SimpleNamespace(parent_ids=["folder9"]), "sheet_id": 5, "new_title": "Renamed"},
        )
        assert ev.should_auto_accept("sheets.rename_sheet", ctx) == (False, "")

    def test_format_range_matches_folder_via_should_auto_accept(self):
        ev = AutoAcceptEvaluator({
            "sheets.format_range": [{"rule": "approved_sandbox_folder", "value": ["folder1"]}],
        })
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 0, "range_a1": "A1:B2"},
            raw_data={"file": SimpleNamespace(parent_ids=["folder1"]), "range_a1": "A1:B2", "format": "bold=true"},
        )
        assert ev.should_auto_accept("sheets.format_range", ctx) == (True, "approved_sandbox_folder")

    def test_format_range_does_not_match_a_different_folder(self):
        ev = AutoAcceptEvaluator({
            "sheets.format_range": [{"rule": "approved_sandbox_folder", "value": ["folder1"]}],
        })
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 0, "range_a1": "A1:B2"},
            raw_data={"file": SimpleNamespace(parent_ids=["folder9"]), "range_a1": "A1:B2", "format": "bold=true"},
        )
        assert ev.should_auto_accept("sheets.format_range", ctx) == (False, "")

    def test_insert_dimensions_matches_folder_via_should_auto_accept(self):
        # New operation key, same generic approved_sandbox_folder rule and
        # raw_data shape as rename_sheet/format_range above -- no new rule
        # code was needed to wire this up, just the TOOL_TO_OPERATION entry.
        ev = AutoAcceptEvaluator({
            "sheets.insert_dimensions": [{"rule": "approved_sandbox_folder", "value": ["folder1"]}],
        })
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "ROWS", "start_index": 0, "count": 1},
            raw_data={"file": SimpleNamespace(parent_ids=["folder1"]), "dimension": "ROWS"},
        )
        assert ev.should_auto_accept("sheets.insert_dimensions", ctx) == (True, "approved_sandbox_folder")

    def test_delete_dimensions_matches_folder_via_should_auto_accept(self):
        ev = AutoAcceptEvaluator({
            "sheets.delete_dimensions": [{"rule": "approved_sandbox_folder", "value": ["folder1"]}],
        })
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 0, "dimension": "ROWS", "start_index": 0, "count": 1},
            raw_data={"file": SimpleNamespace(parent_ids=["folder1"]), "dimension": "ROWS"},
        )
        assert ev.should_auto_accept("sheets.delete_dimensions", ctx) == (True, "approved_sandbox_folder")

    def test_insert_dimensions_scopes_approved_spreadsheet_to_sheet_id_tab(self):
        # sheet_id is present in args, so _sheet_tab_of resolves it the same
        # way rename_sheet/format_range do.
        ev = AutoAcceptEvaluator({
            "sheets.insert_dimensions": [
                {"rule": "approved_spreadsheet", "value": [{"spreadsheet_id": "sheet1", "tab": "5"}]},
            ],
        })
        matching_ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 5, "dimension": "ROWS", "start_index": 0, "count": 1},
            raw_data={"file": SimpleNamespace(parent_ids=[])},
        )
        other_tab_ctx = make_ctx(
            args={"spreadsheet_id": "sheet1", "sheet_id": 9, "dimension": "ROWS", "start_index": 0, "count": 1},
            raw_data={"file": SimpleNamespace(parent_ids=[])},
        )
        assert ev.should_auto_accept("sheets.insert_dimensions", matching_ctx) == (True, "approved_spreadsheet")
        assert ev.should_auto_accept("sheets.insert_dimensions", other_tab_ctx) == (False, "")

    def test_docs_edit_and_format_content_match_owner_via_should_auto_accept(self):
        # docs.* operations reuse the plain Drive-file rules (i_am_owner,
        # approved_sandbox_folder, created_this_session) the same way
        # drive.write_doc does -- no docs-specific rule code needed.
        ev = AutoAcceptEvaluator({
            "docs.edit_content": [{"rule": "i_am_owner"}],
            "docs.format_content": [{"rule": "i_am_owner"}],
        })
        ctx = make_ctx(
            my_email="me@example.com",
            args={"file_id": "f1"},
            raw_data={"file": SimpleNamespace(owners=["me@example.com"])},
        )
        assert ev.should_auto_accept("docs.edit_content", ctx) == (True, "i_am_owner")
        assert ev.should_auto_accept("docs.format_content", ctx) == (True, "i_am_owner")


# --------------------------------------------------------------------------- #
# Contacts rules
# --------------------------------------------------------------------------- #

class TestContactsRules:
    def test_no_contact_info_change(self):
        ev = AutoAcceptEvaluator({})
        assert ev._rule_no_contact_info_change(None, make_ctx(args={})) is True
        assert ev._rule_no_contact_info_change(None, make_ctx(args={"emails": ["a@b.com"]})) is False
        assert ev._rule_no_contact_info_change(None, make_ctx(args={"phones": ["+1"]})) is False


# --------------------------------------------------------------------------- #
# Jira rules
# --------------------------------------------------------------------------- #

class TestJiraRules:
    def test_approved_project_keys_from_project_key_arg(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"project_key": "eng"})
        assert ev._rule_approved_project_keys(["ENG"], ctx) is True

    def test_approved_project_keys_derived_from_issue_key(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"issue_key": "ENG-123"})
        assert ev._rule_approved_project_keys(["ENG"], ctx) is True

    def test_approved_project_keys_no_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"issue_key": "OPS-1"})
        assert ev._rule_approved_project_keys(["ENG"], ctx) is False

    def test_i_am_reporter_object_and_dict_raw_data(self):
        ev = AutoAcceptEvaluator({})
        obj_ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(reporter="me@example.com"))
        dict_ctx = make_ctx(my_email="me@example.com", raw_data={"reporter": "me@example.com"})
        assert ev._rule_i_am_reporter(None, obj_ctx) is True
        assert ev._rule_i_am_reporter(None, dict_ctx) is True

    def test_i_am_assignee(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", raw_data={"assignee": "me@example.com"})
        assert ev._rule_i_am_assignee(None, ctx) is True
        other = make_ctx(my_email="me@example.com", raw_data={"assignee": "other@example.com"})
        assert ev._rule_i_am_assignee(None, other) is False


# --------------------------------------------------------------------------- #
# Confluence rules
# --------------------------------------------------------------------------- #

class TestConfluenceRules:
    def test_approved_space_keys_from_args(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"space_key": "eng"}, raw_data={})
        assert ev._rule_approved_space_keys(["ENG"], ctx) is True

    def test_approved_space_keys_from_raw_data_dict(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={}, raw_data={"space_key": "eng"})
        assert ev._rule_approved_space_keys(["ENG"], ctx) is True

    def test_i_am_author_object_and_dict(self):
        ev = AutoAcceptEvaluator({})
        obj_ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(author="me@example.com"))
        dict_ctx = make_ctx(my_email="me@example.com", raw_data={"author": "me@example.com"})
        assert ev._rule_i_am_author(None, obj_ctx) is True
        assert ev._rule_i_am_author(None, dict_ctx) is True


# --------------------------------------------------------------------------- #
# Telegram rules
# --------------------------------------------------------------------------- #

class TestTelegramRules:
    def test_approved_chats_matches_by_string_comparison(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"chat_id": 12345})
        assert ev._rule_approved_chats([12345], ctx) is True
        assert ev._rule_approved_chats(["12345"], ctx) is True
        assert ev._rule_approved_chats([99999], ctx) is False

    def test_no_media_attachments(self):
        ev = AutoAcceptEvaluator({})
        clean = make_ctx(raw_data=[SimpleNamespace(media_type=""), SimpleNamespace(media_type=None)])
        dirty = make_ctx(raw_data=[SimpleNamespace(media_type="photo")])
        assert ev._rule_no_media_attachments(None, clean) is True
        assert ev._rule_no_media_attachments(None, dirty) is False

    def test_approved_chats_all_results(self):
        ev = AutoAcceptEvaluator({})
        all_approved = make_ctx(raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=222)])
        one_unapproved = make_ctx(raw_data=[SimpleNamespace(chat_id=111), SimpleNamespace(chat_id=999)])
        empty = make_ctx(raw_data=[])
        assert ev._rule_approved_chats_all_results(["111", "222"], all_approved) is True
        assert ev._rule_approved_chats_all_results([111, 222], all_approved) is True  # string comparison
        assert ev._rule_approved_chats_all_results(["111", "222"], one_unapproved) is False
        assert ev._rule_approved_chats_all_results(["111", "222"], empty) is False
        assert ev._rule_approved_chats_all_results([], all_approved) is False


# --------------------------------------------------------------------------- #
# Tasks rules
# --------------------------------------------------------------------------- #

class TestTasksRules:
    def test_approved_task_list_matches_task_list_id(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"task_list_id": "list1", "task_id": "t1"})
        assert ev._rule_approved_task_list(["list1"], ctx) is True
        assert ev._rule_approved_task_list(["list2"], ctx) is False

    def test_approved_task_list_empty_value_never_matches(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"task_list_id": "list1"})
        assert ev._rule_approved_task_list([], ctx) is False
        assert ev._rule_approved_task_list(None, ctx) is False

    def test_approved_task_list_move_requires_both_ends_approved(self):
        # tasks_move_task carries source_list_id/destination_list_id instead
        # of task_list_id -- a move only auto-accepts when neither end can
        # smuggle the task into (or out of) an unapproved list.
        ev = AutoAcceptEvaluator({})
        allowed = ["list1", "list2"]
        both_approved = make_ctx(args={"source_list_id": "list1", "destination_list_id": "list2"})
        one_unapproved = make_ctx(args={"source_list_id": "list1", "destination_list_id": "list3"})
        assert ev._rule_approved_task_list(allowed, both_approved) is True
        assert ev._rule_approved_task_list(allowed, one_unapproved) is False

    def test_approved_task_list_move_missing_ids_does_not_match(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={})
        assert ev._rule_approved_task_list(["list1"], ctx) is False

    def test_approved_task_list_single_string_value_not_wrapped_in_a_list_is_accepted(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"task_list_id": "list1"})
        assert ev._rule_approved_task_list("list1", ctx) is True


# --------------------------------------------------------------------------- #
# Generic rules (no resource identity to scope to)
# --------------------------------------------------------------------------- #

class TestGenericRules:
    def test_always_allow_matches_unconditionally(self):
        ev = AutoAcceptEvaluator({})
        assert ev._rule_always_allow(None, make_ctx()) is True
        assert ev._rule_always_allow(None, make_ctx(args={"anything": "at all"})) is True


# --------------------------------------------------------------------------- #
# Dict-shaped raw_data support (calendar rules now accept dicts too)
# --------------------------------------------------------------------------- #

class TestDictShapedRawData:
    def test_i_am_organizer_accepts_dict_raw_data(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", raw_data={"organizer_email": "me@example.com"})
        assert ev._rule_i_am_organizer(None, ctx) is True

    def test_no_external_attendees_accepts_dict_raw_data_and_string_attendees(self):
        # calendar_create_event/update_event pass plain email strings for
        # attendees (parsed from a comma-separated arg) since the event
        # doesn't exist yet, unlike calendar_get_event_details's dict/object
        # attendee shape.
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(
            my_email="me@example.com",
            raw_data={"attendees": ["a@example.com", "b@example.com"]},
        )
        assert ev._rule_no_external_attendees(None, ctx) is True

        external = make_ctx(
            my_email="me@example.com",
            raw_data={"attendees": ["a@external.com"]},
        )
        assert ev._rule_no_external_attendees(None, external) is False

    def test_i_am_owner_accepts_dict_shaped_wrapper(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(my_email="me@example.com", raw_data={"file": SimpleNamespace(owners=["me@example.com"])})
        assert ev._rule_i_am_owner(None, ctx) is True


# --------------------------------------------------------------------------- #
# should_auto_accept dispatch
# --------------------------------------------------------------------------- #

class TestShouldAutoAccept:
    def test_matches_first_applicable_rule(self):
        ev = AutoAcceptEvaluator({
            "gmail.read_message": [
                {"rule": "no_attachments"},
                {"rule": "i_am_sender"},
            ]
        })
        ctx = make_ctx(raw_data=SimpleNamespace(attachments=[], sender="", labels=[]))
        ok, matched = ev.should_auto_accept("gmail.read_message", ctx)
        assert ok is True
        assert matched == "no_attachments"

    def test_no_rules_configured_for_operation(self):
        ev = AutoAcceptEvaluator({})
        ok, matched = ev.should_auto_accept("gmail.read_message", make_ctx())
        assert (ok, matched) == (False, "")

    def test_null_rules_list_for_operation_is_not_fatal(self):
        # A hand-edited settings.yaml can leave an operation key present with
        # no value (YAML null) instead of an empty list, e.g. after removing
        # every rule under it by hand.
        ev = AutoAcceptEvaluator({"gmail.read_message": None})
        ok, matched = ev.should_auto_accept("gmail.read_message", make_ctx())
        assert (ok, matched) == (False, "")

    def test_unknown_rule_name_is_skipped_not_fatal(self):
        ev = AutoAcceptEvaluator({"gmail.read_message": [{"rule": "does_not_exist"}]})
        ok, matched = ev.should_auto_accept("gmail.read_message", make_ctx(raw_data=SimpleNamespace()))
        assert (ok, matched) == (False, "")

    def test_rule_exception_is_caught_and_skipped(self):
        class Boom:
            @property
            def date(self):
                raise RuntimeError("boom")

        ev = AutoAcceptEvaluator({"gmail.read_message": [{"rule": "age_threshold_days", "value": 5}]})
        ctx = make_ctx(raw_data=Boom())
        ok, matched = ev.should_auto_accept("gmail.read_message", ctx)
        assert (ok, matched) == (False, "")


# --------------------------------------------------------------------------- #
# suggest_rule / describe_rule
# --------------------------------------------------------------------------- #

class TestSuggestRule:
    def test_gmail_suggests_sender_rule_when_i_am_sender(self):
        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="Me <me@example.com>"))
        assert suggest_rule("gmail.read_message", ctx) == ("i_am_sender", None)

    def test_gmail_suggests_domain_rule_otherwise(self):
        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="Alice <alice@other.com>"))
        assert suggest_rule("gmail.read_thread", ctx) == ("trusted_sender_domain", ["other.com"])

    def test_gmail_suggests_nothing_without_domain(self):
        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender=""))
        assert suggest_rule("gmail.read_message", ctx) is None

    def test_drive_suggests_owner_or_folder(self):
        owned = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(owners=["me@example.com"], parent_ids=[]))
        assert suggest_rule("drive.read_file_contents", owned) == ("i_am_owner", None)

        foreign = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(owners=["other@example.com"], parent_ids=["f1"]),
        )
        assert suggest_rule("drive.read_file_contents", foreign) == ("approved_folder", ["f1"])

    def test_slack_suggests_dm_or_channel(self):
        dm = make_ctx(args={"channel_id": "D1"})
        assert suggest_rule("slack.read_messages", dm) == ("dm_with_myself", None)
        channel = make_ctx(args={"channel_id": "C1"})
        assert suggest_rule("slack.read_messages", channel) == ("approved_channel", ["C1"])

    def test_slack_suggests_group_dm_before_approved_channel(self):
        ctx = make_ctx(args={"channel_id": "G1", "is_group_dm": True})
        assert suggest_rule("slack.read_messages", ctx) == ("group_dm", None)

    def test_slack_search_suggests_union_of_result_channels(self):
        # No channel_id in args -- a search spanning multiple channels.
        ctx = make_ctx(
            args={"query": "hello world"},
            raw_data=[SimpleNamespace(channel_id="C2"), SimpleNamespace(channel_id="C1")],
        )
        assert suggest_rule("slack.read_messages", ctx) == ("approved_channel_all_results", ["C1", "C2"])

    def test_slack_search_suggests_nothing_with_no_results(self):
        ctx = make_ctx(args={"query": "hello world"}, raw_data=[])
        assert suggest_rule("slack.read_messages", ctx) is None

    def test_sheets_read_values_suggests_spreadsheet_and_tab(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "Sheet1!A1:B2"})
        assert suggest_rule("sheets.read_values", ctx) == (
            "approved_spreadsheet", [{"spreadsheet_id": "sheet1", "tab": "Sheet1"}],
        )

    def test_sheets_read_values_suggests_spreadsheet_only_without_a_tab(self):
        # No "!" prefix in range_a1 -- _sheet_tab_of can't identify a tab.
        ctx = make_ctx(args={"spreadsheet_id": "sheet1", "range_a1": "A1:B2"})
        assert suggest_rule("sheets.read_values", ctx) == (
            "approved_spreadsheet", [{"spreadsheet_id": "sheet1"}],
        )

    def test_sheets_read_values_suggests_nothing_without_spreadsheet_id(self):
        assert suggest_rule("sheets.read_values", make_ctx(args={})) is None

    def test_calendar_suggests_organizer_or_internal_attendees(self):
        organizer = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(organizer_email="me@example.com"))
        assert suggest_rule("calendar.read_event_details", organizer) == ("i_am_organizer", None)

        internal = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(organizer_email="other@example.com", attendees=[{"email": "x@example.com"}]),
        )
        assert suggest_rule("calendar.read_event_details", internal) == ("no_external_attendees", None)

    def test_calendar_falls_back_to_non_private_event_when_external_but_not_private(self):
        # Neither i_am_organizer nor no_external_attendees apply, but the
        # event isn't private either -- still a plausible suggestion rather
        # than no suggestion at all.
        external = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(
                organizer_email="other@example.com", attendees=[{"email": "x@external.com"}], visibility="public",
            ),
        )
        assert suggest_rule("calendar.read_event_details", external) == ("non_private_event", None)

    def test_calendar_suggests_nothing_for_a_private_event_with_external_attendees(self):
        private_external = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(
                organizer_email="other@example.com", attendees=[{"email": "x@external.com"}], visibility="private",
            ),
        )
        assert suggest_rule("calendar.read_event_details", private_external) is None

    def test_salesforce_suggests_object_type(self):
        ctx = make_ctx(args={"object_type": "Account"})
        assert suggest_rule("salesforce.read_record", ctx) == ("approved_object_types", ["Account"])

    def test_salesforce_search_suggests_scoped_object_types(self):
        ctx = make_ctx(args={"object_types": "Opportunity,Contact"})
        assert suggest_rule("salesforce.search", ctx) == ("approved_object_types", ["Opportunity", "Contact"])

    def test_salesforce_search_suggests_nothing_when_unscoped(self):
        assert suggest_rule("salesforce.search", make_ctx(args={"object_types": ""})) is None

    def test_salesforce_run_report_suggests_approved_report_id(self):
        ctx = make_ctx(args={"report_id": "00O000000000001"})
        assert suggest_rule("salesforce.run_report", ctx) == ("approved_report_ids", ["00O000000000001"])

    def test_salesforce_run_report_suggests_nothing_without_report_id(self):
        assert suggest_rule("salesforce.run_report", make_ctx(args={})) is None

    def test_unrecognized_operation_suggests_nothing(self):
        assert suggest_rule("some.unmapped.operation", make_ctx()) is None

    def test_gmail_suggestion_also_applies_to_download_attachment_and_archive(self):
        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="me@example.com"))
        assert suggest_rule("gmail.download_attachment", ctx) == ("i_am_sender", None)
        assert suggest_rule("gmail.archive_message", ctx) == ("i_am_sender", None)

    def test_jira_suggests_reporter_then_assignee_then_project(self):
        reporter_ctx = make_ctx(my_email="me@example.com", raw_data={"reporter": "me@example.com"})
        assert suggest_rule("jira.read_issue", reporter_ctx) == ("i_am_reporter", None)

        assignee_ctx = make_ctx(
            my_email="me@example.com",
            raw_data={"reporter": "other@example.com", "assignee": "me@example.com"},
        )
        assert suggest_rule("jira.read_issue", assignee_ctx) == ("i_am_assignee", None)

        project_ctx = make_ctx(
            my_email="me@example.com",
            raw_data={"reporter": "other@example.com", "assignee": "other@example.com"},
            args={"issue_key": "ENG-42"},
        )
        assert suggest_rule("jira.read_issue", project_ctx) == ("approved_project_keys", ["ENG"])

    def test_jira_suggestion_accepts_object_shaped_raw_data_for_reporter_assignee(self):
        # suggest_rule's jira branch must accept the same object-or-dict
        # shapes as _rule_i_am_reporter/_rule_i_am_assignee (getattr
        # fallback), not just a dict -- otherwise an object-shaped raw_data
        # silently skips straight to the project-key suggestion.
        reporter_ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(reporter="me@example.com"),
            args={"issue_key": "ENG-42"},
        )
        assert suggest_rule("jira.read_issue", reporter_ctx) == ("i_am_reporter", None)

        assignee_ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(reporter="other@example.com", assignee="me@example.com"),
            args={"issue_key": "ENG-42"},
        )
        assert suggest_rule("jira.read_issue", assignee_ctx) == ("i_am_assignee", None)

    def test_confluence_suggests_author_then_space(self):
        author_ctx = make_ctx(my_email="me@example.com", raw_data={"author": "me@example.com"})
        assert suggest_rule("confluence.read_page", author_ctx) == ("i_am_author", None)

        space_ctx = make_ctx(
            my_email="me@example.com",
            raw_data={"author": "other@example.com", "space_key": "ENG"},
        )
        assert suggest_rule("confluence.read_page", space_ctx) == ("approved_space_keys", ["ENG"])

    def test_confluence_suggestion_accepts_object_shaped_raw_data(self):
        author_ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(author="me@example.com"),
        )
        assert suggest_rule("confluence.read_page", author_ctx) == ("i_am_author", None)

        space_ctx = make_ctx(
            my_email="me@example.com",
            raw_data=SimpleNamespace(author="other@example.com", space_key="ENG"),
        )
        assert suggest_rule("confluence.read_page", space_ctx) == ("approved_space_keys", ["ENG"])

    def test_telegram_suggests_approved_chat(self):
        ctx = make_ctx(args={"chat_id": 12345})
        assert suggest_rule("telegram.read_chat_messages", ctx) == ("approved_chats", ["12345"])

    def test_telegram_suggests_nothing_without_chat_id(self):
        assert suggest_rule("telegram.read_chat_messages", make_ctx(args={})) is None

    def test_telegram_search_suggests_union_of_result_chats(self):
        # No chat_id in args -- a search spanning multiple chats (shares
        # this operation key with telegram_get_messages).
        ctx = make_ctx(
            args={"query": "hello world"},
            raw_data=[SimpleNamespace(chat_id=222), SimpleNamespace(chat_id=111)],
        )
        assert suggest_rule("telegram.read_chat_messages", ctx) == ("approved_chats_all_results", ["111", "222"])

    def test_telegram_search_suggests_nothing_with_no_results(self):
        ctx = make_ctx(args={"query": "hello world"}, raw_data=[])
        assert suggest_rule("telegram.read_chat_messages", ctx) is None

    def test_describe_rule_formats_value(self):
        assert describe_rule("i_am_sender", None) == "Auto-accept future Gmail message/thread reads where you are the sender"
        desc = describe_rule("trusted_sender_domain", ["example.com", "other.com"])
        assert desc == "Auto-accept future Gmail message/thread reads from senders at: example.com, other.com"

    def test_describe_rule_unknown_name_falls_back_to_raw_name(self):
        assert describe_rule("some_future_rule", "x") == "Auto-accept future some_future_rule"

    def test_describe_rule_formats_spreadsheet_entries_with_and_without_tab(self):
        desc = describe_rule("approved_spreadsheet", [
            {"spreadsheet_id": "sheet1", "tab": "Sheet1"},
            {"spreadsheet_id": "sheet2"},
        ])
        assert desc == "Auto-accept future Sheets calls scoped to: sheet1 (tab: Sheet1), sheet2"

    def test_format_spreadsheet_entry_non_dict_falls_back_to_str(self):
        assert auto_accept._format_spreadsheet_entry("not-a-dict") == "not-a-dict"


# --------------------------------------------------------------------------- #
# Rule persistence: add_auto_accept_rule / reload_rules / singleton access
# --------------------------------------------------------------------------- #

class TestRulePersistence:
    def test_add_auto_accept_rule_requires_init_config_path(self):
        with pytest.raises(RuntimeError):
            add_auto_accept_rule("gmail.read_message", "i_am_sender", None)

    def test_add_auto_accept_rule_appends_and_persists(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))

        add_auto_accept_rule("gmail.read_message", "i_am_sender", None)
        add_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["example.com"])

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        rules = on_disk["auto_accept_rules"]["gmail.read_message"]
        assert rules == [
            {"rule": "i_am_sender"},
            {"rule": "trusted_sender_domain", "value": ["example.com"]},
        ]

    def test_add_auto_accept_rule_hot_reloads_live_evaluator(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))
        init_auto_accept_evaluator({})

        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="me@example.com"))
        assert get_auto_accept_evaluator().should_auto_accept("gmail.read_message", ctx) == (False, "")

        add_auto_accept_rule("gmail.read_message", "i_am_sender", None)

        ok, matched = get_auto_accept_evaluator().should_auto_accept("gmail.read_message", ctx)
        assert (ok, matched) == (True, "i_am_sender")

    def test_get_auto_accept_evaluator_lazy_inits_empty(self):
        ev = get_auto_accept_evaluator()
        assert isinstance(ev, AutoAcceptEvaluator)
        assert ev.should_auto_accept("gmail.read_message", make_ctx(raw_data=SimpleNamespace())) == (False, "")

    def test_reload_rules_replaces_rules_on_existing_instance(self):
        init_auto_accept_evaluator({})
        instance_before = get_auto_accept_evaluator()
        reload_rules({"gmail.read_message": [{"rule": "no_attachments"}]})
        assert get_auto_accept_evaluator() is instance_before  # same object, rules swapped in place
        ctx = make_ctx(raw_data=SimpleNamespace(attachments=[]))
        assert get_auto_accept_evaluator().should_auto_accept("gmail.read_message", ctx) == (True, "no_attachments")

    def test_add_auto_accept_rule_is_idempotent_for_identical_rule(self, tmp_path):
        # Confirming the same "Always allow" suggestion twice (e.g. two popups
        # queued back-to-back) must not pile up duplicate rule entries.
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))

        add_auto_accept_rule("gmail.read_message", "i_am_sender", None)
        add_auto_accept_rule("gmail.read_message", "i_am_sender", None)

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_add_auto_accept_rule_allows_same_rule_name_different_value(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))

        add_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["a.com"])
        add_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["b.com"])

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_rules"]["gmail.read_message"] == [
            {"rule": "trusted_sender_domain", "value": ["a.com"]},
            {"rule": "trusted_sender_domain", "value": ["b.com"]},
        ]


# --------------------------------------------------------------------------- #
# remove_auto_accept_rule / get_current_config / mutate_grants -- the
# bridge-facing write/read primitives gate.propose_rule_change() and
# ipc_server.py's list_rules handler build on (see gate.py's docstring).
# --------------------------------------------------------------------------- #

class TestRemoveAutoAcceptRule:
    def test_removes_exact_value_match(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {
                "gmail.read_message": [{"rule": "trusted_sender_domain", "value": ["a.com"]}],
            }}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))

        removed = remove_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["a.com"])

        assert removed is True
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert "gmail.read_message" not in on_disk.get("auto_accept_rules", {})

    def test_value_given_only_removes_matching_value_not_other_values_of_same_rule(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {
                "gmail.read_message": [
                    {"rule": "trusted_sender_domain", "value": ["a.com"]},
                    {"rule": "trusted_sender_domain", "value": ["b.com"]},
                ],
            }}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))

        remove_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["a.com"])

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_rules"]["gmail.read_message"] == [
            {"rule": "trusted_sender_domain", "value": ["b.com"]},
        ]

    def test_value_omitted_removes_every_entry_for_that_rule_name(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {
                "gmail.read_message": [
                    {"rule": "trusted_sender_domain", "value": ["a.com"]},
                    {"rule": "trusted_sender_domain", "value": ["b.com"]},
                    {"rule": "i_am_sender"},
                ],
            }}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))

        remove_auto_accept_rule("gmail.read_message", "trusted_sender_domain")

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_no_match_returns_false_and_does_not_rewrite_the_file(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))

        removed = remove_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["a.com"])

        assert removed is False
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_hot_reloads_the_live_evaluator(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))
        init_auto_accept_evaluator({"gmail.read_message": [{"rule": "i_am_sender"}]})

        ctx = make_ctx(my_email="me@example.com", raw_data=SimpleNamespace(sender="me@example.com"))
        assert get_auto_accept_evaluator().should_auto_accept("gmail.read_message", ctx) == (True, "i_am_sender")

        remove_auto_accept_rule("gmail.read_message", "i_am_sender")

        assert get_auto_accept_evaluator().should_auto_accept("gmail.read_message", ctx) == (False, "")

    def test_fires_the_rules_changed_listener_the_same_way_add_does(self, tmp_path):
        # menu_bar.py wires this listener to rebuild the status-bar menu and
        # (if open) refresh the "Manage Auto-accept Rules…" window -- see
        # menu_bar.py's _on_rules_changed. A rule removed via the bridge's
        # propose_rule_change must trigger that same refresh, not just
        # silently update the live evaluator underneath the open window.
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        remove_auto_accept_rule("gmail.read_message", "i_am_sender")

        assert calls == [1]

    def test_no_op_removal_does_not_fire_the_listener(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}}),
            encoding="utf-8",
        )
        init_config_path(str(config_path))
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        remove_auto_accept_rule("gmail.read_message", "trusted_sender_domain", ["a.com"])

        assert calls == []


class TestKnownRuleNames:
    """auto_accept.known_rule_names() -- the set gate.propose_rule_change()
    validates a bridge-supplied rule_name against before ever showing a
    confirmation popup, so Claude can't silently persist a dead rule under
    a misspelled or made-up name (see that function's docstring)."""

    def test_includes_real_rule_names(self):
        names = auto_accept.known_rule_names()
        assert "i_am_sender" in names
        assert "trusted_sender_domain" in names
        assert "approved_sandbox_folder" in names  # grant-managed, but still a real _rule_* method

    def test_excludes_non_rule_names(self):
        names = auto_accept.known_rule_names()
        assert "session_temp_accept" not in names  # in-memory marker, not a _rule_* method
        assert "made_up_rule" not in names
        assert "" not in names

    def test_every_name_is_actually_dispatchable(self):
        # Cross-check against the same completeness angle menu_bar.py's
        # TestRuleUiCompleteness uses: every name here must correspond to a
        # real, callable _rule_* method, or the set itself would be lying.
        for name in auto_accept.known_rule_names():
            assert callable(getattr(auto_accept.AutoAcceptEvaluator, f"_rule_{name}", None))


class TestGetCurrentConfig:
    def test_returns_both_sections_straight_from_disk(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(
            yaml.dump({
                "auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]},
                "auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "f1", "write": True}]}},
            }),
            encoding="utf-8",
        )
        init_config_path(str(config_path))

        cfg = get_current_config()

        assert cfg == {
            "auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]},
            "auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "f1", "write": True}]}},
        }

    def test_missing_sections_default_to_empty_dicts(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))

        assert get_current_config() == {"auto_accept_rules": {}, "auto_accept_grants": {}}


class TestMutateGrants:
    def test_mutator_reporting_a_change_persists_and_reloads(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_grants": {}}), encoding="utf-8")
        init_config_path(str(config_path))
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        def mutator(cfg):
            cfg["auto_accept_grants"] = {"drive": {"sandbox_folders": [{"id": "f1", "write": True}]}}
            return True

        changed = mutate_grants(mutator)

        assert changed is True
        assert calls == [1]
        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_grants"]["drive"]["sandbox_folders"] == [{"id": "f1", "write": True}]

    def test_mutator_reporting_no_change_does_not_write_or_reload(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        original = yaml.dump({"auto_accept_grants": {}})
        config_path.write_text(original, encoding="utf-8")
        init_config_path(str(config_path))
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        changed = mutate_grants(lambda cfg: False)

        assert changed is False
        assert calls == []
        assert config_path.read_text(encoding="utf-8") == original


class TestDescribeRuleChange:
    def test_add(self):
        description = describe_rule_change("add", "gmail.read_message", "trusted_sender_domain", ["a.com"])
        assert description == (
            "Add auto-accept rule 'trusted_sender_domain' = a.com to 'gmail.read_message'"
        )

    def test_remove_with_value(self):
        description = describe_rule_change("remove", "gmail.read_message", "trusted_sender_domain", ["a.com"])
        assert "Remove auto-accept rule 'trusted_sender_domain' = a.com from 'gmail.read_message'" == description

    def test_remove_without_value_says_every_value(self):
        description = describe_rule_change("remove", "gmail.read_message", "trusted_sender_domain")
        assert "(every value)" in description

    def test_update_shows_old_and_new_value(self):
        description = describe_rule_change(
            "update", "gmail.read_message", "trusted_sender_domain", ["b.com"], old_value=["a.com"]
        )
        assert "a.com -> b.com" in description


# --------------------------------------------------------------------------- #
# Rules-changed listener (drives the menu bar's live rule submenu refresh)
# --------------------------------------------------------------------------- #

class TestRulesChangedListener:
    def test_reload_rules_fires_registered_listener(self):
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))
        reload_rules({})
        assert calls == [1]

    def test_reload_rules_is_safe_with_no_listener_registered(self):
        set_rules_changed_listener(None)
        reload_rules({})  # must not raise

    def test_add_auto_accept_rule_fires_listener_via_reload(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        add_auto_accept_rule("gmail.read_message", "i_am_sender", None)

        assert calls == [1]


# --------------------------------------------------------------------------- #
# Concurrent rule persistence: real OS threads racing on add_auto_accept_rule.
#
# gate.py's popup handling serializes calls through one asyncio.Lock, but
# add_auto_accept_rule() itself is also reachable directly from the menu
# bar's own thread (adding a rule via "+ Add rule…") at the same time the
# IPC server's thread is confirming an "Always allow". _write_lock is what's
# supposed to keep the read-modify-write of the YAML file race-free; these
# tests hammer it with real threads rather than asyncio tasks, since asyncio
# concurrency alone never exercises actual OS-level lock contention or
# genuine interleaving of file reads/writes.
# --------------------------------------------------------------------------- #

class TestConcurrentRulePersistence:
    def test_many_threads_adding_the_identical_rule_produce_no_duplicates(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))

        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()  # maximize actual overlap, not just interleaving
            add_auto_accept_rule("gmail.read_message", "i_am_sender", None)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert on_disk["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_many_threads_adding_distinct_rules_lose_no_writes(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))

        domains = [f"domain{i}.com" for i in range(20)]
        barrier = threading.Barrier(len(domains))

        def worker(domain):
            barrier.wait()
            add_auto_accept_rule("gmail.read_message", "trusted_sender_domain", [domain])

        threads = [threading.Thread(target=worker, args=(d,)) for d in domains]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        rules = on_disk["auto_accept_rules"]["gmail.read_message"]
        # A lost update under a broken lock would show up as fewer than 20
        # entries here; a corrupted concurrent write would fail to parse as
        # YAML at all (read_text/safe_load above would already have raised).
        assert len(rules) == len(domains)
        persisted_domains = {r["value"][0] for r in rules}
        assert persisted_domains == set(domains)

    def test_concurrent_adds_keep_the_live_evaluator_and_disk_file_in_sync(self, tmp_path):
        # Every successful add_auto_accept_rule() call also calls
        # reload_rules() while still holding _write_lock, so the in-memory
        # evaluator used by gate.py should never lag behind what's on disk,
        # even under concurrent writers.
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"auto_accept_rules": {}}), encoding="utf-8")
        init_config_path(str(config_path))
        init_auto_accept_evaluator({})

        domains = [f"domain{i}.com" for i in range(10)]
        barrier = threading.Barrier(len(domains))

        def worker(domain):
            barrier.wait()
            add_auto_accept_rule("gmail.read_message", "trusted_sender_domain", [domain])

        threads = [threading.Thread(target=worker, args=(d,)) for d in domains]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        live_rules = get_auto_accept_evaluator()._rules
        assert live_rules == on_disk["auto_accept_rules"]


# --------------------------------------------------------------------------- #
# Session temp accept -- gate.py's lighter alternative to a standing Always
# allow rule for write ops expected to be called repeatedly against the
# same file (sheets writes/formats, drive comments): an Allow once on one
# of these operations also arms this window, with no separate button.
# Unlike the YAML-backed rules above, this state is in-memory only and never
# persisted.
# --------------------------------------------------------------------------- #

class TestTempAcceptKey:
    def test_eligible_operation_returns_its_configured_arg(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1", "range_a1": "A1:B2"})
        assert temp_accept_key("sheets.write_range", ctx) == "sheet-1"

    def test_drive_comment_uses_file_id(self):
        ctx = make_ctx(args={"file_id": "file-1", "comment": "hi"})
        assert temp_accept_key("drive.comment_file", ctx) == "file-1"

    def test_ineligible_operation_returns_none(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
        assert temp_accept_key("gmail.create_draft", ctx) is None

    def test_eligible_operation_missing_arg_returns_none(self):
        ctx = make_ctx(args={"range_a1": "A1:B2"})
        assert temp_accept_key("sheets.write_range", ctx) is None

    def test_eligible_operation_falsy_arg_returns_none(self):
        ctx = make_ctx(args={"spreadsheet_id": ""})
        assert temp_accept_key("sheets.write_range", ctx) is None

    def test_covers_every_declared_eligible_operation(self):
        # Every entry in TEMP_ACCEPT_ELIGIBLE_OPERATIONS must actually resolve
        # a key when its arg is present -- otherwise the popup would show the
        # temp-accept disclosure caption for an operation that can never
        # actually register one.
        for op_key, arg_name in TEMP_ACCEPT_ELIGIBLE_OPERATIONS.items():
            ctx = make_ctx(args={arg_name: "some-id"})
            assert temp_accept_key(op_key, ctx) == "some-id"

    def test_insert_dimensions_is_eligible_scoped_to_spreadsheet_id(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1", "dimension": "ROWS"})
        assert temp_accept_key("sheets.insert_dimensions", ctx) == "sheet-1"

    def test_delete_dimensions_is_deliberately_not_eligible(self):
        # Resolved design decision: unlike format_range/insert_dimensions,
        # deleting rows/columns is destructive with no undo path through
        # PrivacyFence, so it only ever gets the standing-rule treatment, not
        # the lighter-weight temp-accept grace window.
        assert "sheets.delete_dimensions" not in TEMP_ACCEPT_ELIGIBLE_OPERATIONS
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1", "dimension": "ROWS"})
        assert temp_accept_key("sheets.delete_dimensions", ctx) is None

    def test_docs_edit_and_format_content_are_eligible_scoped_to_file_id(self):
        ctx = make_ctx(args={"file_id": "f1"})
        assert temp_accept_key("docs.edit_content", ctx) == "f1"
        assert temp_accept_key("docs.format_content", ctx) == "f1"


class TestEvaluatorTempAccept:
    def test_not_accepted_before_registration(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
        assert ev.should_auto_accept("sheets.write_range", ctx) == (False, "")

    def test_registered_file_auto_accepts(self):
        ev = AutoAcceptEvaluator({})
        ev.register_temp_accept("sheets.write_range", "sheet-1")
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
        assert ev.should_auto_accept("sheets.write_range", ctx) == (True, "session_temp_accept")

    def test_different_file_key_not_covered(self):
        ev = AutoAcceptEvaluator({})
        ev.register_temp_accept("sheets.write_range", "sheet-1")
        ctx = make_ctx(args={"spreadsheet_id": "sheet-2"})
        assert ev.should_auto_accept("sheets.write_range", ctx) == (False, "")

    def test_different_operation_on_same_file_not_covered(self):
        ev = AutoAcceptEvaluator({})
        ev.register_temp_accept("sheets.write_range", "sheet-1")
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
        assert ev.should_auto_accept("sheets.format_range", ctx) == (False, "")

    def test_standing_rule_takes_priority_over_a_matching_temp_accept(self):
        # Both a YAML rule and a temp accept match this call; the loop over
        # standing rules runs (and returns) before the temp-accept fallback
        # is ever consulted, so the standing rule's name wins.
        ev = AutoAcceptEvaluator({
            "sheets.write_range": [{"rule": "i_am_owner"}],
        })
        ev.register_temp_accept("sheets.write_range", "sheet-1")
        ctx = make_ctx(
            args={"spreadsheet_id": "sheet-1"},
            my_email="me@example.com",
            raw_data={"file": SimpleNamespace(owners=["me@example.com"])},
        )
        assert ev.should_auto_accept("sheets.write_range", ctx) == (True, "i_am_owner")

    def test_expires_after_ttl(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            ev = AutoAcceptEvaluator({})
            ev.register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
            assert ev.should_auto_accept("sheets.write_range", ctx) == (True, "session_temp_accept")

            frozen.tick(delta=301)
            assert ev.should_auto_accept("sheets.write_range", ctx) == (False, "")

    def test_still_valid_just_before_ttl_expires(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            ev = AutoAcceptEvaluator({})
            ev.register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            frozen.tick(delta=299)
            ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
            assert ev.should_auto_accept("sheets.write_range", ctx) == (True, "session_temp_accept")

    def test_re_registering_resets_the_ttl(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            ev = AutoAcceptEvaluator({})
            ev.register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            frozen.tick(delta=290)
            ev.register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            frozen.tick(delta=290)
            ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
            assert ev.should_auto_accept("sheets.write_range", ctx) == (True, "session_temp_accept")

    def test_no_temp_accepts_registered_never_matches(self):
        ev = AutoAcceptEvaluator({})
        ctx = make_ctx(args={})
        assert ev.should_auto_accept("sheets.write_range", ctx) == (False, "")


# --------------------------------------------------------------------------- #
# ARGS_ONLY_RULES / DATA_DEPENDENT_RULES / preflight_from_args: the preflight
# check behind privacyfence_check_policy. Nothing here should reach a real
# connector or the network -- these predict should_auto_accept()'s answer
# from args alone, before anything is fetched.
# --------------------------------------------------------------------------- #

class TestRuleClassificationCompleteness:
    """Guards against a newly added _rule_* method being left unclassified,
    which would silently make preflight_from_args() treat it as permanently
    "undetermined" without anyone deciding that on purpose."""

    def _all_rule_names(self) -> set[str]:
        return {
            name[len("_rule_"):]
            for name in vars(AutoAcceptEvaluator)
            if name.startswith("_rule_") and callable(getattr(AutoAcceptEvaluator, name))
        }

    def test_every_rule_is_classified(self):
        classified = ARGS_ONLY_RULES | DATA_DEPENDENT_RULES
        unclassified = self._all_rule_names() - classified
        assert unclassified == set(), (
            f"_rule_* methods not classified into ARGS_ONLY_RULES or DATA_DEPENDENT_RULES: {unclassified}"
        )

    def test_no_rule_is_in_both_sets(self):
        assert ARGS_ONLY_RULES & DATA_DEPENDENT_RULES == set()

    def test_no_stale_entries_for_removed_rules(self):
        stale = (ARGS_ONLY_RULES | DATA_DEPENDENT_RULES) - self._all_rule_names()
        assert stale == set(), f"Classified rule names with no matching _rule_* method: {stale}"


class TestPreflightFromArgs:
    def test_temp_accept_match_wins_first(self):
        ev = AutoAcceptEvaluator({})
        ev.register_temp_accept("sheets.write_range", "sheet-1")
        verdict, rule, _reason = ev.preflight_from_args("sheets.write_range", {"spreadsheet_id": "sheet-1"})
        assert (verdict, rule) == ("auto_accept", "session_temp_accept")

    def test_args_only_rule_match(self):
        ev = AutoAcceptEvaluator({"slack.read_messages": [{"rule": "approved_channel", "value": ["C123"]}]})
        verdict, rule, _reason = ev.preflight_from_args("slack.read_messages", {"channel_id": "C123"})
        assert (verdict, rule) == ("auto_accept", "approved_channel")

    def test_args_only_rule_no_match_is_requires_review(self):
        ev = AutoAcceptEvaluator({"slack.read_messages": [{"rule": "approved_channel", "value": ["C999"]}]})
        verdict, rule, _reason = ev.preflight_from_args("slack.read_messages", {"channel_id": "C123"})
        assert verdict == "requires_review"
        assert rule == ""

    def test_no_configured_rules_is_requires_review(self):
        ev = AutoAcceptEvaluator({})
        verdict, _rule, _reason = ev.preflight_from_args("gmail.read_message", {})
        assert verdict == "requires_review"

    def test_data_dependent_rule_configured_is_unknown(self):
        ev = AutoAcceptEvaluator({"gmail.read_message": [{"rule": "i_am_sender"}]})
        verdict, rule, reason = ev.preflight_from_args("gmail.read_message", {})
        assert verdict == "unknown"
        assert rule == ""
        assert "i_am_sender" in reason

    def test_undetermined_data_dependent_rule_does_not_produce_a_false_match(self):
        ev = AutoAcceptEvaluator({
            "gmail.read_message": [
                {"rule": "i_am_sender"},
                {"rule": "trusted_sender_domain", "value": ["trusted.com"]},
            ]
        })
        verdict, _rule, _reason = ev.preflight_from_args("gmail.read_message", {})
        assert verdict == "unknown"

    def test_args_only_match_wins_even_behind_an_undetermined_rule(self):
        ev = AutoAcceptEvaluator({
            "jira.read_issue": [
                {"rule": "i_am_reporter"},  # data-dependent, can't be decided from args
                {"rule": "approved_project_keys", "value": ["OPS"]},  # args-only, matches
            ]
        })
        verdict, rule, _reason = ev.preflight_from_args("jira.read_issue", {"issue_key": "OPS-1"})
        assert (verdict, rule) == ("auto_accept", "approved_project_keys")

    def test_absence_rule_never_evaluated_speculatively(self):
        # shared_drive_exclusion (and the other "absence" rules) would
        # falsely report a match if evaluated with raw_data=None -- see the
        # ARGS_ONLY_RULES docstring. Confirms it's routed to "unknown"
        # instead of a false "auto_accept".
        ev = AutoAcceptEvaluator({"drive.read_file_contents": [{"rule": "shared_drive_exclusion"}]})
        verdict, rule, _reason = ev.preflight_from_args("drive.read_file_contents", {})
        assert verdict == "unknown"
        assert rule == ""

    def test_never_calls_the_network_or_touches_raw_data(self):
        # preflight_from_args builds its own ReviewContext with raw_data=None
        # regardless of what's passed in -- there's no argument for raw_data
        # at all, so this is really just documenting the contract.
        ev = AutoAcceptEvaluator({"slack.read_messages": [{"rule": "approved_channel", "value": ["C1"]}]})
        verdict, rule, _reason = ev.preflight_from_args("slack.read_messages", {"channel_id": "C1"})
        assert (verdict, rule) == ("auto_accept", "approved_channel")


# --------------------------------------------------------------------------- #
# TOOL_TO_GATE: exhaustively cross-checked against docs and connector source
# in tests/unit/connectors/test_readme_manifest_alignment.py. This is just a
# couple of direct spot checks for the dict itself.
# --------------------------------------------------------------------------- #

class TestToolToGate:
    def test_auto_tool(self):
        assert TOOL_TO_GATE["gmail_list_messages"] == "auto"

    def test_review_tool(self):
        assert TOOL_TO_GATE["gmail_get_message"] == "review"

    def test_popup_tool(self):
        assert TOOL_TO_GATE["gmail_create_draft"] == "popup"


class TestMigrateTelegramSearchOperationKey:
    """telegram_search_messages now shares telegram.read_chat_messages with
    telegram_get_messages (see TOOL_TO_OPERATION) instead of its own
    telegram.search_messages key -- this one-time migration moves any
    existing hand-authored rules onto the new key. Mirrors
    resource_grants.TestMigrateRulesToGrants's marker/idempotency coverage."""

    def test_entries_move_onto_the_shared_key(self):
        cfg = {
            "auto_accept_rules": {
                "telegram.search_messages": [{"rule": "no_media_attachments"}],
                "telegram.read_chat_messages": [{"rule": "approved_chats", "value": ["111"]}],
            }
        }
        new_cfg, moved = auto_accept.migrate_telegram_search_operation_key(cfg)
        assert moved is True
        assert "telegram.search_messages" not in new_cfg["auto_accept_rules"]
        assert new_cfg["auto_accept_rules"]["telegram.read_chat_messages"] == [
            {"rule": "approved_chats", "value": ["111"]},
            {"rule": "no_media_attachments"},
        ]

    def test_no_op_when_no_search_messages_key_present(self):
        cfg = {"auto_accept_rules": {"telegram.read_chat_messages": [{"rule": "approved_chats", "value": ["111"]}]}}
        new_cfg, moved = auto_accept.migrate_telegram_search_operation_key(cfg)
        assert moved is False
        assert new_cfg["auto_accept_rules"] == cfg["auto_accept_rules"]

    def test_duplicate_entry_is_dropped_not_duplicated(self):
        cfg = {
            "auto_accept_rules": {
                "telegram.search_messages": [{"rule": "approved_chats", "value": ["111"]}],
                "telegram.read_chat_messages": [{"rule": "approved_chats", "value": ["111"]}],
            }
        }
        new_cfg, moved = auto_accept.migrate_telegram_search_operation_key(cfg)
        assert moved is False
        assert new_cfg["auto_accept_rules"]["telegram.read_chat_messages"] == [
            {"rule": "approved_chats", "value": ["111"]},
        ]

    def test_empty_config_has_no_rules_key_afterward(self):
        new_cfg, moved = auto_accept.migrate_telegram_search_operation_key({})
        assert moved is False
        assert "auto_accept_rules" not in new_cfg

    def test_migration_marker_is_set(self):
        new_cfg, _ = auto_accept.migrate_telegram_search_operation_key({})
        assert new_cfg[auto_accept.TELEGRAM_SEARCH_OPERATION_KEY_MIGRATION_MARKER] is True

    def test_already_marked_config_is_returned_unchanged(self):
        cfg = {
            auto_accept.TELEGRAM_SEARCH_OPERATION_KEY_MIGRATION_MARKER: True,
            "auto_accept_rules": {"telegram.search_messages": [{"rule": "no_media_attachments"}]},
        }
        new_cfg, moved = auto_accept.migrate_telegram_search_operation_key(cfg)
        assert new_cfg is cfg
        assert moved is False

    def test_idempotent_second_run_is_a_no_op(self):
        cfg = {
            "auto_accept_rules": {
                "telegram.search_messages": [{"rule": "no_media_attachments"}],
                "telegram.read_chat_messages": [{"rule": "approved_chats", "value": ["111"]}],
            }
        }
        migrated_once, _ = auto_accept.migrate_telegram_search_operation_key(cfg)
        migrated_twice, moved_twice = auto_accept.migrate_telegram_search_operation_key(migrated_once)
        assert moved_twice is False
        assert migrated_twice == migrated_once
