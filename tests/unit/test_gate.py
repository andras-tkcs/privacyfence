"""Unit tests for privacyfence.gate.gated_call — the single choke point every
tool call passes through (auto-accept check -> native popup -> audit log).

These tests stub out the native popup functions and the auto-accept
evaluator so the state machine can be exercised deterministically, without
spawning real osascript dialogs. The one invariant that matters more than
any individual branch: gated_call must never return raw_data when
filtered_data differs from it -- that's the actual privacy boundary.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from privacyfence import gate
from privacyfence.audit_log import get_audit_logger, init_audit_logger
from privacyfence.auto_accept import AutoAcceptEvaluator
from privacyfence.pii_detector import init_pii_detection


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    """Poll ``predicate`` until it's true or ``timeout`` elapses.

    Used from a background thread to synchronize with state mutated by the
    event loop's thread, without an artificial fixed sleep -- ``time.sleep``
    releases the GIL, so the event-loop thread gets to make progress while
    this polls.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeEvaluator:
    def __init__(self, result=(False, "")):
        self.result = result
        self.calls = []
        self.temp_accepts_registered = []

    def should_auto_accept(self, operation_key, ctx):
        self.calls.append((operation_key, ctx))
        return self.result

    def register_temp_accept(self, operation_key, file_key, ttl_seconds=None):
        self.temp_accepts_registered.append((operation_key, file_key))


@pytest.fixture(autouse=True)
def _fresh_popup_lock():
    # gate._popup_lock is a module-level asyncio.Lock, so it outlives any one
    # test. asyncio.Lock only binds itself to a running event loop lazily, the
    # first time a second waiter actually contends for it (see cpython's
    # asyncio.Lock.acquire: an uncontended acquire never calls _get_loop()).
    # pytest-asyncio gives each test function its own event loop, so once one
    # test creates real contention on this lock, it's permanently bound to
    # that (soon-to-be-closed) loop and every later contention test would
    # raise "bound to a different event loop". Give each test a fresh lock so
    # tests that exercise concurrent gated_call() waiters never depend on
    # test execution order.
    gate._popup_lock = asyncio.Lock()
    yield


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


def read_audit_entries(audit_dir):
    from privacyfence.audit_log import current_week
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


RAW = object()      # sentinel: never returned
FILTERED = object()  # sentinel: always what gated_call must return on success


def base_kwargs(**overrides):
    kwargs = dict(
        connector="gmail",
        tool="gmail_get_message",
        tool_name="Read Gmail message",
        summary="from alice@example.com",
        sender="alice@example.com",
        raw_data=RAW,
        filtered_data=FILTERED,
        gate="review",
        preview={"from": "alice@example.com"},
        details_text="full body here",
        my_email="me@example.com",
    )
    kwargs.update(overrides)
    return kwargs


class TestAutoAcceptPath:
    async def test_auto_accepted_returns_filtered_data_without_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "deny", None))

        result = await gate.gated_call(**base_kwargs())

        assert result is FILTERED
        assert called == []  # popup never shown

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "i_am_sender"

    async def test_auto_accept_evaluated_against_raw_not_filtered_data(self, monkeypatch, audit_dir):
        evaluator = FakeEvaluator((True, "some_rule"))
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)

        await gate.gated_call(**base_kwargs())

        assert len(evaluator.calls) == 1
        _, ctx = evaluator.calls[0]
        assert ctx.raw_data is RAW

    async def test_operation_key_uses_tool_to_operation_mapping(self, monkeypatch, audit_dir):
        evaluator = FakeEvaluator((True, "x"))
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)

        await gate.gated_call(**base_kwargs(connector="gmail", tool="gmail_get_message"))

        op_key, _ = evaluator.calls[0]
        assert op_key == "gmail.read_message"

    async def test_operation_key_falls_back_to_connector_dot_tool(self, monkeypatch, audit_dir):
        # Deliberately not a key in TOOL_TO_OPERATION, so this only exercises
        # the f"{connector}.{tool}" fallback formula, independent of however
        # many tools that mapping table grows to cover over time.
        evaluator = FakeEvaluator((True, "x"))
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)

        await gate.gated_call(**base_kwargs(connector="widget", tool="widget_do_thing"))

        op_key, _ = evaluator.calls[0]
        assert op_key == "widget.widget_do_thing"


class TestReviewGateDecisions:
    async def test_deny_raises_and_audits_rejected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_plain_accept_returns_filtered_and_audits_approved(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""

    async def test_show_read_popup_receives_one_choice_when_suggestion_exists(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["accept_all_choices"] = accept_all_choices
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["accept_all_choices"] == [("i_am_sender", "if I'm sender")]

    async def test_show_read_popup_receives_no_choices_without_a_suggestion(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["accept_all_choices"] = accept_all_choices
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["accept_all_choices"] == []

    async def test_show_read_popup_receives_two_choices_for_a_multi_candidate_item(
        self, monkeypatch, audit_dir,
    ):
        # The multi-button window (issue #151): each matching candidate
        # becomes its own (rule_name, short_label) entry, not a single
        # top-priority hint.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(
            gate, "suggest_rule_choices",
            lambda *a, **k: [("i_am_owner", None), ("approved_folder", ["f1"])],
        )
        captured = {}

        def fake_show_read_popup(*args, **kwargs):
            captured["accept_all_choices"] = args[3]  # positional -- see gate.py's own call
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["accept_all_choices"] == [
            ("i_am_owner", "if I own it"), ("approved_folder", "this folder"),
        ]


class TestAcceptAll:
    async def test_accept_all_confirmed_creates_rule_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(
            gate, "suggest_rule_choices", lambda *a, **k: [("trusted_sender_domain", ["example.com"])],
        )
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(gate="review", connector="gmail", tool="gmail_get_message"))

        assert result is FILTERED
        assert added == [("gmail.read_message", "trusted_sender_domain", ["example.com"])]

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "trusted_sender_domain"

    async def test_accept_all_without_suggestion_falls_back_to_plain_approve(self, monkeypatch, audit_dir):
        # Shouldn't happen against the real window (no Always-allow button
        # renders with zero choices) -- but a defensive "no matching
        # candidate" chosen_index must still degrade to a plain accept.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", None))
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_accept_all_cancelled_confirmation_still_returns_data_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []  # no standing rule created
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # accepted once, not via a rule


class TestAcceptAllMultipleChoices:
    """When suggest_rule_choices() returns 2+ candidates for the same item
    (e.g. a Drive file you own that's also in an approved folder), the
    popup renders one "Always allow" button per candidate (issue #151) --
    which rule gets created is decided by *which button was clicked*
    (chosen_index, the popup's own return value), not a second chooser
    dialog shown after a single generic Always-allow click."""

    async def test_second_candidate_clicked_creates_that_specific_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(
            gate, "suggest_rule_choices",
            lambda *a, **k: [("i_am_owner", None), ("approved_folder", ["f1"])],
        )
        # Index 1 -- the second button ("approved_folder"'s), not the first.
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 1))
        confirm_calls = []
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description: confirm_calls.append(description) or True,
        )
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert len(confirm_calls) == 1  # the same single-item confirm dialog every candidate gets
        assert added == [("gmail.read_message", "approved_folder", ["f1"])]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "approved_folder"

    async def test_first_candidate_clicked_creates_that_rule_instead(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(
            gate, "suggest_rule_choices",
            lambda *a, **k: [("i_am_owner", None), ("approved_folder", ["f1"])],
        )
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == [("gmail.read_message", "i_am_owner", None)]

    async def test_multiple_choices_cancelled_confirmation_still_accepts_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(
            gate, "suggest_rule_choices",
            lambda *a, **k: [("i_am_owner", None), ("approved_folder", ["f1"])],
        )
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 1))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)  # cancelled
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # accepted once, not via a rule

    async def test_out_of_range_chosen_index_degrades_to_plain_accept(self, monkeypatch, audit_dir):
        # Defensive bounds-check against the popup's own JS bridge --
        # shouldn't happen against the real button row, but must not raise.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(
            gate, "suggest_rule_choices",
            lambda *a, **k: [("i_am_owner", None), ("approved_folder", ["f1"])],
        )
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 5))
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_single_matching_candidate_still_uses_the_same_confirm_popup(
        self, monkeypatch, audit_dir,
    ):
        # Regression guard: exactly one candidate behaves identically to the
        # multi-candidate case above, just with only index 0 to click.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == [("gmail.read_message", "i_am_sender", None)]


