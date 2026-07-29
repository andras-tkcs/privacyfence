"""Construction-level tests for ApprovalWindowController -- the native
AppKit window every gated_call() review/popup decision ultimately renders
through (approval_popup.show_native_approval).

Before this module, approval_window.py had zero test coverage: every other
test that touches the popup layer (test_approval_popup.py, test_gate.py,
test_menu_bar.py) mocks show_native_approval itself, by design, so no test
run ever pops a real interactive dialog. That's the right call for those
modules, but it left the actual window construction -- which buttons
appear, whether the PII/content-flag card renders, whether §1/§2/§3 hold
the right content -- checked only by a human during a
docs/connector-qa-testing.md run.

These tests call ApprovalWindowController.build_panel() directly and walk
the resulting real AppKit view tree, or (for content that only lives inside
the single card-stack webview) inspect controller._details_html_string --
the exact string handed to loadHTMLString_baseURL_, since WKWebView's own
loaded content isn't synchronously readable back out. They never call
runApproval_() or anything that reaches NSApplication.runModalForWindow_()
-- build_panel() is deliberately pure construction (see its docstring), so
nothing here shows, activates, or makes key any window, and no human or
modal session is needed. That's also why this can run in CI on macos-latest
without any new Accessibility permission or interactive session: it's the
same "real framework, no blocking UI" precedent test_approval_popup_
escaping.py already established for osascript.

approval_window.py has a single rendering (``layout="narrow"``/``"wide"``,
both rendered as one WKWebView card stack) -- this file covers it,
including the couple of genuinely layout-agnostic pieces (buttonClicked_'s
title->result mapping, _connector_icon_path's pure-function contract).
"""
from __future__ import annotations

import base64
import sys

import pytest
from AppKit import NSButton
from WebKit import WKWebView

from privacyfence.approval_window import (
    _WINDOW_WIDTH,
    ApprovalWindowController,
    _connector_icon_path,
    _reading_time_label,
)
from privacyfence.approval_window_html import NARROW, WIDE

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires real AppKit/PyObjC (macOS only, matches project's macOS-only runtime)"
)


def make_controller(
    *,
    layout=NARROW,
    title="Read Calendar Event",
    preview=None,
    details_text="ordinary, non-sensitive content",
    allow_accept_all=False,
    is_read=True,
    claude_reason="Checking the event as requested.",
    visibility=None,
    new_info=None,
    pii_categories=None,
    write_content_flags=None,
    upload_forced=False,
    seen_count=0,
    temp_accept_eligible=False,
    content_kind="generic",
    pdf_bytes=b"",
    connector="",
    preview_bytes=b"",
    preview_mime_type="",
    preview_tables=None,
    preview_blocks=None,
    table_only=False,
    accept_all_hint="",
):
    c = ApprovalWindowController.alloc().init()
    c.layout = layout
    c.title = title
    c.preview = preview if preview is not None else {"Title": "PrivacyFence QA seed event [QATEST]"}
    c.details_text = details_text
    c.allow_accept_all = allow_accept_all
    c.is_read = is_read
    c.claude_reason = claude_reason
    c.visibility = visibility or {}
    c.new_info = new_info or {}
    c.pii_categories = pii_categories or []
    c.write_content_flags = write_content_flags or []
    c.upload_forced = upload_forced
    c.seen_count = seen_count
    c.temp_accept_eligible = temp_accept_eligible
    c.content_kind = content_kind
    c.pdf_bytes = pdf_bytes
    c.connector = connector
    c.preview_bytes = preview_bytes
    c.preview_mime_type = preview_mime_type
    c.preview_tables = preview_tables or []
    c.preview_blocks = preview_blocks or []
    c.table_only = table_only
    c.accept_all_hint = accept_all_hint
    return c


def flatten(view):
    """Every view in the tree rooted at ``view``, ``view`` itself included."""
    yield view
    for child in view.subviews():
        yield from flatten(child)


def build_views(controller):
    panel = controller.build_panel()
    return list(flatten(panel.contentView())), panel


def buttons_by_title(views):
    return {b.title(): b for b in views if isinstance(b, NSButton)}


