"""Windows approval window (pywebview / WebView2), issue #121.

Same role as approval_window.py's ApprovalWindowController/
show_native_approval on macOS: renders the single blocking approval dialog
every gated call resolves through, via
approval_window_html.build_card_stack_html() -- reused completely
unmodified (see that module's own docstring: it has zero AppKit/WebKit
Python imports by design, precisely so a second host like this one can load
its output verbatim). The one thing that differs page-side is the JS bridge
call the button row posts through
(``window.webkit.messageHandlers.pf.postMessage(...)``, a WKWebView-only
browser API) -- webview_bridge_windows.py polyfills that against pywebview's
own bridge instead, so approval_window_html.py's ``_JS`` runs completely
unmodified too. See that module's own docstring for how.

Deliberately does **not** import approval_window.py (or dialog_window.py):
that module imports ``objc``/``AppKit``/``WebKit`` unconditionally at module
scope, which doesn't exist at all on Windows -- importing it from here would
make this whole module fail to import on the one platform it's for. The
small amount of pure-Python logic approval_window.py also happens to need
(icon-path lookup, reading-time estimate) is duplicated here rather than
extracted into a shared module, to avoid touching that file's own
AppKit-import-adjacent structure for a change that can't be exercised
against real AppKit in this project's own (Linux) CI sandbox -- see
docs/coding-and-testing-guidelines.md's "stay dependency-light" pattern for
the same reasoning applied elsewhere (settings_controller.py's guarded
imports).

Window sizing: macOS's NSPanel is deliberately non-resizable, sized from a
pixel-exact worst-case estimate over preview/disclosure/risk-card row counts
(see approval_window.py's own block of constants and their comments for why
-- WebKit renders the actual content into a fixed frame with no built-in
"grow to fit" for a native window). Porting that whole estimation apparatus
here isn't necessary: this window is resizable, sized generously up front
(CONTENT_WIDTH[layout] wide, a flat default tall enough for the large
majority of real dialogs), with a sane minimum -- the HTML's own
``.pf-scroll`` flex region (approval_window_html.py's CSS) already handles
overflow via an internal scrollbar regardless of which host renders it, and
a resizable window lets a reviewer just drag it taller for the rare
dialog that needs more room, rather than this module needing to reproduce
macOS's own pixel-perfect (and empirically-tuned against real
qa_popup_smoke.py screenshots -- see approval_window.py's own comment)
estimate. Flagged in docs/windows-port-status.md as one of the pieces most
worth a real look on an actual Windows machine before release.

Thread-safety: gate.py calls in here from the IPC server thread (via
asyncio.to_thread), same as approval_window.py's own show_native_approval.
pywebview documents webview.create_window() as safe to call from any thread
once webview.start() is already running (tray_windows.py's job -- see that
module's own docstring) and dispatches the actual window creation onto its
own GUI thread internally; this module blocks the calling thread on a
threading.Event set from the bridge callback (or the window's own close
event) until a decision is made, the same synchronous contract
show_native_approval already guarantees gate.py.
"""
from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path

from . import approval_window_html
from .webview_bridge_windows import BridgeApi, inject_bridge_polyfill

try:
    import webview  # pywebview
except ImportError:  # pragma: no cover - exercised only where pywebview is present (Windows)
    webview = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_BRIDGE_RESULTS = ("accept", "deny", "accept_all")

_TEMP_ACCEPT_DISCLOSURE_TEXT = (
    "Approving this also allows further calls like this to the same file "
    "for a few minutes without asking again."
)

# Same round-number-estimate posture as approval_window.py's own constants
# (see that module's comments) -- just far fewer of them, since a resizable
# window doesn't need to get the number exactly right, only "usually enough".
_DEFAULT_WINDOW_HEIGHT = 720.0
_MIN_WINDOW_HEIGHT = 420.0

_popup_lock = threading.Lock()  # only one native window on screen at a time


def _estimate_reading_seconds(text: str) -> int:
    words = len(text.split())
    return max(1, round(words / 200 * 60))


def _reading_time_label(text: str) -> str:
    seconds = _estimate_reading_seconds(text)
    if seconds < 60:
        return f"~{seconds} sec read"
    return f"~{round(seconds / 60)} min read"


