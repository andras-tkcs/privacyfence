"""Tests for approval_window_html.py -- the card-stack HTML template for the
redesigned (layout="narrow"/"wide") approval window.

Pure-function module, no AppKit -- unlike test_approval_window.py, none of
this needs macOS/PyObjC or a real view tree; it asserts directly on the
generated HTML strings, the same "pure function, directly unit-testable"
contract approval_window.py's own _details_html() already holds (see that
module's TestDetailsPane for the precedent this file follows).
"""
from __future__ import annotations

from privacyfence.approval_window_html import (
    DEFAULT_LINE_CLAMP,
    NARROW,
    WIDE,
    _risk_section_html,
    build_card_stack_html,
    build_preview_body_html,
    disclosure_rows_from_visibility,
    line_clamp_for,
)


def _minimal_kwargs(**overrides):
    kwargs = dict(
        layout=NARROW,
        title="Read Calendar Event",
        connector_icon_data_uri="",
        shield_icon_data_uri="",
        is_read=True,
        seen_count_text="",
        preview={"Title": "PrivacyFence QA seed event [QATEST]"},
        claude_reason="Checking the QA event details as requested.",
        disclosure_rows=[],
        pii_categories=[],
        write_content_flags=[],
        upload_forced=False,
        temp_accept_text="",
        preview_kicker="Preview (~2 sec read)",
        preview_body_html=build_preview_body_html("Synthetic event body text."),
    )
    kwargs.update(overrides)
    return kwargs


class TestLineClamp:
    """A value too long for its row is truncated with a CSS ellipsis
    (styles.css's .pf-kv default -webkit-line-clamp:2), never grows the row
    or the window -- some fields get more than the 2-line default (see
    line_clamp_for's own docstring)."""

    def test_default_clamp_for_an_unlisted_label(self):
        assert line_clamp_for("Title") == DEFAULT_LINE_CLAMP == 2

    def test_attendees_gets_a_taller_clamp(self):
        assert line_clamp_for("Attendees") == 3

    def test_description_gets_the_tallest_clamp(self):
        assert line_clamp_for("Description") == 4

    def test_default_clamp_label_has_no_inline_style_override(self):
        html = build_card_stack_html(**_minimal_kwargs(preview={"Title": "x"}))
        assert 'style="-webkit-line-clamp' not in html

    def test_attendees_row_carries_its_own_inline_clamp_override(self):
        html = build_card_stack_html(**_minimal_kwargs(disclosure_rows=[("Attendees", "Alice, Bob")]))
        assert '<span style="-webkit-line-clamp:3">Alice, Bob</span>' in html

    def test_description_row_carries_its_own_inline_clamp_override(self):
        html = build_card_stack_html(**_minimal_kwargs(disclosure_rows=[("Description", "A long paragraph.")]))
        assert '<span style="-webkit-line-clamp:4">A long paragraph.</span>' in html


class TestDisclosureRowsFromVisibility:
    """§3's plain "what's disclosed" sentence per field -- the structural
    change from the old checklist (✓/✗/◐ icons) to prose, per an allow/
    redact/block policy dict (privacy_filter.category_policy()'s ground
    truth, unchanged)."""

    def test_allow_becomes_a_full_disclosure_sentence(self):
        rows = disclosure_rows_from_visibility({"Cell values": "allow"})
        assert rows == [("Cell values", "Full cell values")]

    def test_redact_names_the_field_with_a_caveat(self):
        rows = disclosure_rows_from_visibility({"Sender & metadata": "redact"})
        assert rows == [("Sender & metadata", "Sender & metadata, with some fields redacted")]

    def test_block_discloses_nothing(self):
        rows = disclosure_rows_from_visibility({"Attachments": "block"})
        assert rows == [("Attachments", "None — not disclosed to Claude")]

    def test_preserves_input_order(self):
        rows = disclosure_rows_from_visibility(
            {"Sender & metadata": "redact", "Thread messages": "allow", "Attachments": "block"}
        )
        assert [label for label, _ in rows] == ["Sender & metadata", "Thread messages", "Attachments"]

    def test_empty_dict_yields_no_rows(self):
        assert disclosure_rows_from_visibility({}) == []

    def test_is_a_pure_function(self):
        visibility = {"Message text": "allow", "Usernames": "redact"}
        assert disclosure_rows_from_visibility(visibility) == disclosure_rows_from_visibility(visibility)