_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestWindowShape:
    def test_narrow_layout_window_width(self):
        controller = make_controller(layout=NARROW)
        panel = controller.build_panel()
        assert panel.frame().size.width == _WINDOW_WIDTH[NARROW] == 610.0

    def test_wide_layout_window_width(self):
        controller = make_controller(layout=WIDE)
        panel = controller.build_panel()
        assert panel.frame().size.width == _WINDOW_WIDTH[WIDE] == 980.0

    def test_exactly_one_webview_renders_the_whole_content_area(self):
        # Everything except the native buttons lives in one WKWebView.
        views, _ = build_views(make_controller())
        webviews = [v for v in views if isinstance(v, WKWebView)]
        assert len(webviews) == 1

    def test_javascript_stays_disabled(self):
        views, _ = build_views(make_controller())
        webview = next(v for v in views if isinstance(v, WKWebView))
        assert webview.configuration().preferences().javaScriptEnabled() is False


class TestButtons:
    def test_deny_and_allow_once_are_present(self):
        views, _ = build_views(make_controller())
        titles = buttons_by_title(views)
        assert "Deny" in titles
        assert "Allow once" in titles

    def test_always_allow_present_only_when_requested(self):
        views_without, _ = build_views(make_controller(allow_accept_all=False))
        assert "Always allow" not in buttons_by_title(views_without)

        views_with, _ = build_views(make_controller(allow_accept_all=True))
        assert "Always allow" in buttons_by_title(views_with)

    def test_no_show_more_toggle_in_either_layout(self):
        # No progressive-disclosure toggle at all: every row is
        # CSS-fixed-and-truncated instead -- see
        # approval_window_html.py's module docstring.
        narrow_views, _ = build_views(make_controller(layout=NARROW))
        assert "Show more" not in buttons_by_title(narrow_views)

        wide_views, _ = build_views(make_controller(layout=WIDE))
        assert "Show more" not in buttons_by_title(wide_views)

    def test_deny_keeps_escape_and_allow_once_has_no_return_key_equivalent(self):
        # Hitting Enter the instant the popup appears must not be able to
        # approve a request nobody has read yet -- see approval_window.py's
        # module docstring. Declining via Escape stays bound since that's
        # the safe direction.
        views, _ = build_views(make_controller())
        titles = buttons_by_title(views)
        assert titles["Deny"].keyEquivalent() == "\x1b"
        assert titles["Allow once"].keyEquivalent() != "\r"


class TestAlwaysAllowVerboseLabel:
    """The Always allow button names the specific rule it would create
    (gate.py's accept_all_hint) instead of a plain, unspecific label -- see
    _build_content_view's own comment. Dispatch is tag-based (see
    TestButtonClicked), so a non-literal title here doesn't break the
    click -- these tests confirm the *display* side of that."""

    def test_hint_appends_to_the_plain_label(self):
        views, _ = build_views(make_controller(allow_accept_all=True, accept_all_hint="this folder"))
        titles = buttons_by_title(views)
        assert "Always allow — this folder" in titles
        assert "Always allow" not in titles

    def test_no_hint_keeps_the_plain_label(self):
        # The unconditional always_allow rule (e.g. gmail_create_draft) has
        # no category to name -- gate.py sends an empty hint for it, and
        # the button must stay exactly "Always allow", not "Always allow —"
        # with a dangling separator.
        views, _ = build_views(make_controller(allow_accept_all=True, accept_all_hint=""))
        titles = buttons_by_title(views)
        assert "Always allow" in titles

    def test_hint_is_ignored_when_always_allow_is_not_offered(self):
        # A hint with nothing to attach to (allow_accept_all False) must
        # never leak a floating "— this folder" button onto the row.
        views, _ = build_views(make_controller(allow_accept_all=False, accept_all_hint="this folder"))
        titles = buttons_by_title(views)
        assert not any(t.startswith("Always allow") for t in titles)

    def test_verbose_button_still_dispatches_to_accept_all_when_clicked(self):
        # End-to-end: the real (non-fake-sender) button built with a
        # verbose title still resolves correctly via its tag.
        controller = make_controller(allow_accept_all=True, accept_all_hint="this project")
        views, _ = build_views(controller)
        titles = buttons_by_title(views)
        btn = titles["Always allow — this project"]
        controller.buttonClicked_(btn)
        assert controller.result == "accept_all"


