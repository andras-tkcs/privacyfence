"""Native macOS confirmation/list-picker dialogs (AppKit / WKWebView).

Ports approval_popup.py's four AppleScript-based prompts --
``show_pii_confirmation_popup``, ``show_rule_confirmation_popup``,
``show_rule_choice_popup`` -- and ``settings_controller._osascript_pick``'s
Atlassian multi-resource picker onto issue #141's AppKit+WKWebView bridge
pattern (issue #145), reusing rather than re-deriving that issue's own
pattern: the same ``WKScriptMessageHandler`` ``"pf"`` bridge and
``userContentController_didReceiveScriptMessage_`` dispatch, the same
blocking-wait convention
(``performSelectorOnMainThread_withObject_waitUntilDone_(waitUntilDone=True)``,
called from the same ``asyncio.to_thread(...)`` background-thread call sites
gate.py/settings_controller.py already use), and the same security posture
(buttons start ``aria-disabled`` until the page's own DOMContentLoaded
handling enables them; Escape always resolves the safe direction; a
confirmation dialog's accepting button carries no Enter/Return keyboard
binding) -- see approval_window.py's own module docstring for the pattern
this ports, and dialog_window_html.py's module docstring for the two small
document shapes rendered here.

Much smaller than ApprovalWindowController's full card-stack window: no
preview pane, no PII/content-flag card, no per-call height estimate derived
from field counts -- these two shapes (build_confirmation_html/
build_choice_html) have fully bounded content (fixed, app-authored copy, or
a short options list), so a handful of round-number constants below are
enough, the same "assume worst case, never measure" reasoning as
approval_window.py's own constants -- see that module's own comments.

Shares approval_window._popup_lock rather than a second lock of its own:
"only one native window on screen at a time" is an app-wide invariant, not
one scoped to the card-stack window alone -- a rule-confirmation dialog from
one gated_call() and the main approval window from a concurrent one must
never show simultaneously, any more than two approval windows should.
"""
from __future__ import annotations

import logging

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSModalResponseStop,
    NSPanel,
    NSScreen,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

from . import approval_window
from . import dialog_window_html

logger = logging.getLogger(__name__)

_MESSAGE_HANDLER_NAME = "pf"

# Round-number constants, not measured text -- both document shapes have
# fully bounded content (a handful of short, app-authored lines; or an
# options list whose *count* is all that matters, same "count not content
# length" reasoning as approval_window.py's own _rows_height). A little
# unused whitespace when real content is shorter than assumed is the
# accepted trade-off, same as there.
_HEADER_HEIGHT = 64.0  # kicker + <h2> title
_LINE_HEIGHT = 24.0  # one <p> line of message text
_BUTTON_ROW_HEIGHT = 66.0  # same constant approval_window.py's own button row uses
_PROMPT_HEIGHT = 30.0  # build_choice_html's own <p>{prompt}</p> line
_OPTION_ROW_HEIGHT = 42.0  # one .pf-choice-row, padding included
_PADDING = 48.0  # body's own 24px top+bottom padding (dialog_window_html._document)
_MIN_HEIGHT = 200.0
_MAX_VISIBLE_OPTIONS = 6  # beyond this, .pf-choice-list scrolls internally instead of growing the window forever
_MAX_WINDOW_HEIGHT_FRACTION = 0.8  # of the screen's height, same fraction approval_window.py uses

_popup_lock = approval_window._popup_lock


def _confirmation_window_height(message_lines: list[str]) -> float:
    nonempty = [line for line in message_lines if line]
    height = _HEADER_HEIGHT + _PADDING + _BUTTON_ROW_HEIGHT + len(nonempty) * _LINE_HEIGHT
    return max(_MIN_HEIGHT, height)


def _choice_window_height(options: list[str]) -> float:
    visible = min(len(options), _MAX_VISIBLE_OPTIONS) or 1
    height = _HEADER_HEIGHT + _PADDING + _BUTTON_ROW_HEIGHT + _PROMPT_HEIGHT + visible * _OPTION_ROW_HEIGHT
    screen = NSScreen.mainScreen()
    if screen is not None:
        height = min(height, screen.frame().size.height * _MAX_WINDOW_HEIGHT_FRACTION)
    return max(_MIN_HEIGHT, height)


