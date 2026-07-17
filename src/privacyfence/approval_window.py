"""Native macOS approval window (AppKit / PyObjC).

Renders the single blocking approval dialog every gated call resolves
through: a fence-shield icon top right, a bold title, and — in this order,
per docs/security-review-ui-redesign.md §5 — a summary box with the item's
key fields (WHAT), an "AI will receive" checklist (AI VISIBILITY, read-gate
calls only), a PII warning banner when applicable (RISK), and a scrollable
pane for full content with a reading-time estimate (PREVIEW), before the
buttons. This replaces the AppleScript `display dialog` popups that used to
live in approval_popup.py — those had no room for a real layout, an icon, or
a genuinely scrollable body.

The "AI will receive" checklist renders privacy_filter.category_policy()'s
resolved allow/redact/block per category — ground truth PrivacyFence already
computed before the popup was built, not a new claim invented for display.
Never present for a write (show_popup never sets self.visibility; see its
docstring for why).

When gate.py's PII detector (pii_detector.py) flags categories in the
content of a read (review-gate) popup, the window renders a light-red wash
over the whole panel plus a warning banner naming what was found — the
visual cue that a second, explicit "Are you sure?" confirmation (approval_
popup.show_pii_confirmation_popup) is coming after Allow once, not a
decision by itself. Write (popup-gate) approvals never carry pii_categories,
so this never renders for them.

Allow once has no "\\r" keyEquivalent and the details pane is the panel's
initial first responder — hitting Enter the moment the window appears
cannot approve a request nobody has actually read yet. Deny keeps Escape:
declining via a reflexive keypress is the safe direction, not a risk the
way an accidental approve would be.

AppKit windows must be created and driven on the main thread, but gate.py
calls in here from the IPC server thread (via asyncio.to_thread). show_native_
approval() hands the actual window-building to the main thread with
performSelectorOnMainThread_withObject_waitUntilDone_(waitUntilDone=True),
which blocks the calling thread until the modal session ends — the same
synchronous contract the old osascript-based popups had, so gate.py needs no
changes beyond where it imports from.
"""
from __future__ import annotations

import threading
from html import escape as _html_escape
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBox,
    NSBoxCustom,
    NSButton,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSFontAttributeName,
    NSImage,
    NSImageView,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSModalResponseStop,
    NSNoTitle,
    NSPanel,
    NSScreen,
    NSStringDrawingUsesLineFragmentOrigin,
    NSTextField,
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSString
from WebKit import WKWebView, WKWebViewConfiguration

_WINDOW_WIDTH = 620.0
_MARGIN = 28.0
_DETAILS_HEIGHT = 280.0
_ICON_SIZE = 51.0  # 150% of the original 34pt
_ICON_TITLE_GAP = 14.0
_TITLE_RIGHT_RESERVE = _ICON_SIZE + _ICON_TITLE_GAP
_KICKER_HEIGHT = 22.0
_SUMMARY_LABEL_WIDTH = 84.0
_SUMMARY_ROW_GAP = 9.0
_SUMMARY_PAD = 14.0
_BUTTON_ROW_HEIGHT = 66.0

# Brand colors sampled from resources/icon_512.png — a fixed identity, not a
# themed value, so these stay literal rather than following light/dark mode.
_BLUE = NSColor.colorWithSRGBRed_green_blue_alpha_(0x5B / 255, 0xA4 / 255, 0xFF / 255, 1.0)

# PII warning tint. systemRedColor is a dynamic (light/dark-aware) color, so
# a low-alpha wash of it reads as "light red" in light mode and a muted red
# tint in dark mode, rather than a literal color that fights the OS theme.
_PII_RED = NSColor.systemRedColor()
_PII_BACKGROUND_ALPHA = 0.10
_PII_BANNER_FILL_ALPHA = 0.16

# Write-gate content-flag banner: deliberately NOT the PII red -- this is
# an informational signal (Claude's own drafted content, no second
# confirmation gate attached), not the "possible PII flowed in from an
# external source, confirm before proceeding" signal the red tint means.
# No full-window wash either, only this banner's own fill -- see gate.py's
# write_content_flags comment and approval_popup.show_popup's docstring.
_CONTENT_FLAG_AMBER = NSColor.systemOrangeColor()
_CONTENT_FLAG_FILL_ALPHA = 0.12

