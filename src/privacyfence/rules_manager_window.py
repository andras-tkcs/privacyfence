"""Native window for managing auto-accept rules and trusted-resource grants.

Replaces the old "Auto-accept Rules" cascading NSMenu (connector -> Filters
-> operation -> rule -> value -> Remove, five to seven levels deep -- see
the menu bar redesign review) with one searchable window: a sidebar of
connectors and a scrollable list of that connector's grants/rules.

Pure view layer, deliberately. This module has no idea what an
auto_accept_rule or a Trusted-resource grant actually is -- it only knows
how to render a `Section`/`Row` list and dispatch clicks back to whatever
zero-arg callables the caller attached to them. All of that domain
knowledge -- what sections exist for a connector, and what actually reads
or writes settings.yaml when a row's action fires -- stays in menu_bar.py
(see its "Rule actions"/"Grant actions" sections), which is also where
every one of those callables ultimately bottoms out, so nothing here
duplicates logic that's already unit-tested there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSScrollView,
    NSTextField,
    NSUnderlineStyleAttributeName,
    NSUnderlineStyleSingle,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from .approval_window import _FlippedView, _background_box, _make_label, _text_width

_WINDOW_WIDTH = 720.0
_WINDOW_HEIGHT = 480.0
_SIDEBAR_WIDTH = 168.0
_MARGIN = 16.0
_SEARCH_HEIGHT = 24.0
_ROW_HEIGHT = 22.0
_SECTION_HEADER_HEIGHT = 20.0
_SECTION_GAP = 14.0
_SIDEBAR_ROW_HEIGHT = 24.0


@dataclass
class Row:
    text: str
    indent: bool = False
    actions: list[tuple[str, Callable[[], None]]] = field(default_factory=list)


@dataclass
class Section:
    title: str
    rows: list[Row]
    add_label: str | None = None
    add_action: Callable[[], None] | None = None


def _link_button(title: str) -> NSButton:
    """Small, borderless "link"-style control -- same visual pattern as
    approval_window.py's _build_link_button (which is an instance method
    tied to that controller's own action dispatch and can't be reused
    directly), reimplemented here as a free function since target/action
    get wired up by the caller per-button."""
    btn = NSButton.alloc().init()
    btn.setBordered_(False)
    attrs = {
        NSFontAttributeName: NSFont.systemFontOfSize_(11),
        NSForegroundColorAttributeName: NSColor.linkColor(),
        NSUnderlineStyleAttributeName: NSUnderlineStyleSingle,
    }
    btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
    btn.sizeToFit()
    return btn


class RulesManagerWindowController(NSObject):
    """One long-lived, non-modal window -- unlike approval_window.py's
    one-shot-per-request controllers, this gets created once and reused for
    the app's whole lifetime (see menu_bar.py's _open_rules_manager)."""

    def _configure_window(self, list_connectors, sections_for) -> None:
        """list_connectors: () -> list[(key, label, count)]
        sections_for: (key: str) -> list[Section]

        Both are called fresh on every refresh -- nothing here caches
        connector/rule data, so the window never shows anything stale after
        a mutation, a background grant-name resolution, or a connector
        reauthenticating elsewhere."""
        self.list_connectors = list_connectors
        self.sections_for = sections_for
        self.window = None
        self.selected: str | None = None
        self.search: str = ""
        self._connector_keys: list[str] = []
        self._row_actions: list[Callable[[], None]] = []
        self._sidebar = None
        self._scroll = None
        self._search_field = None

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #

    def _show_window(self) -> None:
        app = NSApplication.sharedApplication()
        # Same reasoning as approval_window.py's runApproval_: a raw,
        # unbundled process defaults to Prohibited, which silently blocks
        # activateIgnoringOtherApps_ below.
        if app.activationPolicy() == NSApplicationActivationPolicyProhibited:
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        if self.window is not None:
            self._refresh_window()
            self.window.makeKeyAndOrderFront_(None)
            app.activateIgnoringOtherApps_(True)
            return

        connectors = self.list_connectors()
        self.selected = connectors[0][0] if connectors else None

        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT), style, NSBackingStoreBuffered, False
        )
        window.setTitle_("Auto-accept Rules")
        window.center()
        window.setDelegate_(self)
        self.window = window

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _WINDOW_WIDTH, _WINDOW_HEIGHT))

        search_w = _WINDOW_WIDTH - _SIDEBAR_WIDTH - 2 * _MARGIN
        search_y = _WINDOW_HEIGHT - _MARGIN - _SEARCH_HEIGHT
        search = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_SIDEBAR_WIDTH + _MARGIN, search_y, search_w, _SEARCH_HEIGHT)
        )
        search.setPlaceholderString_("Search rules…")
        search.setDelegate_(self)
        content.addSubview_(search)
        self._search_field = search

        sidebar_container = _FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, 0, _SIDEBAR_WIDTH, _WINDOW_HEIGHT)
        )
        content.addSubview_(sidebar_container)
        self._sidebar = sidebar_container

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(_SIDEBAR_WIDTH, _MARGIN, _WINDOW_WIDTH - _SIDEBAR_WIDTH - _MARGIN, search_y - 2 * _MARGIN)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        content.addSubview_(scroll)
        self._scroll = scroll

        window.setContentView_(content)
        self._refresh_window()

        window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    def windowWillClose_(self, _notification) -> None:
        # released-when-closed defaults to True for a plain alloc/init
        # window, so the NSWindow object itself is on its way out -- drop
        # our reference rather than risk reusing a deallocated one. Next
        # show() just builds a fresh window.
        self.window = None
        self._sidebar = None
        self._scroll = None
        self._search_field = None

    # ------------------------------------------------------------------ #
    # Refresh
    # ------------------------------------------------------------------ #

    def _refresh_window(self) -> None:
        """Rebuild the sidebar and the row list from current data. Never
        touches self._search_field itself, so typing in it (which calls
        _rebuild_rows_only via controlTextDidChange_, not this) never loses
        focus or cursor position."""
        if self.window is None:
            return
        connectors = self.list_connectors()
        keys = [c[0] for c in connectors]
        if self.selected not in keys and keys:
            self.selected = keys[0]

        new_sidebar = self._build_sidebar(connectors)
        parent = self._sidebar.superview() if self._sidebar is not None else None
        if parent is not None:
            parent.replaceSubview_with_(self._sidebar, new_sidebar)
        self._sidebar = new_sidebar

        self._rebuild_rows_only()

    def _rebuild_rows_only(self) -> None:
        if self.window is None or self._scroll is None:
            return
        sections = self.sections_for(self.selected) if self.selected else []
        width = self._scroll.contentSize().width
        doc = self._build_rows_view(sections, width)
        self._scroll.setDocumentView_(doc)

    # ------------------------------------------------------------------ #
    # Sidebar
    # ------------------------------------------------------------------ #

    def _build_sidebar(self, connectors: list[tuple[str, str, int]]) -> NSView:
        self._connector_keys = [c[0] for c in connectors]
        view = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, _SIDEBAR_WIDTH, _WINDOW_HEIGHT))
        y = 8.0
        for idx, (key, label, count) in enumerate(connectors):
            selected = key == self.selected
            if selected:
                box = _background_box(
                    NSMakeRect(4.0, y, _SIDEBAR_WIDTH - 8.0, _SIDEBAR_ROW_HEIGHT),
                    fill=NSColor.selectedContentBackgroundColor(),
                    corner_radius=6.0,
                )
                view.addSubview_(box)
            text = f"{label}    {count}" if count else label
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(10.0, y, _SIDEBAR_WIDTH - 10.0, _SIDEBAR_ROW_HEIGHT))
            btn.setBordered_(False)
            color = NSColor.whiteColor() if selected else NSColor.labelColor()
            attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(12.5), NSForegroundColorAttributeName: color}
            btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
            btn.setTarget_(self)
            btn.setAction_("sidebarRowClicked:")
            btn.setTag_(idx)
            view.addSubview_(btn)
            y += _SIDEBAR_ROW_HEIGHT
        return view

    def sidebarRowClicked_(self, sender) -> None:
        idx = sender.tag()
        if 0 <= idx < len(self._connector_keys):
            self.selected = self._connector_keys[idx]
            self._refresh_window()

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def controlTextDidChange_(self, notification) -> None:
        field = notification.object()
        self.search = field.stringValue() or ""
        self._rebuild_rows_only()

    # ------------------------------------------------------------------ #
    # Rows
    # ------------------------------------------------------------------ #

    def _actions_width(self, actions: list[tuple[str, Callable[[], None]]]) -> float:
        if not actions:
            return 0.0
        font = NSFont.systemFontOfSize_(11)
        total = sum(_text_width(label, font) + 18.0 for label, _cb in actions)
        return total

    def _build_rows_view(self, sections: list[Section], width: float) -> NSView:
        self._row_actions = []
        rows_view = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
        search = self.search.strip().lower()
        y = 4.0
        any_rendered = False

        for section in sections:
            title_matches = (search in section.title.lower()) if search else True
            if not search or title_matches:
                visible_rows = section.rows
            else:
                visible_rows = [r for r in section.rows if search in r.text.lower()]
            if search and not title_matches and not visible_rows:
                continue
            any_rendered = True

            if section.title:
                header = _make_label(section.title, size=11, bold=True, color=NSColor.secondaryLabelColor())
                header.setFrame_(NSMakeRect(0, y, width, 16.0))
                rows_view.addSubview_(header)
                y += _SECTION_HEADER_HEIGHT

            for row in visible_rows:
                x0 = 14.0 if row.indent else 0.0
                actions_w = self._actions_width(row.actions)
                label = _make_label(row.text, size=12.5)
                label_w = max(20.0, width - x0 - actions_w - 8.0)
                label.setFrame_(NSMakeRect(x0, y, label_w, 16.0))
                rows_view.addSubview_(label)

                ax = width - actions_w
                for action_label, callback in row.actions:
                    btn = _link_button(action_label)
                    btn.setFrameOrigin_((ax, y - 2.0))
                    self._row_actions.append(callback)
                    btn.setTarget_(self)
                    btn.setAction_("rowActionClicked:")
                    btn.setTag_(len(self._row_actions) - 1)
                    rows_view.addSubview_(btn)
                    ax += btn.frame().size.width + 10.0
                y += _ROW_HEIGHT

            if section.add_label and section.add_action:
                add_btn = _link_button(section.add_label)
                indent = 14.0 if section.rows and section.rows[-1].indent else 0.0
                add_btn.setFrameOrigin_((indent, y))
                self._row_actions.append(section.add_action)
                add_btn.setTarget_(self)
                add_btn.setAction_("rowActionClicked:")
                add_btn.setTag_(len(self._row_actions) - 1)
                rows_view.addSubview_(add_btn)
                y += _ROW_HEIGHT

            y += _SECTION_GAP

        if not any_rendered:
            empty_text = "No matches." if search else "Nothing here."
            empty = _make_label(empty_text, size=12.5, color=NSColor.secondaryLabelColor())
            empty.setFrame_(NSMakeRect(0, y, width, 16.0))
            rows_view.addSubview_(empty)
            y += _SECTION_HEADER_HEIGHT

        rows_view.setFrame_(NSMakeRect(0, 0, width, y))
        return rows_view

    def rowActionClicked_(self, sender) -> None:
        idx = sender.tag()
        if 0 <= idx < len(self._row_actions):
            self._row_actions[idx]()
