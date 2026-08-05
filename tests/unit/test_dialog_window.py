"""Construction-level tests for dialog_window.py's DialogWindowController --
the small AppKit+WKWebView host the confirmation/list-picker dialogs
(show_pii_confirmation_popup, show_rule_confirmation_popup,
show_rule_choice_popup, settings_controller._osascript_pick's Atlassian
picker) render through since issue #145 ported them off `osascript display
dialog`/`choose from list`.

Same testing tier and conventions as test_approval_window.py: these call
DialogWindowController.build_panel() directly and either walk the resulting
real AppKit view tree or inspect controller.html_string (the exact string
handed to loadHTMLString_baseURL_), and simulate the "pf"
WKScriptMessageHandler bridge with a fake WKScriptMessage stand-in rather
than actually running the page's JS. Nothing here calls runDialog_() or
anything that reaches NSApplication.runModalForWindow_() -- build_panel() is
deliberately pure construction, so no human or modal session is needed and
this can run in CI on macos-latest with no new Accessibility permission.

show_confirmation_dialog/show_choice_dialog's own result-mapping (bridge
result -> bool / index-or-None) is covered separately in
TestShowConfirmationDialog/TestShowChoiceDialog below by mocking out
_run_dialog -- the one piece of this module that actually blocks on a real
modal session -- the same way test_approval_popup.py mocks
show_native_approval rather than driving a real window.
"""
from __future__ import annotations

import sys

import pytest
from WebKit import WKWebView

from privacyfence import dialog_window, dialog_window_html
from privacyfence.dialog_window import DialogWindowController

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires real AppKit/PyObjC (macOS only, matches project's macOS-only runtime)"
)


class _FakeMessage:
    """Stand-in for the real WKScriptMessage
    userContentController_didReceiveScriptMessage_ receives -- only
    ``.body()`` is ever read, same as test_approval_window.py's own
    _FakeMessage."""

    def __init__(self, body):
        self._body = body

    def body(self):
        return self._body


def make_confirmation_controller(
    *,
    title="PrivacyFence — Confirm Auto-Accept Rule",
    message_lines=None,
    cancel_label="Cancel",
    confirm_label="Confirm",
):
    c = DialogWindowController.alloc().init()
    c.html_string = dialog_window_html.build_confirmation_html(
        title=title,
        message_lines=message_lines if message_lines is not None else ["line one", "line two"],
        cancel_label=cancel_label,
        confirm_label=confirm_label,
    )
    c.width = dialog_window_html.CONFIRM_WIDTH
    c.height = 260.0
    return c


def make_choice_controller(
    *,
    title="PrivacyFence — Choose Auto-Accept Rule",
    prompt="choose one:",
    options=None,
    cancel_label="Cancel",
):
    c = DialogWindowController.alloc().init()
    c.html_string = dialog_window_html.build_choice_html(
        title=title,
        prompt=prompt,
        options=options if options is not None else ["i_am_owner", "approved_folder: f1"],
        cancel_label=cancel_label,
    )
    c.width = dialog_window_html.PICKER_WIDTH
    c.height = 260.0
    return c


def flatten(view):
    """Every view in the tree rooted at ``view``, ``view`` itself included."""
    yield view
    for child in view.subviews():
        yield from flatten(child)


def build_views(controller):
    panel = controller.build_panel()
    return list(flatten(panel.contentView())), panel


class TestWindowShape:
    def test_confirmation_panel_uses_the_confirm_width(self):
        controller = make_confirmation_controller()
        panel = controller.build_panel()
        assert panel.frame().size.width == dialog_window_html.CONFIRM_WIDTH

    def test_choice_panel_uses_the_picker_width(self):
        controller = make_choice_controller()
        panel = controller.build_panel()
        assert panel.frame().size.width == dialog_window_html.PICKER_WIDTH

    def test_exactly_one_webview_renders_the_whole_content_area(self):
        views, _ = build_views(make_confirmation_controller())
        webviews = [v for v in views if isinstance(v, WKWebView)]
        assert len(webviews) == 1

    def test_javascript_is_enabled(self):
        views, _ = build_views(make_confirmation_controller())
        webview = next(v for v in views if isinstance(v, WKWebView))
        assert webview.configuration().preferences().javaScriptEnabled() is True


class TestConfirmationContent:
    def test_title_and_message_render(self):
        controller = make_confirmation_controller(
            title="PrivacyFence — Possible PII Detected",
            message_lines=["PrivacyFence detected possible personal data in this content: Email address."],
        )
        controller.build_panel()
        assert "PrivacyFence — Possible PII Detected" in controller.html_string
        assert "Email address" in controller.html_string

    def test_cancel_and_confirm_buttons_present(self):
        controller = make_confirmation_controller(cancel_label="Cancel", confirm_label="Proceed")
        controller.build_panel()
        assert 'data-pf-action="cancel"' in controller.html_string
        assert 'data-pf-action="confirm"' in controller.html_string

    def test_buttons_start_disabled_in_markup(self):
        controller = make_confirmation_controller()
        controller.build_panel()
        assert controller.html_string.count('role="button" aria-disabled="true"') == 2

    def test_confirm_button_alone_is_marked_primary(self):
        # data-pf-primary is what dialog_window_html.py's _JS keydown
        # handler excludes from Enter/Space activating a focused control --
        # hitting Enter must never silently accept.
        controller = make_confirmation_controller()
        controller.build_panel()
        assert controller.html_string.count('data-pf-primary="1"') == 1


