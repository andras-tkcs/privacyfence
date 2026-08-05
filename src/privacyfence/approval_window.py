"""Native macOS approval window (AppKit / PyObjC).

Renders the single blocking approval dialog every gated call resolves
through: one WKWebView (approval_window_html.build_card_stack_html) filling
the *entire* content area — kicker/icon/title, then §1 (WHAT), §2 (Claude's
stated reason), an optional §3 disclosure card (review-gate calls only:
"what will be provided to Claude") or §4 PII/content-flag risk card, a
scrollable right-hand preview pane for WIDE-shaped tools, and its own
Deny/Allow once/Always allow button row at the bottom -- these three used to
be native NSButtons in a fixed band below the webview; they now render as
part of the same HTML document everything else does (see
approval_window_html.py's ``_button_row_html``/``_JS``), so this window no
longer has any native-vs-webview split in its content area at all.
AppleScript `display dialog` popups (still used elsewhere in
approval_popup.py for secondary confirmations) have no room for a real
layout, an icon, or a genuinely scrollable body — this module renders the
whole content area as one WKWebView instead. See
docs/approval-window-content-reference.md for exactly what each tool
renders, and approval_window_html.py's own module docstring for the
card-stack template itself.

Bridge protocol (JS -> Python only -- unlike settings_window.py's two-way
bridge, this window never needs to push fresh state back into the page after
the initial render): the page posts
``window.webkit.messageHandlers.pf.postMessage({action: 'resolve', result})``
once a button actually resolves the dialog (``result`` is ``'accept'``,
``'deny'``, or ``'accept_all'``), delivered here via
``WKScriptMessageHandler``'s ``userContentController_didReceiveScriptMessage_``
-- the same ``WKUserContentController``/``"pf"``-named-handler pattern
settings_window.py's own bridge already established, reused rather than
inventing a second bridge shape in this codebase (see that module's own
docstring). ``buttonClicked_``'s old ``sender.tag()`` dispatch is gone
entirely -- the bridge message's own ``result`` field is now what sets
``self.result`` before ending the modal session.

The §3 disclosure card renders real per-tool "what's new" values
(``new_info``) or, as a fallback, privacy_filter.category_policy()'s
resolved allow/redact/block sentences (``visibility``) — ground truth
PrivacyFence already computed before the popup was built, not a new claim
invented for display. Never present for a write (show_popup never sets
self.visibility/new_info; see its docstring for why).

When gate.py's PII detector (pii_detector.py) flags categories in the
content of a read (review-gate) popup, the §4 card renders in a distinct
red-tinted style naming what was found — the visual cue that a second,
explicit "Are you sure?" confirmation (approval_popup.show_pii_confirmation_
popup) is coming after Allow once, not a decision by itself. Write
(popup-gate) approvals never carry pii_categories, so this card never
renders in that style for them — see approval_window_html.py's
_risk_section_html for the distinct "write"/"write-forced" variants a write
call's own content-flag match gets instead.

Allow once has no Enter/Return keyboard binding (see approval_window_html.py's
``_JS`` keydown handler -- ``data-pf-primary`` is deliberately excluded from
the Enter/Space-activates-a-focused-control path every other button gets) and
the card-stack webview is the panel's initial first responder — hitting Enter
the moment the window appears cannot approve a request nobody has actually
read yet; the buttons also render disabled (``aria-disabled="true"``) until
the page's own DOMContentLoaded handling enables them, an in-page equivalent
of the old "disabled until webView_didFinishNavigation_ fires" native gate
(see approval_window_html.py's ``_button_row_html``/``_JS`` for both). Deny
still resolves on Escape, from anywhere in the document, not just when
focused: declining via a reflexive keypress is the safe direction, not a risk
the way an accidental approve would be.

The panel itself starts fully transparent (alpha 0) for the same reason:
loadHTMLString_baseURL_ is asynchronous even for this fully local document
(base64 fonts, inlined icons, full CSS bundle), so ordering the window
front immediately would show it empty -- just a bare titlebar with no
content underneath -- for however long that load takes, before snapping to
the real card stack. webView_didFinishNavigation_ (and its two
webView_didFail...  fail-safes, so a load failure can't leave the panel
invisible forever) is what fades it in; those same three methods also force
the page's own button-enabling JS via ``window.__pfEnableButtons`` as a
fail-safe in case DOMContentLoaded itself never fires (an outright load
failure) -- see that JS function's own comment for why calling it more than
once is harmless.

AppKit windows must be created and driven on the main thread, but gate.py
calls in here from the IPC server thread (via asyncio.to_thread). show_native_
approval() hands the actual window-building to the main thread with
performSelectorOnMainThread_withObject_waitUntilDone_(waitUntilDone=True),
which blocks the calling thread until the modal session ends, so gate.py's
calling convention stays synchronous.
"""
from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path

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
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

