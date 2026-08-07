"""Windows settings window (pywebview / WebView2), issue #121.

Windows equivalent of settings_window.py's SettingsWindowController -- same
public shape (``configure(controller)``, ``show_window()``), same one
long-lived, lazily-created, non-modal window reused for the app's whole
lifetime (tray_windows.py constructs one exactly the way menu_bar.py
constructs settings_window.SettingsWindowController). Renders
settings_window_html.build_html()'s markup completely unmodified -- that
module has zero AppKit/WebKit Python imports by design (see its own
docstring), specifically so a second host like this one can load its output
verbatim.

Bridge protocol -- same shape settings_window.py already documents, just
carried over pywebview's bridge instead of WKWebView's (see
webview_bridge_windows.py's own docstring for the polyfill that makes the
page's own JS, unmodified, none the wiser which one it's talking to):
  - JS -> Python: ``action`` dispatches to the identically-named method on
    the configured SettingsController (``getattr(controller, action)
    (**payload)``) -- unchanged from settings_window.py's own _dispatch.
  - Python -> JS: every mutating SettingsController method's fresh
    ``snapshot()`` gets pushed into the page via
    ``window.evaluate_js("window.__pfRender(...)")``, same as
    settings_window.py's own _push_state, called both from here and from
    SettingsController.on_change for state changes that happen out from
    under an open window.

Deliberately does not import settings_window.py (or approval_window.py, for
the icon-loading helpers it reuses) -- both pull in AppKit/WebKit
unconditionally at module scope, which doesn't exist on Windows. The small
amount of pure-Python icon-loading logic they duplicate is duplicated here
too, same reasoning as approval_window_windows.py's own module docstring.

install_org_config()'s file picker: settings_controller.py's default is an
osascript "choose file" AppleScript prompt (macOS only). This module wires
SettingsController.pick_org_config_file_hook to a
window.create_file_dialog() call instead, in configure() below -- see that
attribute's own docstring in settings_controller.py.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from . import settings_window_html
from .platform_open import open_path_or_url
from .settings_controller import REPO_URL, SettingsController
from .webview_bridge_windows import BridgeApi, inject_bridge_polyfill

try:
    import webview  # pywebview
except ImportError:  # pragma: no cover - exercised only where pywebview is present (Windows)
    webview = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_WINDOW_WIDTH = 1200
_WINDOW_HEIGHT = 780


def _connector_icon_path(connector: str) -> str | None:
    if not connector:
        return None
    p = Path(__file__).parent / "resources" / "connector_icons" / f"{connector}.png"
    return str(p) if p.exists() else None


_icon_data_uri_cache: dict[str, str] = {}


def _icon_data_uri(path: str | None) -> str:
    if not path:
        return ""
    if path not in _icon_data_uri_cache:
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        _icon_data_uri_cache[path] = f"data:image/png;base64,{data}"
    return _icon_data_uri_cache[path]


def _augment_connectors_with_icons(state: dict[str, Any]) -> dict[str, Any]:
    for connector in state.get("connectors", []):
        icon_path = _connector_icon_path(connector.get("icon", ""))
        connector["icon_data_uri"] = _icon_data_uri(icon_path)
    return state


class SettingsWindowController:
    """One long-lived, non-modal window -- see module docstring. Plain
    class, not an NSObject subclass (nothing here needs Objective-C
    interop): pywebview windows are plain Python objects."""

    def __init__(self) -> None:
        self.controller: SettingsController | None = None
        self.window = None

    def configure(self, controller: SettingsController) -> None:
        self.controller = controller
        controller.on_change = self._push_state
        controller.pick_org_config_file_hook = self._pick_org_config_file

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #

    def build_window(self):
        assert self.controller is not None
        state = _augment_connectors_with_icons(self.controller.snapshot())
        body = settings_window_html.build_html(state)
        html = inject_bridge_polyfill(
            f"<html><head></head><body style=\"margin:0\">{body}</body></html>"
        )
        window = webview.create_window(
            title="PrivacyFence Settings",
            html=html,
            width=_WINDOW_WIDTH,
            height=_WINDOW_HEIGHT,
            resizable=True,
            js_api=BridgeApi(self._on_message),
        )
        window.events.closed += self._on_closed
        self.window = window
        return window

    def show_window(self) -> None:
        if webview is None:
            logger.error("pywebview is not installed; cannot show the settings window.")
            return
        if self.window is None:
            self.build_window()
        else:
            # pywebview has no direct "bring to front" for an existing
            # window on every backend -- restoring from a minimized state
            # is the one operation documented as broadly supported.
            try:
                self.window.restore()
            except Exception:  # pragma: no cover - best-effort, backend-dependent
                pass

    def _on_closed(self) -> None:
        self.window = None

    # ------------------------------------------------------------------ #
    # JS -> Python
    # ------------------------------------------------------------------ #

    def _on_message(self, payload: dict) -> None:
        action = payload.pop("action", None)
        if not action:
            logger.warning("Settings bridge message with no action: %r", payload)
            return
        self._dispatch(str(action), payload)

    def _dispatch(self, action: str, payload: dict[str, Any]) -> None:
        assert self.controller is not None
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

        kwargs = {k: (int(v) if k == "idx" and v is not None else v) for k, v in payload.items()}
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

    def _push_state(self, state: dict[str, Any]) -> None:
        if self.window is None:
            return
        state = _augment_connectors_with_icons(dict(state))
        self.window.evaluate_js(f"window.__pfRender({json.dumps(state)});")

    # ------------------------------------------------------------------ #
    # Org config file picker (settings_controller.py's injectable hook)
    # ------------------------------------------------------------------ #

    def _pick_org_config_file(self) -> str:
        if self.window is None:
            return ""
        try:
            selected = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("JSON files (*.json)", "All files (*.*)"),
            )
        except Exception:  # pragma: no cover - backend/platform dependent
            logger.exception("Org config file picker failed")
            return ""
        if not selected:
            return ""
        return selected[0]
