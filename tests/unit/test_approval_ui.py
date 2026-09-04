"""Tests for approval_ui.py: the pluggable ApprovalUI seam gate.py depends on
instead of importing a concrete approval-surface implementation directly.

WebApprovalUI (web_approval_ui.py) is the sole implementation since P10
deleted NativeApprovalUI/approval_popup.py (docs/https-connector-refactor-
plan.md §12, D6) -- its own tests (test_web_approval_ui.py) cover its real
behavior; this file stays focused on the seam itself: the singleton
accessors and the deferred_registry contract a future implementation would
also have to satisfy.
"""
from __future__ import annotations

import pytest

from privacyfence.approval_ui import ApprovalUI, get_approval_ui, init_approval_ui

# approval_ui._INSTANCE is reset by tests/conftest.py's autouse _reset_singletons
# fixture, same as auto_accept/audit_log's own singletons.


class TestSingletonAccessors:
    def test_get_approval_ui_lazily_creates_an_unconfigured_instance(self):
        ui = get_approval_ui()
        assert isinstance(ui, ApprovalUI)
        assert ui.deferred_registry is None
        with pytest.raises(RuntimeError, match="No ApprovalUI configured"):
            ui.show_popup("t", {}, "d")

    def test_get_approval_ui_returns_the_same_instance_across_calls(self):
        assert get_approval_ui() is get_approval_ui()

    def test_init_approval_ui_replaces_the_singleton(self):
        class FakeApprovalUI(ApprovalUI):
            def show_popup(self, *a, **kw):
                raise NotImplementedError

            def show_read_popup(self, *a, **kw):
                raise NotImplementedError

            def show_pii_confirmation_popup(self, categories):
                raise NotImplementedError

            def show_rule_confirmation_popup(self, description):
                raise NotImplementedError

        fake = FakeApprovalUI()
        returned = init_approval_ui(fake)

        assert returned is fake
        assert get_approval_ui() is fake

    def test_abstract_class_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            ApprovalUI()  # type: ignore[abstract]


class TestDeferredRegistry:
    """A backend opts into gate.py's deferred/hold-window protocol purely by
    exposing a registry here -- the ABC's own default (None) is what a
    backend with nowhere to send a human a reviewable link would keep, if
    one existed; WebApprovalUI (the only implementation since P10) always
    overrides it."""

    def test_default_backend_has_no_deferred_registry(self):
        class FakeApprovalUI(ApprovalUI):
            def show_popup(self, *a, **kw):
                raise NotImplementedError

            def show_read_popup(self, *a, **kw):
                raise NotImplementedError

            def show_pii_confirmation_popup(self, categories):
                raise NotImplementedError

            def show_rule_confirmation_popup(self, description):
                raise NotImplementedError

        assert FakeApprovalUI().deferred_registry is None

    def test_a_backend_that_overrides_it_is_honored(self):
        sentinel = object()

        class FakeApprovalUI(ApprovalUI):
            def show_popup(self, *a, **kw):
                raise NotImplementedError

            def show_read_popup(self, *a, **kw):
                raise NotImplementedError

            def show_pii_confirmation_popup(self, categories):
                raise NotImplementedError

            def show_rule_confirmation_popup(self, description):
                raise NotImplementedError

            @property
            def deferred_registry(self):
                return sentinel

        assert FakeApprovalUI().deferred_registry is sentinel
