"""Tests for approval_window.py's ApprovalWindowController -- the native
AppKit window every gated_call() review/popup decision ultimately renders
through (approval_popup.show_native_approval).

Design background: docs/gate-popup-audit-testing.md Part B. Before this
module, approval_window.py had zero test coverage: every other test that
touches the popup layer (test_approval_popup.py, test_gate.py,
test_menu_bar.py) mocks show_native_approval itself, by design, so no test
run ever pops a real interactive dialog. That's the right call for those
modules, but it left the actual window construction -- which buttons
appear, whether the PII tint/banner renders, whether the summary box and
details pane hold the right content -- checked only by a human during a
docs/connector-qa-testing.md run.

These tests call ApprovalWindowController.build_panel() directly and walk
the resulting real AppKit view tree. They never call runApproval_() or
anything that reaches NSApplication.runModalForWindow_() -- build_panel()
is deliberately pure construction (see its docstring), so nothing here
shows, activates, or makes key any window, and no human or modal session is
needed. That's also why this can run in CI on macos-latest without any new
Accessibility permission or interactive session: it's the same "real
framework, no blocking UI" precedent test_approval_popup_escaping.py
already established for osascript.
"""
from __future__ import annotations

import sys

import pytest
from AppKit import NSBox, NSButton, NSScrollView, NSTextField