class TestSectionNumbering:
    """Every section is numbered dynamically -- §4-equivalent risk cards
    land on "03" instead of "04" whenever §3 didn't render (write-gate
    calls, or a read-gate call with no `visibility` dict), matching the
    design canvas's own numbering exactly."""

    def test_read_call_with_disclosure_and_pii_numbers_04(self):
        html = build_card_stack_html(**_minimal_kwargs(
            disclosure_rows=[("Cell values", "Full cell values")],
            pii_categories=["Phone number"],
        ))
        assert "01 · What Claude already knows" in html
        assert "02 · Why Claude needs more data" in html
        assert "03 · What will be provided to Claude" in html
        assert "04 · Possible PII detected" in html

    def test_read_call_without_disclosure_but_with_pii_numbers_03(self):
        # A tool with nothing to disclose in §3 (empty disclosure_rows) whose
        # content still matched the PII detector.
        html = build_card_stack_html(**_minimal_kwargs(pii_categories=["Phone number"]))
        assert "03 · Possible PII detected" in html
        assert "04 ·" not in html

    def test_write_call_never_gets_section_3_even_with_a_visibility_like_dict(self):
        # disclosure_rows is only ever consulted when is_read=True -- a
        # write-gate call passing it anyway (which real callers never do)
        # must still not render §3, mirroring show_popup() never setting
        # self.visibility in the real controller.
        html = build_card_stack_html(**_minimal_kwargs(
            is_read=False, title="Create Calendar Event",
            disclosure_rows=[("Should not appear", "Full should not appear")],
            write_content_flags=["Email address"],
        ))
        assert "What will be provided to Claude" not in html
        assert "03 · Possible PII detected" in html

    def test_no_risk_card_when_neither_pii_list_is_populated(self):
        html = build_card_stack_html(**_minimal_kwargs())
        assert "Possible PII detected" not in html

    def test_section_1_is_skipped_entirely_when_preview_is_empty(self):
        html = build_card_stack_html(**_minimal_kwargs(preview={}))
        assert "What Claude already knows" not in html
        # §2 still gets "01", not "02" -- the counter never advanced for §1.
        assert "01 · Why Claude needs more data" in html

    def test_section_2_is_skipped_entirely_when_claude_reason_is_empty(self):
        html = build_card_stack_html(**_minimal_kwargs(claude_reason=""))
        assert "Why Claude needs more data" not in html

    def test_risk_section_html_returns_empty_string_for_no_categories(self):
        # Defense in depth: build_card_stack_html() never calls this with an
        # empty list (it checks first), but the function's own guard is
        # still real behavior worth pinning directly.
        assert _risk_section_html(3, [], variant="read") == ""


class TestRiskCardVariants:
    def test_read_variant_uses_accent_2_tokens_and_review_carefully_copy(self):
        html = build_card_stack_html(**_minimal_kwargs(pii_categories=["IBAN (bank account number)"]))
        assert "var(--color-accent-2-100)" in html
        assert "Review carefully before approving" in html
        assert "IBAN (bank account number)" in html

    def test_write_variant_uses_the_new_pii_write_bg_tokens(self):
        html = build_card_stack_html(**_minimal_kwargs(
            is_read=False, write_content_flags=["Phone number"],
        ))
        assert "var(--pii-w-bg)" in html
        assert "This message appears to contain" in html

    def test_upload_forced_placeholder_reuses_read_styling_not_write(self):
        # drive_upload_file's own PII match forces the same second-
        # confirmation flow the read side gets -- no distinct design exists
        # yet, so this is a deliberate interim stand-in (see module
        # docstring), not the final answer.
        html = build_card_stack_html(**_minimal_kwargs(
            is_read=False, write_content_flags=["Phone number"], upload_forced=True,
        ))
        assert "var(--color-accent-2-100)" in html
        assert "var(--pii-w-bg)" not in html

    def test_upload_forced_is_ignored_when_there_is_no_write_content_flag(self):
        html = build_card_stack_html(**_minimal_kwargs(is_read=False, upload_forced=True))
        assert "Possible PII detected" not in html


class TestLayoutShapes:
    def test_narrow_layout_has_no_two_column_split(self):
        html = build_card_stack_html(**_minimal_kwargs(layout=NARROW))
        assert "flex:0 0 350px" not in html
        assert "width: 610px" in html

    def test_wide_layout_has_the_two_column_split(self):
        html = build_card_stack_html(**_minimal_kwargs(layout=WIDE))
        assert "flex:0 0 350px" in html
        assert "width: 880px" in html

    def test_narrow_layout_has_no_preview_pane_at_all(self):
        # Not a smaller version of WIDE's preview -- genuinely absent, even
        # when preview_kicker/preview_body_html are given non-empty values.
        html = build_card_stack_html(**_minimal_kwargs(layout=NARROW))
        assert "Preview (~2 sec read)" not in html
        assert "Synthetic event body text" not in html

    def test_wide_preview_pane_scrolls_independently(self):
        html = build_card_stack_html(**_minimal_kwargs(layout=WIDE))
        assert "overflow-y:auto" in html
        assert "max-height:520px" in html
        assert html.index("Preview (~2 sec read)") > html.index("What Claude already knows")


