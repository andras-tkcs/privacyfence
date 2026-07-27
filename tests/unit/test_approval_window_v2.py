"""Construction-level tests for ApprovalWindowController's v2 rendering
(layout="compact"/"wide" -- the redesigned card-stack window).

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
from privacyfence.approval_window_html import COMPACT, WIDE

from .test_approval_window import buttons_by_title, flatten


def make_v2_controller(
    *,
    layout=COMPACT,
    title="Read Calendar Event",
    preview=None,
    details_text="ordinary, non-sensitive content",
    allow_accept_all=False,
    is_read=True,
    claude_reason="Checking the event as requested.",
    visibility=None,
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
    return c


def build_views(controller):
    panel = controller.build_panel()
    return list(flatten(panel.contentView())), panel


_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestV2WindowShape:
    def test_compact_layout_window_width(self):
        controller = make_v2_controller(layout=COMPACT)
        panel = controller.build_panel()
        assert panel.frame().size.width == _V2_WINDOW_WIDTH[COMPACT] == 610.0

    def test_wide_layout_window_width(self):
        controller = make_v2_controller(layout=WIDE)
        panel = controller.build_panel()
        assert panel.frame().size.width == _V2_WINDOW_WIDTH[WIDE] == 880.0

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

    def test_show_more_toggle_present_only_for_compact_layout(self):
        compact_views, _ = build_views(make_v2_controller(layout=COMPACT))
        assert "Show more" in buttons_by_title(compact_views)

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

    def test_visibility_dict_becomes_a_disclosure_section(self):
        controller = make_v2_controller(
            is_read=True, visibility={"Cell values": "allow"},
        )
        controller.build_panel()
        assert "What will be provided to Claude" in controller._details_html_string
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
        controller = make_v2_controller(details_text="<script>alert(1)</script>")
        controller.build_panel()
        assert "<script>alert(1)</script>" not in controller._details_html_string
        assert "&lt;script&gt;" in controller._details_html_string


class TestV2ImageAndPdfPreview:
    """v2 renders image/PDF preview content inline via a data URI (<img>/
    <embed>), not a native NSImageView/PDFView overlay -- see
    _build_content_view_v2's docstring for why that's simpler here than in
    the legacy layout."""

    def test_image_preview_bytes_render_as_an_img_data_uri(self):
        controller = make_v2_controller(preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png")
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
            pdf_bytes=b"%PDF-1.1 fake", preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
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
        controller = make_v2_controller(pdf_bytes=b"%PDF-1.1 fake")
        views, _ = build_views(controller)
        assert not [v for v in views if isinstance(v, PDFView)]


class TestV2ProgressiveDisclosure:
    def test_toggling_expanded_grows_the_compact_window(self):
        controller = make_v2_controller(layout=COMPACT, details_text="line\n" * 400)
        controller.build_panel()
        collapsed_height = controller.panel.frame().size.height
        controller.toggleDetailsExpanded_(None)
        expanded_height = controller.panel.frame().size.height
        assert expanded_height > collapsed_height

    def test_wide_layout_has_no_expand_toggle_to_click(self):
        views, _ = build_views(make_v2_controller(layout=WIDE))
        assert "Show more" not in buttons_by_title(views)
        assert "Show less" not in buttons_by_title(views)


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
