"""Tests for the "Manage Auto-accept Rules…" window -- the replacement for
the old cascading "Auto-accept Rules" NSMenu (see menu_bar.py's
_gather_connector_sections for what feeds this).

This module is a pure view layer (see its own module docstring), so these
tests only exercise rendering/dispatch of Section/Row data supplied by
fake list_connectors/sections_for callables -- they never touch
auto_accept_rules/auto_accept_grants shape, which is menu_bar.py's job and
covered by TestGatherConnectorSections there.
"""
from __future__ import annotations

import pytest

from privacyfence.rules_manager_window import RulesManagerWindowController, Row, Section


class _FakeSender:
    def __init__(self, tag: int) -> None:
        self._tag = tag

    def tag(self) -> int:
        return self._tag


class _FakeField:
    def __init__(self, value: str) -> None:
        self._value = value

    def stringValue(self) -> str:
        return self._value


class _FakeNotification:
    def __init__(self, field: _FakeField) -> None:
        self._field = field

    def object(self) -> _FakeField:
        return self._field


@pytest.fixture
def controller():
    ctrl = RulesManagerWindowController.alloc().init()
    yield ctrl
    if ctrl.window is not None:
        ctrl.windowWillClose_(None)


def _configure(controller, connectors, sections_by_key):
    controller._configure_window(
        lambda: connectors,
        lambda key: sections_by_key.get(key, []),
    )


class TestShowWindow:
    def test_builds_window_and_selects_first_connector(self, controller):
        _configure(controller, [("gmail", "Gmail", 2), ("drive", "Drive", 0)], {})
        controller._show_window()
        assert controller.window is not None
        assert controller.selected == "gmail"

    def test_reopening_an_open_window_reuses_it(self, controller):
        _configure(controller, [("gmail", "Gmail", 0)], {})
        controller._show_window()
        first = controller.window
        controller._show_window()
        assert controller.window is first

    def test_no_connectors_leaves_nothing_selected(self, controller):
        _configure(controller, [], {})
        controller._show_window()
        assert controller.selected is None

    def test_refresh_falls_back_to_first_connector_if_selection_disappears(self, controller):
        # e.g. a connector's whole rule/grant bucket goes to zero and drops
        # out of list_connectors() between refreshes.
        connectors = [("gmail", "Gmail", 0), ("drive", "Drive", 0)]
        _configure(controller, connectors, {})
        controller._show_window()
        controller.sidebarRowClicked_(_FakeSender(1))
        assert controller.selected == "drive"

        connectors[:] = [("gmail", "Gmail", 0)]
        controller._refresh_window()
        assert controller.selected == "gmail"


class TestSidebarSelection:
    def test_clicking_a_sidebar_row_switches_selected_connector(self, controller):
        _configure(controller, [("gmail", "Gmail", 0), ("drive", "Drive", 0)], {})
        controller._show_window()
        assert controller.selected == "gmail"

        controller.sidebarRowClicked_(_FakeSender(1))
        assert controller.selected == "drive"

    def test_out_of_range_tag_is_ignored(self, controller):
        _configure(controller, [("gmail", "Gmail", 0)], {})
        controller._show_window()
        controller.sidebarRowClicked_(_FakeSender(99))
        assert controller.selected == "gmail"


class TestRowRendering:
    def test_rows_and_section_headers_render_for_selected_connector(self, controller):
        sections = {
            "gmail": [
                Section("Read message", [
                    Row("i_am_sender", False, [("✕ Remove", lambda: None)]),
                ], "+ Add rule…", lambda: None),
            ],
        }
        _configure(controller, [("gmail", "Gmail", 1)], sections)
        controller._show_window()

        doc = controller._scroll.documentView()
        titles = []
        for view in doc.subviews():
            # Link buttons (_link_button) only ever set attributedTitle, not
            # stringValue -- check that first, since NSButton (unlike
            # NSTextField) responds to both and stringValue would read back
            # empty for them.
            if hasattr(view, "attributedTitle"):
                titles.append(view.attributedTitle().string())
            elif hasattr(view, "stringValue"):
                titles.append(view.stringValue())
        assert any("Read message" in t for t in titles)
        assert any("i_am_sender" in t for t in titles)
        assert any("+ Add rule…" in t for t in titles)

    def test_no_sections_shows_empty_state(self, controller):
        _configure(controller, [("gmail", "Gmail", 0)], {"gmail": []})
        controller._show_window()

        doc = controller._scroll.documentView()
        texts = [v.stringValue() for v in doc.subviews() if hasattr(v, "stringValue")]
        assert any("Nothing here." in t for t in texts)


