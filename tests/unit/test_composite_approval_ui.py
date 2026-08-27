"""Tests for composite_approval_ui.py (issue #55, Phase 1): first-response-
wins racing between a native and a mobile-relay ApprovalUI backend.

Fakes both backends directly rather than importing NativeApprovalUI/
MobileRelayApprovalUI -- these tests are about the race itself (who wins,
whether the loser's abandon_event gets set, what happens when one or both
backends raise), not about either concrete backend's own behavior.
"""
from __future__ import annotations

import threading
import time

import pytest

from privacyfence.composite_approval_ui import CompositeApprovalUI


class _FakeApprovalUI:
    """A stand-in for either ApprovalUI backend. Every method sleeps for
    `delay` seconds (simulating "waiting for a human"), then returns
    `result` or raises `error`. Mobile-shaped methods additionally accept
    (and record) `abandon_event`, mirroring MobileRelayApprovalUI's real
    signature -- see that module. Native-shaped fakes below simply don't
    declare that kwarg, matching NativeApprovalUI's plain passthrough."""

    def __init__(self, *, delay=0.0, result=("accept", None), error=None, accepts_abandon_event=False):
        self.delay = delay
        self.result = result
        self.error = error
        self.accepts_abandon_event = accepts_abandon_event
        self.calls: list[dict] = []
        self.abandon_events: list[threading.Event] = []

    def _respond(self, *args, abandon_event=None, **kwargs):
        if abandon_event is not None:
            self.abandon_events.append(abandon_event)
        self.calls.append({"args": args, "kwargs": kwargs})
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result

    show_popup = _respond
    show_read_popup = _respond
    show_pii_confirmation_popup = _respond
    show_rule_confirmation_popup = _respond


def make_native(**kwargs) -> _FakeApprovalUI:
    return _FakeApprovalUI(**kwargs)


def make_mobile(**kwargs) -> _FakeApprovalUI:
    return _FakeApprovalUI(**kwargs)


class TestFirstResponseWins:
    def test_faster_native_wins(self):
        native = make_native(delay=0.01, result=("accept", None))
        mobile = make_mobile(delay=0.3, result=("deny", None))
        composite = CompositeApprovalUI(native, mobile)

        result = composite.show_popup("Title", {}, "details")

        assert result == ("accept", None)

    def test_faster_mobile_wins(self):
        native = make_native(delay=0.3, result=("deny", None))
        mobile = make_mobile(delay=0.01, result=("accept", None))
        composite = CompositeApprovalUI(native, mobile)

        result = composite.show_popup("Title", {}, "details")

        assert result == ("accept", None)

    def test_does_not_wait_for_the_loser_to_finish(self):
        """Correctness of the race depends on returning as soon as one
        backend answers -- not after both have. This is the actual defect
        class a naive `ThreadPoolExecutor()` context manager introduces
        (its __exit__ blocks until every submitted task completes)."""
        native = make_native(delay=0.02, result=("accept", None))
        mobile = make_mobile(delay=5.0, result=("deny", None))  # would fail the test's own timeout if awaited
        composite = CompositeApprovalUI(native, mobile)

        started = time.monotonic()
        result = composite.show_popup("Title", {}, "details")
        elapsed = time.monotonic() - started

        assert result == ("accept", None)
        assert elapsed < 1.0

    def test_all_four_methods_race_correctly(self):
        native = make_native(delay=0.3, result="native-would-lose")
        mobile = make_mobile(delay=0.01, result="mobile-wins")
        composite = CompositeApprovalUI(native, mobile)

        assert composite.show_popup("t", {}, "d") == "mobile-wins"
        assert composite.show_read_popup("t", {}, "d", None) == "mobile-wins"
        assert composite.show_pii_confirmation_popup(["Email"]) == "mobile-wins"
        assert composite.show_rule_confirmation_popup("desc") == "mobile-wins"


class TestAbandonEventSetOnlyForMobile:
    def test_mobile_backend_receives_an_abandon_event(self):
        native = make_native(delay=0.01)
        mobile = make_mobile(delay=0.3)
        composite = CompositeApprovalUI(native, mobile)

        composite.show_popup("Title", {}, "details")

        assert len(mobile.abandon_events) == 1
        assert isinstance(mobile.abandon_events[0], threading.Event)

    def test_native_backend_never_receives_an_abandon_event(self):
        native = make_native(delay=0.01)
        mobile = make_mobile(delay=0.3)
        composite = CompositeApprovalUI(native, mobile)

        composite.show_popup("Title", {}, "details")

        assert native.calls[0]["kwargs"] == {}

    def test_winning_native_sets_the_mobile_abandon_event(self):
        native = make_native(delay=0.01, result=("accept", None))
        mobile = make_mobile(delay=0.3)
        composite = CompositeApprovalUI(native, mobile)

        composite.show_popup("Title", {}, "details")
        time.sleep(0.02)  # let the abandon_event assignment happen post-return

        assert mobile.abandon_events[0].is_set() is True


class TestBothBackendsFail:
    def test_native_exception_is_reraised_when_both_fail(self):
        native = make_native(delay=0.01, error=RuntimeError("native broke"))
        mobile = make_mobile(delay=0.02, error=RuntimeError("mobile broke"))
        composite = CompositeApprovalUI(native, mobile)

        with pytest.raises(RuntimeError, match="native broke"):
            composite.show_popup("Title", {}, "details")

    def test_one_backend_failing_lets_the_other_still_win(self):
        native = make_native(delay=0.01, error=RuntimeError("native broke"))
        mobile = make_mobile(delay=0.05, result=("accept", None))
        composite = CompositeApprovalUI(native, mobile)

        result = composite.show_popup("Title", {}, "details")

        assert result == ("accept", None)

    def test_mobile_failing_lets_native_still_win(self):
        native = make_native(delay=0.05, result=("deny", None))
        mobile = make_mobile(delay=0.01, error=RuntimeError("relay unreachable"))
        composite = CompositeApprovalUI(native, mobile)

        result = composite.show_popup("Title", {}, "details")

        assert result == ("deny", None)


class TestArgumentForwarding:
    def test_positional_and_keyword_args_reach_both_backends(self):
        native = make_native(delay=0.0)
        mobile = make_mobile(delay=0.3)
        composite = CompositeApprovalUI(native, mobile)

        composite.show_read_popup("Title", {"f": "v"}, "details", None, pii_categories=["Email"])

        assert native.calls[0]["args"] == ("Title", {"f": "v"}, "details", None)
        assert native.calls[0]["kwargs"] == {"pii_categories": ["Email"]}