from . import approval_window_html

logger = logging.getLogger(__name__)

# Name of the WKScriptMessageHandler the card-stack HTML's button row posts
# to (window.webkit.messageHandlers.pf.postMessage(...)) -- same handler name
# settings_window.py's own bridge uses, not a coincidence: see this module's
# docstring for why this reuses that existing pattern instead of inventing a
# second bridge shape.
_MESSAGE_HANDLER_NAME = "pf"
_BRIDGE_RESULTS = ("accept", "deny", "accept_all")

# Shown above the button row for operations
# auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS lists -- Allow once itself
# also arms a 5-minute, same-file auto-accept window for these.
# Deliberately vague about the exact duration (gate.py/auto_accept.py
# still enforce a precise 5-minute TTL) -- the point is disclosure that
# Allow once covers more than this one call, not a number to remember.
_TEMP_ACCEPT_DISCLOSURE_TEXT = (
    "Approving this also allows further calls like this to the same file "
    "for a few minutes without asking again."
)

# ---------------------------------------------------------------------------- #
# layout="narrow"/"wide" -- the card-stack rendering
# (approval_window_html.build_card_stack_html). See approval_window_html.py's
# module docstring for the visual design source.
#
# This doesn't measure text at all: every row in every card has a CSS-fixed,
# truncated size regardless of actual value length (styles.css's
# .pf-kv/.pf-quote), so the window height is fully deterministic from
# field/section *counts* alone -- see _estimate_left_column_height() below.
# No "Show more" toggle exists here (see approval_window_html.py's module
# docstring): every row is fixed-and-truncated instead of an area-expansion
# progressive disclosure. WIDE's right pane is the one exception -- genuine
# free-text body content, not row-shaped, so it keeps its own fixed
# max-height + internal scroll (approval_window_html.py's own CSS)
# independent of this estimate.
# ---------------------------------------------------------------------------- #
# Derived from approval_window_html.CONTENT_WIDTH, not a second hardcoded
# copy of it -- the native window and the HTML body rendered inside it
# must always agree on width, and these two values must stay in sync: see
# CONTENT_WIDTH, the single source of truth this dict is derived from.
_WINDOW_WIDTH = {layout: float(width) for layout, width in approval_window_html.CONTENT_WIDTH.items()}