class DialogWindowController(NSObject):
    """Builds and drives one modal confirmation/list-picker window.
    One-shot, same life cycle as approval_window.ApprovalWindowController:
    create, set ``.html_string``/``.width``/``.height``, call
    ``runDialog_(None)`` on the main thread, read ``.result``. ``result`` is
    whatever the bridge message's own ``"result"`` field carried (see
    dialog_window_html.py's module docstring for the per-shape contract) --
    ``"cancel"`` by default, the safe direction, unless a real bridge
    message overwrites it."""

    def init(self):
        self = objc.super(DialogWindowController, self).init()
        if self is None:
            return None
        self.html_string = ""
        self.width = 440.0
        self.height = 200.0
        self.result = "cancel"
        self.panel = None
        self._details_view = None
        # Kept around (rather than a local in build_panel()) so runDialog_()
        # can explicitly removeScriptMessageHandlerForName_ after the modal
        # session ends -- same teardown discipline approval_window.py's own
        # ApprovalWindowController.init() comment explains.
        self._user_content_controller = None
        return self

    # ------------------------------------------------------------------ #
    # Window construction (safe to call off the main thread, and without
    # ever showing or activating anything -- see approval_window.py's
    # build_panel() docstring for why this split exists).
    # ------------------------------------------------------------------ #

    def build_panel(self):
        return self._build_panel()

    def _build_panel(self):
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.width, self.height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("")
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.center()
        # Invisible until webView_didFinishNavigation_ (or a fail-safe)
        # reveals it -- same loadHTMLString_baseURL_-is-asynchronous
        # reasoning as ApprovalWindowController._build_panel.
        panel.setAlphaValue_(0.0)
        self.panel = panel

        user_content_controller = WKUserContentController.alloc().init()
        user_content_controller.addScriptMessageHandler_name_(self, _MESSAGE_HANDLER_NAME)
        self._user_content_controller = user_content_controller

        config = WKWebViewConfiguration.alloc().init()
        config.setUserContentController_(user_content_controller)
        # JavaScript is on -- the button/option row's click/keyboard
        # dispatch (dialog_window_html.py's _JS) needs it, same "our own
        # inline script only, no navigation, no network" guarantee as
        # approval_window.py's own webview -- see that module's docstring.
        config.preferences().setJavaScriptEnabled_(True)
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, self.width, self.height), config
        )
        # Same explicit-not-inferred appearance timing fix as
        # ApprovalWindowController._build_content_view -- this view has no
        # superview/window yet when loadHTMLString_baseURL_ below evaluates
        # `prefers-color-scheme`.
        webview.setAppearance_(panel.effectiveAppearance())
        webview.setNavigationDelegate_(self)
        webview.loadHTMLString_baseURL_(self.html_string, None)
        self._details_view = webview
        panel.setContentView_(webview)
        panel.setInitialFirstResponder_(webview)
        return panel

    # ------------------------------------------------------------------ #
    # WKNavigationDelegate -- reveals the panel once there's actually
    # something in it to see, and forces the page's own button-enabling JS
    # as a fail-safe -- see approval_window.py's matching methods for the
    # full reasoning (identical here).
    # ------------------------------------------------------------------ #

    def webView_didFinishNavigation_(self, webView, navigation) -> None:
        self._reveal_and_ensure_buttons_enabled()

    def webView_didFailNavigation_withError_(self, webView, navigation, error) -> None:
        self._reveal_and_ensure_buttons_enabled()

    def webView_didFailProvisionalNavigation_withError_(self, webView, navigation, error) -> None:
        self._reveal_and_ensure_buttons_enabled()

    def _reveal_and_ensure_buttons_enabled(self) -> None:
        if self.panel is not None:
            self.panel.setAlphaValue_(1.0)
        if self._details_view is not None:
            self._details_view.evaluateJavaScript_completionHandler_(
                "if (window.__pfEnableButtons) { window.__pfEnableButtons(); }", None,
            )

    # ------------------------------------------------------------------ #
    # JS -> Python (button/option click or keyboard resolution)
    # ------------------------------------------------------------------ #

    def userContentController_didReceiveScriptMessage_(self, _user_content_controller, message) -> None:
        try:
            payload = dict(message.body())
        except (TypeError, ValueError):
            logger.warning("Malformed dialog bridge message: %r", message.body())
            return
        if payload.get("action") != "resolve":
            logger.warning("Unknown dialog bridge action: %r", payload)
            return
        # Falls back to the safe "cancel" default (set in init()) for a
        # missing/malformed result rather than raising -- same defensive
        # posture as approval_window.py's own unrecognized-result fallback.
        self.result = payload.get("result", "cancel")
        NSApplication.sharedApplication().stopModalWithCode_(NSModalResponseStop)

    # ------------------------------------------------------------------ #
    # Entry point (must run on the main thread)
    # ------------------------------------------------------------------ #

    def runDialog_(self, _sender) -> None:
        app = NSApplication.sharedApplication()
        if app.activationPolicy() == NSApplicationActivationPolicyProhibited:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        panel = self.build_panel()
        panel.makeKeyAndOrderFront_(None)
        panel.setLevel_(NSFloatingWindowLevel)
        app.activateIgnoringOtherApps_(True)
        app.runModalForWindow_(panel)
        # close(), not just orderOut_() -- same accumulating-hidden-windows
        # reasoning as approval_window.py's runApproval_ (see its own
        # comment; qa_popup_smoke.py's full-suite run is what surfaced it
        # there).
        panel.close()
        if self._user_content_controller is not None:
            self._user_content_controller.removeScriptMessageHandlerForName_(_MESSAGE_HANDLER_NAME)
            self._user_content_controller = None


