"""Tests for approval_ui.py: the pluggable ApprovalUI seam gate.py depends on
instead of importing approval_popup.py directly (see that module's own
docstring, and gate.py's thin show_popup/show_read_popup/etc. wrappers).

NativeApprovalUI is a pure delegation to approval_popup.py's free functions
-- these tests mock at that boundary, the same way test_approval_popup.py
mocks at show_native_approval's boundary, rather than ever popping up a real
interactive dialog.
"""
from __future__ import annotations

import pytest

from privacyfence import approval_ui
from privacyfence.approval_ui import ApprovalUI, NativeApprovalUI, get_approval_ui, init_approval_ui

# approval_ui._INSTANCE is reset by tests/conftest.py's autouse _reset_singletons
# fixture, same as auto_accept/audit_log's own singletons.


class TestNativeApprovalUIDelegation:
    def test_show_popup_forwards_args_and_returns_result(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            approval_ui.approval_popup, "show_popup",
            lambda *a, **kw: captured.update(args=a, kwargs=kw) or "accept",
        )

        result = NativeApprovalUI().show_popup("Title", {"f": "v"}, "details", seen_count=3)

        assert captured["args"] == ("Title", {"f": "v"}, "details")
        assert captured["kwargs"] == {"seen_count": 3}
        assert result == "accept"

    def test_show_read_popup_forwards_args_and_returns_result(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            approval_ui.approval_popup, "show_read_popup",
            lambda *a, **kw: captured.update(args=a, kwargs=kw) or "accept_all",
        )

        result = NativeApprovalUI().show_read_popup("Title", {}, "details", True, pii_categories=["Email"])

        assert captured["args"] == ("Title", {}, "details", True)
        assert captured["kwargs"] == {"pii_categories": ["Email"]}
        assert result == "accept_all"

    def test_show_pii_confirmation_popup_forwards_categories(self, monkeypatch):
        captured = {}

        def fake(categories):
            captured["categories"] = categories
            return True

        monkeypatch.setattr(approval_ui.approval_popup, "show_pii_confirmation_popup", fake)

        assert NativeApprovalUI().show_pii_confirmation_popup(["Phone number"]) is True
        assert captured["categories"] == ["Phone number"]

    def test_show_rule_choice_popup_forwards_descriptions(self, monkeypatch):
        captured = {}

        def fake(descriptions):
            captured["descriptions"] = descriptions
            return 1

        monkeypatch.setattr(approval_ui.approval_popup, "show_rule_choice_popup", fake)

        assert NativeApprovalUI().show_rule_choice_popup(["a", "b"]) == 1
        assert captured["descriptions"] == ["a", "b"]

    def test_show_rule_confirmation_popup_forwards_description(self, monkeypatch):
        captured = {}

        def fake(description):
            captured["description"] = description
            return False

        monkeypatch.setattr(approval_ui.approval_popup, "show_rule_confirmation_popup", fake)

        assert NativeApprovalUI().show_rule_confirmation_popup("some rule") is False
        assert captured["description"] == "some rule"


class TestSingletonAccessors:
    def test_get_approval_ui_lazily_creates_a_native_instance(self):
        ui = get_approval_ui()
        assert isinstance(ui, NativeApprovalUI)

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

            def show_rule_choice_popup(self, descriptions):
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