class TestCardStackContent:
    """These assert against controller._details_html_string -- the exact
    string handed to loadHTMLString_baseURL_, since WKWebView's own loaded
    content isn't synchronously readable back out."""

    def test_read_call_renders_knowledge_and_reason_sections(self):
        controller = make_controller(is_read=True)
        controller.build_panel()
        assert "What Claude already knows" in controller._details_html_string
        assert "Why Claude needs more data" in controller._details_html_string

    def test_write_call_renders_action_and_details_sections(self):
        controller = make_controller(is_read=False, title="Create Calendar Event")
        controller.build_panel()
        assert "Action to perform" in controller._details_html_string
        assert "Why Claude is doing this" in controller._details_html_string

    def test_new_info_becomes_the_disclosure_section_with_real_values(self):
        # §3 shows real values (calendar_get_event_details's Attendees/
        # Location/Description shape), not an abstract policy sentence.
        controller = make_controller(
            is_read=True, new_info={"Attendees": "Alice, Bob (organizer)", "Location": "Room 1"},
        )
        controller.build_panel()
        assert "What will be provided to Claude" in controller._details_html_string
        assert "Alice, Bob (organizer)" in controller._details_html_string
        assert "Room 1" in controller._details_html_string

    def test_visibility_is_a_fallback_when_new_info_is_empty(self):
        controller = make_controller(
            is_read=True, new_info={}, visibility={"Cell values": "allow"},
        )
        controller.build_panel()
        assert "What will be provided to Claude" in controller._details_html_string
        assert "Full cell values" in controller._details_html_string

    def test_new_info_and_visibility_rows_both_render_when_both_given(self):
        controller = make_controller(
            is_read=True, new_info={"Attendees": "Alice, Bob"}, visibility={"Cell values": "allow"},
        )
        controller.build_panel()
        assert "Alice, Bob" in controller._details_html_string
        assert "Full cell values" in controller._details_html_string

    def test_pii_categories_render_the_read_variant_risk_card(self):
        controller = make_controller(pii_categories=["Phone number"])
        controller.build_panel()
        assert "Possible PII detected" in controller._details_html_string
        assert "Phone number" in controller._details_html_string
        assert "var(--color-accent-2-100)" in controller._details_html_string

    def test_write_content_flags_render_the_write_variant_risk_card(self):
        controller = make_controller(is_read=False, write_content_flags=["Email address"])
        controller.build_panel()
        assert "Possible PII detected" in controller._details_html_string
        assert "var(--pii-w-bg)" in controller._details_html_string

    def test_upload_forced_uses_the_read_style_placeholder(self):
        controller = make_controller(
            is_read=False, write_content_flags=["Phone number"], upload_forced=True,
        )
        controller.build_panel()
        assert "var(--color-accent-2-100)" in controller._details_html_string
        assert "var(--pii-w-bg)" not in controller._details_html_string

    def test_html_escapes_markup_in_details_text(self):
        # WIDE, not NARROW's default -- NARROW doesn't render details_text at
        # all, so this needs the layout that actually shows the preview pane.
        controller = make_controller(layout=WIDE, details_text="<script>alert(1)</script>")
        controller.build_panel()
        assert "<script>alert(1)</script>" not in controller._details_html_string
        assert "&lt;script&gt;" in controller._details_html_string

    def test_narrow_layout_renders_no_preview_content_at_all(self):
        controller = make_controller(layout=NARROW, details_text="should not appear anywhere")
        controller.build_panel()
        assert "should not appear anywhere" not in controller._details_html_string

    def test_wide_layout_renders_the_preview_content(self):
        controller = make_controller(layout=WIDE, details_text="the real body text")
        controller.build_panel()
        assert "the real body text" in controller._details_html_string


