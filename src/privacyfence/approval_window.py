"""Native macOS approval window (AppKit / PyObjC).

Renders the single blocking approval dialog every gated call resolves
through: a fence-shield icon top right, a bold title, a summary box with the
item's key fields (when the caller has them), and a scrollable pane for full
content. This replaces the AppleScript `display dialog` popups that used to
live in approval_popup.py — those had no room for a real layout, an icon, or
a genuinely scrollable body.

When gate.py's PII detector (pii_detector.py) flags categories in the
content of a read (review-gate) popup, the window renders a light-red wash
over the whole panel plus a warning banner naming what was found — the
visual cue that a second, explicit "Are you sure?" confirmation (approval_
popup.show_pii_confirmation_popup) is coming after Accept, not a decision by
itself. Write (popup-gate) approvals never carry pii_categories, so this
never renders for them.

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
    NSLineBorder,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSModalResponseStop,
    NSNoTitle,
    NSPanel,
    NSScreen,
    NSScrollView,
    NSStringDrawingUsesLineFragmentOrigin,
    NSTextField,
    NSTextView,
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSString

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

_popup_lock = threading.Lock()  # only one native window on screen at a time


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
        self.result = "deny"
        self.panel = None
        return self

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
    # Details (scrollable body)
    # ------------------------------------------------------------------ #

    def _build_details_view(self, y: float, width: float) -> NSScrollView:
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(_MARGIN, y, width, _DETAILS_HEIGHT))
        scroll.setBorderType_(NSLineBorder)
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(False)
        scroll.setDrawsBackground_(True)

        text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, width, _DETAILS_HEIGHT))
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setFont_(NSFont.systemFontOfSize_(13))
        text_view.setString_(self.details_text or "(no details)")
        text_view.setTextContainerInset_((6.0, 8.0))
        text_view.setVerticallyResizable_(True)
        text_view.setHorizontallyResizable_(False)
        text_view.textContainer().setContainerSize_((width, 1_000_000.0))
        text_view.textContainer().setWidthTracksTextView_(True)

        scroll.setDocumentView_(text_view)
        return scroll

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
            btn.setKeyEquivalent_("\r")
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
        y = 22.0
        y += _KICKER_HEIGHT + 4.0
        title_h = max(24.0, _text_height(self.title, content_width - _TITLE_RIGHT_RESERVE, NSFont.boldSystemFontOfSize_(21)))
        y += title_h + 18.0
        if self.pii_categories:
            y += self._pii_banner_height(content_width) + 18.0
        if self.preview:
            y += self._summary_height(content_width) + 18.0
        y += 20.0  # "Message" label row
        y += _DETAILS_HEIGHT
        return y, title_h

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

        if self.preview:
            box_h = self._summary_height(content_width)
            bg = _background_box(NSMakeRect(_MARGIN, y, content_width, box_h))
            content.addSubview_(bg)
            overlay, _ = self._build_summary_overlay(y, content_width)
            content.addSubview_(overlay)
            y += box_h + 18.0

        details_label = _make_label("Message", size=12, color=NSColor.secondaryLabelColor())
        details_label.setFrame_(NSMakeRect(_MARGIN, y, 200.0, 16.0))
        content.addSubview_(details_label)
        y += 20.0

        details_view = self._build_details_view(y, content_width)
        content.addSubview_(details_view)
        y += _DETAILS_HEIGHT

        # Button row. content is flipped (y grows downward), so the row
        # sits in the band [content_height, content_height + row height].
        accept_btn = self._build_button("Accept", primary=True)
        button_h = accept_btn.frame().size.height
        button_y = content_height + (_BUTTON_ROW_HEIGHT - button_h) / 2.0

        deny_btn = self._build_button("Deny", danger=True)
        deny_btn.setFrameOrigin_((_MARGIN, button_y))
        content.addSubview_(deny_btn)

        right_x = _WINDOW_WIDTH - _MARGIN - accept_btn.frame().size.width
        accept_btn.setFrameOrigin_((right_x, button_y))
        content.addSubview_(accept_btn)

        if self.allow_accept_all:
            accept_all_btn = self._build_button("Accept All")
            right_x -= accept_all_btn.frame().size.width + 8.0
            accept_all_btn.setFrameOrigin_((right_x, button_y))
            content.addSubview_(accept_all_btn)

        if self.allow_temp_accept:
            temp_accept_btn = self._build_button("Accept for 5 min")
            right_x -= temp_accept_btn.frame().size.width + 8.0
            temp_accept_btn.setFrameOrigin_((right_x, button_y))
            content.addSubview_(temp_accept_btn)

        panel.makeKeyAndOrderFront_(None)
        panel.setLevel_(NSFloatingWindowLevel)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        NSApplication.sharedApplication().runModalForWindow_(panel)
        panel.orderOut_(None)

    def buttonClicked_(self, sender) -> None:
        title = str(sender.title())
        if title == "Accept All":
            self.result = "accept_all"
        elif title == "Accept for 5 min":
            self.result = "accept_temp"
        elif title == "Accept":
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

        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runApproval:", None, True
        )
        return controller.result
