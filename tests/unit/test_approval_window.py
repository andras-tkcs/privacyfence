"""Tests for approval_window.py's ApprovalWindowController -- the native
AppKit window every gated_call() review/popup decision ultimately renders
through (approval_popup.show_native_approval).

Before this
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
    _CONTENT_FLAG_FILL_ALPHA,
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
    visibility=None,
    claude_reason="",
    write_content_flags=None,
    seen_count=0,
):
    c = ApprovalWindowController.alloc().init()
    c.title = title
    c.preview = preview or {}
    c.details_text = details_text
    c.allow_accept_all = allow_accept_all
    c.allow_temp_accept = allow_temp_accept
    c.pii_categories = pii_categories or []
    c.visibility = visibility or {}
    c.claude_reason = claude_reason
    c.write_content_flags = write_content_flags or []
    c.seen_count = seen_count
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
    Deny / Allow once / Always allow / Allow for 5 min, and only the last two
    are conditional on the gate configuration. Labels per
    docs/security-review-ui-redesign.md §7 Phase 1a; the underlying result
    values ("accept"/"accept_all"/"accept_temp"/"deny") are unchanged."""

    def test_accept_and_deny_are_always_present(self):
        views = build_views(make_controller())
        titles = buttons_by_title(views)
        assert "Allow once" in titles
        assert "Deny" in titles

    def test_accept_all_present_only_when_allowed(self):
        with_it = buttons_by_title(build_views(make_controller(allow_accept_all=True)))
        without_it = buttons_by_title(build_views(make_controller(allow_accept_all=False)))
        assert "Always allow" in with_it
        assert "Always allow" not in without_it

    def test_accept_for_5_min_present_only_when_allowed(self):
        with_it = buttons_by_title(build_views(make_controller(allow_temp_accept=True)))
        without_it = buttons_by_title(build_views(make_controller(allow_temp_accept=False)))
        assert "Allow for 5 min" in with_it
        assert "Allow for 5 min" not in without_it

    def test_both_optional_buttons_can_appear_together(self):
        # gate.py never actually requests both at once (review vs. popup
        # gates are mutually exclusive), but the window itself places no
        # such restriction -- this locks in that the two buttons don't
        # collide or hide each other when combined.
        titles = buttons_by_title(build_views(make_controller(allow_accept_all=True, allow_temp_accept=True)))
        assert {"Allow once", "Deny", "Always allow", "Allow for 5 min"} <= titles.keys()

    def test_accept_has_no_enter_shortcut_but_deny_keeps_escape(self):
        # Changed deliberately (was "Accept defaults to Enter") -- see
        # docs/security-review-ui-redesign.md §5.4 and the reasoning in
        # _build_button: hitting Enter the instant the popup appears must
        # not be able to approve a request nobody has read yet. Declining
        # via Escape stays bound since that's the safe direction.
        views = build_views(make_controller())
        titles = buttons_by_title(views)
        assert titles["Allow once"].keyEquivalent() != "\r"
        assert titles["Deny"].keyEquivalent() == "\x1b"

    def test_details_view_is_the_panel_initial_first_responder(self):
        # Default focus lands on the content to read, not on a button --
        # same reasoning as the Enter-shortcut removal above.
        controller = make_controller()
        panel = controller.build_panel()
        assert panel.initialFirstResponder() is controller._details_text_view