class TestTempAcceptDisclosure:
    def test_present_when_text_given(self):
        html = build_card_stack_html(**_minimal_kwargs(
            temp_accept_text="Approving this also allows further calls like this for a few minutes.",
        ))
        assert "Approving this also allows further calls" in html

    def test_absent_when_empty(self):
        html = build_card_stack_html(**_minimal_kwargs(temp_accept_text=""))
        assert "Approving this also allows" not in html


class TestPreviewBody:
    def test_plain_text_is_escaped_and_preserves_whitespace(self):
        body = build_preview_body_html("line one\nline two")
        assert "line one\nline two" in body
        assert "white-space:pre-wrap" in body

    def test_html_in_details_text_is_escaped_not_interpreted(self):
        body = build_preview_body_html("<script>alert(1)</script> & \"x\"")
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_empty_details_text_falls_back_to_a_placeholder(self):
        assert "(no details)" in build_preview_body_html("")

    def test_email_content_kind_prepends_a_structured_header(self):
        body = build_preview_body_html(
            "hello", content_kind="email",
            preview={"From": "a@example.com", "To": "b@example.com", "Subject": "Hi", "Date": "2026-07-16"},
        )
        assert "a@example.com" in body
        assert "b@example.com" in body
        assert "hello" in body

    def test_image_data_uri_takes_priority_over_text_and_email(self):
        body = build_preview_body_html(
            "should not appear", content_kind="email",
            preview={"From": "a@example.com"},
            image_data_uri="data:image/png;base64,AAAA",
        )
        assert "data:image/png;base64,AAAA" in body
        assert "should not appear" not in body
        assert "a@example.com" not in body

    def test_pdf_data_uri_takes_priority_over_image_and_text(self):
        body = build_preview_body_html(
            "should not appear",
            image_data_uri="data:image/png;base64,AAAA",
            pdf_data_uri="data:application/pdf;base64,BBBB",
        )
        assert "data:application/pdf;base64,BBBB" in body
        assert "<embed" in body
        assert "data:image/png;base64,AAAA" not in body
        assert "should not appear" not in body

    def test_is_a_pure_function(self):
        assert build_preview_body_html("abc") == build_preview_body_html("abc")


class TestEscapingAndNoNetwork:
    """Defense in depth, same discipline _details_html() already holds:
    every dynamic string reaching the document must be escaped, and the
    document must never be able to reach out to the network -- fonts are
    embedded as base64 data URIs (see resources/approval_window/styles.css),
    never linked, and there is no <script> tag anywhere."""

    def test_title_is_escaped(self):
        html = build_card_stack_html(**_minimal_kwargs(title="<b>hi</b> & \"x\""))
        assert "<b>hi</b>" not in html
        assert "&lt;b&gt;" in html

    def test_preview_values_are_escaped(self):
        html = build_card_stack_html(**_minimal_kwargs(preview={"Title": "<script>x</script>"}))
        assert "<script>x</script>" not in html

    def test_claude_reason_is_escaped(self):
        html = build_card_stack_html(**_minimal_kwargs(claude_reason="<script>x</script>"))
        assert "<script>x</script>" not in html

    def test_pii_category_labels_are_escaped(self):
        html = build_card_stack_html(**_minimal_kwargs(pii_categories=["<script>x</script>"]))
        assert "<script>x</script>" not in html

    def test_document_has_no_script_tag(self):
        html = build_card_stack_html(**_minimal_kwargs())
        assert "<script" not in html

    def test_document_has_no_http_or_https_references(self):
        # In particular: no Google Fonts (or any other) network fetch --
        # the design canvas's own styles.css imports fonts from
        # fonts.googleapis.com; the vendored copy this module reads must
        # never carry that through.
        html = build_card_stack_html(**_minimal_kwargs())
        assert "http://" not in html
        assert "https://" not in html

    def test_fonts_are_embedded_as_data_uris(self):
        html = build_card_stack_html(**_minimal_kwargs())
        assert "@font-face" in html
        assert "data:font/woff2;base64," in html


class TestCardStackIsAPureFunction:
    def test_same_input_same_output(self):
        assert build_card_stack_html(**_minimal_kwargs()) == build_card_stack_html(**_minimal_kwargs())

    def test_different_input_different_output(self):
        a = build_card_stack_html(**_minimal_kwargs(title="A"))
        b = build_card_stack_html(**_minimal_kwargs(title="B"))
        assert a != b
