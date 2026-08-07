"""Native macOS window hosting the settings webview (AppKit / PyObjC / WebKit).

Replaces the ~2000-line NSMenu tree and rules_manager_window.py's native
window (issue #120) with a single titled, closable, miniaturizable NSWindow
whose entire content area is one WKWebView, rendering
settings_window_html.build_html()'s markup. Same construction style as
approval_window.ApprovalWindowController / the old
rules_manager_window.RulesManagerWindowController -- an NSObject subclass
built via ``alloc().init()`` plus an explicit configure step, not a custom
``init()`` override (this class has no attributes that need a value before
external code can safely touch it, unlike ApprovalWindowController's many
per-call fields).

Bridge protocol (see settings_window_html.py's module docstring for the JS
side):
  - JS -> Python: the page posts
    ``window.webkit.messageHandlers.pf.postMessage({action, ...payload})``,
    delivered here via ``WKScriptMessageHandler``'s
    ``userContentController_didReceiveScriptMessage_``. ``action`` is
    dispatched to the identically-named method on the ``SettingsController``
    this window was configured with (``getattr(controller, action)(**payload)``)
    -- keeping action names equal to method names is what makes this
    dispatch a one-line ``getattr`` instead of a growing if/elif ladder.
  - Python -> JS: every mutating ``SettingsController`` method returns a
    fresh ``snapshot()`` dict, which this module augments with per-connector
    icon data URIs (the one piece of state settings_controller.py itself
    can't compute -- see ``_augment_connectors_with_icons``'s docstring) and
    pushes into the page via
    ``webView.evaluateJavaScript_completionHandler_("window.__pfRender(...)")``.
    The same push happens from ``SettingsController.on_change`` for state
    changes that happen out from under an open window (a rule added via the
    approval popup's Always allow, a background auth flow finishing).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSBackingStoreBuffered,
    NSMakeRect,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

from . import approval_window
from . import settings_window_html
from .platform_open import open_path_or_url
from .settings_controller import REPO_URL, SettingsController

logger = logging.getLogger(__name__)

_WINDOW_WIDTH = 1200.0
_WINDOW_HEIGHT = 780.0
_MESSAGE_HANDLER_NAME = "pf"


def _augment_connectors_with_icons(state: dict[str, Any]) -> dict[str, Any]:
    """Fill in each connector row's ``icon_data_uri`` from the bundled
    per-service PNGs -- settings_controller.py stays free of AppKit/WebKit
    imports (see its own module docstring), but icon embedding needs
    approval_window._icon_data_uri()/_connector_icon_path(), which import
    both. Reused exactly as-is rather than duplicated (base64 data-URI
    embedding, so loadHTMLString_baseURL_(html, None) keeps working with no
    base URL)."""
    for connector in state.get("connectors", []):
        icon_path = approval_window._connector_icon_path(connector.get("icon", ""))
        connector["icon_data_uri"] = approval_window._icon_data_uri(icon_path)
    return state


class SettingsWindowController(NSObject):
    """One long-lived, non-modal window -- unlike approval_window.py's
    one-shot-per-request controllers, this is created once (lazily, on
    menu_bar.py's first "Settings…" click) and reused for the app's
    whole lifetime."""

    @objc.python_method
    def configure(self, controller: SettingsController) -> None:
        self.controller = controller
        self.window = None
        self._webview = None
        self._user_content_controller = None
        controller.on_change = self._push_state

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #

    def build_window(self):
        """Build the window and every subview it contains, with nothing
        shown, activated, or key yet -- pure construction, no side effect on
        window server state. Split out of show_window() specifically so
        tests can assert on the resulting window/webview/message-handler
        wiring without ever calling makeKeyAndOrderFront_/
        activateIgnoringOtherApps_ or needing a real interactive session --
        same reasoning as approval_window.ApprovalWindowController.
        build_panel(). show_window() is the only caller in production code.
        """
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        # _WINDOW_WIDTH/_WINDOW_HEIGHT are the on-screen window size the design
        # mockup specifies -- contentRectForFrameRect_styleMask_ converts that
        # target *frame* size down to the *content* rect initWithContentRect_
        # wants, so the title bar's chrome doesn't get added on top of it.
        content_rect = NSWindow.contentRectForFrameRect_styleMask_(
            NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT), style
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            content_rect, style, NSBackingStoreBuffered, False
        )
        window.setTitle_("PrivacyFence Settings")
        # Default isReleasedWhenClosed is True -- AppKit would issue its own
        # release on close, racing our own windowWillClose_ dropping
        # self.window below. Same fix as approval_window.py's panel /
        # rules_manager_window.py's window, for the same segfault reason.
        window.setReleasedWhenClosed_(False)
        window.center()
        window.setDelegate_(self)
        # Invisible until webView_didFinishNavigation_ below reveals it --
        # loadHTMLString_baseURL_ is asynchronous, so ordering this window
        # front right away (show_window(), immediately after build_window())
        # would show it empty for however long that load takes. Same fix,
        # same reasoning, as approval_window.ApprovalWindowController's own
        # panel.setAlphaValue_(0.0) -- see that class's module docstring.
        window.setAlphaValue_(0.0)
        self.window = window

        user_content_controller = WKUserContentController.alloc().init()
        user_content_controller.addScriptMessageHandler_name_(self, _MESSAGE_HANDLER_NAME)
        self._user_content_controller = user_content_controller

        config = WKWebViewConfiguration.alloc().init()
        config.setUserContentController_(user_content_controller)
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT), config
        )
        webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        webview.setNavigationDelegate_(self)
        self._webview = webview
        window.setContentView_(webview)

        state = _augment_connectors_with_icons(self.controller.snapshot())
        webview.loadHTMLString_baseURL_(settings_window_html.build_html(state), None)
        return window

    # ------------------------------------------------------------------ #
    # WKNavigationDelegate -- reveals the window once there's actually
    # something in it to see (see build_window()'s setAlphaValue_(0.0)
    # comment above).
    # ------------------------------------------------------------------ #

    def webView_didFinishNavigation_(self, webView, navigation) -> None:
        self._reveal_window()

    def webView_didFailNavigation_withError_(self, webView, navigation, error) -> None:
        # Fail-safe: a load failure must still reveal the window rather than
        # leave it permanently invisible -- see ApprovalWindowController's
        # own fail-safe navigation delegates for the same reasoning.
        self._reveal_window()

    def webView_didFailProvisionalNavigation_withError_(self, webView, navigation, error) -> None:
        self._reveal_window()

    @objc.python_method
    def _reveal_window(self) -> None:
        if self.window is not None:
            self.window.setAlphaValue_(1.0)

    def show_window(self) -> None:
        app = NSApplication.sharedApplication()
        # Same reasoning as approval_window.py's runApproval_/rules_manager_
        # window.py's _show_window: a raw, unbundled process defaults to
        # Prohibited, which silently blocks activateIgnoringOtherApps_ below.
        if app.activationPolicy() == NSApplicationActivationPolicyProhibited:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        window = self.window if self.window is not None else self.build_window()
        window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    def windowWillClose_(self, _notification) -> None:
        # released-when-closed defaults to True for a plain alloc/init
        # window (overridden above) -- drop every reference to view-hierarchy
        # objects rather than risk reusing a deallocated one; next
        # show_window() just builds a fresh window/webview. The message
        # handler holds a strong reference back to self via
        # addScriptMessageHandler_name_ -- remove it explicitly or this
        # controller (and its SettingsController) never gets released.
        if self._user_content_controller is not None:
            self._user_content_controller.removeScriptMessageHandlerForName_(_MESSAGE_HANDLER_NAME)
        self.window = None
        self._webview = None
        self._user_content_controller = None

    # ------------------------------------------------------------------ #
    # JS -> Python
    # ------------------------------------------------------------------ #

    def userContentController_didReceiveScriptMessage_(self, _user_content_controller, message) -> None:
        try:
            payload = dict(message.body())
        except (TypeError, ValueError):
            logger.warning("Malformed settings bridge message: %r", message.body())
            return
        action = payload.pop("action", None)
        if not action:
            logger.warning("Settings bridge message with no action: %r", payload)
            return
        self._dispatch(str(action), payload)

    @objc.python_method
    def _dispatch(self, action: str, payload: dict[str, Any]) -> None:
        if action == "quit_app":
            self.controller.quit_app()
            return
        if action == "open_repo":
            open_path_or_url(REPO_URL)
            return

        method = getattr(self.controller, action, None)
        if method is None or not callable(method):
            logger.warning("Unknown settings bridge action: %s", action)
            return

        # message.body() bridges JS strings as objc.pyobjc_unicode (a str
        # subclass), not plain str -- left as-is, one of those ends up
        # stored in cfg (e.g. a grant capability key) and yaml.dump has no
        # representer for the subclass, so it falls back to a
        # !!python/object/apply:builtins.str reconstruction tag that
        # yaml.safe_load can't read back on the next launch.
        kwargs = {
            k: (int(v) if k == "idx" and v is not None else (str(v) if isinstance(v, str) else v))
            for k, v in payload.items()
        }
        try:
            result = method(**kwargs)
        except TypeError as exc:
            logger.warning("Bad payload for settings bridge action %s: %s", action, exc)
            return
        if isinstance(result, dict):
            self._push_state(result)

    # ------------------------------------------------------------------ #
    # Python -> JS
    # ------------------------------------------------------------------ #

    @objc.python_method
    def _push_state(self, state: dict[str, Any]) -> None:
        if self._webview is None:
            return
        state = _augment_connectors_with_icons(dict(state))
        js = f"window.__pfRender({json.dumps(state)});"
        self._webview.evaluateJavaScript_completionHandler_(js, None)
