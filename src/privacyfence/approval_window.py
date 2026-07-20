"""Native macOS approval window (AppKit / PyObjC).

Renders the single blocking approval dialog every gated call resolves
through: a fence-shield icon top right, a bold title, and — in this fixed
order — a summary box with the item's key fields (WHAT), an "AI will
receive" checklist (AI VISIBILITY, read-gate calls only), a PII warning
banner when applicable (RISK), and a scrollable pane for full content with
a reading-time estimate (PREVIEW), before the buttons. This replaces the
AppleScript `display dialog` popups that used to live in approval_popup.py
— those had no room for a real layout, an icon, or a genuinely scrollable
body. See docs/approval-window-content-reference.md for exactly what each
tool renders.

The "AI will receive" checklist renders privacy_filter.category_policy()'s
resolved allow/redact/block per category — ground truth PrivacyFence already
computed before the popup was built, not a new claim invented for display.
Never present for a write (show_popup never sets self.visibility; see its
docstring for why).

When gate.py's PII detector (pii_detector.py) flags categories in the
content of a read (review-gate) popup, the window renders a slim red accent
bar along its left edge plus a warning card naming what was found — the
visual cue that a second, explicit "Are you sure?" confirmation (approval_
popup.show_pii_confirmation_popup) is coming after Allow once, not a
decision by itself. Write (popup-gate) approvals never carry pii_categories,
so this never renders for them. (A full-window red wash used to stand in
for the accent bar; it was dropped for diluting the Allow once/Deny
buttons' own contrast along with everything else on screen.)

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
    NSForegroundColorAttributeName,
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
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSAttributedString, NSData, NSObject, NSString
from Quartz import PDFDocument, PDFView
from WebKit import WKWebView, WKWebViewConfiguration

_WINDOW_WIDTH = 620.0
_MARGIN = 28.0
_DETAILS_HEIGHT = 280.0
# Progressive disclosure: an *area* expansion of the already-fully-visible
# details pane, not an *information* one -- this codebase's own invariant
# (approval_popup.py's module docstring: "full content is always shown
# before the decision") rules out hiding anything behind a click by
# default. Toggled by the
# "Show more"/"Show less" button next to the "Preview" label; see
# ApprovalWindowController.toggleDetailsExpanded_.
_DETAILS_HEIGHT_EXPANDED = 520.0
_ICON_SIZE = 51.0  # 150% of the original 34pt
_ICON_TITLE_GAP = 14.0
_TITLE_RIGHT_RESERVE = _ICON_SIZE + _ICON_TITLE_GAP
# Connector brand icon, top-left alongside the kicker -- a secondary
# indicator, deliberately smaller than the shield's primary brand mark.
_CONNECTOR_ICON_SIZE = 28.0
_KICKER_HEIGHT = 22.0
_SUMMARY_LABEL_WIDTH = 84.0
_SUMMARY_ROW_GAP = 9.0
_SUMMARY_PAD = 14.0
_BUTTON_ROW_HEIGHT = 66.0
_RISK_SPINE_WIDTH = 5.0  # slim left-edge accent bar, shared by both risk signals below

# Brand colors sampled from resources/icon_512.png — a fixed identity, not a
# themed value, so these stay literal rather than following light/dark mode.
_BLUE = NSColor.colorWithSRGBRed_green_blue_alpha_(0x5B / 255, 0xA4 / 255, 0xFF / 255, 1.0)

# PII warning tint. systemRedColor is a dynamic (light/dark-aware) color, so
# a low-alpha wash of it reads as "light red" in light mode and a muted red
# tint in dark mode, rather than a literal color that fights the OS theme.
_PII_RED = NSColor.systemRedColor()
# No longer produced anywhere -- kept only so tests can assert this alpha
# never reappears (it used to be a full-window wash; see _RISK_SPINE_WIDTH
# and the risk-spine blocks in _build_content_view() for what replaced it).
_PII_BACKGROUND_ALPHA = 0.10
_PII_BANNER_FILL_ALPHA = 0.16

# Write-gate content-flag banner: deliberately NOT the PII red -- this is
# an informational signal (Claude's own drafted content, no second
# confirmation gate attached), not the "possible PII flowed in from an
# external source, confirm before proceeding" signal the red tint means.
# Gets the same left-edge spine treatment as the PII case (amber instead
# of red) for glanceability -- see gate.py's write_content_flags comment
# and approval_popup.show_popup's docstring for the no-second-confirmation
# distinction that still holds.
_CONTENT_FLAG_AMBER = NSColor.systemOrangeColor()
_CONTENT_FLAG_FILL_ALPHA = 0.12

# "AI will receive" checklist symbols -- allow/redact/block from
# privacy_filter.category_policy(). No per-row color coding (deliberately):
# the symbol alone is legible without relying on systemGreen/systemRed's
# exact resolved appearance across light/dark/accessibility settings, which
# the PII banner above already uses its alpha-based (not RGB-based) test
# assertions to sidestep for the same reason.
_VISIBILITY_SYMBOL = {"allow": "✓", "redact": "◐", "block": "✗"}  # ✓ ◐ ✗

# Sensitivity badges -- a compact, at-a-glance chip per detected category
# ("🟠 Contains financial figures", "🔴 Possible personal data: IBAN"),
# rendered below the existing PII/content-flag banner text rather than
# replacing it (that text stays the detailed explanation; badges are the
# scannable summary). Colors reuse the same two constants the banners
# already use, not new ones.
_BADGE_COLOR = {"financial": _CONTENT_FLAG_AMBER, "pii": _PII_RED}
_BADGE_EMOJI = {"financial": "\U0001f7e0", "pii": "\U0001f534"}  # 🟠 🔴
_BADGE_FONT_SIZE = 11.0
_BADGE_PAD_X = 8.0
_BADGE_ROW_HEIGHT = 20.0
_BADGE_GAP = 6.0
_BADGE_ROW_GAP = 6.0

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


# Details pane: a local, self-contained HTML document rendered by a
# WKWebView instead of a plain NSTextView -- see _build_details_view.
# "%s" substitution rather than
# str.format() so the CSS's own literal "{"/"}" never need doubling. Two
# slots: an optional per-surface header (e.g. the email header below), then
# the escaped body text -- empty string for the header renders nothing.
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
  .email-header { white-space: normal; margin-bottom: 8px; }
  .email-header .row { margin-bottom: 2px; }
  .email-header .label { color: #6e6e73; margin-right: 4px; }
  .email-header .label + .label { margin-left: 16px; }
  .email-header hr { border: none; border-top: 1px solid rgba(0, 0, 0, 0.12); margin: 8px 0 0 0; }
  @media (prefers-color-scheme: dark) {
    body { color: #f2f2f2; background: #1e1e1e; }
    .email-header .label { color: #98989d; }
    .email-header hr { border-top-color: rgba(255, 255, 255, 0.15); }
  }
</style>
</head>
<body>%s%s</body>
</html>
"""

