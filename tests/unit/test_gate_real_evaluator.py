"""Integration tests for gate.gated_call() driven by a *real*
AutoAcceptEvaluator (auto_accept.py), not the FakeEvaluator test_gate.py
uses for its state-machine coverage.

FakeEvaluator
returns a canned (bool, str) with no rule-matching logic of its own, so
test_gate.py's ~50 tests prove gated_call's state machine is correct given
*some* auto-accept verdict, but not that any specific auto_accept_rules
entry from settings.yaml actually produces that verdict for a given
connector call. That's exactly what a human currently checks by hand, rule
by rule, connector by connector, across docs/connector-qa-testing.md's ten
phases ("should NOT prompt" / "should still prompt" instructions). Each
class below ports one of those checks into a deterministic test: a real
AutoAcceptEvaluator, args/raw_data shaped the way the real connector module
builds them, and an assertion on both the return value and the resulting
AuditEntry fields -- not just "a popup would/wouldn't show."

The native popup layer (approval_popup.show_read_popup / show_popup / etc.)
is still monkeypatched to a scripted answer, same as test_gate.py -- that
mock boundary is correct and unchanged; only the auto-accept side moves
from fake to real. approval_window.py's actual window construction has its
own coverage in test_approval_window.py.

salesforce.read_record's approved_object_types rule already has a
real-evaluator regression test in test_gate.py::TestApprovedObjectTypesNeverPopsUp
(added after a live QA discrepancy) -- not duplicated here.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from privacyfence import auto_accept, gate
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.auto_accept import init_auto_accept_evaluator


@pytest.fixture(autouse=True)
def _fresh_popup_lock():
    # See test_gate.py's identical fixture for why: gate._popup_lock is a
    # module-level asyncio.Lock that must not survive across pytest-asyncio's
    # per-test event loops once anything has actually contended for it.
    gate._popup_lock = asyncio.Lock()
    yield


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


def read_audit_entries(audit_dir):
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


FILTERED = object()


def make_kwargs(**overrides):
    kwargs = dict(
        connector="gmail",
        tool="gmail_get_message",
        tool_name="Read Gmail message",
        summary="test call",
        sender="",
        raw_data=SimpleNamespace(),
        filtered_data=FILTERED,
        gate="review",
        preview={},
        details_text="ordinary, non-sensitive content",
        my_email="me@example.com",
        args={},
    )
    kwargs.update(overrides)
    return kwargs


def fail_if_popup_shown(monkeypatch, *, review=True, popup=True):
    """Assert neither popup function is called -- the auto-accept path must
    resolve without ever reaching the interactive layer."""
    def boom(*a, **k):
        raise AssertionError("a native popup must not be shown for an auto-accepted call")
    if review:
        monkeypatch.setattr(gate, "show_read_popup", boom)
    if popup:
        monkeypatch.setattr(gate, "show_popup", boom)


class TestGmailTrustedSenderDomain:
    """connector-qa-testing.md Phase 1 step 6: trusted_sender_domain must
    match subdomains of the configured value, not just an exact match."""

    RULES = {"gmail.read_message": [{"rule": "trusted_sender_domain", "value": "trusted.com"}]}

    async def test_subdomain_sender_auto_accepts_with_no_popup(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            raw_data=SimpleNamespace(sender="Alice <alice@mail.trusted.com>"),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "trusted_sender_domain"

    async def test_unrelated_domain_still_prompts(self, monkeypatch, audit_dir):
        # Contrast case: proves the rule above is actually reachable, not
        # vacuously matching everything.
        init_auto_accept_evaluator(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            raw_data=SimpleNamespace(sender="mallory@eviltrusted.com"),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"


class TestDriveApprovedFolder:
    """connector-qa-testing.md Phase 2 step 3 (plain auto-accept) and
    steps 21-23 (PII detection overrides a matching approved_folder rule on
    the read side, but a write to the same folder is never scanned)."""

    RULES = {"drive.read_file_contents": [{"rule": "approved_folder", "value": ["qa-folder-id"]}]}

    async def test_read_in_approved_folder_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_get_file_content", gate="review",
            raw_data=SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "approved_folder"

    async def test_pii_content_overrides_the_matching_folder_rule(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_get_file_content", gate="review",
            raw_data=SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]),
            details_text="His SSN is 123-45-6789 on file.",
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        # Not auto_accepted, even though the folder rule matches -- the PII
        # gate routes it to the interactive popup regardless (gate.py's
        # module docstring).
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""
        assert entries[0]["pii_detected"] is True

    async def test_write_of_the_same_pii_content_is_never_scanned(self, monkeypatch, audit_dir):
        # Same folder, same fake-PII body, but a write: gate="popup" never
        # runs the PII scan (gate.py's module docstring) or consults
        # approved_folder (writes never auto-accept via that rule).
        init_auto_accept_evaluator(self.RULES)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_write_doc_content", gate="popup",
            raw_data=SimpleNamespace(parent_ids=["qa-folder-id"], owners=[]),
            details_text="His SSN is 123-45-6789 on file.",
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is False


class TestDriveTempAccept:
    """connector-qa-testing.md Phase 2 steps 5/13/16: 'Allow for 5 min' on
    one call must silently auto-accept a second call for the same file,
    against the real evaluator's own in-memory temp-accept store -- not
    FakeEvaluator's canned register_temp_accept() list (see
    test_gate.py::TestTempAccept for that layer's coverage)."""

    async def test_second_call_for_the_same_file_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept_temp")

        first = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-abc"},
        ))
        assert first is FILTERED
        first_entry = read_audit_entries(audit_dir)[0]
        assert first_entry["decision"] == "accepted_via_temp_session"
        assert first_entry["auto_accept_rule"] == "session_temp_accept"

        fail_if_popup_shown(monkeypatch)  # second call must never reach the popup
        second = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-abc"},
        ))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[1]["decision"] == "auto_accepted"
        assert entries[1]["auto_accept_rule"] == "session_temp_accept"

    async def test_a_different_file_is_not_covered(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept_temp")
        await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-abc"},
        ))

        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")
        result = await gate.gated_call(**make_kwargs(
            connector="drive", tool="drive_add_comment", gate="popup",
            args={"file_id": "file-different"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[1]["decision"] == "approved"


class TestCalendarIAmOrganizer:
    """connector-qa-testing.md Phase 4 step 5."""

    RULES = {"calendar.read_event_details": [{"rule": "i_am_organizer"}]}

    async def test_own_event_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="calendar", tool="calendar_get_event_details", gate="review",
            raw_data=SimpleNamespace(organizer_email="me@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_someone_elses_event_still_prompts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="calendar", tool="calendar_get_event_details", gate="review",
            raw_data=SimpleNamespace(organizer_email="someone-else@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestJiraRules:
    """connector-qa-testing.md Phase 9 steps 2-3 (approved_project_keys,
    with an out-of-allowlist contrast) and step 5 (i_am_reporter /
    i_am_assignee auto-accept independent of the project rule)."""

    async def test_issue_in_approved_project_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"jira.read_issue": [{"rule": "approved_project_keys", "value": ["PFQA"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "PFQA-1"},
            raw_data=SimpleNamespace(reporter="", assignee=""),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "approved_project_keys"

    async def test_issue_in_other_project_still_prompts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"jira.read_issue": [{"rule": "approved_project_keys", "value": ["PFQA"]}]})
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "OTHER-5"},
            raw_data=SimpleNamespace(reporter="", assignee=""),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_reporter_auto_accepts_even_outside_the_approved_project(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"jira.read_issue": [{"rule": "i_am_reporter"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "OTHER-99"},
            raw_data=SimpleNamespace(reporter="me@example.com", assignee="someone-else@example.com"),
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "i_am_reporter"

    async def test_assignee_auto_accepts_independent_of_reporter_rule(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"jira.read_issue": [{"rule": "i_am_assignee"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="jira", tool="jira_get_issue", gate="review",
            args={"issue_key": "OTHER-100"},
            raw_data=SimpleNamespace(reporter="someone-else@example.com", assignee="me@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["auto_accept_rule"] == "i_am_assignee"


class TestConfluenceRules:
    """connector-qa-testing.md Phase 10 steps 2-3 (approved_space_keys, with
    an out-of-allowlist contrast) and step 5 (i_am_author auto-accepts
    independent of the space rule)."""

    async def test_page_in_approved_space_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"confluence.read_page": [{"rule": "approved_space_keys", "value": ["PFQA"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="confluence", tool="confluence_get_page", gate="review",
            args={"space_key": "PFQA"},
            raw_data=SimpleNamespace(author=""),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["auto_accept_rule"] == "approved_space_keys"

    async def test_page_in_other_space_still_prompts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"confluence.read_page": [{"rule": "approved_space_keys", "value": ["PFQA"]}]})
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="confluence", tool="confluence_get_page", gate="review",
            args={"space_key": "OTHERSPACE"},
            raw_data=SimpleNamespace(author=""),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_author_auto_accepts_even_outside_the_approved_space(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"confluence.read_page": [{"rule": "i_am_author"}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="confluence", tool="confluence_get_page", gate="review",
            args={"space_key": "OTHERSPACE"},
            raw_data=SimpleNamespace(author="me@example.com"),
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["auto_accept_rule"] == "i_am_author"


class TestContactsNoContactInfoChange:
    """connector-qa-testing.md Phase 5 steps 5-6: a name/note-only edit may
    auto-accept; the same rule must not cover an edit that also touches
    email/phone."""

    RULES = {"contacts.edit": [{"rule": "no_contact_info_change"}]}

    async def test_name_only_edit_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="contacts", tool="contacts_update", gate="popup",
            args={"contact_id": "c1", "display_name": "New Name (edited)"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_email_change_still_prompts_even_with_the_rule_configured(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(self.RULES)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="contacts", tool="contacts_update", gate="popup",
            args={"contact_id": "c1", "emails": ["new@example.com"]},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestTasksApprovedTaskList:
    """connector-qa-testing.md Phase 6 step 4 (create/update in an approved
    list) and step 6's move variant, which -- unlike every other operation
    this rule covers -- requires BOTH the source and destination list to be
    on the allowlist (auto_accept.py::_rule_approved_task_list's
    docstring)."""

    async def test_update_in_approved_list_auto_accepts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"tasks.update_task": [{"rule": "approved_task_list", "value": ["list-a"]}]})
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_update_task", gate="popup",
            args={"task_list_id": "list-a"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_update_in_unapproved_list_still_prompts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator({"tasks.update_task": [{"rule": "approved_task_list", "value": ["list-a"]}]})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_update_task", gate="popup",
            args={"task_list_id": "list-b"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"

    async def test_move_auto_accepts_only_when_both_ends_are_approved(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(
            {"tasks.move_task": [{"rule": "approved_task_list", "value": ["list-a", "list-b"]}]}
        )
        fail_if_popup_shown(monkeypatch)

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_move_task", gate="popup",
            args={"source_list_id": "list-a", "destination_list_id": "list-b"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "auto_accepted"

    async def test_move_to_an_unapproved_destination_still_prompts(self, monkeypatch, audit_dir):
        init_auto_accept_evaluator(
            {"tasks.move_task": [{"rule": "approved_task_list", "value": ["list-a", "list-b"]}]}
        )
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**make_kwargs(
            connector="tasks", tool="tasks_move_task", gate="popup",
            args={"source_list_id": "list-a", "destination_list_id": "list-c"},
        ))

        assert result is FILTERED
        assert read_audit_entries(audit_dir)[0]["decision"] == "approved"


class TestAcceptAllPersistsARealRule:
    """connector-qa-testing.md's Always allow pattern (e.g. Phase 2 step 12):
    confirming 'Always allow' on one call must persist a real rule that then
    silently covers a second, different-but-matching call -- exercised here
    against the real on-disk persistence path (auto_accept.add_auto_accept_rule),
    not just the in-memory FakeEvaluator assertions test_gate.py::TestAcceptAll
    already covers for the state-machine side of this flow.
    """

    async def test_second_matching_call_is_silently_auto_accepted(self, monkeypatch, audit_dir, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))
        init_auto_accept_evaluator({})

        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

        first = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_get_message", gate="review",
            raw_data=SimpleNamespace(sender="alice@example.com"),
        ))
        assert first is FILTERED
        first_entry = read_audit_entries(audit_dir)[0]
        assert first_entry["decision"] == "accepted_via_accept_all"
        assert first_entry["auto_accept_rule"] == "trusted_sender_domain"

        on_disk = config_path.read_text(encoding="utf-8")
        assert "trusted_sender_domain" in on_disk
        assert "example.com" in on_disk

        fail_if_popup_shown(monkeypatch)  # the newly created rule must cover this one silently
        second = await gate.gated_call(**make_kwargs(
            connector="gmail", tool="gmail_get_message", gate="review",
            raw_data=SimpleNamespace(sender="bob@example.com"),  # different sender, same domain
        ))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[1]["decision"] == "auto_accepted"
        assert entries[1]["auto_accept_rule"] == "trusted_sender_domain"