def _run_dialog(html: str, width: float, height: float) -> str | int:
    with _popup_lock:
        controller = DialogWindowController.alloc().init()
        controller.html_string = html
        controller.width = width
        controller.height = height
        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runDialog:", None, True
        )
        return controller.result


def show_confirmation_dialog(
    *, title: str, message_lines: list[str], cancel_label: str, confirm_label: str,
) -> bool:
    """Blocking Cancel/<confirm_label> dialog. Returns True only if
    ``confirm_label`` was actually clicked -- Cancel, Escape, or any
    unrecognized bridge result all resolve to False, the same
    "default to the safe direction" contract show_pii_confirmation_popup/
    show_rule_confirmation_popup have always had (see their own
    docstrings). Thread-safe: safe to call from any thread, the window
    itself is always built and driven on the main thread (see
    _run_dialog/DialogWindowController.runDialog_)."""
    html = dialog_window_html.build_confirmation_html(
        title=title, message_lines=message_lines, cancel_label=cancel_label, confirm_label=confirm_label,
    )
    height = _confirmation_window_height(message_lines)
    result = _run_dialog(html, dialog_window_html.CONFIRM_WIDTH, height)
    return result == "confirm"


def show_choice_dialog(
    *, title: str, prompt: str, options: list[str], cancel_label: str = "Cancel",
) -> int | None:
    """Blocking list-picker dialog. Returns the chosen option's index into
    ``options``, or None on Cancel/Escape/an out-of-range or unrecognized
    bridge result -- matching ``_run``'s old "non-zero osascript exit
    returns None" contract for show_rule_choice_popup/
    settings_controller._osascript_pick's callers. Thread-safe, same as
    show_confirmation_dialog."""
    html = dialog_window_html.build_choice_html(
        title=title, prompt=prompt, options=options, cancel_label=cancel_label,
    )
    height = _choice_window_height(options)
    result = _run_dialog(html, dialog_window_html.PICKER_WIDTH, height)
    if result == "cancel":
        return None
    try:
        idx = int(result)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(options):
        return idx
    return None