class TestAcceptAllWrites:
    """The write-gate counterpart to TestAcceptAll -- gate.suggest_write_rule()
    drives an "Always allow" button on the write operations listed in
    auto_accept.WRITE_RULE_SUGGESTIONS, using the same accept_all/
    show_rule_confirmation_popup/add_auto_accept_rule machinery the review
    branch already uses, not a separate mechanism."""

    async def test_accept_all_confirmed_creates_rule_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("label_name_allowlist", ["Newsletters"]))
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert result is FILTERED
        assert added == [("gmail.add_label", "label_name_allowlist", ["Newsletters"])]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "label_name_allowlist"

    async def test_confirmation_description_uses_describe_rule_change_not_describe_rule(self, monkeypatch, audit_dir):
        # describe_rule()'s canned templates are read-direction-only English
        # ("... reads ...") and would mislabel a write's own confirmation --
        # describe_rule_change() names operation_key explicitly instead.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("approved_project_keys", ["PFQA"]))
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        captured = {}

        def fake_confirm(description):
            captured["description"] = description
            return True

        monkeypatch.setattr(gate, "show_rule_confirmation_popup", fake_confirm)
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: None)

        await gate.gated_call(**base_kwargs(gate="popup", connector="jira", tool="jira_create_issue"))

        assert captured["description"] == (
            "Add auto-accept rule 'approved_project_keys' = PFQA to 'jira.create_issue'"
        )

    async def test_accept_all_without_suggestion_falls_back_to_plain_approve(self, monkeypatch, audit_dir):
        # gmail_send_message has no entry in WRITE_RULE_SUGGESTIONS at all --
        # even if the (real) popup somehow returned "accept_all" (no
        # Always-allow button ever renders with zero choices), there's no
        # suggestion to act on, so this must behave like a plain accept.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", None))
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_send_message"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_accept_all_cancelled_confirmation_still_accepts_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("label_name_allowlist", ["Newsletters"]))
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_drive_write_offers_approved_sandbox_folder_end_to_end(self, monkeypatch, audit_dir):
        # End-to-end through the real (unmocked) suggest_write_rule, for one
        # of the 13 Drive/Sheets/Docs write operations that now share the
        # sandbox-folder suggestion.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_write_file_content",
            raw_data={"file": SimpleNamespace(parent_ids=["folder1"]), "content_preview": "x"},
        ))

        assert result is FILTERED
        assert added == [("drive.write_file", "approved_sandbox_folder", ["folder1"])]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "approved_sandbox_folder"

    async def test_gmail_create_draft_offers_the_unconditional_always_allow_rule(self, monkeypatch, audit_dir):
        # End-to-end through the real (unmocked) suggest_write_rule -- unlike
        # every other write suggestion, always_allow has no recipient/value
        # to scope it to, so this also exercises describe_rule_change's
        # "no value" formatting for the confirmation popup.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        captured = {}
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description: captured.setdefault("description", description) or True,
        )
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="gmail", tool="gmail_create_draft",
            args={"to": "anyone@example.com"},
        ))

        assert result is FILTERED
        assert added == [("gmail.create_draft", "always_allow", None)]
        assert captured["description"] == "Add auto-accept rule 'always_allow' to 'gmail.create_draft'"
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "always_allow"

    async def test_show_popup_receives_one_choice_when_suggestion_exists(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("label_name_allowlist", ["Newsletters"]))
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["accept_all_choices"] = accept_all_choices
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert captured["accept_all_choices"] == [("label_name_allowlist", "this label")]

    async def test_show_popup_receives_no_choices_without_suggestion(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["accept_all_choices"] = accept_all_choices
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_create_draft"))

        assert captured["accept_all_choices"] == []

    async def test_show_popup_receives_the_short_rule_hint_for_the_suggestion(self, monkeypatch, audit_dir):
        # The write-gate counterpart to the review-gate's own equivalent
        # test above -- same describe_rule_short() derivation, from
        # suggest_write_rule() instead of suggest_rule_choices().
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("label_name_allowlist", ["Newsletters"]))
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured["accept_all_choices"] = args[8]  # positional -- see gate.py's own call
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert captured["accept_all_choices"] == [("label_name_allowlist", "this label")]

    async def test_show_popup_receives_an_empty_hint_for_the_unconditional_always_allow_rule(
        self, monkeypatch, audit_dir,
    ):
        # gmail_create_draft's real suggestion is always_allow -- the one
        # rule with no category to name, so the button stays plain "Always
        # allow" even though a choice is still offered for it.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("always_allow", None))
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured["accept_all_choices"] = args[8]  # positional -- see gate.py's own call
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_create_draft"))

        assert captured["accept_all_choices"] == [("always_allow", "")]

    async def test_show_popup_receives_preview_tables_and_blocks_and_table_only(self, monkeypatch, audit_dir):
        # Regression: these three were threaded through show_read_popup
        # (the review-gate branch below) but never forwarded to show_popup
        # at all -- a write connector passing preview_tables/preview_blocks
        # (e.g. drive_sheets_write_range's own values table, jira_create_
        # issue's Description heading) had it silently dropped before ever
        # reaching the real approval window, even though gated_call()'s own
        # signature accepted the kwargs with no error.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["preview_tables"] = preview_tables
            captured["preview_blocks"] = preview_blocks
            captured["table_only"] = table_only
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        table = {"headers": ["A", "B"], "rows": [["1", "2"]]}
        blocks = [{"type": "heading", "label": "Description"}]

        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range",
            preview_tables=[table], preview_blocks=blocks, table_only=True,
        ))

        assert captured["preview_tables"] == [table]
        assert captured["preview_blocks"] == blocks
        assert captured["table_only"] is True

    async def test_pii_gate_never_applies_to_a_write_accept_all(self, monkeypatch, audit_dir):
        # Sanity check on the module docstring's own claim: the popup branch
        # never touches pii_categories/show_pii_confirmation_popup at all,
        # accept_all included -- there's no "possible PII flowed in from an
        # external source" to confirm on a write.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_write_rule", lambda *a, **k: ("label_name_allowlist", ["Newsletters"]))
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: None)

        def boom(*a, **k):
            raise AssertionError("show_pii_confirmation_popup must never be called for a write")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", boom)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="gmail", tool="gmail_add_label",
            details_text="His SSN is 123-45-6789 on file.",
        ))
        assert result is FILTERED


