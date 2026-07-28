"""Construction-level tests for ApprovalWindowController's v2 rendering
(layout="narrow"/"wide" -- the redesigned card-stack window).

Same "build_panel() directly, walk the real AppKit view tree, never call
runModalForWindow_" contract test_approval_window.py's own tests hold for
the legacy layout (see that module's docstring) -- these are the
construction-level counterpart to approval_window_html.py's own pure-
function tests (test_approval_window_html.py), confirming the controller
actually wires the template's output into a real WKWebView/native button
set rather than just that the template itself is correct in isolation.
"""
from __future__ import annotations

import base64

import pytest
from AppKit import NSButton
from WebKit import WKWebView

from privacyfence.approval_window import (
    _V2_WINDOW_WIDTH,
    ApprovalWindowController,
)
from privacyfence.approval_window_html import NARROW, WIDE

from .test_approval_window import buttons_by_title, flatten


def make_v2_controller(
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
    return c


def build_views(controller):
    panel = controller.build_panel()
    return list(flatten(panel.contentView())), panel


_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestV2WindowShape:
    def test_narrow_layout_window_width(self):
        controller = make_v2_controller(layout=NARROW)
        panel = controller.build_panel()
        assert panel.frame().size.width == _V2_WINDOW_WIDTH[NARROW] == 610.0

    def test_wide_layout_window_width(self):
        controller = make_v2_controller(layout=WIDE)
        panel = controller.build_panel()
        assert panel.frame().size.width == _V2_WINDOW_WIDTH[WIDE] == 980.0

    def test_exactly_one_webview_renders_the_whole_content_area(self):
        # Unlike legacy (one NSBox/NSTextField per section), v2 has exactly
        # one WKWebView holding everything except the native buttons.
        views, _ = build_views(make_v2_controller())
        webviews = [v for v in views if isinstance(v, WKWebView)]
        assert len(webviews) == 1

    def test_javascript_stays_disabled(self):
        views, _ = build_views(make_v2_controller())
        webview = next(v for v in views if isinstance(v, WKWebView))
        assert webview.configuration().preferences().javaScriptEnabled() is False


class TestV2Buttons:
    def test_deny_and_allow_once_are_present(self):
        views, _ = build_views(make_v2_controller())
        titles = buttons_by_title(views)
        assert "Deny" in titles
        assert "Allow once" in titles

    def test_always_allow_present_only_when_requested(self):
        views_without, _ = build_views(make_v2_controller(allow_accept_all=False))
        assert "Always allow" not in buttons_by_title(views_without)

        views_with, _ = build_views(make_v2_controller(allow_accept_all=True))
        assert "Always allow" in buttons_by_title(views_with)

    def test_no_show_more_toggle_in_either_v2_layout(self):
        # v2 has no progressive-disclosure toggle at all (unlike legacy):
        # every row is CSS-fixed-and-truncated instead -- see
        # approval_window_html.py's module docstring.
        narrow_views, _ = build_views(make_v2_controller(layout=NARROW))
        assert "Show more" not in buttons_by_title(narrow_views)

        wide_views, _ = build_views(make_v2_controller(layout=WIDE))
        assert "Show more" not in buttons_by_title(wide_views)

    def test_deny_keeps_escape_and_allow_once_has_no_return_key_equivalent(self):
        # Same keyboard-safety contract as the legacy layout -- see
        # approval_window.py's module docstring for why. v2 reuses
        # _build_button() verbatim, this just confirms that carried over.
        views, _ = build_views(make_v2_controller())
        titles = buttons_by_title(views)
        assert titles["Deny"].keyEquivalent() == "\x1b"
        assert titles["Allow once"].keyEquivalent() != "\r"


class TestV2CardStackContent:
    """These assert against controller._details_html_string -- the exact
    string handed to loadHTMLString_baseURL_ -- since WKWebView's own loaded
    content isn't synchronously readable back out, same approach
    test_approval_window.py's own TestDetailsPane takes for the legacy
    layout's details pane."""

    def test_read_call_renders_knowledge_and_reason_sections(self):
        controller = make_v2_controller(is_read=True)
        controller.build_panel()
        assert "What Claude already knows" in controller._details_html_string
        assert "Why Claude needs more data" in controller._details_html_string

    def test_write_call_renders_action_and_details_sections(self):
        controller = make_v2_controller(is_read=False, title="Create Calendar Event")
        controller.build_panel()
        assert "Action to perform" in controller._details_html_string
        assert "Details — data to write" in controller._details_html_string

    def test_new_info_becomes_the_disclosure_section_with_real_values(self):
        # §3 shows real values (calendar_get_event_details's Attendees/
        # Location/Description shape), not an abstract policy sentence.
        controller = make_v2_controller(
            is_read=True, new_info={"Attendees": "Alice, Bob (organizer)", "Location": "Room 1"},
        )
        controller.build_panel()
        assert "What will be provided to Claude" in controller._details_html_string
        assert "Alice, Bob (organizer)" in controller._details_html_string
        assert "Room 1" in controller._details_html_string

    def test_visibility_is_a_fallback_when_new_info_is_empty(self):
        controller = make_v2_controller(
            is_read=True, new_info={}, visibility={"Cell values": "allow"},
        )
        controller.build_panel()
        assert "What will be provided to Claude" in controller._details_html_string
        assert "Full cell values" in controller._details_html_string

    def test_new_info_and_visibility_rows_both_render_when_both_given(self):
        controller = make_v2_controller(
            is_read=True, new_info={"Attendees": "Alice, Bob"}, visibility={"Cell values": "allow"},
        )
        controller.build_panel()
        assert "Alice, Bob" in controller._details_html_string
        assert "Full cell values" in controller._details_html_string

    def test_pii_categories_render_the_read_variant_risk_card(self):
        controller = make_v2_controller(pii_categories=["Phone number"])
        controller.build_panel()
        assert "Possible PII detected" in controller._details_html_string
        assert "Phone number" in controller._details_html_string
        assert "var(--color-accent-2-100)" in controller._details_html_string

    def test_write_content_flags_render_the_write_variant_risk_card(self):
        controller = make_v2_controller(is_read=False, write_content_flags=["Email address"])
        controller.build_panel()
        assert "Possible PII detected" in controller._details_html_string
        assert "var(--pii-w-bg)" in controller._details_html_string

    def test_upload_forced_uses_the_read_style_placeholder(self):
        controller = make_v2_controller(
            is_read=False, write_content_flags=["Phone number"], upload_forced=True,
        )
        controller.build_panel()
        assert "var(--color-accent-2-100)" in controller._details_html_string
        assert "var(--pii-w-bg)" not in controller._details_html_string

    def test_html_escapes_markup_in_details_text(self):
        # WIDE, not NARROW's default -- NARROW doesn't render details_text at
        # all, so this needs the layout that actually shows the preview pane.
        controller = make_v2_controller(layout=WIDE, details_text="<script>alert(1)</script>")
        controller.build_panel()
        assert "<script>alert(1)</script>" not in controller._details_html_string
        assert "&lt;script&gt;" in controller._details_html_string

    def test_narrow_layout_renders_no_preview_content_at_all(self):
        controller = make_v2_controller(layout=NARROW, details_text="should not appear anywhere")
        controller.build_panel()
        assert "should not appear anywhere" not in controller._details_html_string

    def test_wide_layout_renders_the_preview_content(self):
        controller = make_v2_controller(layout=WIDE, details_text="the real body text")
        controller.build_panel()
        assert "the real body text" in controller._details_html_string


class TestV2ImageAndPdfPreview:
    """v2 renders image/PDF preview content inline via a data URI (<img>/
    <embed>), not a native NSImageView/PDFView overlay -- see
    _build_content_view_v2's docstring for why that's simpler here than in
    the legacy layout. Only meaningful for WIDE -- NARROW has no preview
    pane at all to render into."""

    def test_image_preview_bytes_render_as_an_img_data_uri(self):
        controller = make_v2_controller(
            layout=WIDE, preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
        )
        controller.build_panel()
        # The header's shield icon is also a base64 <img>, so check for the
        # *preview* image's own distinguishing base64 content specifically.
        preview_b64 = base64.b64encode(_TINY_PNG_BYTES).decode("ascii")
        assert f'<img src="data:image/png;base64,{preview_b64}"' in controller._details_html_string
        # No native NSImageView overlay for the *preview* -- v2 never builds
        # one at all (unlike the legacy layout's _build_details_image_view).
        views, _ = build_views(controller)
        from AppKit import NSImageView
        assert not [v for v in views if isinstance(v, NSImageView)]

    def test_pdf_bytes_render_as_an_embed_data_uri_and_take_priority_over_image(self):
        controller = make_v2_controller(
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

    def test_no_pdf_view_in_the_v2_tree(self):
        # Unlike the legacy layout's _build_details_pdf_view -- v2 never
        # builds a native PDFView at all.
        from Quartz import PDFView
        controller = make_v2_controller(layout=WIDE, pdf_bytes=b"%PDF-1.1 fake")
        views, _ = build_views(controller)
        assert not [v for v in views if isinstance(v, PDFView)]


class TestV2HeightEstimate:
    """The window height is deterministic from field/section *counts*
    alone (see _estimate_left_column_height's docstring) -- never from how
    long any actual value is, since every row is CSS-fixed-and-truncated
    (styles.css). These pin the *direction* of the estimate (more fields/
    sections -> taller window), not exact pixel values, which are tuned
    empirically against real screenshots."""

    def test_more_preview_fields_means_a_taller_window(self):
        few = make_v2_controller(preview={"Title": "x"})
        many = make_v2_controller(preview={"Title": "x", "Time": "y", "Location": "z", "Notes": "w"})
        assert many.build_panel().frame().size.height > few.build_panel().frame().size.height

    def test_a_present_section_2_or_3_or_4_grows_the_window(self):
        bare = make_v2_controller(claude_reason="", new_info={}, pii_categories=[])
        with_reason = make_v2_controller(claude_reason="A real reason.", new_info={}, pii_categories=[])
        with_disclosure = make_v2_controller(
            claude_reason="", new_info={"Attendees": "Alice"}, pii_categories=[],
        )
        with_pii = make_v2_controller(claude_reason="", new_info={}, pii_categories=["Phone number"])

        bare_height = bare.build_panel().frame().size.height
        assert with_reason.build_panel().frame().size.height > bare_height
        assert with_disclosure.build_panel().frame().size.height > bare_height
        assert with_pii.build_panel().frame().size.height > bare_height

    def test_a_long_value_never_grows_the_window_only_truncates(self):
        # The core "fixed layout" contract: a value long enough to need
        # truncation (styles.css's ellipsis) must not change the window's
        # own height -- only the field *count* does.
        short = make_v2_controller(preview={"Attendees": "Alice"})
        long = make_v2_controller(preview={"Attendees": "Alice, " * 200})
        assert short.build_panel().frame().size.height == long.build_panel().frame().size.height


class TestV2ColumnsMaxHeight:
    """0 when the window already fits its own estimated content (the
    common case); otherwise the real space left for §3 alone once the
    window's height was actually trimmed below that estimate -- header/
    §1/§2/the risk card are pinned and always get their full
    _pinned_height_v2(), never capped -- see _columns_max_height's own
    docstring."""

    def test_zero_when_webview_height_already_fits_the_estimate(self):
        controller = make_v2_controller(preview={"Title": "x"}, new_info={"X": "y"})
        natural = controller._estimate_left_column_height()
        assert controller._columns_max_height(natural) == 0.0
        assert controller._columns_max_height(natural + 50.0) == 0.0

    def test_positive_when_webview_height_is_smaller_than_the_estimate(self):
        controller = make_v2_controller(
            preview={"Title": "x"}, seen_count=3, new_info={"X": "y"},
        )
        natural = controller._estimate_left_column_height()
        capped_webview_height = natural - 20.0
        result = controller._columns_max_height(capped_webview_height)
        assert result == capped_webview_height - controller._pinned_height_v2()
        # Header/§1/§2 always keep their full height -- only §3's own
        # budget shrinks.
        assert result == controller._scrollable_height_v2() - 20.0

    def test_never_negative(self):
        controller = make_v2_controller(preview={"Title": "x"}, new_info={"X": "y"})
        natural = controller._estimate_left_column_height()
        # A webview_height smaller than even just the pinned portion itself.
        result = controller._columns_max_height(natural - (natural + 1000.0))
        assert result == 0.0


class TestV2PreviewTables:
    def test_table_renders_in_the_wide_right_pane(self):
        controller = make_v2_controller(
            layout=WIDE,
            preview_tables=[{"headers": ["Field", "Value"], "rows": [["Name", "Acme Corp"]]}],
        )
        controller.build_panel()
        assert "<table" in controller._details_html_string
        assert "Acme Corp" in controller._details_html_string

    def test_no_table_by_default(self):
        controller = make_v2_controller(layout=WIDE)
        controller.build_panel()
        assert "<table" not in controller._details_html_string

    def test_table_only_suppresses_details_text_in_the_right_pane(self):
        controller = make_v2_controller(
            layout=WIDE, details_text="should not appear in the right pane",
            preview_tables=[{"headers": ["Field"], "rows": [["x"]]}],
            table_only=True,
        )
        controller.build_panel()
        assert "should not appear in the right pane" not in controller._details_html_string
        assert "<table" in controller._details_html_string

    def test_table_only_has_no_effect_without_a_table(self):
        controller = make_v2_controller(
            layout=WIDE, details_text="still shown", preview_tables=[], table_only=True,
        )
        controller.build_panel()
        assert "still shown" in controller._details_html_string

    def test_preview_blocks_render_and_take_priority_over_details_text(self):
        controller = make_v2_controller(
            layout=WIDE, details_text="should not appear",
            preview_blocks=[{"type": "field", "label": "Reporter", "value": "Alice"}],
        )
        controller.build_panel()
        assert "should not appear" not in controller._details_html_string
        assert '<span class="pf-preview-label">Reporter:</span>' in controller._details_html_string


class TestV2ButtonsDisabledUntilWebviewLoads:
    """webView_didFinishNavigation_ is what re-enables Deny/Allow once/
    Always allow once the card-stack webview has actually painted --
    loadHTMLString_baseURL_ is asynchronous even for this fully local
    document (base64 fonts, full CSS bundle), so without this a fast or
    reflexive click could resolve the decision before the reviewer has
    seen any content at all. See _build_content_view_v2's own comment."""

    def test_buttons_start_disabled(self):
        controller = make_v2_controller(allow_accept_all=True)
        panel = controller.build_panel()

        buttons = buttons_by_title(flatten(panel.contentView()))
        assert buttons["Deny"].isEnabled() is False
        assert buttons["Allow once"].isEnabled() is False
        assert buttons["Always allow"].isEnabled() is False

    def test_buttons_enabled_after_navigation_finishes(self):
        controller = make_v2_controller(allow_accept_all=True)
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
        controller = make_v2_controller(allow_accept_all=True)
        panel = controller.build_panel()

        getattr(controller, failure_method)(controller._details_view, None, None)

        buttons = buttons_by_title(flatten(panel.contentView()))
        assert buttons["Deny"].isEnabled() is True
        assert buttons["Allow once"].isEnabled() is True
        assert buttons["Always allow"].isEnabled() is True

    def test_legacy_layout_buttons_are_unaffected(self):
        # Legacy's content isn't behind a single full-window webview (most
        # of it is native, synchronously-drawn views), so this gate is v2
        # only -- legacy buttons must start enabled, same as before this
        # existed.
        controller = make_v2_controller(layout="legacy")
        panel = controller.build_panel()

        buttons = buttons_by_title(flatten(panel.contentView()))
        assert buttons["Deny"].isEnabled() is True
        assert buttons["Allow once"].isEnabled() is True


class TestV2LegacyUnaffected:
    """The default layout ("legacy") must render exactly as before -- these
    mirror a couple of test_approval_window.py's own assertions as a belt-
    and-braces check that adding v2 didn't change the default path."""

    def test_default_layout_is_legacy(self):
        controller = ApprovalWindowController.alloc().init()
        assert controller.layout == "legacy"

    def test_legacy_layout_builds_the_original_fixed_width_window(self):
        from privacyfence.approval_window import _WINDOW_WIDTH
        controller = make_v2_controller(layout="legacy")
        panel = controller.build_panel()
        assert panel.frame().size.width == _WINDOW_WIDTH == 620.0