class TestChoiceContent:
    def test_title_and_prompt_render(self):
        controller = make_choice_controller(title="PrivacyFence", prompt="Choose the Atlassian site to connect:")
        controller.build_panel()
        assert "PrivacyFence" in controller.html_string
        assert "Choose the Atlassian site to connect:" in controller.html_string

    def test_every_option_renders_its_own_row(self):
        # Asserted as one combined index+text pattern per row, not a bare
        # "is this URL a substring of the document" check (CodeQL's
        # incomplete-url-substring-sanitization query flags the latter shape
        # as if it were an origin check being bypassed by a crafted
        # substring -- a false positive here, since this is a test
        # assertion, not a security check, but the combined pattern is also
        # a strictly more precise assertion: it pins each option to its own
        # row/index, not just "this text appears somewhere in the 20KB
        # document" -- which also includes vendored CSS/fonts).
        controller = make_choice_controller(options=["https://a.atlassian.net", "https://b.atlassian.net"])
        controller.build_panel()
        html = controller.html_string
        assert 'data-pf-index="0">https://a.atlassian.net<' in html
        assert 'data-pf-index="1">https://b.atlassian.net<' in html

    def test_cancel_button_present_no_confirm_button(self):
        controller = make_choice_controller()
        controller.build_panel()
        assert 'data-pf-action="cancel"' in controller.html_string
        assert 'data-pf-action="confirm"' not in controller.html_string


class TestPanelRevealOnNavigation:
    """Same fail-safe/reveal contract as approval_window.
    ApprovalWindowController -- see TestPanelRevealOnNavigation in
    test_approval_window.py for the full reasoning; this is the identical
    behavior on the smaller host."""

    def test_panel_starts_invisible(self):
        controller = make_confirmation_controller()
        panel = controller.build_panel()
        assert panel.alphaValue() == 0.0

    def test_panel_becomes_visible_after_navigation_finishes(self):
        controller = make_confirmation_controller()
        panel = controller.build_panel()

        controller.webView_didFinishNavigation_(controller._details_view, None)

        assert panel.alphaValue() == 1.0

    @pytest.mark.parametrize(
        "failure_method",
        ["webView_didFailNavigation_withError_", "webView_didFailProvisionalNavigation_withError_"],
    )
    def test_panel_becomes_visible_even_if_navigation_fails(self, failure_method):
        controller = make_confirmation_controller()
        panel = controller.build_panel()

        getattr(controller, failure_method)(controller._details_view, None, None)

        assert panel.alphaValue() == 1.0


class TestBridgeMessage:
    """userContentController_didReceiveScriptMessage_ -- the "pf" bridge
    message's own ``result`` field is what self.result resolves to, same
    mapping as approval_window.ApprovalWindowController's own bridge
    handler. Doesn't need build_panel() at all -- a minimal _FakeMessage is
    enough."""

    def test_confirm_result_is_stored_verbatim(self):
        controller = make_confirmation_controller()
        controller.userContentController_didReceiveScriptMessage_(
            None, _FakeMessage({"action": "resolve", "result": "confirm"}),
        )
        assert controller.result == "confirm"

    def test_choice_index_result_is_stored_verbatim(self):
        controller = make_choice_controller()
        controller.userContentController_didReceiveScriptMessage_(
            None, _FakeMessage({"action": "resolve", "result": 1}),
        )
        assert controller.result == 1

    def test_missing_result_defaults_to_cancel(self):
        controller = make_confirmation_controller()
        controller.userContentController_didReceiveScriptMessage_(
            None, _FakeMessage({"action": "resolve"}),
        )
        assert controller.result == "cancel"

    def test_unrecognized_action_is_ignored(self):
        controller = make_confirmation_controller()
        controller.result = "unset"
        controller.userContentController_didReceiveScriptMessage_(
            None, _FakeMessage({"action": "something_else", "result": "confirm"}),
        )
        assert controller.result == "unset"

    def test_malformed_message_body_is_ignored(self):
        controller = make_confirmation_controller()
        controller.result = "unset"
        controller.userContentController_didReceiveScriptMessage_(None, _FakeMessage("not a dict"))
        assert controller.result == "unset"


class TestShowConfirmationDialog:
    """The blocking entry point itself always drives a real modal session
    (_run_dialog), so these mock that one seam out -- same "mock the thing
    that would actually pop a window" discipline test_approval_popup.py
    uses for show_native_approval."""

    def test_confirm_result_returns_true(self, monkeypatch):
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: "confirm")
        result = dialog_window.show_confirmation_dialog(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert result is True

    def test_cancel_result_returns_false(self, monkeypatch):
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: "cancel")
        result = dialog_window.show_confirmation_dialog(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert result is False

    def test_unrecognized_result_returns_false(self, monkeypatch):
        # Defensive default -- not reachable via the real bridge/markup,
        # same reasoning as approval_window.py's own unrecognized-result
        # fallback.
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: "something_else")
        result = dialog_window.show_confirmation_dialog(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert result is False


class TestShowChoiceDialog:
    def test_valid_index_is_returned(self, monkeypatch):
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: 1)
        result = dialog_window.show_choice_dialog(title="T", prompt="p", options=["a", "b"])
        assert result == 1

    def test_cancel_result_returns_none(self, monkeypatch):
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: "cancel")
        result = dialog_window.show_choice_dialog(title="T", prompt="p", options=["a", "b"])
        assert result is None

    def test_out_of_range_index_returns_none(self, monkeypatch):
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: 5)
        result = dialog_window.show_choice_dialog(title="T", prompt="p", options=["a", "b"])
        assert result is None

    def test_non_numeric_result_returns_none(self, monkeypatch):
        monkeypatch.setattr(dialog_window, "_run_dialog", lambda *a, **kw: "garbage")
        result = dialog_window.show_choice_dialog(title="T", prompt="p", options=["a", "b"])
        assert result is None