# Pixel constants behind _estimate_left_column_height() -- deliberately
# "assume every row is at its own label's line-clamp maximum"
# (approval_window_html.line_clamp_for -- the single source of truth for
# which labels get more than the 2-line default, so the template and this
# estimate can never disagree) rather than measured, so a short value never
# causes clipping; the cost is a little unused whitespace when a row's real
# content is shorter than its allowance, never the other direction.
# Re-derived empirically against real qa_popup_smoke.py screenshots, not
# computed from the CSS alone -- adjust here if a future style change to
# styles.css's card/row rules drifts from these.
_HEADER_HEIGHT = 90.0
_SEEN_COUNT_HEIGHT = 22.0
_CARD_CHROME = 62.0  # card padding (2x15) + margin-bottom (18) + kicker line (~14)
_ROW_BASE_HEIGHT = 12.0  # a .pf-kv row's share of the card's own gap, on top of its clamped lines
_ROW_LINE_HEIGHT = 18.0  # one line of a .pf-kv value at 14px/~1.3 line-height
_QUOTE_CARD_HEIGHT = 96.0  # §2's whole card: chrome + 3-line-clamped quote + the "unverified" meta line
_RISK_CARD_BASE_HEIGHT = 96.0  # §4 card: chrome + the "⚠ ..." line + one row of category tags
# The button row's (.pf-btn-row, approval_window_html.py's _button_row_html)
# own rendered height: no longer a fixed native-chrome band the webview sat
# above (see module docstring) -- this is now the same kind of round-number
# CSS estimate as every other constant in this block, just for the one card
# that's guaranteed present on every single dialog regardless of shape.
_BUTTON_ROW_HEIGHT = 66.0
_MIN_CONTENT_HEIGHT = 260.0
# Every constant above is a round-number guess, not a real text
# measurement (see their own comments) -- since <body> is height:100vh
# (build_card_stack_html's flex-based containment, not a per-region pixel
# cap), a window sized to *exactly* the raw estimate leaves zero room for
# WebKit's real render to land even a few px taller than guessed. That's
# harmless for genuinely long content (its own flex:1;overflow-y:auto
# region just grows an internal scrollbar, correctly), but for a short
# dialog sitting right at _MIN_CONTENT_HEIGHT's floor -- a couple of
# .pf-kv rows and nothing else -- a few px of real-vs-estimated drift is
# enough to trip a scrollbar on a dialog that's supposed to fit with
# nothing to scroll. This margin is pure headroom against that drift, not
# a per-section value -- it doesn't change how tall any card is *expected*
# to be, only how much slack the actual window gets over that expectation.
_HEIGHT_SAFETY_MARGIN = 24.0
_MAX_WINDOW_HEIGHT_FRACTION = 0.8  # of the screen's height -- see _window_height

_popup_lock = threading.Lock()  # only one native window on screen at a time


def _estimate_reading_seconds(text: str) -> int:
    """~200 words/minute silent-reading estimate, floored at 1 second so an
    empty/tiny body still renders a sane label rather than "~0 sec read"."""
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
    """Real per-service brand icon (Gmail/Drive/Slack/etc.), top-left,
    alongside the "PrivacyFence" kicker -- a secondary "which service is
    this" indicator, distinct from the shield's "this is PrivacyFence"
    mark at top-right. Same silent-skip fallback as _icon_path(): missing
    or unrecognized connector just renders no icon, never an error --
    see resources/connector_icons/README for where the bundled assets
    come from."""
    if not connector:
        return None
    p = Path(__file__).parent / "resources" / "connector_icons" / f"{connector}.png"
    return str(p) if p.exists() else None


_icon_data_uri_cache: dict[str, str] = {}


def _icon_data_uri(path: str | None) -> str:
    """Base64 data: URI for a vendored PNG icon, or "" if missing -- embeds
    _icon_path()/_connector_icon_path()'s file directly into the card-stack
    HTML as an <img> src. Data URIs (not a file:// reference) keep the
    document loadable via loadHTMLString_baseURL_(html, None) -- nothing
    here needs a base URL to resolve anything against. Cached (these are a
    fixed, small set of bundled resources, not user data) so repeated
    popups don't re-read/re-encode the same file."""
    if not path:
        return ""
    if path not in _icon_data_uri_cache:
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        _icon_data_uri_cache[path] = f"data:image/png;base64,{data}"
    return _icon_data_uri_cache[path]


class _FlippedView(NSView):
    """Top-down coordinates so layout math reads the way the design does."""

    def isFlipped(self):
        return True