class TestRequestFingerprint:
    """The "Seen N times this week" caption -- AuditLogger.recent_matches
    surfaced. Silent on a first-time request, present for both read and
    write gates."""

    def test_zero_renders_no_caption(self):
        controller = make_controller(seen_count=0)
        controller.build_panel()
        assert "this week" not in controller._details_html_string

    def test_positive_count_renders_the_caption(self):
        controller = make_controller(seen_count=3)
        controller.build_panel()
        assert "Seen 3 times this week" in controller._details_html_string

    def test_singular_count_uses_singular_wording(self):
        controller = make_controller(seen_count=1)
        controller.build_panel()
        assert "Seen 1 time this week" in controller._details_html_string

    def test_seen_count_grows_the_window(self):
        base = make_controller(seen_count=0)
        with_seen = make_controller(seen_count=3)
        assert with_seen.build_panel().frame().size.height > base.build_panel().frame().size.height


class TestImageAndPdfPreview:
    """Image/PDF preview content renders inline via a data URI (<img>/
    <embed>), never a native NSImageView/PDFView overlay. Only meaningful
    for WIDE -- NARROW has no preview pane at all to render into."""

    def test_image_preview_bytes_render_as_an_img_data_uri(self):
        controller = make_controller(
            layout=WIDE, preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
        )
        controller.build_panel()
        # The header's shield icon is also a base64 <img>, so check for the
        # *preview* image's own distinguishing base64 content specifically.
        preview_b64 = base64.b64encode(_TINY_PNG_BYTES).decode("ascii")
        assert f'<img src="data:image/png;base64,{preview_b64}"' in controller._details_html_string
        # No native NSImageView overlay for the *preview* is ever built.
        views, _ = build_views(controller)
        from AppKit import NSImageView
        assert not [v for v in views if isinstance(v, NSImageView)]

    def test_pdf_bytes_render_as_an_embed_data_uri_and_take_priority_over_image(self):
        controller = make_controller(
            layout=WIDE, pdf_bytes=b"%PDF-1.1 fake",
            preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
        )
        controller.build_panel()
        # The header's own shield-icon <img> is expected regardless -- check
        # for the *preview image's* own base64 content specifically, not
        # just any "<img... data:image/png" prefix.
        preview_b64 = base64.b64encode(_TINY_PNG_BYTES).decode("ascii")
        assert "<embed src=\"data:application/pdf;base64," in controller._details_html_string
        assert preview_b64 not in controller._details_html_string

    def test_no_pdf_view_in_the_view_tree(self):
        from Quartz import PDFView
        controller = make_controller(layout=WIDE, pdf_bytes=b"%PDF-1.1 fake")
        views, _ = build_views(controller)
        assert not [v for v in views if isinstance(v, PDFView)]


class TestHeightEstimate:
    """The window height is deterministic from field/section *counts*
    alone (see _estimate_left_column_height's docstring) -- never from how
    long any actual value is, since every row is CSS-fixed-and-truncated
    (styles.css). These pin the *direction* of the estimate (more fields/
    sections -> taller window), not exact pixel values, which are tuned
    empirically against real screenshots."""

    def test_more_preview_fields_means_a_taller_window(self):
        few = make_controller(preview={"Title": "x"})
        many = make_controller(preview={"Title": "x", "Time": "y", "Location": "z", "Notes": "w"})
        assert many.build_panel().frame().size.height > few.build_panel().frame().size.height

    def test_a_present_section_2_or_3_or_4_grows_the_window(self):
        bare = make_controller(claude_reason="", new_info={}, pii_categories=[])
        with_reason = make_controller(claude_reason="A real reason.", new_info={}, pii_categories=[])
        with_disclosure = make_controller(
            claude_reason="", new_info={"Attendees": "Alice"}, pii_categories=[],
        )
        with_pii = make_controller(claude_reason="", new_info={}, pii_categories=["Phone number"])

        bare_height = bare.build_panel().frame().size.height
        assert with_reason.build_panel().frame().size.height > bare_height
        assert with_disclosure.build_panel().frame().size.height > bare_height
        assert with_pii.build_panel().frame().size.height > bare_height

    def test_a_long_value_never_grows_the_window_only_truncates(self):
        # The core "fixed layout" contract: a value long enough to need
        # truncation (styles.css's ellipsis) must not change the window's
        # own height -- only the field *count* does.
        short = make_controller(preview={"Attendees": "Alice"})
        long = make_controller(preview={"Attendees": "Alice, " * 200})
        assert short.build_panel().frame().size.height == long.build_panel().frame().size.height