class TestProposeRuleChange:
    """gate.propose_rule_change() -- the bridge-facing counterpart to the
    popup's own "Always allow" flow (see its docstring in gate.py). Every
    proposal reaches the same show_rule_confirmation_popup() dialog; there
    is no auto-accept short-circuit and no silent no-op for a duplicate
    proposal -- confirming again is cheap, unlike gated_call's regular path."""

    async def test_confirmed_rule_add_persists_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: added.append((op, name, value)))

        result = await gate.propose_rule_change(
            target="rule", operation="add", reason="Trusting example.com.",
            operation_key="gmail.read_message", rule_name="trusted_sender_domain", value=["example.com"],
        )

        assert result == {
            "confirmed": True, "changed": True,
            "description": "Add auto-accept rule 'trusted_sender_domain' = example.com to 'gmail.read_message'",
        }
        assert added == [("gmail.read_message", "trusted_sender_domain", ["example.com"])]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rule_changed_via_bridge_proposal"
        assert entries[0]["claude_reason"] == "Trusting example.com."

    async def test_confirmed_rule_remove_persists_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        removed = []
        monkeypatch.setattr(gate, "remove_auto_accept_rule", lambda op, name, value=None: removed.append((op, name, value)) or True)

        result = await gate.propose_rule_change(
            target="rule", operation="remove", reason="Cleaning up.",
            operation_key="sheets.format_range", rule_name="approved_sandbox_folder", value=["folder1"],
        )

        assert result["confirmed"] is True
        assert removed == [("sheets.format_range", "approved_sandbox_folder", ["folder1"])]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rule_removed_via_bridge_proposal"

    async def test_confirmed_rule_remove_that_changes_nothing_audits_as_no_op(self, monkeypatch, audit_dir):
        # remove_auto_accept_rule() returns False when the named rule/value
        # never matched anything to begin with (e.g. Claude proposed
        # removing a value that was already gone) -- the human still said
        # yes, but config didn't actually change, so this must not be
        # recorded as though a removal happened.
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        monkeypatch.setattr(gate, "remove_auto_accept_rule", lambda op, name, value=None: False)

        result = await gate.propose_rule_change(
            target="rule", operation="remove", reason="Cleaning up.",
            operation_key="sheets.format_range", rule_name="approved_sandbox_folder", value=["folder1"],
        )

        assert result == {
            "confirmed": True, "changed": False,
            "description": "Remove auto-accept rule 'approved_sandbox_folder' = folder1 from 'sheets.format_range'",
        }
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "bridge_proposal_no_op"

    async def test_rule_update_removes_old_value_then_adds_new_one(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        calls = []
        monkeypatch.setattr(gate, "remove_auto_accept_rule", lambda op, name, value=None: calls.append(("remove", op, name, value)) or True)
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: calls.append(("add", op, name, value)))

        await gate.propose_rule_change(
            target="rule", operation="update", reason="Replacing.",
            operation_key="gmail.read_message", rule_name="trusted_sender_domain",
            value=["b.com"], old_value=["a.com"],
        )

        assert calls == [
            ("remove", "gmail.read_message", "trusted_sender_domain", ["a.com"]),
            ("add", "gmail.read_message", "trusted_sender_domain", ["b.com"]),
        ]

    async def test_confirmed_grant_add_persists_via_mutate_grants_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        mutate_calls = []
        monkeypatch.setattr(gate, "mutate_grants", lambda mutator: mutate_calls.append(mutator) or True)

        result = await gate.propose_rule_change(
            target="grant", operation="add", reason="Trusting the sandbox folder.",
            connector="drive", config_key="sandbox_folders", resource_id="folder1",
            name="Team sandbox", capabilities={"write": True},
        )

        assert result["confirmed"] is True
        assert len(mutate_calls) == 1
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "grant_changed_via_bridge_proposal"
        assert entries[0]["auto_accept_rule"] == "folder1"

    async def test_confirmed_grant_remove_persists_via_mutate_grants_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        monkeypatch.setattr(gate, "mutate_grants", lambda mutator: True)

        result = await gate.propose_rule_change(
            target="grant", operation="remove", reason="No longer needed.",
            connector="drive", config_key="sandbox_folders", resource_id="folder1",
        )

        assert result["confirmed"] is True
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "grant_removed_via_bridge_proposal"

    async def test_confirmed_grant_remove_that_changes_nothing_audits_as_no_op(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        monkeypatch.setattr(gate, "mutate_grants", lambda mutator: False)

        result = await gate.propose_rule_change(
            target="grant", operation="remove", reason="No longer needed.",
            connector="drive", config_key="sandbox_folders", resource_id="folder1",
        )

        assert result["confirmed"] is True
        assert result["changed"] is False
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "bridge_proposal_no_op"

    async def test_unknown_rule_name_raises_value_error_without_showing_a_popup(self, monkeypatch):
        # rule_name comes straight from Claude here, unlike the "Always
        # allow" flow (which only ever offers names suggest_rule() itself
        # produces) -- a misspelled/made-up name must be rejected up front,
        # not persisted as a rule that silently never matches anything.
        called = []
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: called.append(1) or True)

        with pytest.raises(ValueError, match="Unknown auto-accept rule"):
            await gate.propose_rule_change(
                target="rule", operation="add", reason="x",
                operation_key="gmail.read_message", rule_name="made_up_rule", value="x",
            )

        assert called == []

    async def test_unknown_grant_resource_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown grant resource type"):
            await gate.propose_rule_change(
                target="grant", operation="add", reason="x",
                connector="nope", config_key="nope", resource_id="x",
            )

    async def test_unknown_target_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown target"):
            await gate.propose_rule_change(target="nope", operation="add", reason="x")

    async def test_unknown_operation_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown operation"):
            await gate.propose_rule_change(
                target="rule", operation="destroy", reason="x",
                operation_key="gmail.read_message", rule_name="i_am_sender",
            )

    async def test_declined_confirmation_raises_and_audits_rejected_without_applying(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        with pytest.raises(RuntimeError, match="denied by user"):
            await gate.propose_rule_change(
                target="rule", operation="add", reason="x",
                operation_key="gmail.read_message", rule_name="i_am_sender",
            )

        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_unattended_connection_denies_without_showing_a_popup(self, monkeypatch, audit_dir):
        called = []
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: called.append(1) or True)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.propose_rule_change(
                    target="rule", operation="add", reason="x",
                    operation_key="gmail.read_message", rule_name="i_am_sender",
                )

        assert called == []
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"


