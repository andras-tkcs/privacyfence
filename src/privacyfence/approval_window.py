"""Native macOS approval window (AppKit / PyObjC).

Renders the single blocking approval dialog every gated call resolves
through: one WKWebView (approval_window_html.build_card_stack_html) filling
the whole content area — kicker/icon/title, then §1 (WHAT), §2 (Claude's
stated reason), an optional §3 disclosure card (review-gate calls only:
"what will be provided to Claude") or §4 PII/content-flag risk card, and a
scrollable right-hand preview pane for WIDE-shaped tools — with native
Deny/Allow once/Always allow buttons in a fixed band below it. AppleScript
`display dialog` popups (still used elsewhere in approval_popup.py for
secondary confirmations) have no room for a real layout, an icon, or a
genuinely scrollable body — this module renders the whole content area as
one WKWebView instead. See docs/approval-window-content-reference.md for
exactly what each tool renders, and approval_window_html.py's own module
docstring for the card-stack template itself.

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

Allow once has no "\\r" keyEquivalent and the card-stack webview is the
panel's initial first responder — hitting Enter the moment the window
appears cannot approve a request nobody has actually read yet; the action
buttons also start disabled until the webview finishes painting (see
webView_didFinishNavigation_). Deny keeps Escape: declining via a reflexive
keypress is the safe direction, not a risk the way an accidental approve
would be.

AppKit windows must be created and driven on the main thread, but gate.py
calls in here from the IPC server thread (via asyncio.to_thread). show_native_
approval() hands the actual window-building to the main thread with
performSelectorOnMainThread_withObject_waitUntilDone_(waitUntilDone=True),
which blocks the calling thread until the modal session ends, so gate.py's
calling convention stays synchronous.
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSModalResponseStop,
    NSPanel,
    NSScreen,
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSAttributedString, NSObject
from WebKit import WKWebView, WKWebViewConfiguration

from . import approval_window_html

_MARGIN = 28.0
_BUTTON_ROW_HEIGHT = 66.0

# NSButton tags for buttonClicked_'s dispatch -- not title-string matching,
# since Always allow's own title varies per call (accept_all_hint), so
# dispatch can't key on an exact string. NSButton's own default tag is 0,
# so Deny (the safe fallback direction) doesn't need its own constant set
# explicitly anywhere it's built.
_TAG_DENY = 0
_TAG_ACCEPT = 1
_TAG_ACCEPT_ALL = 2

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

# Brand colors sampled from resources/icon_512.png — a fixed identity, not a
# themed value, so these stay literal rather than following light/dark mode.
_BLUE = NSColor.colorWithSRGBRed_green_blue_alpha_(0x5B / 255, 0xA4 / 255, 0xFF / 255, 1.0)

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
        # Deny/Allow once/Always allow start disabled and only become
        # clickable once the card-stack webview has actually finished
        # loading, so a fast or reflexive click can't resolve the decision
        # before its content is even visible -- the same "don't approve
        # what wasn't reviewed" principle _build_button() already applies
        # to Allow once's missing Enter keyEquivalent, just covering the
        # window's initial paint too (see webView_didFinishNavigation_).
        self._action_buttons: list = []
        return self

    # ------------------------------------------------------------------ #
    # Request fingerprint caption ("Seen N times this week")
    # ------------------------------------------------------------------ #

    def _seen_count_text(self) -> str:
        n = self.seen_count
        return f"Seen {n} time{'s' if n != 1 else ''} this week"

    # ------------------------------------------------------------------ #
    # Buttons
    # ------------------------------------------------------------------ #

    def _build_button(
        self, title: str, *, tag: int, primary: bool = False, danger: bool = False,
    ) -> NSButton:
        btn = NSButton.alloc().init()
        btn.setTitle_(title)
        btn.setTag_(tag)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_("buttonClicked:")
        btn.sizeToFit()
        frame = btn.frame()
        min_width = 90.0
        if frame.size.width < min_width:
            btn.setFrameSize_((min_width, frame.size.height))
        if primary:
            # Deliberately no "\r" keyEquivalent: hitting Enter
            # shouldn't be able to approve a request the reviewer hasn't
            # actually looked at yet. Allow once still keeps its blue "this
            # is the affirmative action" styling; only the Enter-key muscle
            # memory is removed. Deny keeps Escape (danger branch below) --
            # declining via a reflexive keypress is the safe direction, not
            # a risk the way an accidental approve would be.
            if hasattr(btn, "setBezelColor_"):
                btn.setBezelColor_(_BLUE)
                btn.setContentTintColor_(NSColor.whiteColor())
        elif danger:
            btn.setKeyEquivalent_("\x1b")
            if hasattr(btn, "setContentTintColor_"):
                btn.setContentTintColor_(NSColor.systemRedColor())
        return btn

    def _build_link_button(self, title: str, *, tag: int) -> NSButton:
        """Small, borderless "link"-style control for the low-frequency,
        high-consequence standing-rule action (Always allow) -- deliberately
        not the same pill styling as Deny/Allow once, so a fast, confident
        click aimed at the primary action can't land on it by accident. No
        existing precedent for a link-style NSButton in this codebase: built
        via an attributed title rather than a bezel style, since
        NSBezelStyleRounded has no "no border, small, underlined" variant.
        Dispatch is via ``tag``, not ``title()`` -- this button's title
        varies per call (see _build_content_view's accept_all_hint
        comment), so buttonClicked_ can't key on an exact string."""
        btn = NSButton.alloc().init()
        btn.setBordered_(False)
        btn.setTag_(tag)
        btn.setTarget_(self)
        btn.setAction_("buttonClicked:")
        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
            NSUnderlineStyleAttributeName: NSUnderlineStyleSingle,
        }
        btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        btn.sizeToFit()
        return btn

    # ------------------------------------------------------------------ #
    # Window construction (safe to call off the main thread, and without
    # ever showing or activating anything -- see build_panel()'s docstring)
    # ------------------------------------------------------------------ #

    def build_panel(self):
        """Build the panel and every subview it contains, with nothing shown,
        activated, or key yet -- pure construction, no side effect on window
        server state. Split out of runApproval_() specifically so tests can
        assert on the resulting view hierarchy (button set, PII/content-flag
        card, §1/§2/§3 content) without ever calling runModalForWindow_ or
        needing a real interactive session -- see test_approval_window.py.

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
        webview_height = window_height - _BUTTON_ROW_HEIGHT

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
        )
        # Kept purely for testability -- see test_approval_window_html.py
        # for build_card_stack_html()'s own direct pure-function coverage;
        # this just confirms the controller actually hands the real thing
        # to loadHTMLString_baseURL_.
        self._details_html_string = html

        config = WKWebViewConfiguration.alloc().init()
        # No script needed -- this document is 100% static, self-contained
        # markup (fonts/icons/images already inlined as data URIs); nothing
        # here has ever needed a JS bridge back to Python. Same "no code
        # execution, no network" guarantee _build_details_web_view() holds.
        config.preferences().setJavaScriptEnabled_(False)
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, window_width, webview_height), config
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

        y = webview_height

        # Button row, anchored under the webview. Disabled until
        # webView_didFinishNavigation_ fires below -- see _action_buttons'
        # own comment in init().
        self._action_buttons = []
        accept_btn = self._build_button("Allow once", tag=_TAG_ACCEPT, primary=True)
        button_h = accept_btn.frame().size.height
        button_y = y + (_BUTTON_ROW_HEIGHT - button_h) / 2.0

        deny_btn = self._build_button("Deny", tag=_TAG_DENY, danger=True)
        deny_btn.setFrameOrigin_((_MARGIN, button_y))
        content.addSubview_(deny_btn)
        self._action_buttons.append(deny_btn)

        right_x = window_width - _MARGIN - accept_btn.frame().size.width
        accept_btn.setFrameOrigin_((right_x, button_y))
        content.addSubview_(accept_btn)
        self._action_buttons.append(accept_btn)

        if self.allow_accept_all:
            # Names the specific rule this would create (e.g. "Always
            # allow — this folder") instead of a plain, unspecific "Always
            # allow" -- so the reviewer knows roughly what standing rule
            # they're about to create before clicking, not only in the
            # confirmation dialog that follows. Falls back to the plain
            # label when gate.py has no hint for this rule (the one
            # unconditional rule, always_allow, or any future rule name
            # describe_rule_short doesn't recognize yet -- see its own
            # docstring).
            accept_all_label = (
                f"Always allow — {self.accept_all_hint}" if self.accept_all_hint else "Always allow"
            )
            link_x = _MARGIN + deny_btn.frame().size.width + 16.0
            accept_all_btn = self._build_link_button(accept_all_label, tag=_TAG_ACCEPT_ALL)
            link_y = y + (_BUTTON_ROW_HEIGHT - accept_all_btn.frame().size.height) / 2.0
            accept_all_btn.setFrameOrigin_((link_x, link_y))
            content.addSubview_(accept_all_btn)
            self._action_buttons.append(accept_all_btn)

        for btn in self._action_buttons:
            btn.setEnabled_(False)

        return content

    def webView_didFinishNavigation_(self, webView, navigation) -> None:
        """WKNavigationDelegate callback: the card-stack webview has
        actually finished loading and painting, so it's now safe to let
        Deny/Allow once/Always allow be clicked -- see _build_content_view
        (where they start disabled) and _action_buttons' comment in
        init() for why."""
        self._enable_action_buttons()

    def webView_didFailNavigation_withError_(self, webView, navigation, error) -> None:
        """Fail-safe counterpart to webView_didFinishNavigation_ above: a
        load that fails outright must still enable the buttons, not leave
        them permanently disabled. This document is fully local/self-
        contained (no network, nil base URL), so an actual failure here
        would be unexpected -- but leaving a reviewer stuck in a modal
        dialog with no way to even click Deny would be a far worse outcome
        than the cosmetic issue this whole mechanism exists to fix."""
        self._enable_action_buttons()

    def webView_didFailProvisionalNavigation_withError_(self, webView, navigation, error) -> None:
        """Same fail-safe as webView_didFailNavigation_withError_ above, for
        the earlier (provisional) failure point in WKNavigationDelegate's
        callback sequence."""
        self._enable_action_buttons()

    def _enable_action_buttons(self) -> None:
        for btn in self._action_buttons:
            btn.setEnabled_(True)

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

    def buttonClicked_(self, sender) -> None:
        # Internal result values ("accept"/"accept_all"/"deny") stay as-is --
        # gate.py/audit_log.py/tests key on them throughout. Dispatch is via
        # sender.tag() (_TAG_DENY/_TAG_ACCEPT/_TAG_ACCEPT_ALL), not the
        # button's displayed title -- Always allow's own title varies per
        # call (accept_all_hint), so an exact-string match on the title
        # would be fragile. Any tag this doesn't recognize still defaults
        # to the safe direction.
        tag = sender.tag()
        if tag == _TAG_ACCEPT_ALL:
            self.result = "accept_all"
        elif tag == _TAG_ACCEPT:
            self.result = "accept"
        else:
            self.result = "deny"
        NSApplication.sharedApplication().stopModalWithCode_(NSModalResponseStop)


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