def _icon_path() -> str | None:
    here = Path(__file__).parent / "resources"
    for name in ("icon_64.png", "icon_512.png", "icon_32.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


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


def _disclosure_rows(
    is_read: bool, new_info: dict[str, str], visibility: dict[str, str]
) -> list[tuple[str, str]]:
    if not is_read:
        return []
    rows = list(new_info.items())
    if visibility:
        rows += approval_window_html.disclosure_rows_from_visibility(visibility)
    return rows


def show_native_approval(
    *,
    title: str,
    preview: dict[str, str],
    details_text: str,
    accept_all_choices: list[tuple[str, str]] | None = None,
    pii_categories: list[str] | None = None,
    temp_accept_eligible: bool = False,
    visibility: dict[str, str] | None = None,
    claude_reason: str = "",
    write_content_flags: list[str] | None = None,
    seen_count: int = 0,
    content_kind: str = "generic",
    pdf_bytes: bytes = b"",
    connector: str = "",
    preview_bytes: bytes = b"",
    preview_mime_type: str = "",
    layout: str = approval_window_html.NARROW,
    is_read: bool = True,
    upload_forced: bool = False,
    new_info: dict[str, str] | None = None,
    preview_tables: list[dict] | None = None,
    preview_blocks: list[dict] | None = None,
    table_only: bool = False,
) -> tuple[str, int | None]:
    """Windows equivalent of approval_window.show_native_approval -- same
    signature, same blocking-until-decided contract, same
    ``(decision, chosen_index)`` return shape. See that function's own
    docstring for the full parameter-by-parameter reference; every
    parameter here means exactly the same thing, since both hosts render
    the same approval_window_html.build_card_stack_html() output."""
    if webview is None:
        # No pywebview on this machine -- fail safe (deny), the same
        # direction every other defensive fallback in this bridge takes,
        # rather than silently letting a gated call through with nothing
        # ever shown to a human.
        logger.error("pywebview is not installed; cannot show the approval window. Denying.")
        return "deny", None

    accept_all_choices = accept_all_choices or []
    new_info = new_info or {}
    visibility = visibility or {}

    pdf_data_uri = ""
    if pdf_bytes:
        pdf_data_uri = f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode('ascii')}"
    image_data_uri = ""
    if not pdf_data_uri and preview_bytes and preview_mime_type.startswith("image/"):
        image_data_uri = f"data:{preview_mime_type};base64,{base64.b64encode(preview_bytes).decode('ascii')}"

    body_text = "" if table_only and preview_tables and not preview_blocks else details_text
    preview_body_html = approval_window_html.build_preview_body_html(
        body_text, image_data_uri=image_data_uri, pdf_data_uri=pdf_data_uri,
        tables=preview_tables, blocks=preview_blocks,
    )

    accept_all_labels = [
        f"Always allow — {hint}" if hint else "Always allow"
        for _rule_name, hint in accept_all_choices
    ]

    html = approval_window_html.build_card_stack_html(
        layout=layout,
        title=title,
        connector_icon_data_uri=_icon_data_uri(_connector_icon_path(connector)),
        shield_icon_data_uri=_icon_data_uri(_icon_path()),
        is_read=is_read,
        seen_count_text=(f"Seen {seen_count} time{'s' if seen_count != 1 else ''} this week" if seen_count > 0 else ""),
        preview=preview or {},
        claude_reason=claude_reason or "",
        disclosure_rows=_disclosure_rows(is_read, new_info, visibility),
        pii_categories=pii_categories or [],
        write_content_flags=write_content_flags or [],
        upload_forced=upload_forced,
        temp_accept_text=_TEMP_ACCEPT_DISCLOSURE_TEXT if temp_accept_eligible else "",
        preview_kicker=f"Preview ({_reading_time_label(details_text)})",
        preview_body_html=preview_body_html,
        accept_all_labels=accept_all_labels,
    )
    html = inject_bridge_polyfill(html)

    window_width = approval_window_html.CONTENT_WIDTH[layout]

    result: dict[str, object] = {"decision": "deny", "chosen_index": None}
    done = threading.Event()

    def _on_message(payload: dict) -> None:
        if payload.get("action") != "resolve" or done.is_set():
            return
        decision = payload.get("result")
        result["decision"] = decision if decision in _BRIDGE_RESULTS else "deny"
        choice = payload.get("choice")
        result["chosen_index"] = (
            int(choice) if isinstance(choice, (int, float)) and result["decision"] == "accept_all" else None
        )
        done.set()

    with _popup_lock:
        window = webview.create_window(
            title="",
            html=html,
            width=int(window_width),
            height=int(_DEFAULT_WINDOW_HEIGHT),
            min_size=(int(window_width), int(_MIN_WINDOW_HEIGHT)),
            resizable=True,
            on_top=True,
            js_api=BridgeApi(_on_message),
        )

        def _on_closed() -> None:
            # The reviewer closed the window via its own [x] rather than
            # clicking a button -- same "defaults to deny" posture Escape
            # gets on macOS (see approval_window.py's own module docstring).
            done.set()

        window.events.closed += _on_closed

        # Fail-safe button-enabling, mirroring approval_window.py's
        # webView_didFinishNavigation_ -- forces window.__pfEnableButtons in
        # case the page's own DOMContentLoaded handling somehow didn't
        # already run it first.
        window.events.loaded += lambda: window.evaluate_js(
            "if (window.__pfEnableButtons) { window.__pfEnableButtons(); }"
        )

        done.wait()
        try:
            window.destroy()
        except Exception:  # pragma: no cover - best-effort teardown
            pass

    return str(result["decision"]), result["chosen_index"]  # type: ignore[return-value]