class TestWindowHeightSafetyMargin:
    """<body> is height:100vh (build_card_stack_html's flex containment),
    not a per-region pixel cap -- so a window sized to *exactly*
    _estimate_left_column_height() leaves zero room for WebKit's real
    render to land even a few px taller than the (unmeasured, round-number)
    these constants' guesses assume. A tiny dialog sitting right at
    _MIN_CONTENT_HEIGHT's floor has the least margin for that drift --
    see _HEIGHT_SAFETY_MARGIN's own comment."""

    def test_window_height_exceeds_the_raw_estimate_by_the_safety_margin_and_body_padding(self):
        # webview_height must cover both the WebKit-render-drift margin AND
        # body's own fixed vertical CSS padding (box-sizing:border-box
        # carves that out of the 100vh before .pf-scroll ever sees it) --
        # see _window_height's own comment for the real overflow this
        # was found from when only the drift margin was reserved.
        from privacyfence.approval_window import _HEIGHT_SAFETY_MARGIN
        from privacyfence.approval_window_html import BODY_VERTICAL_PADDING

        controller = make_controller(preview={"Contact": "x", "Label": "y"})
        raw_estimate = controller._estimate_left_column_height()
        webview_height = controller._window_height() - 66.0
        assert webview_height == raw_estimate + _HEIGHT_SAFETY_MARGIN + BODY_VERTICAL_PADDING

    def test_tiny_dialog_gets_real_slack_over_its_own_pinned_estimate(self):
        # The reported case: a two-row preview, no reason/disclosure/PII --
        # _MIN_CONTENT_HEIGHT's floor wins over the raw pinned estimate,
        # leaving very little native margin (12px, pre-fix) before real
        # WebKit rendering drift could trip a scrollbar on a dialog with
        # nothing that actually needs to scroll.
        controller = make_controller(
            preview={"Contact": "PrivacyFence QA Contact [QATEST]", "Label": "QATEST"},
            claude_reason="", new_info={}, pii_categories=[],
        )
        pinned = controller._pinned_height()
        webview_height = controller._window_height() - 66.0
        assert webview_height - pinned >= 24.0


class TestPreviewTables:
    def test_table_renders_in_the_wide_right_pane(self):
        controller = make_controller(
            layout=WIDE,
            preview_tables=[{"headers": ["Field", "Value"], "rows": [["Name", "Acme Corp"]]}],
        )
        controller.build_panel()
        assert "<table" in controller._details_html_string
        assert "Acme Corp" in controller._details_html_string

    def test_no_table_by_default(self):
        controller = make_controller(layout=WIDE)
        controller.build_panel()
        assert "<table" not in controller._details_html_string

    def test_table_only_suppresses_details_text_in_the_right_pane(self):
        controller = make_controller(
            layout=WIDE, details_text="should not appear in the right pane",
            preview_tables=[{"headers": ["Field"], "rows": [["x"]]}],
            table_only=True,
        )
        controller.build_panel()
        assert "should not appear in the right pane" not in controller._details_html_string
        assert "<table" in controller._details_html_string

    def test_table_only_has_no_effect_without_a_table(self):
        controller = make_controller(
            layout=WIDE, details_text="still shown", preview_tables=[], table_only=True,
        )
        controller.build_panel()
        assert "still shown" in controller._details_html_string

    def test_preview_blocks_render_and_take_priority_over_details_text(self):
        controller = make_controller(
            layout=WIDE, details_text="should not appear",
            preview_blocks=[{"type": "field", "label": "Reporter", "value": "Alice"}],
        )
        controller.build_panel()
        assert "should not appear" not in controller._details_html_string
        assert '<span class="pf-preview-label">Reporter:</span>' in controller._details_html_string