# Gmail's preview dict shape (connectors/gmail.py's _get_message/_get_thread)
# -- content_kind="email" is only ever set at those two call sites, so these
# are the only keys this ever needs to read. Never assumed to be full/valid
# HTML: values still get _html_escape()'d individually, same as the body.
_EMAIL_HEADER_TEMPLATE = """<div class="email-header">
  <div class="row"><span class="label">From:</span>%s<span class="label">To:</span>%s</div>
  <div class="row"><span class="label">Subject:</span>%s<span class="label">Date:</span>%s</div>
  <hr>
</div>"""


def _email_header_html(preview: dict[str, str]) -> str:
    """Structured From/To/Subject/Date header for content_kind="email" --
    styled like a real email instead of the generic label/value summary
    box every other surface gets. Pure function, same testability
    contract as _details_html()."""
    return _EMAIL_HEADER_TEMPLATE % (
        _html_escape(preview.get("From", "") or "(unknown)"),
        _html_escape(preview.get("To", "") or "(unknown)"),
        _html_escape(preview.get("Subject", "") or "(no subject)"),
        _html_escape(preview.get("Date", "") or "(unknown)"),
    )


def _details_html(
    details_text: str, *, preview: dict[str, str] | None = None, content_kind: str = "generic"
) -> str:
    """Self-contained HTML for the details/body pane's WKWebView.

    Plain text only -- details_text arrives already HTML-stripped (see
    html_to_text.py), so this only ever escapes and preserves whitespace,
    never renders markup someone else authored. No <script>, no external
    resources, no network: loaded via loadHTMLString_baseURL_(html, None)
    with a nil base URL, so there's nothing here for it to even attempt to
    load out to. Pure function -- directly unit-testable, the same
    "must mirror the real render" contract _compute_layout() has for
    build_panel()'s AppKit layout.

    content_kind="email" (an explicit, connector-set hint -- never guessed
    from preview's shape) prepends a structured From/To/Subject/Date header
    built from preview; every other content_kind renders body text alone,
    unchanged from before this parameter existed.
    """
    header = _email_header_html(preview or {}) if content_kind == "email" else ""
    escaped = _html_escape(details_text or "(no details)")
    return _DETAILS_HTML_TEMPLATE % (header, escaped)


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
    real logo assets aren't bundled by this change (see
    resources/connector_icons/README, if/when one exists)."""
    if not connector:
        return None
    p = Path(__file__).parent / "resources" / "connector_icons" / f"{connector}.png"
    return str(p) if p.exists() else None


def _text_height(text: str, width: float, font) -> float:
    ns = NSString.stringWithString_(text)
    rect = ns.boundingRectWithSize_options_attributes_(
        (width, 1_000_000.0),
        NSStringDrawingUsesLineFragmentOrigin,
        {NSFontAttributeName: font},
    )
    return float(rect.size.height)


def _text_width(text: str, font) -> float:
    """Single-line width, unbounded -- unlike _text_height, which measures
    height at a *fixed* width for wrapped text. Used by _badge_rows() to
    size each badge chip to its own label."""
    ns = NSString.stringWithString_(text)
    rect = ns.boundingRectWithSize_options_attributes_(
        (1_000_000.0, 1_000_000.0),
        NSStringDrawingUsesLineFragmentOrigin,
        {NSFontAttributeName: font},
    )
    return float(rect.size.width)


def _badge_kind(category: str) -> str:
    """"financial" (🟠) vs "pii" (🔴) -- which color/emoji a detected
    pii_detector.py category gets as a sensitivity badge. Currency/salary
    figures get the orange "financial" treatment, every other category
    (IBAN, tax IDs, SSNs, ...) gets red. No new detector logic -- purely
    a presentation
    split over labels pii_detector.py already returns."""
    if category in ("Financial figures (currency amounts)", "Salary/compensation information"):
        return "financial"
    return "pii"


def _badge_rows(categories: list[str], width: float) -> tuple[list[list[tuple[str, str, float]]], float]:
    """Greedy left-to-right wrap of category badges into rows that fit
    within ``width``. Returns (rows, total_height); each row is a list of
    (label, kind, badge_width) tuples. Pure function, no AppKit view
    construction -- directly testable, same "must mirror the real render"
    contract _compute_layout() has for the rest of this window's layout."""
    if not categories:
        return [], 0.0
    font = NSFont.boldSystemFontOfSize_(_BADGE_FONT_SIZE)
    rows: list[list[tuple[str, str, float]]] = []
    current: list[tuple[str, str, float]] = []
    current_x = 0.0
    for category in categories:
        kind = _badge_kind(category)
        label = f"{_BADGE_EMOJI[kind]} {category}"
        badge_w = _text_width(label, font) + 2 * _BADGE_PAD_X
        if current and current_x + badge_w > width:
            rows.append(current)
            current = []
            current_x = 0.0
        current.append((label, kind, badge_w))
        current_x += badge_w + _BADGE_GAP
    rows.append(current)
    total_h = len(rows) * _BADGE_ROW_HEIGHT + (len(rows) - 1) * _BADGE_ROW_GAP
    return rows, total_h


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
        self.content_kind: str = "generic"
        self.pdf_bytes: bytes = b""
        self.connector: str = ""
        self.result = "deny"
        self.panel = None
        self._details_view = None
        self._details_html_string = ""
        self._details_expanded = False
        self._details_height = _DETAILS_HEIGHT
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

    def _show_summary_box(self) -> bool:
        """False for content_kind="email": gmail_get_message's preview dict
        is exactly {From, To, Date, Subject} -- the same four fields
        _email_header_html() below already renders, from that same dict, as
        a structured header directly above the body. Showing the summary
        box too would put every one of those fields on screen twice. No
        other content_kind's preview is a strict subset of what its details
        pane already shows, so this only ever suppresses the box for email."""
        return bool(self.preview) and self.content_kind != "email"

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
    # category_policy() made visible, not a new promise. Read-gate
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
        return "\u26a0 Possible PII detected — review carefully:"

    # ------------------------------------------------------------------ #
    # Write-gate content-flag banner -- informational, no confirmation
    # gate attached (unlike the PII banner above). See gate.py's
    # write_content_flags comment for why this is a separate signal from
    # pii_categories.
    # ------------------------------------------------------------------ #

    def _content_flag_banner_text(self) -> str:
        return "ⓘ This message appears to contain:"

    # ------------------------------------------------------------------ #
    # Risk section: banner framing text plus its category badges, nested
    # inside one shared background box -- not two differently-styled
    # elements stacked with a gap between them (that duplicated the same
    # category names twice: once in the banner sentence, once per badge).
    # Both callers pass their own categories list explicitly (pii_categories
    # / write_content_flags) rather than this reading self.-state itself,
    # matching the two banners' own "coded independently... nothing here
    # assumes they're mutually exclusive" convention -- these two lists
    # could in principle both be non-empty at once, and each gets its own
    # card.
    # ------------------------------------------------------------------ #

    def _badges_height(self, categories: list[str], width: float) -> float:
        _, total_h = _badge_rows(categories, width)
        return total_h

    def _build_badges_view(
        self, categories: list[str], y: float, width: float, *, x: float = _MARGIN
    ) -> tuple[NSView, float]:
        rows, total_h = _badge_rows(categories, width)
        container = _FlippedView.alloc().initWithFrame_(NSMakeRect(x, y, width, total_h))
        row_y = 0.0
        for row in rows:
            row_x = 0.0
            for label, kind, badge_w in row:
                color = _BADGE_COLOR[kind]
                badge_box = _background_box(
                    NSMakeRect(row_x, row_y, badge_w, _BADGE_ROW_HEIGHT),
                    fill=color.colorWithAlphaComponent_(0.18),
                    corner_radius=_BADGE_ROW_HEIGHT / 2.0,
                )
                container.addSubview_(badge_box)
                label_field = _make_label(label, size=_BADGE_FONT_SIZE, bold=True, color=color)
                label_field.setFrame_(NSMakeRect(
                    row_x + _BADGE_PAD_X, (_BADGE_ROW_HEIGHT - 14.0) / 2.0,
                    badge_w - 2 * _BADGE_PAD_X, 14.0,
                ))
                container.addSubview_(label_field)
                row_x += badge_w + _BADGE_GAP
            row_y += _BADGE_ROW_HEIGHT + _BADGE_ROW_GAP
        return container, total_h

    def _risk_section_height(self, banner_text: str, categories: list[str], width: float) -> float:
        """Combined height of one risk card: framing banner text plus its
        inset category badges. Shared by _compute_layout() and
        _build_risk_section() so layout and the real render can never
        disagree about how tall the merged card is -- the same "must
        mirror the real render" contract _badge_rows()'s own docstring
        already calls out."""
        if not categories:
            return 0.0
        inset_width = width - 2 * _SUMMARY_PAD
        text_h = max(20.0, _text_height(banner_text, inset_width, NSFont.boldSystemFontOfSize_(13)))
        badges_h = self._badges_height(categories, inset_width)
        return text_h + _BADGE_ROW_GAP + badges_h + _SUMMARY_PAD

    def _build_risk_section(
        self, banner_text: str, categories: list[str], color, fill_alpha: float, y: float, width: float,
    ) -> tuple[NSView, float]:
        """One shared card: the risk banner's framing text (the detailed
        "review carefully"/"appears to contain" sentence, minus the
        category list it used to repeat), then its category badges nested
        directly below -- inside the same bordered/tinted box, not a
        separately-styled element underneath it."""
        inset_width = width - 2 * _SUMMARY_PAD
        text_h = max(20.0, _text_height(banner_text, inset_width, NSFont.boldSystemFontOfSize_(13)))
        card_h = self._risk_section_height(banner_text, categories, width)

        card = _FlippedView.alloc().initWithFrame_(NSMakeRect(_MARGIN, y, width, card_h))
        bg = _background_box(NSMakeRect(0, 0, width, card_h), fill=color.colorWithAlphaComponent_(fill_alpha))
        card.addSubview_(bg)

        label = _make_label(banner_text, size=13, bold=True, color=color)
        label.setFrame_(NSMakeRect(_SUMMARY_PAD, _SUMMARY_PAD / 2, inset_width, text_h))
        card.addSubview_(label)

        badges_view, _ = self._build_badges_view(categories, text_h + _BADGE_ROW_GAP, inset_width, x=_SUMMARY_PAD)
        card.addSubview_(badges_view)

        return card, card_h

    # ------------------------------------------------------------------ #
    # "Claude says" -- self-reported, unverified. See gate.py's
    # reason_scope docstring: this is Claude's own stated reason for the
    # call, never checked
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
    # Details (scrollable body): a local WKWebView rendering
    # _details_html()'s self-contained HTML document, rather than a plain
    # NSTextView. This is what unlocks real typography/wrapping control
    # and per-surface rendering (the Gmail-style structured header,
    # native PDFView) without hand-building each new layout in raw
    # AppKit constraints.
    # ------------------------------------------------------------------ #

    def _build_details_view(self, y: float, width: float) -> WKWebView | PDFView:
        # pdf_bytes is only ever non-empty when gate.py's caller (drive.py's
        # _get_file_content) already confirmed category_policy allows it --
        # see gate.py's gated_call docstring. _build_details_pdf_view still
        # falls back to the WKWebView on a corrupt/unparseable document,
        # since that condition can't be checked before this point.
        if self.pdf_bytes:
            pdf_view = self._build_details_pdf_view(y, width)
            if pdf_view is not None:
                return pdf_view
        return self._build_details_web_view(y, width)

    def _build_details_pdf_view(self, y: float, width: float) -> PDFView | None:
        data = NSData.dataWithBytes_length_(self.pdf_bytes, len(self.pdf_bytes))
        document = PDFDocument.alloc().initWithData_(data)
        if document is None:
            return None
        pdf_view = PDFView.alloc().initWithFrame_(NSMakeRect(_MARGIN, y, width, self._details_height))
        pdf_view.setDocument_(document)
        pdf_view.setAutoScales_(True)
        self._details_view = pdf_view
        return pdf_view

    def _build_details_web_view(self, y: float, width: float) -> WKWebView:
        config = WKWebViewConfiguration.alloc().init()
        # No script needed for anything this pane renders today (plain text,
        # escaped) -- see _details_html()'s docstring. Disabled explicitly,
        # not just left unused, to actually guarantee no network access and
        # no code execution from this pane, not merely happen to have none.
        config.preferences().setJavaScriptEnabled_(False)

        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(_MARGIN, y, width, self._details_height), config
        )
        html = _details_html(self.details_text, preview=self.preview, content_kind=self.content_kind)
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

    def _build_link_button(self, title: str) -> NSButton:
        """Small, borderless "link"-style control for the low-frequency,
        high-consequence standing-rule actions (Always allow / Allow for
        5 min) -- deliberately not the same pill styling as Deny/Allow
        once, so a fast, confident click aimed at the primary action
        can't land on one of these by accident. No existing precedent for
        a link-style NSButton in this codebase: built via an attributed
        title rather than a bezel style, since NSBezelStyleRounded has no
        "no border, small, underlined" variant. Dispatch is unaffected --
        buttonClicked_ keys on sender.title(), which stays the plain
        string even with an attributed title set."""
        btn = NSButton.alloc().init()
        btn.setBordered_(False)
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

    def _build_expand_toggle_button(self) -> NSButton:
        """"Show more"/"Show less" -- the details pane's progressive-
        disclosure toggle. Its own action (toggleDetailsExpanded_), not
        buttonClicked_ -- it never resolves the approval decision, so it
        must never be reachable through that title-based dispatch."""
        btn = NSButton.alloc().init()
        btn.setTitle_("Show less" if self._details_expanded else "Show more")
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.systemFontOfSize_(11))
        btn.setTarget_(self)
        btn.setAction_("toggleDetailsExpanded:")
        btn.sizeToFit()
        return btn

    # ------------------------------------------------------------------ #
    # Layout height (dry pass — must mirror runApproval_'s real layout)
    # ------------------------------------------------------------------ #

    def _compute_layout(self, content_width: float) -> tuple[float, float]:
        # Fixed section order: WHAT (summary/preview) -> AI VISIBILITY -> RISK (PII banner) ->
        # PREVIEW (details) -> decision (buttons, laid out separately
        # below). Must stay in lockstep with build_panel()'s real layout.
        y = 22.0
        y += _KICKER_HEIGHT + 4.0
        title_h = max(24.0, _text_height(self.title, content_width - _TITLE_RIGHT_RESERVE, NSFont.boldSystemFontOfSize_(21)))
        y += title_h + 18.0
        if self.seen_count > 0:
            y += 18.0  # request-fingerprint caption row ("Seen N times this week")
        if self._show_summary_box():
            y += self._summary_height(content_width) + 18.0
        if self.visibility:
            y += 20.0  # "AI will receive" label row
            y += self._visibility_height(content_width) + 18.0
        if self.pii_categories:
            y += self._risk_section_height(self._pii_banner_text(), self.pii_categories, content_width) + 18.0
        if self.write_content_flags:
            y += self._risk_section_height(
                self._content_flag_banner_text(), self.write_content_flags, content_width
            ) + 18.0
        if self.claude_reason:
            y += 20.0  # "Claude says (unverified)" label row
            y += self._claude_reason_height(content_width) + 18.0
        y += 20.0  # "Preview" label row
        y += self._details_height
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
        window_height = self._window_height(content_width)

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

        content = self._build_content_view(content_width, window_height)
        panel.setContentView_(content)
        if self._details_view is not None:
            panel.setInitialFirstResponder_(self._details_view)
        return panel

    def _window_height(self, content_width: float) -> float:
        content_height, _ = self._compute_layout(content_width)
        window_height = content_height + _BUTTON_ROW_HEIGHT
        screen = NSScreen.mainScreen()
        if screen is not None:
            window_height = min(window_height, screen.frame().size.height - 80.0)
        return window_height

    def _build_content_view(self, content_width: float, window_height: float):
        """Build the whole content view (everything build_panel() used to
        build inline) without touching the panel itself -- reused by
        toggleDetailsExpanded_'s _rebuild_content to regenerate content at a
        new details-pane height without replacing the NSPanel instance
        runModalForWindow_ is blocking on."""
        content_height, title_h = self._compute_layout(content_width)
        content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, _WINDOW_WIDTH, window_height))

        if self.pii_categories:
            # Slim, full-strength (not alpha-washed) accent bar along the
            # left edge -- replaces the old full-window red wash, which
            # diluted the Allow/Deny buttons' own contrast along with
            # everything else. The actual "what was detected" detail
            # still lives in the banner card below, unchanged by this.
            spine = _background_box(
                NSMakeRect(0, 0, _RISK_SPINE_WIDTH, window_height),
                fill=_PII_RED, corner_radius=0.0,
            )
            content.addSubview_(spine)

        if self.write_content_flags:
            # Same slim-spine treatment as the PII case above, for visual
            # consistency between the two risk signals -- amber, not red,
            # per the existing distinction. Coded independently, not
            # if/elif, matching the pattern already used for the two risk
            # cards further down (gate.py never populates both at once,
            # but nothing here assumes that).
            spine = _background_box(
                NSMakeRect(0, 0, _RISK_SPINE_WIDTH, window_height),
                fill=_CONTENT_FLAG_AMBER, corner_radius=0.0,
            )
            content.addSubview_(spine)

        y = 22.0

        # Connector brand icon (Gmail/Drive/Slack/etc.), top-left --
        # "which service is this," distinct from the shield's "this is
        # PrivacyFence" mark at top-right, which stays exactly where it
        # is below. Silently absent (no icon, no reserved space) until a
        # real logo asset exists for this connector -- see
        # _connector_icon_path()'s docstring.
        connector_icon_path = _connector_icon_path(self.connector)
        kicker_x = _MARGIN
        if connector_icon_path:
            connector_image = NSImage.alloc().initWithContentsOfFile_(connector_icon_path)
            connector_icon_view = NSImageView.alloc().initWithFrame_(
                NSMakeRect(_MARGIN, y, _CONNECTOR_ICON_SIZE, _CONNECTOR_ICON_SIZE)
            )
            connector_icon_view.setImage_(connector_image)
            content.addSubview_(connector_icon_view)
            kicker_x = _MARGIN + _CONNECTOR_ICON_SIZE + _ICON_TITLE_GAP

        kicker = _make_label("PrivacyFence", size=12, color=NSColor.secondaryLabelColor())
        kicker.setFrame_(NSMakeRect(kicker_x, y, 200.0, _KICKER_HEIGHT))
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

        # Request fingerprint: how many times this exact (connector, tool,
        # summary) was already approved this week, from
        # AuditLogger.recent_matches -- helps a reviewer spot a routine
        # repeat versus something novel. Silent when zero (a first-time
        # request needs no such caption).
        if self.seen_count > 0:
            seen_label = _make_label(self._seen_count_text(), size=12, color=NSColor.secondaryLabelColor())
            seen_label.setFrame_(NSMakeRect(_MARGIN, y, content_width, 16.0))
            content.addSubview_(seen_label)
            y += 18.0

        # WHAT: resources/summary box, first -- the data, not the decision,
        # is the visual lead. Suppressed for content_kind="email" -- see
        # _show_summary_box().
        if self._show_summary_box():
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

        # RISK: the PII banner (framing text + inset category badges, one
        # shared card -- see _build_risk_section()'s docstring for why
        # these aren't two separately-styled elements).
        if self.pii_categories:
            card, card_h = self._build_risk_section(
                self._pii_banner_text(), self.pii_categories, _PII_RED, _PII_BANNER_FILL_ALPHA,
                y, content_width,
            )
            content.addSubview_(card)
            y += card_h + 18.0

        # RISK (write side): content-flag banner -- informational only, no
        # confirmation gate, deliberately amber not red (see class-level
        # comment above _content_flag_banner_text). In practice never
        # renders alongside the PII banner above (gate.py only populates
        # one or the other depending on gate direction), but coded
        # independently rather than as an if/elif -- nothing here assumes
        # they're mutually exclusive.
        if self.write_content_flags:
            card, card_h = self._build_risk_section(
                self._content_flag_banner_text(), self.write_content_flags,
                _CONTENT_FLAG_AMBER, _CONTENT_FLAG_FILL_ALPHA, y, content_width,
            )
            content.addSubview_(card)
            y += card_h + 18.0

        # "Claude says" -- self-reported, unverified (see class-level
        # comment above _claude_reason_height). Its own label, its own
        # (unbolded, secondary-colored) text, no card/border behind it --
        # deliberately not styled like the verified sections above it,
        # which would lend it a weight it hasn't earned.
        if self.claude_reason:
            reason_label = _make_label("Claude says (unverified)", size=12, color=NSColor.secondaryLabelColor())
            reason_label.setFrame_(NSMakeRect(_MARGIN, y, 300.0, 16.0))
            content.addSubview_(reason_label)
            y += 20.0

            box_h = self._claude_reason_height(content_width)
            overlay, _ = self._build_claude_reason_overlay(y, content_width)
            content.addSubview_(overlay)
            y += box_h + 18.0

        # PREVIEW: full content, last section before the buttons -- with a
        # reading-time estimate so opening it doesn't feel like an
        # open-ended commitment (§5's "Inspect" framing). The "Show more"/
        # "Show less" toggle on the same row is progressive disclosure as
        # an *area* expansion (grows the already-fully-visible pane), not
        # an *information* one -- see toggleDetailsExpanded_'s docstring
        # for why that's the only honest reading available here.
        expand_btn = self._build_expand_toggle_button()
        expand_w = expand_btn.frame().size.width
        details_label = _make_label(
            f"Preview ({_reading_time_label(self.details_text)})", size=12, color=NSColor.secondaryLabelColor()
        )
        details_label.setFrame_(NSMakeRect(_MARGIN, y, content_width - expand_w - 8.0, 16.0))
        content.addSubview_(details_label)
        expand_btn.setFrameOrigin_((_MARGIN + content_width - expand_w, y - 4.0))
        content.addSubview_(expand_btn)
        y += 20.0

        details_view = self._build_details_view(y, content_width)
        content.addSubview_(details_view)
        y += self._details_height

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

        # Always allow / Allow for 5 min: small link-style controls
        # anchored near Deny on the left -- separated from Allow once by
        # both size and position so a fast, confident click aimed at the
        # primary action can't land on one of these standing-rule actions
        # by accident. Allow once itself keeps its far-right position,
        # untouched.
        link_x = _MARGIN + deny_btn.frame().size.width + 16.0

        if self.allow_accept_all:
            accept_all_btn = self._build_link_button("Always allow")
            link_y = content_height + (_BUTTON_ROW_HEIGHT - accept_all_btn.frame().size.height) / 2.0
            accept_all_btn.setFrameOrigin_((link_x, link_y))
            content.addSubview_(accept_all_btn)
            link_x += accept_all_btn.frame().size.width + 10.0

        if self.allow_temp_accept:
            temp_accept_btn = self._build_link_button("Allow for 5 min")
            link_y = content_height + (_BUTTON_ROW_HEIGHT - temp_accept_btn.frame().size.height) / 2.0
            temp_accept_btn.setFrameOrigin_((link_x, link_y))
            content.addSubview_(temp_accept_btn)

        return content

    def toggleDetailsExpanded_(self, _sender) -> None:
        """"Show more"/"Show less" -- progressive disclosure as an *area*
        expansion of the already-fully-visible details pane, not an
        *information* one: this codebase's own invariant (approval_popup.py's
        module docstring,
        "full content is always shown before the decision") rules out
        hiding anything behind a click by default, so this only ever grows
        or shrinks how much of that same content is visible without
        scrolling -- it never changes *what* content exists.

        Rebuilds the window's content in place rather than replacing
        self.panel: runModalForWindow_ (in runApproval_) is bound to that
        specific NSPanel instance, so swapping in a different window object
        mid-modal-session would break stopModalWithCode_'s association with
        it. Only the content view and the panel's own frame change.
        """
        self._details_expanded = not self._details_expanded
        self._details_height = _DETAILS_HEIGHT_EXPANDED if self._details_expanded else _DETAILS_HEIGHT
        self._rebuild_content()

    def _rebuild_content(self) -> None:
        content_width = _WINDOW_WIDTH - 2 * _MARGIN
        window_height = self._window_height(content_width)
        content = self._build_content_view(content_width, window_height)

        # window_height is a *content* height (what NSPanel's own
        # initWithContentRect_... takes); the window's frame is taller by
        # its title bar. frameRectForContentRect_ is the panel's own
        # conversion, not a hardcoded constant, so this stays correct
        # regardless of title bar height across macOS versions/settings.
        new_frame = self.panel.frameRectForContentRect_(NSMakeRect(0, 0, _WINDOW_WIDTH, window_height))
        old_frame = self.panel.frame()
        # NSWindow's frame origin is bottom-left (screen coordinates), so
        # keeping the window's visible top edge fixed while its height
        # changes means shifting origin.y by the same delta, in the
        # opposite direction.
        delta = new_frame.size.height - old_frame.size.height
        new_frame = NSMakeRect(old_frame.origin.x, old_frame.origin.y - delta, _WINDOW_WIDTH, new_frame.size.height)
        self.panel.setContentView_(content)
        self.panel.setFrame_display_(new_frame, True)
        if self._details_view is not None:
            self.panel.setInitialFirstResponder_(self._details_view)

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
        # Only the button labels themselves ("Allow once" / "Allow for 5
        # min" / "Always allow") are user-facing.
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
    content_kind: str = "generic",
    pdf_bytes: bytes = b"",
    connector: str = "",
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
        controller.content_kind = content_kind or "generic"
        controller.pdf_bytes = pdf_bytes or b""
        controller.connector = connector or ""

        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runApproval:", None, True
        )
        return controller.result
