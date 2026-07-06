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

import pytest

from privacyfence import gate
from privacyfence.audit_log import init_audit_logger


class FakeEvaluator:
    def __init__(self, result=(False, "")):
        self.result = result
        self.calls = []

    def should_auto_accept(self, operation_key, ctx):
        self.calls.append((operation_key, ctx))
        return self.result


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

        def fake_show_read_popup(title, preview, details, allow_accept_all):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all):
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


class TestPopupSerialization:
    async def test_only_one_popup_shown_at_a_time(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)

        concurrent = 0
        max_concurrent = 0

        def fake_show_read_popup(title, preview, details, allow_accept_all):
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