class TestSearch:
    def test_search_filters_out_non_matching_rows(self, controller):
        sections = {
            "gmail": [
                Section("Read message", [
                    Row("trusted_sender_domain", False, [("+ Add value…", lambda: None)]),
                    Row("acme-corp.com", True, [("✕ Remove", lambda: None)]),
                    Row("legal-partners.eu", True, [("✕ Remove", lambda: None)]),
                ], "+ Add rule…", lambda: None),
            ],
        }
        _configure(controller, [("gmail", "Gmail", 2)], sections)
        controller._show_window()

        controller.controlTextDidChange_(_FakeNotification(_FakeField("acme")))
        doc = controller._scroll.documentView()
        texts = [v.stringValue() for v in doc.subviews() if hasattr(v, "stringValue")]
        assert any("acme-corp.com" in t for t in texts)
        assert not any("legal-partners.eu" in t for t in texts)

    def test_search_matching_nothing_shows_no_matches(self, controller):
        sections = {"gmail": [Section("Read message", [Row("i_am_sender", False, [])])]}
        _configure(controller, [("gmail", "Gmail", 1)], sections)
        controller._show_window()

        controller.controlTextDidChange_(_FakeNotification(_FakeField("nonexistent")))
        doc = controller._scroll.documentView()
        texts = [v.stringValue() for v in doc.subviews() if hasattr(v, "stringValue")]
        assert any("No matches." in t for t in texts)

    def test_search_does_not_touch_the_search_field_itself(self, controller):
        # Rebuilding the search field on every keystroke would drop focus
        # and cursor position -- _rebuild_rows_only must leave it alone.
        _configure(controller, [("gmail", "Gmail", 0)], {"gmail": []})
        controller._show_window()
        field_before = controller._search_field

        controller.controlTextDidChange_(_FakeNotification(_FakeField("x")))
        assert controller._search_field is field_before


class TestRowActionDispatch:
    def test_clicking_a_row_action_invokes_its_callback(self, controller):
        calls = []
        sections = {
            "gmail": [
                Section("Read message", [
                    Row("i_am_sender", False, [("✕ Remove", lambda: calls.append("removed"))]),
                ], "+ Add rule…", lambda: calls.append("added")),
            ],
        }
        _configure(controller, [("gmail", "Gmail", 1)], sections)
        controller._show_window()

        assert controller._row_actions  # at least the Remove + Add actions were registered
        # Row actions are appended in render order: the "✕ Remove" action
        # for the one row, then the section's "+ Add rule…" action.
        controller.rowActionClicked_(_FakeSender(0))
        controller.rowActionClicked_(_FakeSender(1))
        assert calls == ["removed", "added"]

    def test_out_of_range_tag_is_ignored(self, controller):
        _configure(controller, [("gmail", "Gmail", 0)], {"gmail": []})
        controller._show_window()
        controller.rowActionClicked_(_FakeSender(999))  # must not raise


class TestWindowClose:
    def test_closing_drops_the_window_reference(self, controller):
        _configure(controller, [("gmail", "Gmail", 0)], {"gmail": []})
        controller._show_window()
        assert controller.window is not None

        controller.windowWillClose_(None)
        assert controller.window is None
        assert controller._sidebar is None
        assert controller._scroll is None
        assert controller._search_field is None

    def test_refresh_before_show_is_a_no_op(self, controller):
        _configure(controller, [("gmail", "Gmail", 0)], {"gmail": []})
        controller._refresh_window()  # must not raise despite no window yet
