"""Tests for approval_popup.py's dialog plumbing: that show_pii_confirmation_
popup/show_rule_confirmation_popup/show_rule_choice_popup forward to
dialog_window.py's show_confirmation_dialog/show_choice_dialog with the
right title/copy/default-button contract, and that show_popup/show_read_popup
forward to show_native_approval with the right allow_accept_all contract.
dialog_window.show_confirmation_dialog/show_choice_dialog and
show_native_approval are mocked throughout -- these must never pop up a real
interactive dialog in a test run.

The AppleScript-injection-relevant string-escaping coverage this module used
to need (test_approval_popup_escaping.py, round-tripping content through a
real `osascript` process) no longer applies: these three functions build
their content through dialog_window_html.py's HTML template now, not
AppleScript source text -- see test_dialog_window_html.py for that module's
own HTML-escaping coverage.
"""
from __future__ import annotations

from privacyfence import approval_popup


class TestShowRuleConfirmationPopup:
    def test_confirm_returns_true(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", lambda **kw: True)
        assert approval_popup.show_rule_confirmation_popup("i_am_sender") is True

    def test_cancel_returns_false(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", lambda **kw: False)
        assert approval_popup.show_rule_confirmation_popup("i_am_sender") is False

    def test_default_button_is_cancel_not_confirm(self, monkeypatch):
        captured = {}
        def fake_show_confirmation_dialog(**kwargs):
            captured.update(kwargs)
            return False
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", fake_show_confirmation_dialog)

        approval_popup.show_rule_confirmation_popup("trusted_sender_domain: a.com")

        # "Cancel is the default" is enforced by dialog_window.py's own
        # markup (no Enter binding on the confirm button) -- what this
        # forwarding call controls is just the button's own label, which
        # must read as an affirmative action, not a plain default like "OK".
        assert captured["cancel_label"] == "Cancel"
        assert captured["confirm_label"] == "Confirm"
        assert captured["title"] == "PrivacyFence — Confirm Auto-Accept Rule"
        assert any("trusted_sender_domain: a.com" in line for line in captured["message_lines"])


class TestShowRuleChoicePopup:
    def test_returns_whatever_show_choice_dialog_returns(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "show_choice_dialog", lambda **kw: 1)
        result = approval_popup.show_rule_choice_popup(["i_am_owner", "approved_folder: f1"])
        assert result == 1

    def test_cancelled_choice_returns_none(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "show_choice_dialog", lambda **kw: None)
        assert approval_popup.show_rule_choice_popup(["i_am_owner", "approved_folder: f1"]) is None

    def test_descriptions_are_forwarded_as_the_options(self, monkeypatch):
        captured = {}
        def fake_show_choice_dialog(**kwargs):
            captured.update(kwargs)
            return None
        monkeypatch.setattr(approval_popup, "show_choice_dialog", fake_show_choice_dialog)

        approval_popup.show_rule_choice_popup(["i_am_owner", "approved_folder: f1"])

        assert captured["options"] == ["i_am_owner", "approved_folder: f1"]
        assert captured["title"] == "PrivacyFence — Choose Auto-Accept Rule"


class TestShowPopupAndShowReadPopup:
    def test_show_popup_forwards_with_allow_accept_all_false(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        result = approval_popup.show_popup("Title", {"Field": "Value"}, "details")

        assert captured == {
            "title": "Title", "preview": {"Field": "Value"}, "details_text": "details", "allow_accept_all": False,
            "temp_accept_eligible": False, "claude_reason": "", "write_content_flags": None, "seen_count": 0,
            "connector": "", "preview_bytes": b"", "preview_mime_type": "",
            "preview_tables": None, "preview_blocks": None, "table_only": False,
            "upload_forced": False, "layout": "narrow", "is_read": False, "accept_all_hint": "",
        }
        assert result == "accept"

    def test_show_popup_forwards_allow_accept_all_true(self, monkeypatch):
        # The write-gate counterpart to show_read_popup's own allow_accept_all
        # forwarding -- offered only for the handful of write ops with a
        # resource-scoped rule to suggest (see auto_accept.WRITE_RULE_SUGGESTIONS).
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept_all")

        result = approval_popup.show_popup("Title", {}, "details", allow_accept_all=True)

        assert captured["allow_accept_all"] is True
        assert result == "accept_all"

    def test_show_popup_forwards_temp_accept_eligible_true(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept"
        )

        result = approval_popup.show_popup("Title", {}, "details", temp_accept_eligible=True)

        assert captured["temp_accept_eligible"] is True
        assert result == "accept"

    def test_show_read_popup_forwards_allow_accept_all_true(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept_all")

        result = approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=True)

        assert captured["allow_accept_all"] is True
        assert result == "accept_all"

    def test_show_read_popup_forwards_allow_accept_all_false(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=False)

        assert captured["allow_accept_all"] is False

    def test_show_read_popup_forwards_pii_categories(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup(
            "Title", {}, "details", allow_accept_all=False, pii_categories=["Phone number"]
        )

        assert captured["pii_categories"] == ["Phone number"]

    def test_show_read_popup_defaults_pii_categories_to_none(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=False)

        assert captured["pii_categories"] is None

    def test_show_read_popup_forwards_visibility(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup(
            "Title", {}, "details", allow_accept_all=False, visibility={"Body": "allow", "Attachments": "block"}
        )

        assert captured["visibility"] == {"Body": "allow", "Attachments": "block"}

    def test_show_read_popup_defaults_visibility_to_none(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=False)

        assert captured["visibility"] is None

    def test_show_popup_never_carries_visibility(self, monkeypatch):
        # Write (popup-gate) approvals don't get the checklist -- see
        # show_popup's docstring. Locking this in as an explicit test so a
        # future refactor that accidentally threads visibility through here
        # too gets caught, not just documented.
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup("Title", {"Field": "Value"}, "details")

        assert "visibility" not in captured

    def test_show_popup_forwards_write_content_flags(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup(
            "Title", {"Field": "Value"}, "details", write_content_flags=["IBAN (bank account number)"]
        )

        assert captured["write_content_flags"] == ["IBAN (bank account number)"]

    def test_show_popup_defaults_write_content_flags_to_none(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup("Title", {"Field": "Value"}, "details")

        assert captured["write_content_flags"] is None

    def test_show_read_popup_never_carries_write_content_flags(self, monkeypatch):
        # The read side has its own, differently-behaved signal
        # (pii_categories, which does gate a second confirmation) -- see
        # gate.py's write_content_flags comment for why these are kept
        # separate rather than reusing one field for both directions.
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=False)

        assert "write_content_flags" not in captured

    def test_show_popup_forwards_seen_count(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup("Title", {"Field": "Value"}, "details", seen_count=3)

        assert captured["seen_count"] == 3

    def test_show_read_popup_forwards_seen_count(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=False, seen_count=5)

        assert captured["seen_count"] == 5

    def test_show_read_popup_forwards_preview_bytes_and_mime_type(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup(
            "Title", {}, "details", allow_accept_all=False,
            preview_bytes=b"\x89PNG", preview_mime_type="image/png",
        )

        assert captured["preview_bytes"] == b"\x89PNG"
        assert captured["preview_mime_type"] == "image/png"

    def test_show_read_popup_defaults_preview_bytes_to_empty(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "deny")

        approval_popup.show_read_popup("Title", {}, "details", allow_accept_all=False)

        assert captured["preview_bytes"] == b""
        assert captured["preview_mime_type"] == ""

    def test_show_popup_forwards_preview_bytes_and_mime_type(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup(
            "Title", {"Field": "Value"}, "details",
            preview_bytes=b"\x89PNG", preview_mime_type="image/png",
        )

        assert captured["preview_bytes"] == b"\x89PNG"
        assert captured["preview_mime_type"] == "image/png"

    def test_show_popup_defaults_preview_bytes_to_empty(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup("Title", {"Field": "Value"}, "details")

        assert captured["preview_bytes"] == b""
        assert captured["preview_mime_type"] == ""


class TestShowPiiConfirmationPopup:
    def test_proceed_returns_true(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", lambda **kw: True)
        assert approval_popup.show_pii_confirmation_popup(["Email address"]) is True

    def test_cancel_returns_false(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", lambda **kw: False)
        assert approval_popup.show_pii_confirmation_popup(["Email address"]) is False

    def test_default_button_is_cancel_not_proceed(self, monkeypatch):
        captured = {}
        def fake_show_confirmation_dialog(**kwargs):
            captured.update(kwargs)
            return False
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", fake_show_confirmation_dialog)

        approval_popup.show_pii_confirmation_popup(["Email address", "Phone number"])

        # Same "Cancel is enforced by dialog_window.py's own markup" split
        # as TestShowRuleConfirmationPopup's matching test above -- this
        # forwarding call only controls the labels/copy.
        assert captured["cancel_label"] == "Cancel"
        assert captured["confirm_label"] == "Proceed"
        assert captured["title"] == "PrivacyFence — Possible PII Detected"
        assert any("Email address, Phone number" in line for line in captured["message_lines"])

    def test_empty_categories_still_shows_generic_dialog(self, monkeypatch):
        captured = {}
        def fake_show_confirmation_dialog(**kwargs):
            captured.update(kwargs)
            return False
        monkeypatch.setattr(approval_popup, "show_confirmation_dialog", fake_show_confirmation_dialog)

        approval_popup.show_pii_confirmation_popup([])

        assert any("personal data" in line for line in captured["message_lines"])
