"""Tests for dialog_window_html.py -- the small confirmation/list-picker
HTML template dialog_window.py's DialogWindowController renders.

Pure-function module, no AppKit (same as approval_window_html.py, which this
imports from) -- these assert directly on the generated HTML strings, no
macOS/PyObjC required.

The real, injection-relevant escaping coverage this replaces used to live in
test_approval_popup_escaping.py, round-tripping content through a real
`osascript` process to prove AppleScript string-literal breakout was
impossible. That vector doesn't exist anymore -- a webview bridge call takes
a string as a real DOM text value, never source text to be interpreted (see
this module's own docstring) -- so what's worth pinning down here is just
that build_confirmation_html/build_choice_html actually HTML-escape
everything they interpolate, the same defensive posture
build_card_stack_html takes with details_text.
"""
from __future__ import annotations

from privacyfence.dialog_window_html import (
    CONFIRM_WIDTH,
    PICKER_WIDTH,
    build_choice_html,
    build_confirmation_html,
)


class TestBuildConfirmationHtml:
    def test_title_and_message_lines_render(self):
        html = build_confirmation_html(
            title="PrivacyFence — Confirm Auto-Accept Rule",
            message_lines=["PrivacyFence will create an auto-accept rule:", "i_am_sender"],
            cancel_label="Cancel",
            confirm_label="Confirm",
        )
        assert "PrivacyFence — Confirm Auto-Accept Rule" in html
        assert "PrivacyFence will create an auto-accept rule:" in html
        assert "i_am_sender" in html

    def test_empty_lines_are_dropped_not_rendered_as_empty_paragraphs(self):
        html = build_confirmation_html(
            title="T", message_lines=["a", "", "b"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert "<p></p>" not in html

    def test_cancel_and_confirm_buttons_present(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Proceed",
        )
        assert 'data-pf-action="cancel"' in html
        assert 'data-pf-action="confirm"' in html
        assert ">Cancel<" in html
        assert ">Proceed<" in html

    def test_only_the_confirm_button_is_marked_primary(self):
        # data-pf-primary is what _JS's keydown handler excludes from
        # Enter/Space activating a focused control -- hitting Enter must
        # never silently accept. Confirmed on exactly the confirm button.
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert html.count('data-pf-primary="1"') == 1
        assert 'data-pf-primary="1" aria-label="Confirm" data-pf-action="confirm"' in html

    def test_buttons_start_disabled_in_markup(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert html.count('role="button" aria-disabled="true"') == 2

    def test_confirm_and_cancel_labels_are_html_escaped(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="<b>Cancel</b>", confirm_label="<i>Go</i>",
        )
        assert "<b>Cancel</b>" not in html
        assert "<i>Go</i>" not in html
        assert "&lt;b&gt;Cancel&lt;/b&gt;" in html
        assert "&lt;i&gt;Go&lt;/i&gt;" in html

    def test_title_is_html_escaped(self):
        html = build_confirmation_html(
            title="<script>alert(1)</script>", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_message_lines_are_html_escaped(self):
        html = build_confirmation_html(
            title="T", message_lines=['x" & do shell script "touch pwned" & "'],
            cancel_label="Cancel", confirm_label="Confirm",
        )
        assert '<p>x&quot; &amp; do shell script &quot;touch pwned&quot; &amp; &quot;</p>' in html

    def test_uses_the_confirm_width(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert f"width: {CONFIRM_WIDTH}px" in html

    def test_bridge_script_is_present(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert "window.webkit.messageHandlers.pf.postMessage" in html


class TestBuildChoiceHtml:
    def test_title_and_prompt_render(self):
        html = build_choice_html(
            title="PrivacyFence — Choose Auto-Accept Rule",
            prompt="More than one rule could be created from this item — choose one:",
            options=["i_am_owner", "approved_folder: f1"],
        )
        assert "PrivacyFence — Choose Auto-Accept Rule" in html
        assert "More than one rule could be created from this item — choose one:" in html

    def test_every_option_renders_as_its_own_row_with_its_index(self):
        html = build_choice_html(
            title="T", prompt="p", options=["i_am_owner", "approved_folder: f1"],
        )
        assert 'data-pf-action="choice" data-pf-index="0">i_am_owner<' in html
        assert 'data-pf-action="choice" data-pf-index="1">approved_folder: f1<' in html

    def test_cancel_button_present_and_not_primary(self):
        html = build_choice_html(title="T", prompt="p", options=["a", "b"])
        assert 'data-pf-action="cancel"' in html
        assert "data-pf-primary" not in html

    def test_options_are_html_escaped(self):
        # The real bug this replaces: settings_controller._osascript_pick
        # used to interpolate an unescaped option (e.g. a live Atlassian
        # accessible-resources URL) directly into AppleScript source text.
        html = build_choice_html(
            title="T", prompt="p",
            options=['https://x.atlassian.net/" with administrator privileges'],
        )
        assert 'https://x.atlassian.net/" with administrator privileges' not in html
        assert "&quot;" in html

    def test_prompt_is_html_escaped(self):
        html = build_choice_html(title="T", prompt="<script>alert(1)</script>", options=["a"])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_empty_options_renders_no_rows(self):
        html = build_choice_html(title="T", prompt="p", options=[])
        assert 'data-pf-action="choice"' not in html
        assert 'data-pf-action="cancel"' in html

    def test_uses_the_picker_width(self):
        html = build_choice_html(title="T", prompt="p", options=["a"])
        assert f"width: {PICKER_WIDTH}px" in html

    def test_buttons_start_disabled_in_markup(self):
        html = build_choice_html(title="T", prompt="p", options=["a", "b"])
        # Two option rows + the Cancel button, all start disabled.
        assert html.count('role="button" aria-disabled="true"') == 3