class TestPopupGateWrites:
    async def test_accept_returns_filtered_and_audits_approved(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        read_popup_called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (read_popup_called.append(1) or "deny", None))
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        assert read_popup_called == []  # write gate never shows the read popup
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_deny_raises_and_audits_rejected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_matching_rule_auto_accepts_without_a_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "trusted_sender_domain")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"

    async def test_write_gate_never_triggers_the_pii_confirmation_gate(self, monkeypatch, audit_dir):
        # Unlike the review (read) gate -- see TestPIIGate -- writes are
        # content Claude itself generated, not personal data flowing in from
        # an external source, so this gate's confirmation-dialog machinery
        # (pii_categories / show_pii_confirmation_popup / the audit log's
        # pii_detected field) never engages for a write. It's still scanned
        # for the separate, informational write_content_flags signal -- see
        # TestWriteContentFlags below -- which doesn't touch any of these.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["details"] = details
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert result is FILTERED
        assert confirm_calls == []
        assert "DE89370400440532013000" in captured["details"]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is False


class TestUploadPiiGate:
    """upload_pii_scan_text is the one deliberate exception to
    TestPopupGateWrites's "the write gate never triggers the PII
    confirmation gate" -- only ever set by drive_upload_file, since its
    payload can be external content Claude never read. When set, this
    behaves like the review-gate's own pii_categories: forces
    show_pii_confirmation_popup, overrides a matching auto-accept rule, and
    folds into the audit log's pii_detected field -- unlike
    write_content_flags, which stays informational-only regardless.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_no_upload_pii_scan_text_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="drive_upload_file"))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_clean_upload_pii_scan_text_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text="nothing sensitive here",
        ))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_flagged_upload_pii_scan_text_forces_confirmation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_declining_confirmation_denies_the_whole_upload(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(
                gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
            ))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_flagged_upload_pii_scan_text_sets_upload_forced_true_on_the_popup(
        self, monkeypatch, audit_dir,
    ):
        # v2's "write-forced" PII card (see approval_window_html.py's
        # _risk_section_html) needs to know this popup is about to force the
        # same second confirmation the read side gets -- show_popup's
        # upload_forced kwarg is how gate.py signals that.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
        ))

        assert captured["upload_forced"] is True

    async def test_clean_upload_pii_scan_text_leaves_upload_forced_false_on_the_popup(
        self, monkeypatch, audit_dir,
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text="nothing sensitive here",
        ))

        assert captured["upload_forced"] is False

    async def test_flagged_content_overrides_a_matching_auto_accept_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "parent_folder_allowlist")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
        ))

        assert result is FILTERED
        assert popup_calls == [1]  # the popup was NOT skipped, despite auto_ok
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # not "auto_accepted"
        assert entries[0]["pii_detected"] is True

    async def test_matching_rule_without_flagged_content_still_auto_accepts_silently(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "parent_folder_allowlist")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text="nothing sensitive here",
        ))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_detected"] is False

    async def test_write_content_flags_still_computed_independently(self, monkeypatch, audit_dir):
        # upload_pii_scan_text and details_text are scanned separately --
        # confirms adding the real gate didn't remove the existing
        # informational write_content_flags signal (TestWriteContentFlags).
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file",
            details_text=self.PII_TEXT, upload_pii_scan_text=self.PII_TEXT,
        ))

        assert captured["write_content_flags"] == ["IBAN (bank account number)"]


class TestRequestFingerprint:
    """seen_count: AuditLogger.recent_matches(connector, tool, summary),
    computed once per gated_call and forwarded to both popup functions."""

    async def test_first_time_request_has_zero_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 0

    async def test_repeated_approval_increments_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        # Two prior approvals of the exact same (connector, tool, summary).
        await gate.gated_call(**base_kwargs(gate="review"))
        await gate.gated_call(**base_kwargs(gate="review"))

        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 2

    async def test_different_summary_does_not_count_toward_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review", summary="from bob@example.com"))

        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review", summary="from alice@example.com"))

        assert captured["seen_count"] == 0

    async def test_seen_count_forwarded_to_show_popup_too(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["seen_count"] == 1

    async def test_rejected_prior_call_does_not_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 0


class TestWriteContentFlags:
    """The separate, informational-only signal computed for the popup
    (write) gate -- see gate.py's write_content_flags comment. Distinct
    from pii_categories (TestPIIGate): no confirmation gate, never touches
    AuditEntry.pii_detected."""

    async def test_flags_computed_from_details_and_forwarded_to_show_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert captured["write_content_flags"] == ["IBAN (bank account number)"]

    async def test_no_flags_when_content_has_nothing_flaggable(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft", details_text="See you at 3pm tomorrow.",
        ))

        assert captured["write_content_flags"] == []

    async def test_review_gate_call_succeeds_without_write_content_flags_kwarg(self, monkeypatch, audit_dir):
        # show_read_popup's signature has no write_content_flags param at
        # all (it's popup-gate only, unlike pii_categories/visibility,
        # which are read-gate signals) -- if gated_call's review branch
        # ever tried to pass it, this call would raise a TypeError.
        # Succeeding here is the assertion.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text="full body here",
        ))

        assert result is FILTERED

    async def test_flags_never_affect_pii_detected_audit_field(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is False  # write_content_flags never feeds this field

    async def test_disabling_pii_detection_also_suppresses_write_content_flags(self, monkeypatch, audit_dir):
        # write_content_flags calls the same detect_pii_categories() entry
        # point, which already respects the menu-bar enable/disable toggle
        # -- no separate toggle needed for this signal.
        from privacyfence import pii_detector
        monkeypatch.setattr(pii_detector, "_enabled", False)
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert captured["write_content_flags"] == []


class TestTempAccept:
    """The 5-minute, in-memory-only grace window that used to require a
    distinct "Allow for 5 min" popup button -- for the operations expected
    to be called repeatedly against the same file in quick succession
    (auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS), it's now armed as a side
    effect of a plain "accept" (Allow once) whenever a file_key resolves,
    with no separate choice offered. show_popup itself only ever returns
    'accept' or 'deny' now (see approval_window.py); gate.py is what decides
    whether an 'accept' also registers the grace window.
    """

    SHEETS_ARGS = {"spreadsheet_id": "sheet-1", "range_a1": "A1:B2"}

    async def test_show_popup_receives_temp_accept_eligible_true_for_eligible_op_with_file_key(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["temp_accept_eligible"] = temp_accept_eligible
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
            ))

        assert captured["temp_accept_eligible"] is True

    async def test_show_popup_receives_temp_accept_eligible_false_for_ineligible_op(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["temp_accept_eligible"] = temp_accept_eligible
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["temp_accept_eligible"] is False

    async def test_show_popup_receives_temp_accept_eligible_false_when_file_key_missing(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["temp_accept_eligible"] = temp_accept_eligible
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="popup", connector="drive", tool="drive_sheets_write_range", args={"range_a1": "A1:B2"},
            ))

        assert captured["temp_accept_eligible"] is False

    async def test_accept_on_eligible_op_registers_temp_accept_and_audits(self, monkeypatch, audit_dir):
        evaluator = FakeEvaluator()
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))

        assert result is FILTERED
        assert evaluator.temp_accepts_registered == [("sheets.write_range", "sheet-1")]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_temp_session"
        assert entries[0]["auto_accept_rule"] == "session_temp_accept"

    async def test_second_write_to_same_file_auto_accepts_without_a_second_popup(
        self, monkeypatch, audit_dir
    ):
        from privacyfence.auto_accept import AutoAcceptEvaluator

        evaluator = AutoAcceptEvaluator({})
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        result1 = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))
        result2 = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))

        assert result1 is FILTERED
        assert result2 is FILTERED
        assert len(popup_calls) == 1  # second call skipped the popup entirely

        entries = read_audit_entries(audit_dir)
        decisions = sorted(e["decision"] for e in entries)
        assert decisions == ["accepted_via_temp_session", "auto_accepted"]

    async def test_a_different_spreadsheet_still_shows_its_own_popup(self, monkeypatch, audit_dir):
        from privacyfence.auto_accept import AutoAcceptEvaluator

        evaluator = AutoAcceptEvaluator({})
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))
        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range",
            args={"spreadsheet_id": "sheet-2", "range_a1": "A1:B2"},
        ))

        assert len(popup_calls) == 2

    async def test_accept_for_ineligible_op_audits_as_plain_approved(
        self, monkeypatch, audit_dir
    ):
        # No file_key resolves for an ineligible operation, so a plain
        # accept must never register a temp accept or use the
        # accepted_via_temp_session decision -- it's an ordinary approval.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_pii_shaped_content_does_not_gate_a_temp_accept(self, monkeypatch, audit_dir):
        # The write (popup) gate never scans for PII -- see TestPopupGateWrites
        # below -- so PII-shaped content in a temp-accept-eligible write must
        # register the temp accept exactly as any other content would, with
        # no confirmation popup and no "pii_detected" in the audit entry.
        evaluator = FakeEvaluator()
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert result is FILTERED
        assert confirm_calls == []
        assert evaluator.temp_accepts_registered == [("sheets.write_range", "sheet-1")]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_temp_session"
        assert entries[0]["pii_detected"] is False


class TestPIIGate:
    """gate.py runs pii_detector.detect_pii_categories() over ``details``
    before the review (read) popup only -- see TestPopupGateWrites for the
    write gate, which never scans. A match forces a second, explicit
    confirmation dialog on top of the popup's own Allow once/Always allow --
    declining it is treated as a full deny, same as clicking Deny on the
    original popup.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_read_popup_receives_detected_categories(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]

    async def test_read_popup_receives_empty_list_when_no_pii(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert captured["pii_categories"] == []

    async def test_no_pii_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_pii_confirmed_returns_data_and_audits_pii_detected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_pii_declined_denies_the_whole_request(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_non_pii_deny_audits_pii_detected_false(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is False

    async def test_pii_confirmation_happens_before_accept_all_rule_confirmation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        call_order = []
        monkeypatch.setattr(
            gate, "show_pii_confirmation_popup",
            lambda categories: call_order.append("pii") or True,
        )
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description: call_order.append("rule") or True,
        )
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: None)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        assert call_order == ["pii", "rule"]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["pii_detected"] is True

    async def test_declining_pii_confirmation_on_accept_all_skips_rule_creation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)
        rule_confirm_calls = []
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description: rule_confirm_calls.append(1) or True,
        )
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert rule_confirm_calls == []  # never reached: PII confirmation already denied
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_pii_detection_overrides_a_matching_auto_accept_rule(self, monkeypatch, audit_dir):
        # Auto-accept rules are scoped to metadata (sender domain, folder,
        # "I am the organizer"), not content -- a rule that would otherwise
        # silently pass this through must still stop for human review when
        # the content itself contains likely PII.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        assert popup_calls == [1]  # the popup was NOT skipped, despite auto_ok
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # not "auto_accepted"
        assert entries[0]["auto_accept_rule"] == ""
        assert entries[0]["pii_detected"] is True

    async def test_pii_override_still_requires_its_own_confirmation_and_can_be_denied(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_matching_rule_without_pii_still_auto_accepts_silently(self, monkeypatch, audit_dir):
        # Confirms the override is specific to PII-flagged content -- an
        # otherwise-identical rule match with no PII in the content still
        # takes the silent fast path, exactly as before this feature existed.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_detected"] is False


class TestPiiCategoriesAndMatchDetailsInAuditLog:
    """pii_categories is always recorded (category labels only -- the same
    thing the popup banner already shows). pii_match_details is the opt-in
    PII-refinement trial capture (pii_detection.audit_match_details), off by
    default: '' unless turned on, and even then only the literal/redacted
    matched text for an approved request -- a fixed placeholder, never the
    matched text, for anything else."""

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_pii_categories_always_populated_regardless_of_trial_setting(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_categories"] == ["IBAN (bank account number)"]
        assert entries[0]["pii_match_details"] == ""  # trial setting is off by default

    async def test_no_pii_leaves_categories_and_details_empty(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_categories"] == []
        assert entries[0]["pii_match_details"] == ""

    async def test_approved_request_gets_redacted_match_text_when_trial_setting_on(self, monkeypatch, audit_dir):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        details = entries[0]["pii_match_details"]
        assert details.startswith("IBAN (bank account number): ")
        assert "DE89370400440532013000" not in details  # value-bearing category -- redacted, not literal
        assert "•" in details

    async def test_denied_request_gets_hidden_placeholder_not_matched_text_when_trial_setting_on(
        self, monkeypatch, audit_dir,
    ):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_match_details"] == "User confirmed: details hidden"
        assert "DE89370400440532013000" not in entries[0]["pii_match_details"]

    async def test_label_category_logs_literal_text_when_approved_and_trial_setting_on(self, monkeypatch, audit_dir):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text="Please confirm your salary before Friday.",
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_match_details"] == "Salary/compensation information: salary"

    async def test_auto_accepted_with_no_pii_leaves_match_details_empty_even_with_trial_setting_on(
        self, monkeypatch, audit_dir,
    ):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))  # must never be called

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_match_details"] == ""


class TestPiiAlreadyReviewed:
    """pii_already_reviewed lets a caller that can prove nothing has
    touched this exact content since PrivacyFence's own last write to it
    (connectors/drive.py's own_write_revisions) skip the PII gate's *forced
    confirmation* -- not PII detection itself, and not the ordinary review
    popup when no auto-accept rule matches. See gate.py's module docstring
    ("A second, narrower exception...") for the full reasoning.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000."

    async def test_matching_rule_with_pii_already_reviewed_auto_accepts_silently(
        self, monkeypatch, audit_dir,
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text=self.PII_TEXT, pii_already_reviewed=True,
        ))

        assert result is FILTERED
        assert popup_calls == []  # no popup at all -- same fast path as the no-PII case
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        # Audit trail stays honest about what pii_detector actually found,
        # even though the confirmation step it would normally force here
        # was suppressed.
        assert entries[0]["pii_detected"] is True

    async def test_pii_already_reviewed_without_a_matching_rule_shows_the_ordinary_popup_unflagged(
        self, monkeypatch, audit_dir,
    ):
        # pii_already_reviewed only ever suppresses the *extra* PII
        # confirmation step, never the ordinary review popup itself: with no
        # auto-accept rule matching, the popup still appears -- just without
        # the PII banner or the second "Are you sure?" confirmation.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        confirm_calls = []
        monkeypatch.setattr(
            gate, "show_pii_confirmation_popup",
            lambda categories: confirm_calls.append(categories) or True,
        )

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text=self.PII_TEXT, pii_already_reviewed=True,
        ))

        assert result is FILTERED
        assert captured["pii_categories"] == []  # no PII banner shown
        assert confirm_calls == []  # no second confirmation forced
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_pii_already_reviewed_defaults_to_false_and_changes_nothing(
        self, monkeypatch, audit_dir,
    ):
        # Confirms a caller that doesn't pass this parameter at all gets
        # exactly today's behavior -- the override is strictly opt-in.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        assert popup_calls == [1]  # still shown -- pii_already_reviewed defaults False
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is True

    async def test_pii_already_reviewed_has_no_effect_on_the_write_gate(self, monkeypatch, audit_dir):
        # gate="popup" never computes pii_categories at all (see gate.py's
        # module docstring) -- pii_already_reviewed has nothing to suppress
        # there, and must not accidentally weaken upload_pii_scan_text's own,
        # separate forced confirmation for drive_upload_file.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(
            gate, "show_pii_confirmation_popup",
            lambda categories: confirm_calls.append(categories) or True,
        )

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file",
            upload_pii_scan_text=self.PII_TEXT, pii_already_reviewed=True,
        ))

        assert result is FILTERED
        assert confirm_calls == [["IBAN (bank account number)"]]  # still forced


class TestPiiScanText:
    """``pii_scan_text`` lets a caller scan different text than what's shown
    in the popup (``details_text``) -- e.g. an email body without its From/To
    headers, which could otherwise flag PII found only in metadata the
    message itself doesn't actually contain.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000."

    async def test_pii_scan_text_overrides_details_text_for_detection(self, monkeypatch, audit_dir):
        # details_text (shown in the popup) has PII in the "headers", but the
        # caller-supplied pii_scan_text (the actual body) does not.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="review",
                details_text=f"From: {self.PII_TEXT}\n\nnothing sensitive in the body",
                pii_scan_text="nothing sensitive in the body",
            ))

        assert captured["pii_categories"] == []

    async def test_pii_scan_text_can_detect_pii_absent_from_details_text(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="review",
                details_text="nothing sensitive here",
                pii_scan_text=self.PII_TEXT,
            ))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]

    async def test_pii_scan_text_empty_string_skips_detection_even_if_details_has_pii(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="review",
                details_text=self.PII_TEXT,
                pii_scan_text="",
            ))

        assert captured["pii_categories"] == []

    async def test_pii_scan_text_omitted_falls_back_to_details_text(self, monkeypatch, audit_dir):
        # No pii_scan_text passed at all -- same behavior as before this
        # parameter existed.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]


