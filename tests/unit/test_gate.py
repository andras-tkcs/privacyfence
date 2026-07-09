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

    def should_auto_accept(self, operation_key, ctx):
        self.calls.append((operation_key, ctx))
        return self.result


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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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


class TestPIIGate:
    """gate.py runs pii_detector.detect_pii_categories() over ``details``
    before either popup. A match forces a second, explicit confirmation
    dialog on top of the popup's own Accept/Accept All -- declining it is
    treated as a full deny, same as clicking Deny on the original popup.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_read_popup_receives_detected_categories(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "suggest_rule", lambda *a, **k: None)
        captured = {}

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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


class TestPIIGateWrites:
    """Same PII confirmation contract, for the popup (write) gate."""

    PII_TEXT = "Please wire the deposit to DE89370400440532013000."

    async def test_write_popup_receives_detected_categories(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, pii_categories=None):
            captured["pii_categories"] = pii_categories
            return "deny"

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(
                **base_kwargs(gate="popup", tool="gmail_create_draft", details_text=self.PII_TEXT)
            )

        assert captured["pii_categories"] == ["IBAN (bank account number)"]

    async def test_pii_confirmed_accepts_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(
            **base_kwargs(gate="popup", tool="gmail_create_draft", details_text=self.PII_TEXT)
        )

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_pii_declined_denies_the_write(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(
                **base_kwargs(gate="popup", tool="gmail_create_draft", details_text=self.PII_TEXT)
            )

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_no_pii_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: "accept")
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(
            **base_kwargs(gate="popup", tool="gmail_create_draft", details_text="nothing sensitive here")
        )

        assert result is FILTERED
        assert confirm_calls == []

    async def test_pii_detection_overrides_a_matching_auto_accept_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "trusted_sender_domain")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: popup_calls.append(1) or "accept")
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(
            **base_kwargs(gate="popup", tool="gmail_create_draft", details_text=self.PII_TEXT)
        )

        assert result is FILTERED
        assert popup_calls == [1]  # the popup was NOT skipped, despite auto_ok
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # not "auto_accepted"
        assert entries[0]["auto_accept_rule"] == ""
        assert entries[0]["pii_detected"] is True

    async def test_matching_rule_without_pii_still_auto_accepts_silently(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "get_auto_accept_evaluator", lambda: FakeEvaluator((True, "trusted_sender_domain")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: popup_calls.append(1) or "deny")

        result = await gate.gated_call(
            **base_kwargs(gate="popup", tool="gmail_create_draft", details_text="nothing sensitive here")
        )

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"


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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
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
    already approved via Accept All (or via a rule added out-of-band, e.g.
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

        def fake_show_read_popup(title, preview, details, allow_accept_all, pii_categories=None):
            popup_calls.append(title)
            return "accept_all"

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        # Both calls target the same operation. The first (created first,
        # so it acquires _popup_lock first under asyncio's scheduling) shows
        # a real popup and creates a standing rule via Accept All. The
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
        # Unlike the review gate, the popup (write) gate has no Accept All of
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

        def fake_show_popup(title, preview, details, pii_categories=None):
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
