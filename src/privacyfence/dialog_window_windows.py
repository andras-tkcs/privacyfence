"""Windows confirmation/list-picker dialogs (pywebview / WebView2), issue #121.

Windows equivalent of dialog_window.py's DialogWindowController/
show_confirmation_dialog/show_choice_dialog -- same role, same pattern this
port already uses for approval_window_windows.py (see that module's own
docstring for the shared reasoning: reuse dialog_window_html.py's output
completely unmodified, polyfill the WKWebView-only JS bridge call via
webview_bridge_windows.py, don't import dialog_window.py itself since it
pulls in AppKit unconditionally at module scope).

Much smaller than approval_window_windows.py, same as dialog_window.py is
smaller than approval_window.py: no preview pane, fully bounded content, so
a single flat default size covers both document shapes (build_confirmation_
html's short fixed copy, build_choice_html's options list) without needing
any per-call estimate.
"""
from __future__ import annotations

import logging
import threading

from . import dialog_window_html
from .webview_bridge_windows import BridgeApi, inject_bridge_polyfill

try:
    import webview  # pywebview
except ImportError:  # pragma: no cover - exercised only where pywebview is present (Windows)
    webview = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_HEIGHT = 260.0
_MIN_HEIGHT = 180.0

_popup_lock = threading.Lock()  # shared app-wide "one native window at a time" invariant


def _run_dialog(html: str, width: float) -> str:
    if webview is None:
        logger.error("pywebview is not installed; cannot show the dialog. Defaulting to cancel.")
        return "cancel"

    html = inject_bridge_polyfill(html)
    result = {"value": "cancel"}
    done = threading.Event()

    def _on_message(payload: dict) -> None:
        if payload.get("action") != "resolve" or done.is_set():
            return
        result["value"] = payload.get("result", "cancel")
        done.set()

    with _popup_lock:
        window = webview.create_window(
            title="",
            html=html,
            width=int(width),
            height=int(_DEFAULT_HEIGHT),
            min_size=(int(width), int(_MIN_HEIGHT)),
            resizable=True,
            on_top=True,
            js_api=BridgeApi(_on_message),
        )

        def _on_closed() -> None:
            done.set()  # defaults to "cancel", the safe direction -- see result's initial value

        window.events.closed += _on_closed
        window.events.loaded += lambda: window.evaluate_js(
            "if (window.__pfEnableButtons) { window.__pfEnableButtons(); }"
        )

        done.wait()
        try:
            window.destroy()
        except Exception:  # pragma: no cover - best-effort teardown
            pass

    return str(result["value"])


def show_confirmation_dialog(
    *, title: str, message_lines: list[str], cancel_label: str, confirm_label: str,
) -> bool:
    """Windows equivalent of dialog_window.show_confirmation_dialog -- same
    signature, same "defaults to False (the safe direction) unless
    confirm_label is actually clicked" contract."""
    html = dialog_window_html.build_confirmation_html(
        title=title, message_lines=message_lines, cancel_label=cancel_label, confirm_label=confirm_label,
    )
    result = _run_dialog(html, dialog_window_html.CONFIRM_WIDTH)
    return result == "confirm"


def show_choice_dialog(
    *, title: str, prompt: str, options: list[str], cancel_label: str = "Cancel",
) -> int | None:
    """Windows equivalent of dialog_window.show_choice_dialog -- same
    signature, same "None on Cancel/close/anything unrecognized" contract."""
    html = dialog_window_html.build_choice_html(
        title=title, prompt=prompt, options=options, cancel_label=cancel_label,
    )
    result = _run_dialog(html, dialog_window_html.PICKER_WIDTH)
    if result == "cancel":
        return None
    try:
        idx = int(result)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(options):
        return idx
    return None