class TestPopupSerialization:
    async def test_only_one_popup_shown_at_a_time(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])

        concurrent = 0
        max_concurrent = 0

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            import time
            time.sleep(0.05)
            concurrent -= 1
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        await asyncio.gather(*[
            gate.gated_call(**base_kwargs(gate="review", tool=f"gmail_get_message_{i}"))
            for i in range(5)
        ])

        assert max_concurrent == 1


class TestQueuedRequestReCheck:
    """Regression for the race fixed alongside the stale-menu bug: gated_call
    re-checks should_auto_accept() *after* acquiring _popup_lock, not just
    before. Without that re-check, a request that was merely queued behind
    another popup would show its own dialog for something the user had
    already approved via Always allow (or via a rule added out-of-band, e.g.
    from the menu bar) a moment earlier.

    A plain FakeEvaluator with a fixed answer can't exercise this: the whole
    point is that should_auto_accept()'s answer changes *while a second call
    is already queued*. These tests use a stateful evaluator whose answer
    flips only once the racing call has done its work.
    """

    async def test_second_read_request_auto_accepts_after_first_creates_rule_via_accept_all(
        self, monkeypatch, audit_dir
    ):
        rules_created: list[str] = []

        class LiveEvaluator:
            def should_auto_accept(self, operation_key, ctx):
                if rules_created:
                    return True, rules_created[0]
                return False, ""

        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: LiveEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: rules_created.append(name))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

        popup_calls = []

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            popup_calls.append(title)
            return "accept_all", 0

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        # Both calls target the same operation. The first (created first,
        # so it acquires _popup_lock first under asyncio's scheduling) shows
        # a real popup and creates a standing rule via Always allow. The
        # second is queued behind the lock the whole time.
        results = await asyncio.gather(
            gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message")),
            gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message")),
        )

        assert results == [FILTERED, FILTERED]
        # The dialog must have been shown exactly once -- the second caller
        # was auto-accepted by the re-check, not popped up again.
        assert len(popup_calls) == 1

        entries = read_audit_entries(audit_dir)
        decisions = sorted(e["decision"] for e in entries)
        assert decisions == ["accepted_via_accept_all", "auto_accepted"]

    async def test_second_write_request_auto_accepts_if_rule_added_while_first_holds_lock(
        self, monkeypatch, audit_dir
    ):
        # Unlike the review gate, the popup (write) gate has no Always allow of
        # its own -- but a rule can still appear mid-flight if the user adds
        # one from the menu bar's "Auto-accept Rules" submenu while a write
        # popup is on screen. The second, queued write request must not pop
        # its own dialog once that happens.
        #
        # should_auto_accept() is consulted twice per call: once *before* the
        # lock (a fast path for the common case) and once *inside* it (the
        # re-check this test targets). To make sure this test actually
        # exercises the in-lock re-check -- and doesn't just pass "by
        # accident" because the pre-lock check happened to win a timing race
        # -- the rule is only flipped on after both callers' pre-lock checks
        # have already run (3rd should_auto_accept call: A's pre-lock check,
        # A's in-lock re-check, B's pre-lock check). At that point B must
        # already be blocked waiting for the lock, since a write gated_call
        # has no other await point in between.
        rule_now_active = threading.Event()
        check_calls: list[None] = []

        class LiveEvaluator:
            def should_auto_accept(self, operation_key, ctx):
                check_calls.append(None)
                if rule_now_active.is_set():
                    return True, "manually_added_rule"
                return False, ""

        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: LiveEvaluator())

        popup_calls = []

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            popup_calls.append(title)
            wait_until(lambda: len(check_calls) >= 3, timeout=1.0)
            # Simulate a rule appearing (e.g. added from the menu bar) while
            # this dialog is up, independent of anything gated_call did.
            rule_now_active.set()
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        results = await asyncio.gather(
            gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft")),
            gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft")),
            return_exceptions=True,
        )

        assert len(popup_calls) == 1  # only the first request showed a dialog
        assert isinstance(results[0], RuntimeError)  # denied, as its popup said
        assert results[1] is FILTERED  # auto-accepted via the re-check, no popup of its own

        entries = read_audit_entries(audit_dir)
        decisions = sorted(e["decision"] for e in entries)
        assert decisions == ["auto_accepted", "rejected"]