from privacyfence.approval_window import (
    _PII_BACKGROUND_ALPHA,
    _PII_BANNER_FILL_ALPHA,
    ApprovalWindowController,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires real AppKit/PyObjC (macOS only, matches project's macOS-only runtime)"
)


def make_controller(
    *,
    title="PrivacyFence — Read Gmail message",
    preview=None,
    details_text="ordinary, non-sensitive content",
    allow_accept_all=False,
    allow_temp_accept=False,
    pii_categories=None,
):
    c = ApprovalWindowController.alloc().init()
    c.title = title
    c.preview = preview or {}
    c.details_text = details_text
    c.allow_accept_all = allow_accept_all
    c.allow_temp_accept = allow_temp_accept
    c.pii_categories = pii_categories or []
    return c


def flatten(view):
    """Every view in the tree rooted at ``view``, ``view`` itself included."""
    yield view
    for child in view.subviews():
        yield from flatten(child)


def build_views(controller):
    panel = controller.build_panel()
    return list(flatten(panel.contentView()))


def buttons_by_title(views):
    return {b.title(): b for b in views if isinstance(b, NSButton)}


def text_field_values(views):
    return [f.stringValue() for f in views if isinstance(f, NSTextField)]


class TestButtonSet:
    """Ground rule in connector-qa-testing.md: the popup offers exactly
    Deny / Accept / Accept All / Accept for 5 min, and only the last two are
    conditional on the gate configuration."""

    def test_accept_and_deny_are_always_present(self):
        views = build_views(make_controller())
        titles = buttons_by_title(views)
        assert "Accept" in titles
        assert "Deny" in titles

    def test_accept_all_present_only_when_allowed(self):
        with_it = buttons_by_title(build_views(make_controller(allow_accept_all=True)))
        without_it = buttons_by_title(build_views(make_controller(allow_accept_all=False)))
        assert "Accept All" in with_it
        assert "Accept All" not in without_it

    def test_accept_for_5_min_present_only_when_allowed(self):
        with_it = buttons_by_title(build_views(make_controller(allow_temp_accept=True)))
        without_it = buttons_by_title(build_views(make_controller(allow_temp_accept=False)))
        assert "Accept for 5 min" in with_it
        assert "Accept for 5 min" not in without_it

    def test_both_optional_buttons_can_appear_together(self):
        # gate.py never actually requests both at once (review vs. popup
        # gates are mutually exclusive), but the window itself places no
        # such restriction -- this locks in that the two buttons don't
        # collide or hide each other when combined.
        titles = buttons_by_title(build_views(make_controller(allow_accept_all=True, allow_temp_accept=True)))
        assert {"Accept", "Deny", "Accept All", "Accept for 5 min"} <= titles.keys()

    def test_accept_defaults_to_enter_and_deny_to_escape(self):
        views = build_views(make_controller())
        titles = buttons_by_title(views)
        assert titles["Accept"].key_equivalent == "\r"
        assert titles["Deny"].key_equivalent == "\x1b"


class TestPiiTintAndBanner:
    """connector-qa-testing.md Phase 2 steps 18-19/21-23: a read popup with
    PII-flagged content must render tinted with a category banner; a plain
    popup (including every write, per gate.py's module docstring) must not.
    """

    def _pii_boxes(self, views):
        return [
            v for v in views
            if isinstance(v, NSBox) and getattr(v, "fill_color", None) is not None
            and getattr(v.fill_color, "tag", None) == "systemRed"
        ]

    def test_no_pii_categories_renders_no_red_tint_anywhere(self):
        views = build_views(make_controller(pii_categories=[]))
        assert self._pii_boxes(views) == []

    def test_pii_categories_render_a_full_window_wash_and_a_banner_box(self):
        views = build_views(make_controller(pii_categories=["US Social Security Number"]))
        red_boxes = self._pii_boxes(views)
        alphas = sorted(round(b.fill_color.a, 4) for b in red_boxes)
        assert round(_PII_BACKGROUND_ALPHA, 4) in alphas
        assert round(_PII_BANNER_FILL_ALPHA, 4) in alphas

    def test_banner_text_names_every_detected_category(self):
        controller = make_controller(pii_categories=["US Social Security Number", "IBAN (bank account number)"])
        views = build_views(controller)
        values = text_field_values(views)
        assert controller._pii_banner_text() in values
        assert "US Social Security Number" in controller._pii_banner_text()
        assert "IBAN (bank account number)" in controller._pii_banner_text()

    def test_write_style_popup_with_pii_shaped_text_in_details_still_has_no_tint(self):
        # gate.py never populates pii_categories for a popup (write) gate in
        # the first place (see gate.py's module docstring) -- this locks in
        # that the *window* has no independent tinting logic that could
        # rediscover PII from details_text on its own if that contract ever
        # slipped. pii_categories=[] is what a write call always passes.
        views = build_views(make_controller(
            details_text="His SSN is 123-45-6789 on file.", pii_categories=[],
        ))
        assert self._pii_boxes(views) == []


class TestSummaryBox:
    def test_preview_fields_all_appear_as_label_value_pairs(self):
        preview = {"from": "alice@example.com", "subject": "Q3 numbers"}
        views = build_views(make_controller(preview=preview))
        values = text_field_values(views)
        for key, value in preview.items():
            assert key in values
            assert value in values

    def test_empty_preview_renders_no_summary_labels(self):
        # With no preview dict, the only NSTextFields on screen are the
        # kicker, title, and "Message" label -- none of them should collide
        # with a value a summary row would have shown.
        views = build_views(make_controller(preview={}))
        values = text_field_values(views)
        assert "alice@example.com" not in values

    def test_non_string_preview_values_are_stringified(self):
        views = build_views(make_controller(preview={"attachments": 3}))
        assert "3" in text_field_values(views)


class TestDetailsPane:
    def test_scroll_view_document_holds_the_full_details_text_verbatim(self):
        long_body = "line one\n" * 500 + "the last line, still present"
        views = build_views(make_controller(details_text=long_body))
        scroll_views = [v for v in views if isinstance(v, NSScrollView)]
        assert len(scroll_views) == 1
        assert scroll_views[0].documentView().string() == long_body

    def test_empty_details_text_falls_back_to_a_placeholder(self):
        views = build_views(make_controller(details_text=""))
        scroll_views = [v for v in views if isinstance(v, NSScrollView)]
        assert scroll_views[0].documentView().string() == "(no details)"


class TestButtonClicked:
    """Doesn't need build_panel() at all -- buttonClicked_ only reads
    sender.title(), so a minimal fake sender is enough. Locks in the title
    -> result mapping approval_popup.py's return-value contract depends on
    (show_native_approval() just returns controller.result)."""

    class _FakeSender:
        def __init__(self, title):
            self._title = title

        def title(self):
            return self._title

    @pytest.mark.parametrize(
        "button_title,expected_result",
        [
            ("Accept", "accept"),
            ("Deny", "deny"),
            ("Accept All", "accept_all"),
            ("Accept for 5 min", "accept_temp"),
        ],
    )
    def test_title_maps_to_the_documented_result(self, button_title, expected_result):
        controller = make_controller()
        controller.buttonClicked_(self._FakeSender(button_title))
        assert controller.result == expected_result

    def test_unrecognized_title_defaults_to_deny(self):
        # Defensive default, not a reachable case with the fixed button set
        # this window ever creates -- see _build_button.
        controller = make_controller()
        controller.buttonClicked_(self._FakeSender("Something else entirely"))
        assert controller.result == "deny"


class TestComputeLayout:
    """_compute_layout is a pure function of title/pii_categories/preview --
    cheap regression coverage for the details pane quietly clipping content,
    which a human eyeballing the popup would only notice if the clip were
    obvious (see docs/gate-popup-audit-testing.md Part B)."""

    def test_pii_banner_adds_height_relative_to_no_banner(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_pii = make_controller(pii_categories=["IBAN (bank account number)"])._compute_layout(560.0)[0]
        assert with_pii > base

    def test_preview_summary_adds_height_relative_to_no_preview(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_preview = make_controller(preview={"from": "alice@example.com"})._compute_layout(560.0)[0]
        assert with_preview > base

    def test_a_longer_wrapping_title_never_shrinks_the_computed_height(self):
        short = make_controller(title="Short")._compute_layout(560.0)[0]
        long_title = "A " * 80 + "very long title that has to wrap onto several lines"
        long_ = make_controller(title=long_title)._compute_layout(560.0)[0]
        assert long_ >= short

    def test_layout_height_is_always_positive(self):
        for controller in (
            make_controller(),
            make_controller(preview={"a": "b"}, pii_categories=["IBAN (bank account number)"]),
            make_controller(title=""),
        ):
            content_height, title_h = controller._compute_layout(560.0)
            assert content_height > 0
            assert title_h > 0