class ApprovalWindowController(NSObject):
    """Builds and drives one modal approval window. One-shot: create, set
    fields, call runApproval_(None) on the main thread, read .result."""

    def init(self):
        self = objc.super(ApprovalWindowController, self).init()
        if self is None:
            return None
        self.title = ""
        self.preview: dict[str, str] = {}
        self.details_text = ""
        self.allow_accept_all = False
        self.temp_accept_eligible = False
        self.pii_categories: list[str] = []
        self.visibility: dict[str, str] = {}
        self.claude_reason: str = ""
        self.write_content_flags: list[str] = []
        self.seen_count: int = 0
        self.content_kind: str = "generic"
        self.pdf_bytes: bytes = b""
        self.connector: str = ""
        self.preview_bytes: bytes = b""
        self.preview_mime_type: str = ""
        self.layout: str = approval_window_html.NARROW
        self.is_read: bool = True
        self.upload_forced: bool = False
        self.new_info: dict[str, str] = {}
        self.preview_tables: list[dict] = []
        self.preview_blocks: list[dict] = []
        self.table_only: bool = False
        self.accept_all_hint: str = ""
        self.result = "deny"
        self.panel = None
        self._details_view = None
        self._details_html_string = ""
        # Set once build_panel()'s WKUserContentController is created --
        # kept around (rather than just a local in _build_content_view) so
        # runApproval_() can explicitly removeScriptMessageHandlerForName_
        # after the modal session ends, the same teardown discipline
        # settings_window.py's own windowWillClose_ already established for
        # the identical leak: the message handler holds a strong reference
        # back to self via addScriptMessageHandler_name_, so nothing here
        # would otherwise be released once the panel closes.
        self._user_content_controller = None
        return self

    # ------------------------------------------------------------------ #
    # Request fingerprint caption ("Seen N times this week")
    # ------------------------------------------------------------------ #

    def _seen_count_text(self) -> str:
        n = self.seen_count
        return f"Seen {n} time{'s' if n != 1 else ''} this week"

    # ------------------------------------------------------------------ #
    # Window construction (safe to call off the main thread, and without
    # ever showing or activating anything -- see build_panel()'s docstring)
    # ------------------------------------------------------------------ #

    def build_panel(self):
        """Build the panel and every subview it contains, with nothing shown,
        activated, or key yet -- pure construction, no side effect on window
        server state. Split out of runApproval_() specifically so tests can
        assert on the resulting webview/content (button row, PII/content-flag
        card, §1/§2/§3 content -- all of it lives in ``_details_html_string``
        now, see that field's own comment) without ever calling
        runModalForWindow_ or needing a real interactive session -- see
        test_approval_window.py.

        runApproval_() is the only caller in production code; it does
        nothing but this, then the actual show/activate/modal-block/hide
        sequence.
        """
        return self._build_panel()

    def _build_panel(self):
        window_width = _WINDOW_WIDTH[self.layout]
        window_height = self._window_height()

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, window_width, window_height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("")
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.center()
        # Invisible until webView_didFinishNavigation_ (or a fail-safe)
        # reveals it -- see this class's own module-docstring paragraph on
        # why. Doesn't show/activate/key anything (still consistent with
        # this method's "pure construction" contract), just keeps whatever
        # does get ordered front from being seen until there's content in it.
        panel.setAlphaValue_(0.0)
        self.panel = panel

        content = self._build_content_view(window_width, window_height)
        panel.setContentView_(content)
        if self._details_view is not None:
            panel.setInitialFirstResponder_(self._details_view)
        return panel

    def _disclosure_rows(self) -> list[tuple[str, str]]:
        """§3's rows -- ``new_info`` (real values a connector builds directly,
        e.g. calendar_get_event_details's Attendees/Location/Description, or
        gmail_get_message's To/Labels) come first, followed by the
        visibility-derived policy sentences for tools that also carry a
        privacy-category checklist (Gmail/Drive/Slack/Contacts/Tasks/
        Confluence). Several tools genuinely need both at once -- e.g.
        gmail_get_message discloses a literal ``To`` alongside a policy
        sentence for ``Message body`` -- so this concatenates rather than
        picking one source over the other."""
        if not self.is_read:
            return []
        rows = list(self.new_info.items())
        if self.visibility:
            rows += approval_window_html.disclosure_rows_from_visibility(self.visibility)
        return rows

    @staticmethod
    def _rows_height(labels) -> float:
        """Sum of each row's own worst-case height -- per-label line-clamp
        via approval_window_html.line_clamp_for (Attendees/Description get
        more room than a typical short field; see that function's own
        comment), never a single uniform per-row constant."""
        return sum(
            _ROW_BASE_HEIGHT + approval_window_html.line_clamp_for(str(label)) * _ROW_LINE_HEIGHT
            for label in labels
        )

    def _pinned_height(self) -> float:
        """Header, §1, §2, and the PII/content-flag risk card -- always
        fully visible, never inside the scrollable region (see
        approval_window_html.build_card_stack_html's own docstring for
        why the risk card in particular must never be one scroll away
        from being missed). Only §3 (self._scrollable_height) ever
        gets capped/scrolled."""
        height = _HEADER_HEIGHT
        if self.seen_count > 0:
            height += _SEEN_COUNT_HEIGHT
        if self.preview:
            height += _CARD_CHROME + self._rows_height(self.preview.keys())
        if self.claude_reason:
            height += _QUOTE_CARD_HEIGHT
        if self.pii_categories or self.write_content_flags:
            height += _RISK_CARD_BASE_HEIGHT
        return height

    def _scrollable_height(self) -> float:
        """§3 ("What will be provided to Claude") alone -- the one card
        whose row count genuinely varies per tool/call, and the only one
        ever capped when the window's height is trimmed (see
        _pinned_height)."""
        disclosure_rows = self._disclosure_rows()
        if not disclosure_rows:
            return 0.0
        return _CARD_CHROME + self._rows_height(label for label, _ in disclosure_rows)

    def _estimate_left_column_height(self) -> float:
        """Deterministic from field/section *counts* alone -- never from how
        long any actual value is (every row is CSS-fixed-and-truncated, see
        styles.css). See these pixel constants' own comments for the
        "assume worst case" reasoning."""
        return max(_MIN_CONTENT_HEIGHT, self._pinned_height() + self._scrollable_height())

    def _window_height(self) -> float:
        # _HEIGHT_SAFETY_MARGIN added here, not inside
        # _estimate_left_column_height() -- this is headroom for the
        # actual window, not a change to what any card/row is expected to
        # need (other callers of _estimate_left_column_height()/
        # _pinned_height()/_scrollable_height() reason about relative
        # proportions between sections, which this must leave alone). Body's
        # own vertical CSS padding is carved out of its 100vh (box-sizing:
        # border-box -- see build_card_stack_html's <style> block) before the
        # .pf-scroll flex child ever sees a pixel of the window's assigned
        # height, so it has to be reserved here explicitly -- found via a
        # real scrollHeight/clientHeight measurement showing a NARROW write
        # dialog (a short preview + reason + risk card, not enough of its
        # own estimate-vs-real-content slack to also absorb this) overflowing
        # its .pf-scroll container by a few px despite _HEIGHT_SAFETY_MARGIN,
        # because that margin was only ever sized for WebKit's own
        # render drift, never for this separate, fixed, previously-
        # nowhere-accounted-for amount.
        content_height = (
            self._estimate_left_column_height()
            + _HEIGHT_SAFETY_MARGIN
            + approval_window_html.BODY_VERTICAL_PADDING
        )
        window_height = content_height + _BUTTON_ROW_HEIGHT
        screen = NSScreen.mainScreen()
        if screen is not None:
            window_height = min(window_height, screen.frame().size.height * _MAX_WINDOW_HEIGHT_FRACTION)
        return window_height

    def _build_content_view(self, window_width: float, window_height: float):
        content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, window_width, window_height))

        # pdf_bytes/preview_bytes render inline via a standard <embed>/<img>
        # data URI -- no native PDFView/NSImageView overlay needed, the
        # whole content area is already one WKWebView. Precedence: pdf_bytes,
        # then an image preview_bytes, then plain text -- see
        # build_preview_body_html's docstring.
        pdf_data_uri = ""
        if self.pdf_bytes:
            pdf_data_uri = f"data:application/pdf;base64,{base64.b64encode(self.pdf_bytes).decode('ascii')}"
        image_data_uri = ""
        if not pdf_data_uri and self.preview_bytes and self.preview_mime_type.startswith("image/"):
            image_data_uri = (
                f"data:{self.preview_mime_type};base64,"
                f"{base64.b64encode(self.preview_bytes).decode('ascii')}"
            )

        # table_only suppresses details_text only when there's a real table
        # to show instead -- and never when preview_blocks is set, which
        # already controls exactly what renders on its own.
        body_text = (
            "" if self.table_only and self.preview_tables and not self.preview_blocks
            else self.details_text
        )
        preview_body_html = approval_window_html.build_preview_body_html(
            body_text, image_data_uri=image_data_uri, pdf_data_uri=pdf_data_uri,
            tables=self.preview_tables, blocks=self.preview_blocks,
        )
        disclosure_rows = self._disclosure_rows()

        # Names the specific rule Always allow would create (e.g. "Always
        # allow — this folder") instead of a plain, unspecific "Always
        # allow" -- so the reviewer knows roughly what standing rule they're
        # about to create before clicking, not only in the confirmation
        # dialog that follows. Falls back to the plain label when gate.py
        # has no hint for this rule (the one unconditional rule,
        # always_allow, or any future rule name describe_rule_short doesn't
        # recognize yet -- see its own docstring). Computed unconditionally
        # (not just when self.allow_accept_all) -- build_card_stack_html
        # simply ignores it when allow_accept_all is False, same as always.
        accept_all_label = (
            f"Always allow — {self.accept_all_hint}" if self.accept_all_hint else "Always allow"
        )

        html = approval_window_html.build_card_stack_html(
            layout=self.layout,
            title=self.title,
            connector_icon_data_uri=_icon_data_uri(_connector_icon_path(self.connector)),
            shield_icon_data_uri=_icon_data_uri(_icon_path()),
            is_read=self.is_read,
            seen_count_text=self._seen_count_text() if self.seen_count > 0 else "",
            preview=self.preview,
            claude_reason=self.claude_reason,
            disclosure_rows=disclosure_rows,
            pii_categories=self.pii_categories,
            write_content_flags=self.write_content_flags,
            upload_forced=self.upload_forced,
            temp_accept_text=_TEMP_ACCEPT_DISCLOSURE_TEXT if self.temp_accept_eligible else "",
            preview_kicker=f"Preview ({_reading_time_label(self.details_text)})",
            preview_body_html=preview_body_html,
            allow_accept_all=self.allow_accept_all,
            accept_all_label=accept_all_label,
        )
        # Kept purely for testability -- see test_approval_window_html.py
        # for build_card_stack_html()'s own direct pure-function coverage;
        # this just confirms the controller actually hands the real thing
        # to loadHTMLString_baseURL_.
        self._details_html_string = html

        # The button row's JS posts back through this -- see module
        # docstring's "Bridge protocol" paragraph and
        # userContentController_didReceiveScriptMessage_ below. Same
        # WKUserContentController/"pf"-handler-name construction
        # settings_window.py's own build_window() uses for its bridge.
        user_content_controller = WKUserContentController.alloc().init()
        user_content_controller.addScriptMessageHandler_name_(self, _MESSAGE_HANDLER_NAME)
        self._user_content_controller = user_content_controller

        config = WKWebViewConfiguration.alloc().init()
        config.setUserContentController_(user_content_controller)
        # JavaScript is on now -- the button row (Deny/Allow once/Always
        # allow) lives in this document's own markup and needs it to
        # dispatch clicks/keyboard events back to Python over the "pf"
        # bridge above (see module docstring). Still the same "no code
        # execution beyond our own inline script, no navigation, no
        # network" guarantee as before: this remains a fully
        # self-contained, app-authored document loaded via
        # loadHTMLString_baseURL_(html, None) with no base URL to navigate
        # anywhere from -- just no longer one with script execution turned
        # off entirely. approval_window_html.py's own docstring covers what
        # actually runs.
        config.preferences().setJavaScriptEnabled_(True)
        # No more separate native button band below it -- the webview now
        # owns the *entire* content area (see module docstring), full
        # window_height rather than window_height - _BUTTON_ROW_HEIGHT.
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, window_width, window_height), config
        )
        # Explicit, not inferred from view-hierarchy timing: loadHTMLString_
        # baseURL_ below evaluates the page's `prefers-color-scheme` media
        # query immediately, and at that point this view has no superview/
        # window yet (content.addSubview_ hasn't run) -- left to infer its
        # own appearance it resolves to the WebKit default (light) rather
        # than the panel's actual one. Setting it directly from the panel
        # sidesteps that timing entirely.
        webview.setAppearance_(self.panel.effectiveAppearance())
        webview.setNavigationDelegate_(self)
        webview.loadHTMLString_baseURL_(html, None)
        self._details_view = webview
        content.addSubview_(webview)

        return content

    def webView_didFinishNavigation_(self, webView, navigation) -> None:
        """WKNavigationDelegate callback: the card-stack webview has
        actually finished loading and painting, so it's safe to actually
        reveal the panel (see _build_panel's alphaValue comment) -- the
        reviewer never sees an empty window snap to its real content,
        because there was nothing on screen to see until this fired. Button
        click-ability itself is no longer this method's concern: the page's
        own DOMContentLoaded handling drives that now (see
        approval_window_html.py's ``_JS``) -- this only forces it too, as a
        fail-safe, in case that in-page signal somehow didn't already fire
        first."""
        self._reveal_and_ensure_buttons_enabled()

    def webView_didFailNavigation_withError_(self, webView, navigation, error) -> None:
        """Fail-safe counterpart to webView_didFinishNavigation_ above: a
        load that fails outright must still reveal the panel and force the
        buttons clickable (DOMContentLoaded may never have fired at all),
        not leave a reviewer staring at an invisible, unresponsive modal
        dialog forever. This document is fully local/self-contained (no
        network, nil base URL), so an actual failure here would be
        unexpected -- but that outcome would be far worse than the cosmetic
        issue this whole mechanism exists to fix."""
        self._reveal_and_ensure_buttons_enabled()

    def webView_didFailProvisionalNavigation_withError_(self, webView, navigation, error) -> None:
        """Same fail-safe as webView_didFailNavigation_withError_ above, for
        the earlier (provisional) failure point in WKNavigationDelegate's
        callback sequence."""
        self._reveal_and_ensure_buttons_enabled()

    def _reveal_and_ensure_buttons_enabled(self) -> None:
        if self.panel is not None:
            self.panel.setAlphaValue_(1.0)
        if self._details_view is not None:
            # Idempotent (removeAttribute/setAttribute) -- safe to call even
            # when the page's own DOMContentLoaded handler already ran, see
            # window.__pfEnableButtons's own comment in
            # approval_window_html.py's ``_JS``.
            self._details_view.evaluateJavaScript_completionHandler_(
                "if (window.__pfEnableButtons) { window.__pfEnableButtons(); }", None,
            )

    # ------------------------------------------------------------------ #
    # JS -> Python (button row clicks/keyboard resolution)
    # ------------------------------------------------------------------ #

    def userContentController_didReceiveScriptMessage_(self, _user_content_controller, message) -> None:
        """WKScriptMessageHandler callback for the "pf" bridge -- see module
        docstring's "Bridge protocol" paragraph. Replaces the old
        buttonClicked_'s sender.tag() dispatch: the message's own ``result``
        field is what self.result resolves to now, before ending the modal
        session the same way buttonClicked_ always did."""
        try:
            payload = dict(message.body())
        except (TypeError, ValueError):
            logger.warning("Malformed approval bridge message: %r", message.body())
            return
        if payload.get("action") != "resolve":
            logger.warning("Unknown approval bridge action: %r", payload)
            return
        # Any result this doesn't recognize still defaults to the safe
        # direction -- same defensive fallback buttonClicked_'s own
        # unrecognized-tag branch always had, even though _JS's own
        # data-pf-action markup never actually produces one.
        result = payload.get("result")
        self.result = result if result in _BRIDGE_RESULTS else "deny"
        NSApplication.sharedApplication().stopModalWithCode_(NSModalResponseStop)

    # ------------------------------------------------------------------ #
    # Entry point (must run on the main thread)
    # ------------------------------------------------------------------ #

    def runApproval_(self, _sender) -> None:
        app = NSApplication.sharedApplication()
        # A raw, unbundled process defaults to NSApplicationActivationPolicy
        # Prohibited, which silently blocks activateIgnoringOtherApps_ below
        # and leaves whatever app the user last clicked as "active" — at
        # which point NSPanel's default hidesOnDeactivate makes this window
        # vanish behind it. Accessory matches how the menu bar app already
        # runs (no Dock icon) and is enough to let it become key and stay up.
        if app.activationPolicy() == NSApplicationActivationPolicyProhibited:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        panel = self.build_panel()

        panel.makeKeyAndOrderFront_(None)
        panel.setLevel_(NSFloatingWindowLevel)
        app.activateIgnoringOtherApps_(True)
        app.runModalForWindow_(panel)
        # close(), not just orderOut_() -- orderOut_() only hides the
        # window, it never actually tears down its view hierarchy (the
        # WKWebView included). A fresh panel/webview is built for every
        # single call here, never reused, so leaving the old one merely
        # hidden means many approval popups shown back to back accumulate
        # windows AppKit still considers alive, without a matching
        # explicit release -- see qa_popup_smoke.py's full-suite run,
        # which segfaults deep in AppKit's tracking-area management after
        # enough popups (each with its own hover-tooltip-bearing webview)
        # have been shown and hidden this way. close() actually releases
        # the window (NSWindow's default isReleasedWhenClosed) instead.
        panel.close()
        # See init()'s own comment on self._user_content_controller: drop
        # the strong reference the message handler holds back to self now
        # that the modal session is over, the same explicit teardown
        # settings_window.py's windowWillClose_ already does for its own
        # (longer-lived) bridge.
        if self._user_content_controller is not None:
            self._user_content_controller.removeScriptMessageHandlerForName_(_MESSAGE_HANDLER_NAME)
            self._user_content_controller = None