class TestApprovedObjectTypesNeverPopsUp:
    """Regression/repro for a QA discrepancy that couldn't be resolved from
    the audit log alone: the operator reported seeing a live approval popup
    for a Salesforce Account read (salesforce_get_record), while the audit
    log said "auto_accepted" for that same call -- a genuine contradiction,
    since gated_call's own logic makes the two mutually exclusive: the popup
    functions are never invoked once should_auto_accept() has already
    returned True with no PII detected. This drives the real (non-Fake)
    AutoAcceptEvaluator configured the way the Salesforce connector's
    approved_object_types rule is meant to be used, args shaped exactly like
    connectors/salesforce.py::_get_record builds them, to lock in that
    invariant -- if this ever starts failing, that's the actual bug; if it
    keeps passing, a future recurrence of the live discrepancy is a config
    or observation issue (e.g. the popup belonged to a different call), not
    a gate.py bug.
    """

    async def test_approved_object_type_read_never_shows_a_popup(self, monkeypatch, audit_dir):
        evaluator = AutoAcceptEvaluator({
            "salesforce.read_record": [{"rule": "approved_object_types", "value": ["Account"]}],
        })
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)

        def fail_if_called(*a, **k):
            raise AssertionError("show_read_popup must not be called when the object type is auto-accepted")

        monkeypatch.setattr(gate, "show_read_popup", fail_if_called)

        result = await gate.gated_call(**base_kwargs(
            connector="salesforce", tool="salesforce_get_record", gate="review",
            args={"object_type": "Account", "record_id": "001xx0000012345"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "approved_object_types"

    async def test_object_type_outside_allowlist_still_shows_the_popup(self, monkeypatch, audit_dir):
        # Contrast case: Opportunity isn't in the allowlist, so it must take
        # the normal interactive path -- proving the guard above is actually
        # meaningful (it can be reached) and not vacuously always-skipped.
        evaluator = AutoAcceptEvaluator({
            "salesforce.read_record": [{"rule": "approved_object_types", "value": ["Account"]}],
        })
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        result = await gate.gated_call(**base_kwargs(
            connector="salesforce", tool="salesforce_get_record", gate="review",
            args={"object_type": "Opportunity", "record_id": "006xx"},
        ))

        assert result is FILTERED
        assert popup_calls == [1]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"


class TestRequestId:
    async def test_decision_entries_carry_a_non_empty_request_id(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))

        await gate.gated_call(**base_kwargs())

        entries = read_audit_entries(audit_dir)
        assert entries[0]["request_id"]

    async def test_each_call_gets_a_distinct_request_id(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))

        await gate.gated_call(**base_kwargs())
        await gate.gated_call(**base_kwargs())

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[0]["request_id"] != entries[1]["request_id"]


class TestAuditGapSafety:
    """Regression for a real audit-log gap found during QA: a call that
    visibly ran to completion (real data returned, the user saw and
    completed the approval flow) left zero matching entries in the log.
    gated_call now guarantees a decision entry on every exit path, including
    one triggered by an exception from code nobody expected to fail (e.g. a
    native popup call itself raising) -- see the `finally` block in
    gated_call.
    """

    async def test_unexpected_exception_in_review_gate_still_leaves_an_audit_entry(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])

        def boom(*a, **k):
            raise RuntimeError("native popup crashed")

        monkeypatch.setattr(gate, "show_read_popup", boom)

        with pytest.raises(RuntimeError, match="native popup crashed"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "error"
        assert entries[0]["request_id"]

    async def test_unexpected_exception_in_popup_gate_still_leaves_an_audit_entry(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())

        def boom(*a, **k):
            raise RuntimeError("native popup crashed")

        monkeypatch.setattr(gate, "show_popup", boom)

        with pytest.raises(RuntimeError, match="native popup crashed"):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "error"

    async def test_exception_while_persisting_an_accept_all_rule_still_audits(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [("i_am_sender", None)])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

        def boom(*a, **k):
            raise OSError("rules file write failed")

        monkeypatch.setattr(gate, "add_auto_accept_rule", boom)

        with pytest.raises(OSError, match="rules file write failed"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "error"

    async def test_normal_decision_paths_are_not_double_audited(self, monkeypatch, audit_dir):
        # The finally-block safety net must not add a second entry on top of
        # a normal decision.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review"))

        assert len(read_audit_entries(audit_dir)) == 1


class TestUnattendedMode:
    """gate.is_unattended()/unattended_scope() back the fail-fast path for
    scheduled/unattended Cowork tasks: ipc_server.py wraps a request in
    unattended_scope(True) when its connection called privacyfence_begin_
    unattended_session(). See docs/TECHNICAL_REFERENCE.md's "Scheduled /
    unattended Cowork tasks" section.

    The one invariant that matters more than any individual branch: this
    must never change what auto-accepts -- only what happens when nothing
    does (denies fast instead of opening a popup nobody will answer).
    """

    @pytest.fixture(autouse=True)
    def _reset_unattended_flag(self):
        # unattended_scope always resets on its own __exit__, but guard
        # against a test raising before reaching that point and leaking the
        # flag into a later, unrelated test.
        token = gate._unattended_ctx.set(False)
        yield
        gate._unattended_ctx.reset(token)

    def test_is_unattended_defaults_false(self):
        assert gate.is_unattended() is False

    def test_unattended_scope_sets_and_resets(self):
        assert gate.is_unattended() is False
        with gate.unattended_scope(True):
            assert gate.is_unattended() is True
        assert gate.is_unattended() is False

    def test_unattended_scope_restores_prior_value_not_just_false(self):
        with gate.unattended_scope(True):
            with gate.unattended_scope(False):
                assert gate.is_unattended() is False
            assert gate.is_unattended() is True

    async def test_review_gate_denies_without_popup_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "accept", None))

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="review"))

        assert called == []  # popup never shown
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"

    async def test_popup_gate_denies_without_popup_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        called = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (called.append(a) or "accept", None))

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="popup"))

        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"

    async def test_matching_rule_still_auto_accepts_silently_even_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "deny", None))

        with gate.unattended_scope(True):
            result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"

    async def test_matching_temp_accept_still_auto_accepts_on_writes_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "session_temp_accept")))
        called = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (called.append(a) or "deny", None))

        with gate.unattended_scope(True):
            result = await gate.gated_call(**base_kwargs(gate="popup"))

        assert result is FILTERED
        assert called == []

    async def test_rule_matched_but_pii_detected_still_denies_unattended(self, monkeypatch, audit_dir):
        # A matching rule alone isn't enough once the PII gate fires -- see
        # gate.py's module docstring on how PII overrides a matching rule.
        # Unattended mode must deny this exactly like the no-match case.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "trusted_sender_domain")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "accept", None))

        pii_text = "Please wire the deposit to DE89370400440532013000, thanks."
        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="review", details_text=pii_text))

        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"
        assert entries[0]["pii_detected"] is True

    async def test_not_unattended_still_shows_popup_as_before(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert len(called) == 1


class TestClaudeReason:
    """The mandatory "reason" ToolSpec param, carried the same way
    is_unattended() is: a contextvar set by ipc_server.py, read
    internally by gated_call() via
    current_reason() -- no caller passes it as an explicit kwarg."""

    async def test_reason_scope_value_reaches_the_audit_entry(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        with gate.reason_scope("Summarizing the Q3 budget for the user."):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == "Summarizing the Q3 budget for the user."

    async def test_no_reason_scope_defaults_to_empty_string(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == ""

    async def test_reason_forwarded_to_show_read_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["claude_reason"] = claude_reason
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with gate.reason_scope("Checking for calendar conflicts."):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["claude_reason"] == "Checking for calendar conflicts."

    async def test_reason_forwarded_to_show_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["claude_reason"] = claude_reason
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with gate.reason_scope("Sending the confirmation the user asked for."):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["claude_reason"] == "Sending the confirmation the user asked for."

    async def test_auto_accepted_call_still_records_reason(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))

        with gate.reason_scope("Reading my own sent mail."):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["claude_reason"] == "Reading my own sent mail."

    async def test_scope_does_not_leak_to_calls_outside_it(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule_choices", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        with gate.reason_scope("Only for this one call."):
            pass  # scope already exited before gated_call runs
        await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == ""


class TestDefaultDetails:
    def test_object_with_dict_is_json_dumped(self):
        class Obj:
            def __init__(self):
                self.sender = "alice@example.com"
                self.subject = "hi"

        out = gate._default_details(Obj())
        assert json.loads(out) == {"sender": "alice@example.com", "subject": "hi"}

    def test_plain_dict_is_json_dumped(self):
        out = gate._default_details({"a": 1, "b": [1, 2]})
        assert json.loads(out) == {"a": 1, "b": [1, 2]}

    def test_unserializable_falls_back_to_str(self):
        # json.dumps(..., default=str) succeeds for almost anything, so to
        # exercise the except-path we need attribute access itself to raise.
        class Weird:
            def __getattribute__(self, item):
                if item == "__dict__":
                    raise RuntimeError("boom")
                return object.__getattribute__(self, item)

            def __str__(self):
                return "weird-fallback"

        out = gate._default_details(Weird())
        assert out == "weird-fallback"


class TestPiiAndAuditWorkOffTheEventLoop:
    """R9 (docs/slack-performance-review.md): detect_pii_categories/
    scan_pii_for_audit and AuditLogger.recent_matches used to run inline on
    gated_call's own coroutine -- synchronous, CPU-bound-ish work that
    blocked every other concurrently-dispatched request on the IPC server's
    single event loop for however long it took. Proven here the standard
    way: a slow stand-in for each, run concurrently with a ticker coroutine
    that must keep making progress throughout -- if the slow call still ran
    inline, the ticker would freeze for its whole duration instead.
    """

    @staticmethod
    async def _ticks_while(coro) -> list[float]:
        ticks: list[float] = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(ticker())
        try:
            await coro
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task
        return ticks

    async def test_detect_pii_categories_does_not_block_concurrent_tasks(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "rule")))
        monkeypatch.setattr(gate, "detect_pii_categories", lambda text: time.sleep(0.15) or [])

        ticks = await self._ticks_while(gate.gated_call(**base_kwargs(gate="review")))

        # >5 ticks in 0.15s (a 0.01s ticker interval) means the event loop
        # kept running throughout -- inline, blocked for the whole sleep, it
        # would show at most one or two.
        assert len(ticks) > 5

    async def test_recent_matches_does_not_block_concurrent_tasks(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "rule")))
        monkeypatch.setattr(
            get_audit_logger(), "recent_matches", lambda *a, **k: time.sleep(0.15) or 0
        )

        ticks = await self._ticks_while(gate.gated_call(**base_kwargs(gate="review")))

        assert len(ticks) > 5


