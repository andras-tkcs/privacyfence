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
import json
import threading
import time

import pytest

from privacyfence import gate
from privacyfence.audit_log import init_audit_logger
from privacyfence.auto_accept import AutoAcceptEvaluator


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
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: called.append(a) or "deny")

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "deny")

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_plain_accept_returns_filtered_and_audits_approved(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""

    async def test_show_read_popup_receives_allow_accept_all_true_when_suggestion_exists(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("i_am_sender", None))
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["allow_accept_all"] = allow_accept_all
            return "deny"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["allow_accept_all"] is True

    async def test_show_read_popup_receives_allow_accept_all_false_without_suggestion(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["allow_accept_all"] = allow_accept_all
            return "deny"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["allow_accept_all"] is False


class TestAcceptAll:
    async def test_accept_all_confirmed_creates_rule_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("trusted_sender_domain", ["example.com"]))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
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
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_accept_all_cancelled_confirmation_still_returns_data_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("i_am_sender", None))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)
        added = []
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda *a: added.append(a))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []  # no standing rule created
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # accepted once, not via a rule


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
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: read_popup_called.append(1) or "deny")
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        assert read_popup_called == []  # write gate never shows the read popup
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_deny_raises_and_audits_rejected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "deny")

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_matching_rule_auto_accepts_without_a_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "trusted_sender_domain")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: popup_calls.append(1) or "deny")

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

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["details"] = details
            return "accept"

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


class TestRequestFingerprint:
    """seen_count: AuditLogger.recent_matches(connector, tool, summary),
    computed once per gated_call and forwarded to both popup functions."""

    async def test_first_time_request_has_zero_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["seen_count"] = seen_count
            return "accept"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 0

    async def test_repeated_approval_increments_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        # Two prior approvals of the exact same (connector, tool, summary).
        await gate.gated_call(**base_kwargs(gate="review"))
        await gate.gated_call(**base_kwargs(gate="review"))

        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["seen_count"] = seen_count
            return "accept"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 2

    async def test_different_summary_does_not_count_toward_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        await gate.gated_call(**base_kwargs(gate="review", summary="from bob@example.com"))

        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["seen_count"] = seen_count
            return "accept"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review", summary="from alice@example.com"))

        assert captured["seen_count"] == 0

    async def test_seen_count_forwarded_to_show_popup_too(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

        await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        captured = {}

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["seen_count"] = seen_count
            return "accept"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["seen_count"] == 1

    async def test_rejected_prior_call_does_not_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "deny")

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["seen_count"] = seen_count
            return "accept"

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

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["write_content_flags"] = write_content_flags
            return "accept"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert captured["write_content_flags"] == ["IBAN (bank account number)"]

    async def test_no_flags_when_content_has_nothing_flaggable(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["write_content_flags"] = write_content_flags
            return "accept"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            return "accept"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text="full body here",
        ))

        assert result is FILTERED

    async def test_flags_never_affect_pii_detected_audit_field(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")

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

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["write_content_flags"] = write_content_flags
            return "accept"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert captured["write_content_flags"] == []


class TestTempAccept:
    """"Allow for 5 min" -- a lighter, in-memory-only alternative to a
    standing Always allow rule, offered on the write-gate popup only for
    operations expected to be called repeatedly against the same file in
    quick succession (auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS).
    """

    SHEETS_ARGS = {"spreadsheet_id": "sheet-1", "range_a1": "A1:B2"}

    async def test_show_popup_receives_allow_temp_accept_true_for_eligible_op_with_file_key(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["allow_temp_accept"] = allow_temp_accept
            return "deny"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
            ))

        assert captured["allow_temp_accept"] is True

    async def test_show_popup_receives_allow_temp_accept_false_for_ineligible_op(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["allow_temp_accept"] = allow_temp_accept
            return "deny"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["allow_temp_accept"] is False

    async def test_show_popup_receives_allow_temp_accept_false_when_file_key_missing(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["allow_temp_accept"] = allow_temp_accept
            return "deny"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="popup", connector="drive", tool="drive_sheets_write_range", args={"range_a1": "A1:B2"},
            ))

        assert captured["allow_temp_accept"] is False

    async def test_accept_temp_registers_rule_and_audits(self, monkeypatch, audit_dir):
        evaluator = FakeEvaluator()
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: evaluator)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept_temp")

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
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: popup_calls.append(1) or "accept_temp")

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
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: popup_calls.append(1) or "accept_temp")

        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))
        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range",
            args={"spreadsheet_id": "sheet-2", "range_a1": "A1:B2"},
        ))

        assert len(popup_calls) == 2

    async def test_accept_temp_without_a_file_key_falls_back_to_a_plain_accept(
        self, monkeypatch, audit_dir
    ):
        # Defensive path: the "Allow for 5 min" button is never offered for
        # an ineligible operation, so accept_temp should never actually come
        # back for one -- but if it somehow did, this must not be treated as
        # a denial of a click the user clearly meant as approval.
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept_temp")

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
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept_temp")
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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["pii_categories"] = pii_categories
            return "deny"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]

    async def test_read_popup_receives_empty_list_when_no_pii(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["pii_categories"] = pii_categories
            return "deny"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert captured["pii_categories"] == []

    async def test_no_pii_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_pii_confirmed_returns_data_and_audits_pii_detected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_pii_declined_denies_the_whole_request(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_non_pii_deny_audits_pii_detected_false(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "deny")

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is False

    async def test_pii_confirmation_happens_before_accept_all_rule_confirmation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("i_am_sender", None))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("i_am_sender", None))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: popup_calls.append(1) or "accept")
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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")
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
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: popup_calls.append(1) or "deny")

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_detected"] is False


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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["pii_categories"] = pii_categories
            return "deny"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["pii_categories"] = pii_categories
            return "deny"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["pii_categories"] = pii_categories
            return "deny"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["pii_categories"] = pii_categories
            return "deny"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]