def show_native_approval(
    *,
    title: str,
    preview: dict[str, str],
    details_text: str,
    allow_accept_all: bool,
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
    accept_all_hint: str = "",
) -> str:
    """Show the approval window and block until the user picks a button.

    Returns 'accept', 'deny', or 'accept_all' (only reachable when
    allow_accept_all is True). ``temp_accept_eligible`` adds an
    informational disclosure caption above the buttons (see
    ApprovalWindowController._build_content_view); it doesn't change which
    buttons render.
    Whether Allow once also arms auto_accept.py's 5-minute, same-file grace
    window is decided by gate.py after the fact, from the same eligibility
    check that produced this flag -- not from a distinct user choice here.
    Thread-safe: safe to call from any thread, the window itself is always
    built and driven on the main thread.

    ``layout`` selects one of approval_window_html's NARROW/WIDE card-stack
    shapes (see approval_window_html.py's module docstring) -- gate.py picks
    this per tool from its own _TOOL_LAYOUT table. ``is_read`` selects the
    §1/§2 kicker wording and color, and whether the §3 disclosure card can
    render at all (write calls never carry one) -- show_read_popup/
    show_popup set this from which one of them was called, not a
    per-tool choice. ``upload_forced``, ``new_info`` (§3's real "what's new"
    field/value pairs -- e.g. calendar_get_event_details's Attendees/
    Location/Description; falls back to a ``visibility``-derived policy
    summary when empty, see ApprovalWindowController._disclosure_rows),
    ``preview_tables`` (the WIDE right-pane preview as structured table(s)
    instead of plain text -- see approval_window_html.py's _table_html),
    ``preview_blocks`` (an ordered list of text/field/table blocks, letting
    them interleave -- takes full precedence over both ``details_text``
    and ``preview_tables`` when given), and ``table_only`` (suppresses
    ``details_text`` in the WIDE right pane when a table already covers
    the same data -- no-op when ``preview_blocks`` is set).

    ``accept_all_hint``, when set (and ``allow_accept_all`` is True),
    renders as part of the Always allow button's own label (e.g. "Always
    allow — this folder") instead of the plain "Always allow" -- see
    _build_content_view's own comment for the exact format. Empty for
    the one unconditional rule (``always_allow``) with no category to
    name, and always empty when ``allow_accept_all`` is False.
    """
    with _popup_lock:
        controller = ApprovalWindowController.alloc().init()
        controller.title = title
        controller.preview = preview or {}
        controller.details_text = details_text
        controller.allow_accept_all = allow_accept_all
        controller.temp_accept_eligible = temp_accept_eligible
        controller.pii_categories = pii_categories or []
        controller.visibility = visibility or {}
        controller.claude_reason = claude_reason or ""
        controller.write_content_flags = write_content_flags or []
        controller.seen_count = seen_count or 0
        controller.content_kind = content_kind or "generic"
        controller.pdf_bytes = pdf_bytes or b""
        controller.connector = connector or ""
        controller.preview_bytes = preview_bytes or b""
        controller.preview_mime_type = preview_mime_type or ""
        controller.layout = layout or approval_window_html.NARROW
        controller.is_read = is_read
        controller.upload_forced = upload_forced
        controller.new_info = new_info or {}
        controller.preview_tables = preview_tables or []
        controller.preview_blocks = preview_blocks or []
        controller.table_only = table_only
        controller.accept_all_hint = accept_all_hint or ""

        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runApproval:", None, True
        )
        return controller.result
