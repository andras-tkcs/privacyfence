"""Tests for web_approval_ui.py -- the ApprovalUI implementation backing the
web approval surface. Blocking contract must match NativeApprovalUI's (see
approval_ui.py's ABC): show_popup/show_read_popup/the two confirmation
methods block the calling thread until WebApprovalUI.resolve() is called for
the pending card, then return exactly what gate.py expects back.
"""
from __future__ import annotations

import threading
import time

from privacyfence.web_approval_ui import WebApprovalUI, get_web_approval_ui


def _resolve_soon(ui: WebApprovalUI, result: str, choice: int | None = None, delay: float = 0.05) -> None:
    """Waits for a card to appear, then resolves it -- mirrors a human
    clicking a button on the web page shortly after it loads."""
    deadline = time.monotonic() + 2
    while ui.current() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    card = ui.current()
    assert card is not None, "no pending card appeared in time"
    time.sleep(delay)
    ui.resolve(card.id, result, choice)


def _thread(target, *args) -> threading.Thread:
    # daemon=True everywhere below: if a test's own assertion fails before
    # the card is resolved (or before t.join() runs), an un-daemonized
    # thread blocked on show_popup()'s Event would otherwise hang the whole
    # pytest process at interpreter exit, not just fail the one test.
    return threading.Thread(target=target, args=args, daemon=True)


class TestShowPopup:
    def test_blocks_until_resolved_then_returns_the_decision(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "accept")
        t.start()
        result = ui.show_popup("Send email", {"To": "a@b.com"}, "body", connector="gmail")
        t.join(timeout=2)
        assert result == ("accept", None)

    def test_accept_all_returns_the_chosen_index(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "accept_all", 0)
        t.start()
        result = ui.show_popup(
            "Send email", {"To": "a@b.com"}, "body",
            accept_all_choices=[("always_allow", "")],
        )
        t.join(timeout=2)
        assert result == ("accept_all", 0)

    def test_unrecognized_result_degrades_to_deny(self):
        # Same defensive fallback approval_window.py's own bridge handler
        # takes for a malformed/unexpected result.
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "not-a-real-result")
        t.start()
        result = ui.show_popup("Send email", {}, "body")
        t.join(timeout=2)
        assert result == ("deny", None)

    def test_the_pending_card_is_a_full_html_document(self):
        ui = WebApprovalUI()

        def _grab_and_resolve():
            deadline = time.monotonic() + 2
            while ui.current() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            card = ui.current()
            assert card is not None
            assert card.kind == "card"
            assert card.html.startswith("<!DOCTYPE html>")
            assert "Send email" in card.html
            ui.resolve(card.id, "deny")

        t = _thread(_grab_and_resolve)
        t.start()
        ui.show_popup("Send email", {}, "body")
        t.join(timeout=2)


class TestShowReadPopup:
    def test_blocks_until_resolved_then_returns_the_decision(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "accept")
        t.start()
        result = ui.show_read_popup("Get message", {"From": "a@b.com"}, "body", None)
        t.join(timeout=2)
        assert result == ("accept", None)

    def test_deny_returns_deny(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "deny")
        t.start()
        result = ui.show_read_popup("Get message", {}, "body", None)
        t.join(timeout=2)
        assert result == ("deny", None)


class TestConfirmationPopups:
    def test_pii_confirmation_confirm_returns_true(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "confirm")
        t.start()
        assert ui.show_pii_confirmation_popup(["Email address"]) is True
        t.join(timeout=2)

    def test_pii_confirmation_cancel_returns_false(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "cancel")
        t.start()
        assert ui.show_pii_confirmation_popup(["Email address"]) is False
        t.join(timeout=2)

    def test_rule_confirmation_confirm_returns_true(self):
        ui = WebApprovalUI()
        t = _thread(_resolve_soon, ui, "confirm")
        t.start()
        assert ui.show_rule_confirmation_popup("Always allow this sender") is True
        t.join(timeout=2)

    def test_the_pending_confirmation_is_a_smaller_document_not_a_card(self):
        ui = WebApprovalUI()

        def _grab_and_resolve():
            deadline = time.monotonic() + 2
            while ui.current() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            card = ui.current()
            assert card is not None
            assert card.kind == "confirm"
            ui.resolve(card.id, "cancel")

        t = _thread(_grab_and_resolve)
        t.start()
        ui.show_rule_confirmation_popup("Always allow this sender")
        t.join(timeout=2)


class TestResolve:
    def test_resolve_with_no_pending_card_returns_false(self):
        ui = WebApprovalUI()
        assert ui.resolve("nonexistent", "accept") is False

    def test_resolve_with_wrong_id_returns_false(self):
        ui = WebApprovalUI()
        t = _thread(lambda: ui.show_popup("t", {}, "d"))
        t.start()
        deadline = time.monotonic() + 2
        while ui.current() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ui.resolve("wrong-id", "accept") is False
        # Clean up the still-blocked thread.
        card = ui.current()
        ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_second_resolve_for_the_same_card_is_rejected(self):
        # Idempotent decisions -- the first accepted decision for an id
        # wins, any later one is rejected (see
        # docs/https-connector-refactor-plan.md §7.1).
        ui = WebApprovalUI()
        t = _thread(lambda: ui.show_popup("t", {}, "d"))
        t.start()
        deadline = time.monotonic() + 2
        while ui.current() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        card = ui.current()
        assert ui.resolve(card.id, "accept") is True
        assert ui.resolve(card.id, "deny") is False
        t.join(timeout=2)

    def test_current_returns_none_after_resolution(self):
        ui = WebApprovalUI()
        t = _thread(lambda: ui.show_popup("t", {}, "d"))
        t.start()
        deadline = time.monotonic() + 2
        while ui.current() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        card = ui.current()
        ui.resolve(card.id, "accept")
        t.join(timeout=2)
        assert ui.current() is None


class TestSingleton:
    def test_get_web_approval_ui_returns_the_same_instance(self):
        assert get_web_approval_ui() is get_web_approval_ui()