# "AI will receive" checklist symbols -- allow/redact/block from
# privacy_filter.category_policy(). No per-row color coding (deliberately):
# the symbol alone is legible without relying on systemGreen/systemRed's
# exact resolved appearance across light/dark/accessibility settings, which
# the PII banner above already uses its alpha-based (not RGB-based) test
# assertions to sidestep for the same reason.
_VISIBILITY_SYMBOL = {"allow": "✓", "redact": "◐", "block": "✗"}  # ✓ ◐ ✗

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


# Details pane, Phase 3 (docs/security-review-ui-redesign.md §7): a local,
# self-contained HTML document rendered by a WKWebView instead of a plain
# NSTextView -- see _build_details_view. "%s" substitution rather than
# str.format() so the CSS's own literal "{"/"}" never need doubling.
_DETAILS_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<style>
  html, body { margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 13px;
    line-height: 1.45;
    color: #1d1d1f;
    background: #ffffff;
    padding: 6px 8px;
    white-space: pre-wrap;
    word-wrap: break-word;
    box-sizing: border-box;
  }
  @media (prefers-color-scheme: dark) {
    body { color: #f2f2f2; background: #1e1e1e; }
  }
</style>
</head>
<body>%s</body>
</html>
"""


def _details_html(details_text: str) -> str:
    """Self-contained HTML for the details/body pane's WKWebView.

    Plain text only -- details_text arrives already HTML-stripped (see
    html_to_text.py), so this only ever escapes and preserves whitespace,
    never renders markup someone else authored. No <script>, no external
    resources, no network: loaded via loadHTMLString_baseURL_(html, None)
    with a nil base URL, so there's nothing here for it to even attempt to
    load out to. Pure function -- directly unit-testable, the same
    "must mirror the real render" contract _compute_layout() has for
    build_panel()'s AppKit layout.
    """
    escaped = _html_escape(details_text or "(no details)")
    return _DETAILS_HTML_TEMPLATE % escaped


def _icon_path() -> str | None:
    here = Path(__file__).parent / "resources"
    for name in ("icon_64.png", "icon_512.png", "icon_32.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


def _text_height(text: str, width: float, font) -> float:
    ns = NSString.stringWithString_(text)
    rect = ns.boundingRectWithSize_options_attributes_(
        (width, 1_000_000.0),
        NSStringDrawingUsesLineFragmentOrigin,
        {NSFontAttributeName: font},
    )
    return float(rect.size.height)


def _make_label(text: str, *, size: float, bold: bool = False, color=None) -> NSTextField:
    field = NSTextField.alloc().init()
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    field.setTextColor_(color or NSColor.labelColor())
    cell = field.cell()
    cell.setWraps_(True)
    cell.setLineBreakMode_(NSLineBreakByWordWrapping)
    return field


def _background_box(frame, *, fill=None, corner_radius: float = 8.0) -> NSBox:
    """Plain decorative NSBox (fill + rounded corners, no children) — kept
    separate from the label/content view stacked on top of it so we never
    need to bridge a raw CGColorRef through a CALayer."""
    box = NSBox.alloc().initWithFrame_(frame)
    box.setBoxType_(NSBoxCustom)
    box.setTitlePosition_(NSNoTitle)
    box.setFillColor_(fill or NSColor.controlBackgroundColor())
    box.setBorderWidth_(0)
    box.setCornerRadius_(corner_radius)
    return box


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
        self.allow_temp_accept = False
        self.pii_categories: list[str] = []
        self.visibility: dict[str, str] = {}
        self.claude_reason: str = ""
        self.write_content_flags: list[str] = []
        self.seen_count: int = 0
        self.result = "deny"
        self.panel = None
        self._details_view = None
        self._details_html_string = ""
        return self

    # ------------------------------------------------------------------ #
    # Request fingerprint caption ("Seen N times this week")
    # ------------------------------------------------------------------ #

    def _seen_count_text(self) -> str:
        n = self.seen_count
        return f"Seen {n} time{'s' if n != 1 else ''} this week"

    # ------------------------------------------------------------------ #
    # Summary box
    # ------------------------------------------------------------------ #

    def _summary_rows(self, width: float) -> tuple[list[tuple[str, str, float]], float]:
        value_width = width - 2 * _SUMMARY_PAD - _SUMMARY_LABEL_WIDTH - 14.0
        font = NSFont.systemFontOfSize_(13)
        rows = []
        for key, value in self.preview.items():
            h = max(16.0, _text_height(str(value), value_width, font))
            rows.append((key, str(value), h))
        return rows, value_width

    def _summary_height(self, width: float) -> float:
        rows, _ = self._summary_rows(width)
        if not rows:
            return 0.0
        rows_h = sum(h for _, _, h in rows) + max(0, len(rows) - 1) * _SUMMARY_ROW_GAP
        return rows_h + 2 * _SUMMARY_PAD

    def _build_summary_overlay(self, y: float, width: float) -> tuple[NSView, float]:
        """Transparent view holding the label/value pairs. Stacked on top of
        a plain _background_box() sibling of the same frame."""
        rows, value_width = self._summary_rows(width)
        box_h = self._summary_height(width)

        box = _FlippedView.alloc().initWithFrame_(NSMakeRect(_MARGIN, y, width, box_h))

        row_y = _SUMMARY_PAD
        for key, value, h in rows:
            label = _make_label(key, size=13, color=NSColor.secondaryLabelColor())
            label.setFrame_(NSMakeRect(_SUMMARY_PAD, row_y, _SUMMARY_LABEL_WIDTH, h))
            box.addSubview_(label)

            value_field = _make_label(value, size=13, bold=True)
            value_field.setFrame_(
                NSMakeRect(_SUMMARY_PAD + _SUMMARY_LABEL_WIDTH + 14.0, row_y, value_width, h)
            )
            box.addSubview_(value_field)

            row_y += h + _SUMMARY_ROW_GAP

        return box, box_h

    # ------------------------------------------------------------------ #
    # "AI will receive" visibility checklist -- privacy_filter.py's
    # category_policy() made visible, not a new promise: see
    # docs/security-review-ui-redesign.md §4 and §7 Phase 1a. Read-gate
    # calls only -- show_popup never sets self.visibility (see its
    # docstring), so this section never renders for a write.
    # ------------------------------------------------------------------ #

    def _visibility_lines(self) -> list[str]:
        return [
            f"{_VISIBILITY_SYMBOL.get(policy, '?')} {label}"
            for label, policy in self.visibility.items()
        ]

    def _visibility_height(self, width: float) -> float:
        lines = self._visibility_lines()
        if not lines:
            return 0.0
        value_width = width - 2 * _SUMMARY_PAD
        font = NSFont.systemFontOfSize_(13)
        rows_h = sum(max(16.0, _text_height(t, value_width, font)) for t in lines)
        rows_h += max(0, len(lines) - 1) * _SUMMARY_ROW_GAP
        return rows_h + 2 * _SUMMARY_PAD

    def _build_visibility_overlay(self, y: float, width: float) -> tuple[NSView, float]:
        """Same box+overlay construction as _build_summary_overlay, just a
        single column of "symbol label" lines instead of label/value pairs
        -- a checklist has no natural second column."""
        lines = self._visibility_lines()
        box_h = self._visibility_height(width)
        value_width = width - 2 * _SUMMARY_PAD
        font = NSFont.systemFontOfSize_(13)

        box = _FlippedView.alloc().initWithFrame_(NSMakeRect(_MARGIN, y, width, box_h))
        row_y = _SUMMARY_PAD
        for text in lines:
            h = max(16.0, _text_height(text, value_width, font))
            label = _make_label(text, size=13)
            label.setFrame_(NSMakeRect(_SUMMARY_PAD, row_y, value_width, h))
            box.addSubview_(label)
            row_y += h + _SUMMARY_ROW_GAP

        return box, box_h

    # ------------------------------------------------------------------ #
    # PII warning banner
    # ------------------------------------------------------------------ #

    def _pii_banner_text(self) -> str:
        return "\u26a0 Possible PII detected — review carefully: " + ", ".join(self.pii_categories)

    def _pii_banner_height(self, width: float) -> float:
        if not self.pii_categories:
            return 0.0
        text_h = _text_height(
            self._pii_banner_text(), width - 2 * _SUMMARY_PAD, NSFont.boldSystemFontOfSize_(13)
        )
        return max(20.0, text_h) + _SUMMARY_PAD

    # ------------------------------------------------------------------ #
    # Write-gate content-flag banner -- informational, no confirmation
    # gate attached (unlike the PII banner above). See gate.py's
    # write_content_flags comment and docs/security-review-ui-redesign.md
    # §7 Phase 2 for why this is a separate signal from pii_categories.
    # ------------------------------------------------------------------ #

    def _content_flag_banner_text(self) -> str:
        return "ⓘ This message appears to contain: " + ", ".join(self.write_content_flags)

    def _content_flag_banner_height(self, width: float) -> float:
        if not self.write_content_flags:
            return 0.0
        text_h = _text_height(
            self._content_flag_banner_text(), width - 2 * _SUMMARY_PAD, NSFont.boldSystemFontOfSize_(13)
        )
        return max(20.0, text_h) + _SUMMARY_PAD

    # ------------------------------------------------------------------ #
    # "Claude says" -- self-reported, unverified. See gate.py's
    # reason_scope docstring and docs/security-review-ui-redesign.md §4:
    # this is Claude's own stated reason for the call, never checked
    # against anything, so it must never be styled or merged as if it were
    # a verified field (WHAT / AI VISIBILITY / RISK above all come from
    # data PrivacyFence itself computed). Present for both read and write
    # gates -- unlike the visibility checklist, "why am I doing this"
    # applies equally to a write.
    # ------------------------------------------------------------------ #

    def _claude_reason_height(self, width: float) -> float:
        if not self.claude_reason:
            return 0.0
        value_width = width - 2 * _SUMMARY_PAD
        text_h = _text_height(self.claude_reason, value_width, NSFont.systemFontOfSize_(13))
        return max(16.0, text_h) + 2 * _SUMMARY_PAD

    def _build_claude_reason_overlay(self, y: float, width: float) -> tuple[NSView, float]:
        box_h = self._claude_reason_height(width)
        box = _FlippedView.alloc().initWithFrame_(NSMakeRect(_MARGIN, y, width, box_h))
        label = _make_label(self.claude_reason, size=13, color=NSColor.secondaryLabelColor())
        label.setFrame_(NSMakeRect(_SUMMARY_PAD, _SUMMARY_PAD, width - 2 * _SUMMARY_PAD, box_h - 2 * _SUMMARY_PAD))
        box.addSubview_(label)
        return box, box_h

    # ------------------------------------------------------------------ #
    # Details (scrollable body) -- Phase 3 (docs/security-review-ui-redesign.md
    # §7): a local WKWebView rendering _details_html()'s self-contained HTML
    # document, replacing the plain NSTextView Phases 0-2 used. This is what
    # unlocks real typography/wrapping control and (in a later pass) a
    # Gmail-style structured header or per-file-type rendering, without
    # hand-building each new layout in raw AppKit constraints -- see this
    # module's own docstring and docs/security-review-ui-redesign.md §7
    # Phase 1a's note on why the Gmail-specific layout was deferred to here.
    # ------------------------------------------------------------------ #

    def _build_details_view(self, y: float, width: float) -> WKWebView:
        config = WKWebViewConfiguration.alloc().init()
        # No script needed for anything this pane renders today (plain text,
        # escaped) -- see _details_html()'s docstring. Disabled explicitly,
        # not just left unused, as the actual "no network, no code
        # execution" guarantee docs/security-review-ui-redesign.md §7 Phase
        # 3 and §5.5 ("keep it local and synchronous") call for.
        config.preferences().setJavaScriptEnabled_(False)

        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(_MARGIN, y, width, _DETAILS_HEIGHT), config
        )
        html = _details_html(self.details_text)
        # Kept purely for testability -- WKWebView's own loaded content
        # isn't synchronously readable back out the way NSTextView.string()
        # was, so tests assert against this instead of the live view. See
        # test_approval_window.py's TestDetailsPane.
        self._details_html_string = html
        webview.loadHTMLString_baseURL_(html, None)

        # Kept for build_panel() to set as the panel's initial first
        # responder -- default focus lands on the content to read, not on
        # Allow once (§5.4, same reasoning as dropping its "\r" above).
        self._details_view = webview
        return webview

    # ------------------------------------------------------------------ #
    # Buttons
    # ------------------------------------------------------------------ #

    def _build_button(self, title: str, *, primary: bool = False, danger: bool = False) -> NSButton:
        btn = NSButton.alloc().init()
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_("buttonClicked:")
        btn.sizeToFit()
        frame = btn.frame()
        min_width = 90.0
        if frame.size.width < min_width:
            btn.setFrameSize_((min_width, frame.size.height))
        if primary:
            # Deliberately no "\r" keyEquivalent -- see
            # docs/security-review-ui-redesign.md §5.4: hitting Enter
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

    # ------------------------------------------------------------------ #
    # Layout height (dry pass — must mirror runApproval_'s real layout)
    # ------------------------------------------------------------------ #

    def _compute_layout(self, content_width: float) -> tuple[float, float]:
        # Section order matches docs/security-review-ui-redesign.md §5:
        # WHAT (summary/preview) -> AI VISIBILITY -> RISK (PII banner) ->
        # PREVIEW (details) -> decision (buttons, laid out separately
        # below). Must stay in lockstep with build_panel()'s real layout.
        y = 22.0
        y += _KICKER_HEIGHT + 4.0
        title_h = max(24.0, _text_height(self.title, content_width - _TITLE_RIGHT_RESERVE, NSFont.boldSystemFontOfSize_(21)))
        y += title_h + 18.0
        if self.seen_count > 0:
            y += 18.0  # request-fingerprint caption row ("Seen N times this week")
        if self.preview:
            y += self._summary_height(content_width) + 18.0
        if self.visibility:
            y += 20.0  # "AI will receive" label row
            y += self._visibility_height(content_width) + 18.0
        if self.pii_categories:
            y += self._pii_banner_height(content_width) + 18.0
        if self.write_content_flags:
            y += self._content_flag_banner_height(content_width) + 18.0
        if self.claude_reason:
            y += 20.0  # "Claude says (unverified)" label row
            y += self._claude_reason_height(content_width) + 18.0
        y += 20.0  # "Preview" label row
        y += _DETAILS_HEIGHT
        return y, title_h

    # ------------------------------------------------------------------ #
    # Window construction (safe to call off the main thread, and without
    # ever showing or activating anything -- see build_panel()'s docstring)
    # ------------------------------------------------------------------ #

    def build_panel(self):
        """Build the panel and every subview it contains, with nothing shown,
        activated, or key yet -- pure construction, no side effect on window
        server state. Split out of runApproval_() specifically so tests can
        assert on the resulting view hierarchy (button set, PII tint/banner,
        summary rows, details content) without ever calling runModalForWindow_
        or needing a real interactive session -- see test_approval_window.py.

        runApproval_() is the only caller in production code; it does
        nothing but this, then the actual show/activate/modal-block/hide
        sequence.
        """
        content_width = _WINDOW_WIDTH - 2 * _MARGIN
        content_height, title_h = self._compute_layout(content_width)
        window_height = content_height + _BUTTON_ROW_HEIGHT

        screen = NSScreen.mainScreen()
        if screen is not None:
            window_height = min(window_height, screen.frame().size.height - 80.0)

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _WINDOW_WIDTH, window_height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("")
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.center()
        self.panel = panel

        content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, _WINDOW_WIDTH, window_height))
        panel.setContentView_(content)

        if self.pii_categories:
            # Full-window wash, added first so every other subview draws on
            # top of it — this is the "the popup window becomes light red"
            # signal, independent of the more specific banner text below.
            tint = _background_box(
                NSMakeRect(0, 0, _WINDOW_WIDTH, window_height),
                fill=_PII_RED.colorWithAlphaComponent_(_PII_BACKGROUND_ALPHA),
                corner_radius=0.0,
            )
            content.addSubview_(tint)

        y = 22.0

        kicker = _make_label("PrivacyFence", size=12, color=NSColor.secondaryLabelColor())
        kicker.setFrame_(NSMakeRect(_MARGIN, y, 200.0, _KICKER_HEIGHT))
        content.addSubview_(kicker)

        icon_path = _icon_path()
        if icon_path:
            image = NSImage.alloc().initWithContentsOfFile_(icon_path)
            icon_view = NSImageView.alloc().initWithFrame_(
                NSMakeRect(_WINDOW_WIDTH - _MARGIN - _ICON_SIZE, y, _ICON_SIZE, _ICON_SIZE)
            )
            icon_view.setImage_(image)
            content.addSubview_(icon_view)

        y += _KICKER_HEIGHT + 4.0

        title_field = _make_label(self.title, size=21, bold=True)
        title_field.setFrame_(NSMakeRect(_MARGIN, y, content_width - _TITLE_RIGHT_RESERVE, title_h))
        content.addSubview_(title_field)
        y += title_h + 18.0

        # Request fingerprint (docs/security-review-ui-redesign.md §7
        # Phase 2): how many times this exact (connector, tool, summary)
        # was already approved this week, from AuditLogger.recent_matches
        # -- helps a reviewer spot a routine repeat versus something novel.
        # Silent when zero (a first-time request needs no such caption).
        if self.seen_count > 0:
            seen_label = _make_label(self._seen_count_text(), size=12, color=NSColor.secondaryLabelColor())
            seen_label.setFrame_(NSMakeRect(_MARGIN, y, content_width, 16.0))
            content.addSubview_(seen_label)
            y += 18.0

        # WHAT: resources/summary box, first -- the data, not the decision,
        # is the visual lead (docs/security-review-ui-redesign.md §5.1).
        if self.preview:
            box_h = self._summary_height(content_width)
            bg = _background_box(NSMakeRect(_MARGIN, y, content_width, box_h))
            content.addSubview_(bg)
            overlay, _ = self._build_summary_overlay(y, content_width)
            content.addSubview_(overlay)
            y += box_h + 18.0

        # AI VISIBILITY: the "AI will receive" checklist, from
        # privacy_filter.category_policy() -- ground truth, not a promise
        # (§4). Never present for a write (show_popup never sets it).
        if self.visibility:
            visibility_label = _make_label("AI will receive", size=12, color=NSColor.secondaryLabelColor())
            visibility_label.setFrame_(NSMakeRect(_MARGIN, y, 200.0, 16.0))
            content.addSubview_(visibility_label)
            y += 20.0

            box_h = self._visibility_height(content_width)
            bg = _background_box(NSMakeRect(_MARGIN, y, content_width, box_h))
            content.addSubview_(bg)
            overlay, _ = self._build_visibility_overlay(y, content_width)
            content.addSubview_(overlay)
            y += box_h + 18.0

        # RISK: the PII banner, relabeled in framing (not literal text) as
        # the risk section per the design -- content unchanged from before.
        if self.pii_categories:
            banner_h = self._pii_banner_height(content_width)
            banner_bg = _background_box(
                NSMakeRect(_MARGIN, y, content_width, banner_h),
                fill=_PII_RED.colorWithAlphaComponent_(_PII_BANNER_FILL_ALPHA),
            )
            content.addSubview_(banner_bg)
            banner_label = _make_label(self._pii_banner_text(), size=13, bold=True, color=_PII_RED)
            banner_label.setFrame_(NSMakeRect(
                _MARGIN + _SUMMARY_PAD, y + _SUMMARY_PAD / 2,
                content_width - 2 * _SUMMARY_PAD, banner_h - _SUMMARY_PAD,
            ))
            content.addSubview_(banner_label)
            y += banner_h + 18.0

        # RISK (write side): content-flag banner -- informational only, no
        # confirmation gate, deliberately amber not red (see class-level
        # comment above _content_flag_banner_height). In practice never
        # renders alongside the PII banner above (gate.py only populates
        # one or the other depending on gate direction), but coded
        # independently rather than as an if/elif -- nothing here assumes
        # they're mutually exclusive.
        if self.write_content_flags:
            flag_h = self._content_flag_banner_height(content_width)
            flag_bg = _background_box(
                NSMakeRect(_MARGIN, y, content_width, flag_h),
                fill=_CONTENT_FLAG_AMBER.colorWithAlphaComponent_(_CONTENT_FLAG_FILL_ALPHA),
            )
            content.addSubview_(flag_bg)
            flag_label = _make_label(self._content_flag_banner_text(), size=13, bold=True, color=_CONTENT_FLAG_AMBER)
            flag_label.setFrame_(NSMakeRect(
                _MARGIN + _SUMMARY_PAD, y + _SUMMARY_PAD / 2,
                content_width - 2 * _SUMMARY_PAD, flag_h - _SUMMARY_PAD,
            ))
            content.addSubview_(flag_label)
            y += flag_h + 18.0

        # "Claude says" -- self-reported, unverified (see class-level
        # comment above _claude_reason_height). Its own label, its own
        # (unbolded, secondary-colored) text -- deliberately not styled
        # like the verified sections above it.
        if self.claude_reason:
            reason_label = _make_label("Claude says (unverified)", size=12, color=NSColor.secondaryLabelColor())
            reason_label.setFrame_(NSMakeRect(_MARGIN, y, 300.0, 16.0))
            content.addSubview_(reason_label)
            y += 20.0

            box_h = self._claude_reason_height(content_width)
            bg = _background_box(NSMakeRect(_MARGIN, y, content_width, box_h))
            content.addSubview_(bg)
            overlay, _ = self._build_claude_reason_overlay(y, content_width)
            content.addSubview_(overlay)
            y += box_h + 18.0

        # PREVIEW: full content, last section before the buttons -- with a
        # reading-time estimate so opening it doesn't feel like an
        # open-ended commitment (§5's "Inspect" framing).
        details_label = _make_label(
            f"Preview ({_reading_time_label(self.details_text)})", size=12, color=NSColor.secondaryLabelColor()
        )
        details_label.setFrame_(NSMakeRect(_MARGIN, y, content_width, 16.0))
        content.addSubview_(details_label)
        y += 20.0

        details_view = self._build_details_view(y, content_width)
        content.addSubview_(details_view)
        y += _DETAILS_HEIGHT

        # Button row. content is flipped (y grows downward), so the row
        # sits in the band [content_height, content_height + row height].
        accept_btn = self._build_button("Allow once", primary=True)
        button_h = accept_btn.frame().size.height
        button_y = content_height + (_BUTTON_ROW_HEIGHT - button_h) / 2.0

        deny_btn = self._build_button("Deny", danger=True)
        deny_btn.setFrameOrigin_((_MARGIN, button_y))
        content.addSubview_(deny_btn)

        right_x = _WINDOW_WIDTH - _MARGIN - accept_btn.frame().size.width
        accept_btn.setFrameOrigin_((right_x, button_y))
        content.addSubview_(accept_btn)

        if self.allow_accept_all:
            accept_all_btn = self._build_button("Always allow")
            right_x -= accept_all_btn.frame().size.width + 8.0
            accept_all_btn.setFrameOrigin_((right_x, button_y))
            content.addSubview_(accept_all_btn)

        if self.allow_temp_accept:
            temp_accept_btn = self._build_button("Allow for 5 min")
            right_x -= temp_accept_btn.frame().size.width + 8.0
            temp_accept_btn.setFrameOrigin_((right_x, button_y))
            content.addSubview_(temp_accept_btn)

        if self._details_view is not None:
            panel.setInitialFirstResponder_(self._details_view)

        return panel

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
        panel.orderOut_(None)

    def buttonClicked_(self, sender) -> None:
        # Internal result values ("accept"/"accept_all"/"accept_temp"/"deny")
        # stay as-is -- gate.py/audit_log.py/tests key on them throughout.
        # Only the button labels themselves changed, per
        # docs/security-review-ui-redesign.md §7 Phase 1a's "Allow once" /
        # "Allow for 5 min" / "Always allow" relabeling.
        title = str(sender.title())
        if title == "Always allow":
            self.result = "accept_all"
        elif title == "Allow for 5 min":
            self.result = "accept_temp"
        elif title == "Allow once":
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
    allow_temp_accept: bool = False,
    visibility: dict[str, str] | None = None,
    claude_reason: str = "",
    write_content_flags: list[str] | None = None,
    seen_count: int = 0,
) -> str:
    """Show the approval window and block until the user picks a button.

    Returns 'accept', 'deny', 'accept_all' (only reachable when
    allow_accept_all is True), or 'accept_temp' (only reachable when
    allow_temp_accept is True). Thread-safe: safe to call from any thread,
    the window itself is always built and driven on the main thread.
    """
    with _popup_lock:
        controller = ApprovalWindowController.alloc().init()
        controller.title = title
        controller.preview = preview or {}
        controller.details_text = details_text
        controller.allow_accept_all = allow_accept_all
        controller.allow_temp_accept = allow_temp_accept
        controller.pii_categories = pii_categories or []
        controller.visibility = visibility or {}
        controller.claude_reason = claude_reason or ""
        controller.write_content_flags = write_content_flags or []
        controller.seen_count = seen_count or 0

        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runApproval:", None, True
        )
        return controller.result
