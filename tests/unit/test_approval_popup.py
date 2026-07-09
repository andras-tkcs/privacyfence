"""Tests for approval_popup.py's dialog plumbing: _run's osascript
invocation/temp-file handling, _display_dialog's script assembly, the
Cancel-vs-Confirm mapping in show_rule_confirmation_popup, and that
show_popup/show_read_popup forward to show_native_approval with the right
allow_accept_all contract. subprocess.run and show_native_approval are
mocked throughout -- these must never pop up a real interactive dialog in
a test run. _as_str/_build_message (the actual injection-relevant string
escaping) have their own real-osascript round-trip tests in
test_approval_popup_escaping.py.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from privacyfence import approval_popup


def fake_run_result(returncode: int = 0, stdout: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


class TestRun:
    def test_button_returned_prefix_is_stripped(self, monkeypatch):
        monkeypatch.setattr(
            approval_popup.subprocess, "run",
            lambda *a, **kw: fake_run_result(0, "button returned:Accept\n"),
        )
        assert approval_popup._run("some script") == "Accept"

    def test_plain_stdout_without_prefix_returned_as_is(self, monkeypatch):
        monkeypatch.setattr(
            approval_popup.subprocess, "run",
            lambda *a, **kw: fake_run_result(0, "chosen-value\n"),
        )
        assert approval_popup._run("some script") == "chosen-value"

    def test_empty_stdout_returns_none(self, monkeypatch):
        monkeypatch.setattr(approval_popup.subprocess, "run", lambda *a, **kw: fake_run_result(0, ""))
        assert approval_popup._run("some script") is None

    def test_nonzero_returncode_returns_none(self, monkeypatch):
        monkeypatch.setattr(approval_popup.subprocess, "run", lambda *a, **kw: fake_run_result(1, "button returned:Accept"))
        assert approval_popup._run("some script") is None

    def test_script_written_to_a_temp_applescript_file_and_invoked_via_osascript(self, monkeypatch):
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            with open(cmd[1], encoding="utf-8") as f:
                captured["file_contents"] = f.read()
            return fake_run_result(0, "")
        monkeypatch.setattr(approval_popup.subprocess, "run", fake_run)

        approval_popup._run("display dialog \"hi\"")

        assert captured["cmd"][0] == "osascript"
        assert captured["cmd"][1].endswith(".applescript")
        assert captured["file_contents"] == "display dialog \"hi\""

    def test_temp_file_is_cleaned_up_after_run(self, monkeypatch):
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["path"] = cmd[1]
            return fake_run_result(0, "")
        monkeypatch.setattr(approval_popup.subprocess, "run", fake_run)

        approval_popup._run("script")

        assert not os.path.exists(captured["path"])

    def test_temp_file_cleaned_up_even_when_osascript_fails(self, monkeypatch):
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["path"] = cmd[1]
            return fake_run_result(1, "")
        monkeypatch.setattr(approval_popup.subprocess, "run", fake_run)

        approval_popup._run("script")

        assert not os.path.exists(captured["path"])


class TestDisplayDialog:
    def test_assembles_title_buttons_and_default_button_into_the_script(self, monkeypatch):
        captured = {}
        def fake_run(script):
            captured["script"] = script
            return "Confirm"
        monkeypatch.setattr(approval_popup, "_run", fake_run)

        result = approval_popup._display_dialog("My Title", ["line one", "line two"], ["Cancel", "Confirm"], default="Cancel")

        script = captured["script"]
        assert 'with title "My Title"' in script
        assert 'buttons {"Cancel", "Confirm"}' in script
        assert 'default button "Cancel"' in script
        assert result == "Confirm"

    def test_lines_are_joined_via_build_message(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "_run", lambda script: captured.setdefault("script", script))

        approval_popup._display_dialog("T", ["a", "b"], ["OK"], default="OK")

        assert captured["script"].count("return") >= 1  # AppleScript line-join token


class TestShowRuleConfirmationPopup:
    def test_confirm_returns_true(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "_display_dialog", lambda *a, **kw: "Confirm")
        assert approval_popup.show_rule_confirmation_popup("i_am_sender") is True

    def test_cancel_returns_false(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "_display_dialog", lambda *a, **kw: "Cancel")
        assert approval_popup.show_rule_confirmation_popup("i_am_sender") is False

    def test_none_returns_false(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "_display_dialog", lambda *a, **kw: None)
        assert approval_popup.show_rule_confirmation_popup("i_am_sender") is False

    def test_default_button_is_cancel_not_confirm(self, monkeypatch):
        captured = {}
        def fake_display_dialog(title, lines, buttons, default):
            captured["default"] = default
            captured["buttons"] = buttons
            captured["lines"] = lines
            return "Cancel"
        monkeypatch.setattr(approval_popup, "_display_dialog", fake_display_dialog)

        approval_popup.show_rule_confirmation_popup("trusted_sender_domain: a.com")

        assert captured["default"] == "Cancel"
        assert captured["buttons"] == ["Cancel", "Confirm"]
        assert any("trusted_sender_domain: a.com" in line for line in captured["lines"])


class TestShowPopupAndShowReadPopup:
    def test_show_popup_forwards_with_allow_accept_all_false(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        result = approval_popup.show_popup("Title", {"Field": "Value"}, "details")

        assert captured == {
            "title": "Title", "preview": {"Field": "Value"}, "details_text": "details", "allow_accept_all": False,
            "pii_categories": None, "allow_temp_accept": False,
        }
        assert result == "accept"

    def test_show_popup_forwards_pii_categories(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept")

        approval_popup.show_popup("Title", {}, "details", pii_categories=["Email address"])

        assert captured["pii_categories"] == ["Email address"]

    def test_show_popup_forwards_allow_temp_accept_true(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            approval_popup, "show_native_approval", lambda **kw: captured.update(kw) or "accept_temp"
        )

        result = approval_popup.show_popup("Title", {}, "details", allow_temp_accept=True)

        assert captured["allow_temp_accept"] is True
        assert result == "accept_temp"

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


class TestShowPiiConfirmationPopup:
    def test_proceed_returns_true(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "_display_dialog", lambda *a, **kw: "Proceed")
        assert approval_popup.show_pii_confirmation_popup(["Email address"]) is True

    def test_cancel_returns_false(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "_display_dialog", lambda *a, **kw: "Cancel")
        assert approval_popup.show_pii_confirmation_popup(["Email address"]) is False

    def test_none_returns_false(self, monkeypatch):
        monkeypatch.setattr(approval_popup, "_display_dialog", lambda *a, **kw: None)
        assert approval_popup.show_pii_confirmation_popup(["Email address"]) is False

    def test_default_button_is_cancel_not_proceed(self, monkeypatch):
        captured = {}
        def fake_display_dialog(title, lines, buttons, default):
            captured["default"] = default
            captured["buttons"] = buttons
            captured["lines"] = lines
            return "Cancel"
        monkeypatch.setattr(approval_popup, "_display_dialog", fake_display_dialog)

        approval_popup.show_pii_confirmation_popup(["Email address", "Phone number"])

        assert captured["default"] == "Cancel"
        assert captured["buttons"] == ["Cancel", "Proceed"]
        assert any("Email address, Phone number" in line for line in captured["lines"])

    def test_empty_categories_still_shows_generic_dialog(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            approval_popup, "_display_dialog",
            lambda title, lines, buttons, default: captured.setdefault("lines", lines) and "Cancel",
        )

        approval_popup.show_pii_confirmation_popup([])

        assert any("personal data" in line for line in captured["lines"])