class TestPopupSerialization:
    async def test_only_one_popup_shown_at_a_time(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)

        concurrent = 0
        max_concurrent = 0

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            import time
            time.sleep(0.05)
            concurrent -= 1
            return "accept"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("i_am_sender", None))
        monkeypatch.setattr(gate, "add_auto_accept_rule", lambda op, name, value: rules_created.append(name))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

        popup_calls = []

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            popup_calls.append(title)
            return "accept_all"

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

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            popup_calls.append(title)
            wait_until(lambda: len(check_calls) >= 3, timeout=1.0)
            # Simulate a rule appearing (e.g. added from the menu bar) while
            # this dialog is up, independent of anything gated_call did.
            rule_now_active.set()
            return "deny"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: popup_calls.append(1) or "accept")

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: ("i_am_sender", None))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept_all")
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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: called.append(a) or "accept")

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="review"))

        assert called == []  # popup never shown
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"

    async def test_popup_gate_denies_without_popup_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        called = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: called.append(a) or "accept")

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="popup"))

        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"

    async def test_matching_rule_still_auto_accepts_silently_even_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "i_am_sender")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: called.append(a) or "deny")

        with gate.unattended_scope(True):
            result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"

    async def test_matching_temp_accept_still_auto_accepts_on_writes_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "session_temp_accept")))
        called = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: called.append(a) or "deny")

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
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: called.append(a) or "accept")

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: called.append(a) or "accept")

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        with gate.reason_scope("Summarizing the Q3 budget for the user."):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == "Summarizing the Q3 budget for the user."

    async def test_no_reason_scope_defaults_to_empty_string(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

        await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == ""

    async def test_reason_forwarded_to_show_read_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector=""):
            captured["claude_reason"] = claude_reason
            return "accept"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with gate.reason_scope("Checking for calendar conflicts."):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["claude_reason"] == "Checking for calendar conflicts."

    async def test_reason_forwarded_to_show_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, allow_temp_accept=False, claude_reason="", write_content_flags=None, seen_count=0, connector=""):
            captured["claude_reason"] = claude_reason
            return "accept"

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
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: "accept")

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