class TestPiiTintAndBanner:
    """connector-qa-testing.md Phase 2 steps 18-19/21-23: a read popup with
    PII-flagged content must render tinted with a category banner; a plain
    popup (including every write, per gate.py's module docstring) must not.
    """

    def _boxes_with_alpha(self, views, alpha, tolerance=1e-6):
        # Matches on the box's own fillColor() alpha -- the one property
        # gate.py/approval_window.py's PII wash and banner actually control
        # (_PII_BACKGROUND_ALPHA / _PII_BANNER_FILL_ALPHA). Not matching on
        # RGB components: systemRedColor() is a dynamic, appearance-aware
        # color, so its resolved components can vary by light/dark mode and
        # accessibility settings -- alpha is the stable, code-controlled
        # signal to assert on.
        matches = []
        for v in views:
            if not isinstance(v, NSBox):
                continue
            color = v.fillColor()
            if color is None:
                continue
            if abs(color.alphaComponent() - alpha) < tolerance:
                matches.append(v)
        return matches

    def test_no_pii_categories_renders_no_red_tint_anywhere(self):
        views = build_views(make_controller(pii_categories=[]))
        assert self._boxes_with_alpha(views, _PII_BACKGROUND_ALPHA) == []
        assert self._boxes_with_alpha(views, _PII_BANNER_FILL_ALPHA) == []

    def test_pii_categories_render_a_full_window_wash_and_a_banner_box(self):
        views = build_views(make_controller(pii_categories=["US Social Security Number"]))
        assert len(self._boxes_with_alpha(views, _PII_BACKGROUND_ALPHA)) >= 1
        assert len(self._boxes_with_alpha(views, _PII_BANNER_FILL_ALPHA)) >= 1

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
        assert self._boxes_with_alpha(views, _PII_BACKGROUND_ALPHA) == []
        assert self._boxes_with_alpha(views, _PII_BANNER_FILL_ALPHA) == []


class TestContentFlagBanner:
    """The write-gate "content flags" banner -- informational only, no
    confirmation gate, deliberately distinct from the PII banner's
    alpha/color. See gate.py's write_content_flags comment."""

    def _boxes_with_alpha(self, views, alpha, tolerance=1e-6):
        matches = []
        for v in views:
            if not isinstance(v, NSBox):
                continue
            color = v.fillColor()
            if color is None:
                continue
            if abs(color.alphaComponent() - alpha) < tolerance:
                matches.append(v)
        return matches

    def test_no_flags_renders_no_amber_banner(self):
        views = build_views(make_controller(write_content_flags=[]))
        assert self._boxes_with_alpha(views, _CONTENT_FLAG_FILL_ALPHA) == []

    def test_flags_render_a_banner_box_but_no_full_window_wash(self):
        # Unlike the PII banner, this never gets the full-window red wash
        # (_PII_BACKGROUND_ALPHA) -- it's informational, not "confirm this
        # before proceeding".
        views = build_views(make_controller(write_content_flags=["IBAN (bank account number)"]))
        assert len(self._boxes_with_alpha(views, _CONTENT_FLAG_FILL_ALPHA)) >= 1
        assert self._boxes_with_alpha(views, _PII_BACKGROUND_ALPHA) == []

    def test_banner_text_names_every_flagged_category(self):
        controller = make_controller(write_content_flags=["IBAN (bank account number)", "Salary/compensation information"])
        views = build_views(controller)
        values = text_field_values(views)
        assert controller._content_flag_banner_text() in values
        assert "IBAN (bank account number)" in controller._content_flag_banner_text()
        assert "Salary/compensation information" in controller._content_flag_banner_text()

    def test_flags_and_pii_categories_use_visually_distinct_alphas(self):
        # Not the same banner styling reused for both directions -- a
        # reviewer must be able to tell "informational, write-side" apart
        # from "confirmation-gated, read-side" at a glance.
        assert _CONTENT_FLAG_FILL_ALPHA != _PII_BANNER_FILL_ALPHA


class TestClaudeSaysBlock:
    """Claude's self-reported, unverified reason for the call -- see
    gate.py's reason_scope docstring. Present for both read and write
    gates, unlike the visibility checklist."""

    def test_no_reason_renders_no_claude_says_label(self):
        views = build_views(make_controller(claude_reason=""))
        values = text_field_values(views)
        assert "Claude says (unverified)" not in values

    def test_reason_present_renders_the_label_and_text(self):
        views = build_views(make_controller(claude_reason="Summarizing the Q3 budget for the user."))
        values = text_field_values(views)
        assert "Claude says (unverified)" in values
        assert "Summarizing the Q3 budget for the user." in values