class TestButtonsDisabledUntilWebviewLoads:
    """webView_didFinishNavigation_ is what re-enables Deny/Allow once/
    Always allow once the card-stack webview has actually painted --
    loadHTMLString_baseURL_ is asynchronous even for this fully local
    document (base64 fonts, full CSS bundle), so without this a fast or
    reflexive click could resolve the decision before the reviewer has
    seen any content at all. See _build_content_view's own comment."""

    def test_buttons_start_disabled(self):
        controller = make_controller(allow_accept_all=True)
        panel = controller.build_panel()

        buttons = buttons_by_title(flatten(panel.contentView()))
        assert buttons["Deny"].isEnabled() is False
        assert buttons["Allow once"].isEnabled() is False
        assert buttons["Always allow"].isEnabled() is False

    def test_buttons_enabled_after_navigation_finishes(self):
        controller = make_controller(allow_accept_all=True)
        panel = controller.build_panel()

        controller.webView_didFinishNavigation_(controller._details_view, None)

        buttons = buttons_by_title(flatten(panel.contentView()))
        assert buttons["Deny"].isEnabled() is True
        assert buttons["Allow once"].isEnabled() is True
        assert buttons["Always allow"].isEnabled() is True

    @pytest.mark.parametrize(
        "failure_method",
        ["webView_didFailNavigation_withError_", "webView_didFailProvisionalNavigation_withError_"],
    )
    def test_buttons_enabled_even_if_navigation_fails(self, failure_method):
        # Fail-safe: a load failure must still enable the buttons rather
        # than permanently trap the reviewer in an unresponsive modal.
        controller = make_controller(allow_accept_all=True)
        panel = controller.build_panel()

        getattr(controller, failure_method)(controller._details_view, None, None)

        buttons = buttons_by_title(flatten(panel.contentView()))
        assert buttons["Deny"].isEnabled() is True
        assert buttons["Allow once"].isEnabled() is True
        assert buttons["Always allow"].isEnabled() is True


class TestConnectorIconPath:
    """_connector_icon_path degrades gracefully (no icon, no reserved
    layout space) for a connector with no matching asset -- see its own
    docstring."""

    def test_empty_connector_has_no_icon_path(self):
        assert _connector_icon_path("") is None

    def test_unknown_connector_has_no_icon_path(self):
        assert _connector_icon_path("not-a-real-connector") is None


class TestReadingTimeLabel:
    def test_short_text_uses_seconds_not_minutes(self):
        assert "sec read" in _reading_time_label("a short message")

    def test_long_text_uses_minutes(self):
        assert "min read" in _reading_time_label("word " * 1000)  # ~5 min at 200wpm


class TestButtonClicked:
    """Doesn't need build_panel() at all -- buttonClicked_ only reads
    sender.tag(), so a minimal fake sender is enough. Locks in the tag ->
    result mapping approval_popup.py's return-value contract depends on
    (show_native_approval() just returns controller.result). Dispatch is
    tag-based, not title-based -- Always allow's own displayed title now
    varies per call (accept_all_hint), so it can't be the dispatch key."""

    class _FakeSender:
        def __init__(self, tag, title="irrelevant"):
            self._tag = tag
            self._title = title

        def tag(self):
            return self._tag

        def title(self):
            return self._title

    @pytest.mark.parametrize(
        "tag,expected_result",
        [
            (1, "accept"),       # _TAG_ACCEPT
            (0, "deny"),         # _TAG_DENY
            (2, "accept_all"),   # _TAG_ACCEPT_ALL
        ],
    )
    def test_tag_maps_to_the_documented_result(self, tag, expected_result):
        controller = make_controller()
        controller.buttonClicked_(self._FakeSender(tag))
        assert controller.result == expected_result

    def test_always_allow_button_with_a_verbose_title_still_resolves_via_tag(self):
        # The whole point of tag-based dispatch: a title that no longer
        # reads as the literal word "Always allow" (accept_all_hint
        # appended) must still resolve to accept_all, not silently fall
        # through to deny.
        controller = make_controller()
        controller.buttonClicked_(self._FakeSender(2, title="Always allow — this folder"))
        assert controller.result == "accept_all"

    def test_unrecognized_tag_defaults_to_deny(self):
        # Defensive default, not a reachable case with the fixed button set
        # this window ever creates -- see _build_button.
        controller = make_controller()
        controller.buttonClicked_(self._FakeSender(99))
        assert controller.result == "deny"
