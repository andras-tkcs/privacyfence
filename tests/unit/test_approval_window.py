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
from AppKit import NSBox, NSButton, NSImageView, NSTextField
from Quartz import PDFView
from WebKit import WKWebView

from privacyfence.approval_window import (
    _CONTENT_FLAG_FILL_ALPHA,
    _MARGIN,
    _PII_BACKGROUND_ALPHA,
    _PII_BANNER_FILL_ALPHA,
    _RISK_SPINE_WIDTH,
    _WINDOW_WIDTH,
    ApprovalWindowController,
    _badge_kind,
    _badge_rows,
    _connector_icon_path,
    _details_html,
    _email_header_html,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires real AppKit/PyObjC (macOS only, matches project's macOS-only runtime)"
)


def make_controller(
    *,
    title="Read Gmail message",
    preview=None,
    details_text="ordinary, non-sensitive content",
    allow_accept_all=False,
    allow_temp_accept=False,
    pii_categories=None,
    visibility=None,
    claude_reason="",
    write_content_flags=None,
    seen_count=0,
    content_kind="generic",
    pdf_bytes=b"",
    connector="",
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
    c.content_kind = content_kind
    c.pdf_bytes = pdf_bytes
    c.connector = connector
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
    are conditional on the gate configuration. The underlying result
    values ("accept"/"accept_all"/"accept_temp"/"deny") are unchanged
    regardless of what the buttons are labeled."""

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
        # the reasoning in _build_button: hitting Enter the instant the
        # popup appears must
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
        assert panel.initialFirstResponder() is controller._details_view

    def test_always_allow_and_allow_for_5_min_are_borderless_deny_and_allow_once_are_not(self):
        # Always allow / Allow for 5 min are standing-rule actions taken
        # rarely; Deny/Allow once are the two things people do constantly.
        # The former render as small borderless/link-style controls, the
        # latter keep their full pill-button styling -- see
        # _build_link_button()'s docstring for why.
        titles = buttons_by_title(build_views(make_controller(allow_accept_all=True, allow_temp_accept=True)))
        assert titles["Always allow"].isBordered() is False
        assert titles["Allow for 5 min"].isBordered() is False
        assert titles["Allow once"].isBordered() is True
        assert titles["Deny"].isBordered() is True

    def test_always_allow_and_allow_for_5_min_sit_left_of_allow_once_near_deny(self):
        # Separated from Allow once by both size and position so a fast,
        # confident click aimed at the primary action can't land on a
        # standing-rule action by accident -- Allow once stays alone at
        # the far right.
        titles = buttons_by_title(build_views(make_controller(allow_accept_all=True, allow_temp_accept=True)))
        deny_right_edge = titles["Deny"].frame().origin.x + titles["Deny"].frame().size.width
        allow_once_left_edge = titles["Allow once"].frame().origin.x
        for name in ("Always allow", "Allow for 5 min"):
            x = titles[name].frame().origin.x
            assert x >= deny_right_edge
            assert x < allow_once_left_edge


class TestPiiTintAndBanner:
    """connector-qa-testing.md Phase 2 steps 18-19/21-23: a read popup with
    PII-flagged content must render tinted with a category banner; a plain
    popup (including every write, per gate.py's module docstring) must not.
    """

    def _boxes_with_alpha(self, views, alpha, tolerance=1e-6):
        # Matches on the box's own fillColor() alpha -- the one property
        # gate.py/approval_window.py's PII banner actually controls
        # (_PII_BANNER_FILL_ALPHA). Not matching on RGB components:
        # systemRedColor() is a dynamic, appearance-aware color, so its
        # resolved components can vary by light/dark mode and
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

    def _spine_boxes(self, views, tolerance=0.5):
        # The left-edge risk spine that replaced the old full-window wash
        # -- matched on frame geometry (flush with the window's left edge,
        # _RISK_SPINE_WIDTH wide) rather than color/alpha, since both the
        # PII and content-flag spines share this same shape.
        return [
            v for v in views
            if isinstance(v, NSBox)
            and abs(v.frame().origin.x) < tolerance
            and abs(v.frame().size.width - _RISK_SPINE_WIDTH) < tolerance
        ]

    def test_no_pii_categories_renders_no_spine_or_banner(self):
        views = build_views(make_controller(pii_categories=[]))
        assert self._boxes_with_alpha(views, _PII_BANNER_FILL_ALPHA) == []
        assert self._spine_boxes(views) == []

    def test_pii_categories_render_a_left_edge_spine_and_a_banner_box(self):
        views = build_views(make_controller(pii_categories=["US Social Security Number"]))
        assert len(self._spine_boxes(views)) >= 1
        assert len(self._boxes_with_alpha(views, _PII_BANNER_FILL_ALPHA)) >= 1

    def test_banner_text_is_framing_only_categories_live_in_the_badges(self):
        # The banner sentence used to repeat every category inline
        # ("...review carefully: X, Y") right above a badge row that named
        # them again -- see TestSensitivityBadges's docstring for why that
        # duplication was removed. The banner is now just the framing
        # sentence; category coverage is TestSensitivityBadges's job.
        controller = make_controller(pii_categories=["US Social Security Number", "IBAN (bank account number)"])
        views = build_views(controller)
        values = text_field_values(views)
        assert controller._pii_banner_text() in values
        assert controller._pii_banner_text().endswith(":")
        assert "US Social Security Number" not in controller._pii_banner_text()
        assert "IBAN (bank account number)" not in controller._pii_banner_text()

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
        assert self._spine_boxes(views) == []


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

    def _spine_boxes(self, views, tolerance=0.5):
        return [
            v for v in views
            if isinstance(v, NSBox)
            and abs(v.frame().origin.x) < tolerance
            and abs(v.frame().size.width - _RISK_SPINE_WIDTH) < tolerance
        ]

    def test_no_flags_renders_no_amber_banner(self):
        views = build_views(make_controller(write_content_flags=[]))
        assert self._boxes_with_alpha(views, _CONTENT_FLAG_FILL_ALPHA) == []
        assert self._spine_boxes(views) == []

    def test_flags_render_a_banner_box_and_a_left_edge_spine(self):
        # Content flags get the same glanceable left-edge spine treatment
        # as the PII case now (amber, not red) -- never the old
        # full-window wash (_PII_BACKGROUND_ALPHA), which neither case
        # produces anymore.
        views = build_views(make_controller(write_content_flags=["IBAN (bank account number)"]))
        assert len(self._boxes_with_alpha(views, _CONTENT_FLAG_FILL_ALPHA)) >= 1
        assert len(self._spine_boxes(views)) >= 1
        assert self._boxes_with_alpha(views, _PII_BACKGROUND_ALPHA) == []

    def test_banner_text_is_framing_only_categories_live_in_the_badges(self):
        controller = make_controller(write_content_flags=["IBAN (bank account number)", "Salary/compensation information"])
        views = build_views(controller)
        values = text_field_values(views)
        assert controller._content_flag_banner_text() in values
        assert controller._content_flag_banner_text().endswith(":")
        assert "IBAN (bank account number)" not in controller._content_flag_banner_text()
        assert "Salary/compensation information" not in controller._content_flag_banner_text()

    def test_flags_and_pii_categories_use_visually_distinct_alphas(self):
        # Not the same banner styling reused for both directions -- a
        # reviewer must be able to tell "informational, write-side" apart
        # from "confirmation-gated, read-side" at a glance.
        assert _CONTENT_FLAG_FILL_ALPHA != _PII_BANNER_FILL_ALPHA


class TestSensitivityBadges:
    """Sensitivity badges ("🟠 Contains financial figures",
    "🔴 Possible personal data: IBAN") -- a compact badge per category,
    nested inside the same card as whichever banner (PII or content-flag)
    is present, right below its now category-free framing sentence -- see
    TestRiskSectionMerge for the "one shared card" structure this and the
    banner text render inside."""

    def test_financial_categories_get_the_financial_kind(self):
        assert _badge_kind("Financial figures (currency amounts)") == "financial"
        assert _badge_kind("Salary/compensation information") == "financial"

    def test_every_other_category_gets_the_pii_kind(self):
        for category in (
            "US Social Security Number", "IBAN (bank account number)",
            "Credit card number", "IP address", "Hungarian TAJ number (social security)",
        ):
            assert _badge_kind(category) == "pii"

    def test_badge_rows_wraps_to_a_new_row_when_it_would_overflow(self):
        long_categories = [f"Category number {i} with a fairly long label" for i in range(10)]
        rows, total_h = _badge_rows(long_categories, width=300.0)
        assert len(rows) > 1
        for row in rows:
            row_width = sum(w for _, _, w in row) + (len(row) - 1) * 6.0  # _BADGE_GAP
            assert row_width <= 300.0 + 1e-6
        assert total_h > 20.0  # more than one row's worth of height

    def test_badge_rows_empty_for_no_categories(self):
        assert _badge_rows([], width=300.0) == ([], 0.0)

    def test_a_single_short_category_always_fits_on_one_row(self):
        rows, _ = _badge_rows(["IBAN (bank account number)"], width=300.0)
        assert len(rows) == 1

    def test_pii_categories_render_one_badge_per_category(self):
        controller = make_controller(
            pii_categories=["US Social Security Number", "Financial figures (currency amounts)"],
        )
        views = build_views(controller)
        values = text_field_values(views)
        assert any("US Social Security Number" in v and "\U0001f534" in v for v in values)
        assert any("Financial figures (currency amounts)" in v and "\U0001f7e0" in v for v in values)

    def test_write_content_flags_render_one_badge_per_flag(self):
        controller = make_controller(write_content_flags=["IBAN (bank account number)"])
        views = build_views(controller)
        values = text_field_values(views)
        assert any("IBAN (bank account number)" in v and "\U0001f534" in v for v in values)

    def test_no_categories_renders_no_badges(self):
        views = build_views(make_controller(pii_categories=[], write_content_flags=[]))
        values = text_field_values(views)
        assert not any("\U0001f7e0" in v or "\U0001f534" in v for v in values)


class TestRiskSectionMerge:
    """The risk banner's framing text and its category badges now render
    inside one shared card (_build_risk_section()) instead of two
    differently-styled elements stacked with a small gap between them --
    the box behind the banner text must be tall enough to also hold the
    badge row, not just the text alone, and the badges sit inset to match
    the card's own padding."""

    def _card_box(self, views, alpha, tolerance=1e-6):
        matches = [
            v for v in views
            if isinstance(v, NSBox) and v.fillColor() is not None
            and abs(v.fillColor().alphaComponent() - alpha) < tolerance
        ]
        assert len(matches) == 1, f"expected exactly one card box at alpha={alpha}, found {len(matches)}"
        return matches[0]

    def test_pii_card_box_spans_both_banner_text_and_badges(self):
        controller = make_controller(
            pii_categories=["US Social Security Number", "IBAN (bank account number)"],
        )
        views = build_views(controller)
        card_box = self._card_box(views, _PII_BANNER_FILL_ALPHA)
        content_width = _WINDOW_WIDTH - 2 * _MARGIN
        expected_h = controller._risk_section_height(
            controller._pii_banner_text(), controller.pii_categories, content_width,
        )
        assert abs(card_box.frame().size.height - expected_h) < 1.0

    def test_content_flag_card_box_spans_both_banner_text_and_badges(self):
        controller = make_controller(write_content_flags=["IBAN (bank account number)"])
        views = build_views(controller)
        card_box = self._card_box(views, _CONTENT_FLAG_FILL_ALPHA)
        content_width = _WINDOW_WIDTH - 2 * _MARGIN
        expected_h = controller._risk_section_height(
            controller._content_flag_banner_text(), controller.write_content_flags, content_width,
        )
        assert abs(card_box.frame().size.height - expected_h) < 1.0


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

    def test_reason_present_adds_no_new_background_box(self):
        # The label/text used to sit on a bordered card, borrowing the
        # same visual weight as the verified WHAT/AI-visibility sections
        # above it -- dropped so "(unverified)" isn't fighting its own
        # container. No box should appear just because claude_reason is
        # set.
        no_reason = len([v for v in build_views(make_controller(claude_reason="")) if isinstance(v, NSBox)])
        with_reason = len([
            v for v in build_views(make_controller(claude_reason="Summarizing the Q3 budget for the user."))
            if isinstance(v, NSBox)
        ])
        assert with_reason == no_reason


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


class TestConnectorIcon:
    """Per-connector brand icon (Gmail/Drive/Slack/etc.), top-left --
    degrades gracefully (no icon, no reserved layout space) for a
    connector with no matching asset; see _connector_icon_path()'s
    docstring. Real logo assets are bundled for all ALL_CONNECTORS
    entries (resources/connector_icons/), so "missing asset" is
    exercised via a connector name that isn't a real one."""

    def test_empty_connector_has_no_icon_path(self):
        assert _connector_icon_path("") is None

    def test_unknown_connector_has_no_icon_path(self):
        assert _connector_icon_path("not-a-real-connector") is None

    def test_connector_round_trips_onto_the_controller(self):
        controller = make_controller(connector="slack")
        assert controller.connector == "slack"

    def test_missing_asset_renders_the_same_view_tree_as_no_connector(self):
        # A connector name with no matching file must never change what's
        # on screen (no extra NSImageView, no shifted kicker).
        no_connector_views = build_views(make_controller(connector=""))
        unknown_connector_views = build_views(make_controller(connector="not-a-real-connector"))
        no_connector_images = [v for v in no_connector_views if isinstance(v, NSImageView)]
        unknown_connector_images = [
            v for v in unknown_connector_views if isinstance(v, NSImageView)
        ]
        assert len(no_connector_images) == len(unknown_connector_images)

    def test_real_connector_asset_adds_an_extra_image_view(self):
        # gmail.png is a bundled real asset -- naming that connector must
        # add exactly one NSImageView versus no connector at all.
        no_connector_views = build_views(make_controller(connector=""))
        with_connector_views = build_views(make_controller(connector="gmail"))
        no_connector_images = [v for v in no_connector_views if isinstance(v, NSImageView)]
        with_connector_images = [v for v in with_connector_views if isinstance(v, NSImageView)]
        assert len(with_connector_images) == len(no_connector_images) + 1


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
    surfaced, not a new promise."""

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
    """The details/body pane is a WKWebView rendering _details_html()'s
    output, not a plain NSTextView. WKWebView's own loaded content isn't
    synchronously readable
    back out the way NSTextView.string() was (loadHTMLString_baseURL_ is
    asynchronous even for local content), so these tests work at two
    levels: _details_html() directly (a pure function, same "must mirror
    the real render" contract _compute_layout() has), and
    controller._details_html_string -- the exact string build_panel()
    actually handed to loadHTMLString_baseURL_, kept on the controller
    purely for this."""

    def test_web_view_is_present_in_the_view_tree(self):
        views = build_views(make_controller())
        webviews = [v for v in views if isinstance(v, WKWebView)]
        assert len(webviews) == 1

    def test_loaded_html_holds_the_full_details_text_verbatim(self):
        long_body = "line one\n" * 500 + "the last line, still present"
        controller = make_controller(details_text=long_body)
        controller.build_panel()
        assert _details_html(long_body) == controller._details_html_string
        assert long_body in controller._details_html_string

    def test_empty_details_text_falls_back_to_a_placeholder(self):
        controller = make_controller(details_text="")
        controller.build_panel()
        assert "(no details)" in controller._details_html_string

    def test_html_escapes_markup_in_the_details_text(self):
        # details_text arrives already HTML-stripped (html_to_text.py), so
        # this is defense in depth, not a real-world "user writes HTML"
        # case -- but a literal "<script>"/"&" in a message body must never
        # be interpreted as markup by the WKWebView that renders it.
        raw = "<script>alert(1)</script> & \"quoted\" 'text'"
        html = _details_html(raw)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html

    def test_html_has_no_script_tag_and_disables_javascript(self):
        # "Keep it local and synchronous": no code execution, no network --
        # nothing in this pane should ever need JS, so it's turned off at
        # the WKWebViewConfiguration level, not just unused by omission.
        html = _details_html("some content")
        assert "<script" not in html
        views = build_views(make_controller())
        webview = next(v for v in views if isinstance(v, WKWebView))
        assert webview.configuration().preferences().javaScriptEnabled() is False

    def test_details_html_is_a_pure_function_of_its_argument(self):
        # Same guarantee _compute_layout() has: nothing here reads
        # self.-anything, so it's testable and reasoned about independent
        # of the controller/AppKit entirely.
        assert _details_html("abc") == _details_html("abc")
        assert _details_html("abc") != _details_html("xyz")


class TestProgressiveDisclosure:
    """The "Show more"/"Show less" toggle is an *area* expansion of the
    already-fully-visible details pane, not an *information* one --
    approval_popup.py's "full
    content is always shown before the decision" invariant rules out
    hiding anything by default. Toggling must resize the same NSPanel
    instance in place (not replace it), since runModalForWindow_ binds to
    a specific window object."""

    def test_starts_collapsed_with_a_show_more_button(self):
        controller = make_controller()
        views = build_views(controller)
        assert controller._details_expanded is False
        assert "Show more" in buttons_by_title(views)
        assert "Show less" not in buttons_by_title(views)

    def test_toggle_expands_and_grows_the_details_pane(self):
        controller = make_controller()
        panel = controller.build_panel()
        webview_before = controller._details_view
        height_before = webview_before.frame().size.height

        controller.toggleDetailsExpanded_(None)

        assert controller._details_expanded is True
        assert controller._details_view.frame().size.height > height_before
        # Same NSPanel instance -- never replaced.
        assert controller.panel is panel

    def test_toggle_twice_returns_to_the_original_frame(self):
        controller = make_controller()
        panel = controller.build_panel()
        original_frame = panel.frame()

        controller.toggleDetailsExpanded_(None)
        controller.toggleDetailsExpanded_(None)

        assert controller._details_expanded is False
        new_frame = panel.frame()
        assert (new_frame.origin.x, new_frame.origin.y) == (original_frame.origin.x, original_frame.origin.y)
        assert (new_frame.size.width, new_frame.size.height) == (
            original_frame.size.width, original_frame.size.height,
        )

    def test_toggle_relabels_the_button(self):
        controller = make_controller()
        controller.build_panel()

        controller.toggleDetailsExpanded_(None)

        # Read the current (rebuilt) content view directly -- build_views()
        # would call build_panel() again, discarding the toggle.
        views = list(flatten(controller.panel.contentView()))
        assert "Show less" in buttons_by_title(views)
        assert "Show more" not in buttons_by_title(views)

    def test_toggle_preserves_all_details_text(self):
        # The point of "area, not information" -- expanding must not
        # truncate or otherwise change what's in the pane, only how much
        # of it is visible without scrolling.
        long_text = "line one\n" * 500 + "the last line, still present"
        controller = make_controller(details_text=long_text)
        controller.build_panel()

        controller.toggleDetailsExpanded_(None)

        assert long_text in controller._details_html_string

    def test_toggle_keeps_the_details_view_as_initial_first_responder(self):
        controller = make_controller()
        panel = controller.build_panel()

        controller.toggleDetailsExpanded_(None)

        assert panel.initialFirstResponder() is controller._details_view


class TestEmailStyleHeader:
    """The "Email (Gmail-style)" layout: content_kind="email" (an explicit hint gate.py's
    gmail_get_message sets -- never guessed from preview's shape) prepends a
    structured From/To/Subject/Date header to the details pane, built from
    connectors/gmail.py's own preview dict shape."""

    GMAIL_PREVIEW = {"From": "alice@example.com", "To": "bob@example.com",
                      "Subject": "Q3 numbers", "Date": "2026-07-01"}

    def test_generic_content_kind_renders_no_header(self):
        # The .email-header CSS class is always defined in the static
        # <style> block -- what must be absent is the actual header <div>
        # (and its From:/To:/etc. labels), not the class name.
        html = _details_html("body text", preview=self.GMAIL_PREVIEW, content_kind="generic")
        assert '<div class="email-header">' not in html
        assert "From:" not in html

    def test_email_content_kind_renders_all_four_fields(self):
        html = _details_html("body text", preview=self.GMAIL_PREVIEW, content_kind="email")
        assert "alice@example.com" in html
        assert "bob@example.com" in html
        assert "Q3 numbers" in html
        assert "2026-07-01" in html
        assert "body text" in html  # the header supplements the body, doesn't replace it

    def test_email_header_falls_back_when_a_field_is_missing(self):
        # Defensive only -- content_kind="email" is only ever set at
        # gmail.py's _get_message call site, alongside the exact preview
        # shape above, so this should never actually happen in production.
        html = _email_header_html({"From": "alice@example.com"})
        assert "alice@example.com" in html
        assert "(unknown)" in html  # To, Date
        assert "(no subject)" in html

    def test_email_header_escapes_its_fields(self):
        html = _email_header_html({"From": "<script>alert(1)</script>"})
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_build_panel_wires_content_kind_and_preview_through(self):
        controller = make_controller(
            preview=self.GMAIL_PREVIEW, details_text="body text", content_kind="email",
        )
        controller.build_panel()
        assert "alice@example.com" in controller._details_html_string
        assert "Q3 numbers" in controller._details_html_string

    def test_email_content_kind_suppresses_the_summary_box(self):
        # gmail_get_message's preview dict is exactly {From, To, Date,
        # Subject} -- the same four fields the email header above already
        # renders from that same dict. Showing the summary box too would
        # put each of them on screen twice, so it's suppressed specifically
        # for content_kind="email" -- see _show_summary_box()'s docstring.
        views = build_views(make_controller(preview=self.GMAIL_PREVIEW, content_kind="email"))
        values = text_field_values(views)
        assert "alice@example.com" not in values
        assert "bob@example.com" not in values
        assert "Q3 numbers" not in values

    def test_generic_content_kind_still_shows_the_summary_box(self):
        # Same preview dict, but content_kind="generic" -- confirms the
        # suppression above is keyed on content_kind, not preview shape.
        views = build_views(make_controller(preview=self.GMAIL_PREVIEW, content_kind="generic"))
        values = text_field_values(views)
        assert "alice@example.com" in values

    def test_email_content_kind_reduces_computed_layout_height(self):
        with_generic = make_controller(
            preview=self.GMAIL_PREVIEW, content_kind="generic"
        )._compute_layout(560.0)[0]
        with_email = make_controller(
            preview=self.GMAIL_PREVIEW, content_kind="email"
        )._compute_layout(560.0)[0]
        assert with_email < with_generic


class TestPdfViewEmbed:
    """pdf_bytes, when non-empty, renders a native PDFView instead of
    the usual WKWebView --
    connectors/drive.py is the only caller, and only after confirming
    category_policy allows it (see gate.py's gated_call docstring); this
    layer just needs to render whatever bytes it's handed, or fall back
    cleanly if they don't parse as a real PDF."""

    # Minimal single-page PDF -- enough for PDFDocument to parse successfully
    # (confirmed via a real PDFDocument.alloc().initWithData_() call), not a
    # claim this is a spec-perfect PDF.
    VALID_PDF = (
        b"%PDF-1.1\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n"
        b"trailer << /Size 4 /Root 1 0 R >>\n"
        b"startxref\n0\n%%EOF"
    )

    def test_no_pdf_bytes_renders_the_web_view(self):
        views = build_views(make_controller(pdf_bytes=b""))
        assert any(isinstance(v, WKWebView) for v in views)
        assert not any(isinstance(v, PDFView) for v in views)

    def test_valid_pdf_bytes_renders_a_pdf_view_instead(self):
        views = build_views(make_controller(pdf_bytes=self.VALID_PDF))
        assert any(isinstance(v, PDFView) for v in views)
        assert not any(isinstance(v, WKWebView) for v in views)

    def test_pdf_view_holds_the_parsed_document(self):
        controller = make_controller(pdf_bytes=self.VALID_PDF)
        controller.build_panel()
        assert isinstance(controller._details_view, PDFView)
        assert controller._details_view.document() is not None
        assert controller._details_view.document().pageCount() == 1

    def test_pdf_view_is_the_panel_initial_first_responder(self):
        controller = make_controller(pdf_bytes=self.VALID_PDF)
        panel = controller.build_panel()
        assert panel.initialFirstResponder() is controller._details_view

    def test_garbage_pdf_bytes_falls_back_to_the_web_view(self):
        # A caller passing non-empty, non-PDF bytes shouldn't happen in
        # practice (connectors/drive.py only ever sets pdf_bytes after
        # confirming a real "application/pdf" mime type), but this pane
        # must never silently render a blank/broken PDFView instead of
        # falling back to something the reviewer can actually read.
        views = build_views(make_controller(pdf_bytes=b"not a pdf at all"))
        assert any(isinstance(v, WKWebView) for v in views)
        assert not any(isinstance(v, PDFView) for v in views)


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