class TestRequestFingerprint:
    """The "Seen N times this week" caption -- AuditLogger.recent_matches
    surfaced. Silent on a first-time request, present for both read and
    write gates."""

    def test_zero_renders_no_caption(self):
        views = build_views(make_controller(seen_count=0))
        values = text_field_values(views)
        assert not any("this week" in v for v in values)

    def test_positive_count_renders_the_caption(self):
        views = build_views(make_controller(seen_count=3))
        values = text_field_values(views)
        assert "Seen 3 times this week" in values

    def test_singular_count_uses_singular_wording(self):
        views = build_views(make_controller(seen_count=1))
        values = text_field_values(views)
        assert "Seen 1 time this week" in values


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


class TestVisibilityChecklist:
    """The "AI will receive" checklist -- privacy_filter.category_policy()
    surfaced, not a new promise. See docs/security-review-ui-redesign.md §4."""

    def test_no_visibility_renders_no_checklist_label(self):
        views = build_views(make_controller(visibility={}))
        values = text_field_values(views)
        assert "AI will receive" not in values

    def test_visibility_present_renders_the_checklist_label(self):
        views = build_views(make_controller(visibility={"Body": "allow"}))
        values = text_field_values(views)
        assert "AI will receive" in values

    def test_each_category_renders_with_its_policy_symbol(self):
        views = build_views(make_controller(
            visibility={"Body": "allow", "Attachments": "block", "Notes": "redact"}
        ))
        values = text_field_values(views)
        assert "✓ Body" in values
        assert "✗ Attachments" in values
        assert "◐ Notes" in values

    def test_write_style_popup_never_has_visibility(self):
        # gate.py never populates visibility for a popup (write) gate in the
        # first place (see approval_popup.show_popup's docstring) -- this
        # locks in that the window itself renders nothing when it's empty,
        # the same "no independent rediscovery logic" guarantee
        # TestPiiTintAndBanner asserts for PII tinting.
        views = build_views(make_controller(visibility={}))
        values = text_field_values(views)
        assert not any(v.startswith(("✓ ", "✗ ", "◐ ")) for v in values)


class TestReadingTimeLabel:
    def test_preview_label_includes_a_reading_time_estimate(self):
        views = build_views(make_controller(details_text="word " * 400))  # ~2 min at 200wpm
        values = text_field_values(views)
        assert any(v.startswith("Preview (~") and "read" in v for v in values)

    def test_short_text_uses_seconds_not_minutes(self):
        views = build_views(make_controller(details_text="a short message"))
        values = text_field_values(views)
        assert any("sec read" in v for v in values if v.startswith("Preview"))

    def test_long_text_uses_minutes(self):
        views = build_views(make_controller(details_text="word " * 1000))  # ~5 min at 200wpm
        values = text_field_values(views)
        assert any("min read" in v for v in values if v.startswith("Preview"))


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
            ("Allow once", "accept"),
            ("Deny", "deny"),
            ("Always allow", "accept_all"),
            ("Allow for 5 min", "accept_temp"),
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
    obvious."""

    def test_pii_banner_adds_height_relative_to_no_banner(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_pii = make_controller(pii_categories=["IBAN (bank account number)"])._compute_layout(560.0)[0]
        assert with_pii > base

    def test_preview_summary_adds_height_relative_to_no_preview(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_preview = make_controller(preview={"from": "alice@example.com"})._compute_layout(560.0)[0]
        assert with_preview > base

    def test_visibility_checklist_adds_height_relative_to_none(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_visibility = make_controller(visibility={"Body": "allow"})._compute_layout(560.0)[0]
        assert with_visibility > base

    def test_content_flag_banner_adds_height_relative_to_none(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_flags = make_controller(write_content_flags=["IBAN (bank account number)"])._compute_layout(560.0)[0]
        assert with_flags > base

    def test_claude_reason_adds_height_relative_to_none(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_reason = make_controller(claude_reason="Summarizing for the user.")._compute_layout(560.0)[0]
        assert with_reason > base

    def test_seen_count_adds_height_relative_to_zero(self):
        base = make_controller()._compute_layout(560.0)[0]
        with_seen = make_controller(seen_count=3)._compute_layout(560.0)[0]
        assert with_seen > base

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