class TestRunInPopupExecutor:
    """gate._run_in_popup_executor -- the dedicated single-thread executor
    every native dialog call runs on (see docs/slack-performance-review.md's
    R6), instead of asyncio.to_thread's default pool shared with every
    connector's own blocking I/O.
    """

    async def test_runs_the_call_and_returns_its_result(self):
        def fn(x, *, y):
            return x, y

        result = await gate._run_in_popup_executor(fn, 5, y=9)

        assert result == (5, 9)

    async def test_runs_on_a_dedicated_thread_not_the_default_pool(self):
        seen = {}

        def fn():
            seen["thread"] = threading.current_thread().name

        await gate._run_in_popup_executor(fn)

        assert seen["thread"].startswith("pf-popup")

    async def test_stays_prompt_while_the_default_to_thread_pool_is_saturated(self):
        # The scenario this executor exists for: a handful of slow
        # connector calls (a Slack rate-limit retry sleeping out
        # Retry-After, worst case) occupy every worker in the default
        # asyncio.to_thread pool. A popup dispatched at the same time must
        # not queue behind them.
        default_pool_size = min(32, (__import__("os").cpu_count() or 1) + 4)
        release = threading.Event()

        def occupy_a_worker():
            release.wait(timeout=2.0)

        occupiers = [asyncio.to_thread(occupy_a_worker) for _ in range(default_pool_size)]
        await asyncio.sleep(0.05)  # let every occupier actually start running

        def popup():
            return "still responsive"

        try:
            result = await asyncio.wait_for(gate._run_in_popup_executor(popup), timeout=1.0)
        finally:
            release.set()
            await asyncio.gather(*occupiers)

        assert result == "still responsive"

    async def test_exceptions_propagate(self):
        def fn():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await gate._run_in_popup_executor(fn)
