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
    CONTENT_WIDTH,
    DEFAULT_LINE_CLAMP,
    NARROW,
    WIDE,
    _risk_section_html,
    _WIDE_LEFT_COLUMN_WIDTH,
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
        assert '<span style="-webkit-line-clamp:3" title="Alice, Bob">Alice, Bob</span>' in html

    def test_description_row_carries_its_own_inline_clamp_override(self):
        html = build_card_stack_html(**_minimal_kwargs(disclosure_rows=[("Description", "A long paragraph.")]))
        assert '<span style="-webkit-line-clamp:4" title="A long paragraph.">A long paragraph.</span>' in html


class TestHoverTooltips:
    """Truncated values need a way to read the full text -- since this
    document runs with JavaScript disabled (approval_window.py's
    setJavaScriptEnabled_(False)), a native title="..." attribute is the
    only hover-tooltip mechanism available; WebKit shows it with no script
    needed. Set unconditionally (not just when a value happens to actually
    clamp) since predicting that in advance would need real text
    measurement, which this layout deliberately avoids -- see
    _kv_rows_html's own comment."""

    def test_kv_row_value_has_a_title_attribute_with_the_full_text(self):
        html = build_card_stack_html(**_minimal_kwargs(preview={"Title": "A fairly long event title"}))
        assert 'title="A fairly long event title"' in html

    def test_kv_row_title_is_escaped(self):
        html = build_card_stack_html(**_minimal_kwargs(preview={"Title": '<script>alert(1)</script> & "x"'}))
        assert 'title="&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;"' in html

    def test_disclosure_row_value_has_a_title_attribute(self):
        html = build_card_stack_html(**_minimal_kwargs(disclosure_rows=[("Attendees", "Alice, Bob, Carol")]))
        assert 'title="Alice, Bob, Carol"' in html

    def test_claude_reason_quote_has_a_title_attribute_with_the_full_text(self):
        html = build_card_stack_html(**_minimal_kwargs(claude_reason="A fairly long stated reason."))
        assert 'title="A fairly long stated reason."' in html

    def test_claude_reason_title_is_escaped(self):
        html = build_card_stack_html(**_minimal_kwargs(claude_reason='<script>alert(1)</script> & "x"'))
        assert 'title="&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;"' in html


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
    """Every section is numbered dynamically -- the risk card renders (and
    is numbered) right after §2, *before* §3 -- pinned, never one scroll
    away from being missed -- so §3 lands on "04" instead of "03" whenever
    a risk card is also present. Absent a risk card, §3 (or nothing at
    all) simply takes the next number, matching the design canvas's own
    numbering."""

    def test_read_call_with_disclosure_and_pii_numbers_pii_before_disclosure(self):
        html = build_card_stack_html(**_minimal_kwargs(
            disclosure_rows=[("Cell values", "Full cell values")],
            pii_categories=["Phone number"],
        ))
        assert "01 · What Claude already knows" in html
        assert "02 · Why Claude needs more data" in html
        assert "03 · Possible PII detected" in html
        assert "04 · What will be provided to Claude" in html
        # Pinned before §3 in the actual rendered order too, not just numbered
        # first -- see build_card_stack_html's docstring.
        assert html.index("Possible PII detected") < html.index("What will be provided to Claude")

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
        assert "flex:0 0" not in html
        assert "width: 610px" in html

    def test_wide_layout_has_the_two_column_split(self):
        html = build_card_stack_html(**_minimal_kwargs(layout=WIDE))
        assert f"flex:0 0 {_WIDE_LEFT_COLUMN_WIDTH}px" in html
        assert f"width: {CONTENT_WIDTH[WIDE]}px" in html

    def test_narrow_layout_has_no_preview_pane_at_all(self):
        # Not a smaller version of WIDE's preview -- genuinely absent, even
        # when preview_kicker/preview_body_html are given non-empty values.
        html = build_card_stack_html(**_minimal_kwargs(layout=NARROW))
        assert "Preview (~2 sec read)" not in html
        assert "Synthetic event body text" not in html

    def test_no_section_3_cap_by_default(self):
        # columns_max_height defaults to 0 -- the common case, where the
        # window was already sized to fit header/§1/§2/risk/§3 exactly, so
        # no artificial cap is applied to §3 (see build_card_stack_html's
        # own docstring for why an unconditional cap risked clipping
        # content over a few pixels of estimate-vs-actual rendering drift).
        # The right pane's own (unconditional, see the next test) cap is
        # the only "max-height" that should appear at all here.
        html = build_card_stack_html(**_minimal_kwargs(
            layout=WIDE, disclosure_rows=[("Cell values", "x")],
        ))
        assert html.count("max-height") == 1

    def test_right_pane_is_always_capped_for_wide_regardless_of_columns_max_height(self):
        # Unlike §3, the right pane's cap is unconditional -- its content
        # (an email/document/report) is unrelated in length to the left
        # column and always needs its own scroll bound. See
        # build_card_stack_html's own docstring.
        html = build_card_stack_html(**_minimal_kwargs(layout=WIDE, right_pane_max_height=444.0))
        assert "max-height:444px;overflow-y:auto" in html

    def test_columns_max_height_caps_only_section_3_not_the_right_pane(self):
        html = build_card_stack_html(**_minimal_kwargs(
            layout=WIDE, disclosure_rows=[("Cell values", "x")],
            columns_max_height=300.0, right_pane_max_height=444.0,
        ))
        assert html.count("max-height:300px;overflow-y:auto") == 1
        assert html.count("max-height:444px;overflow-y:auto") == 1

    def test_narrow_columns_max_height_caps_the_single_column(self):
        html = build_card_stack_html(**_minimal_kwargs(layout=NARROW, columns_max_height=300.0))
        assert "max-height:300px;overflow-y:auto" in html

    def test_wide_preview_pane_still_renders_after_the_left_column(self):
        html = build_card_stack_html(**_minimal_kwargs(layout=WIDE))
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

    def test_image_data_uri_takes_priority_over_text(self):
        body = build_preview_body_html(
            "should not appear",
            image_data_uri="data:image/png;base64,AAAA",
        )
        assert "data:image/png;base64,AAAA" in body
        assert "should not appear" not in body

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

    def test_table_renders_headers_and_rows(self):
        body = build_preview_body_html(
            "", tables=[{"headers": ["Field", "Value"], "rows": [["Name", "Acme Corp"], ["Phone", "555-0100"]]}],
        )
        assert "<table" in body
        assert "<th>Field</th>" in body
        assert "<th>Value</th>" in body
        assert "<td>Name</td>" in body
        assert "<td>Acme Corp</td>" in body

    def test_table_cells_are_escaped(self):
        body = build_preview_body_html(
            "", tables=[{"headers": ["X"], "rows": [["<script>alert(1)</script>"]]}],
        )
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_table_caption_and_footer_render_when_given(self):
        body = build_preview_body_html(
            "", tables=[{"caption": "Group A", "headers": ["X"], "rows": [["1"]], "footer": "Total: 1"}],
        )
        assert "Group A" in body
        assert "Total: 1" in body

    def test_multiple_tables_all_render(self):
        body = build_preview_body_html(
            "", tables=[
                {"caption": "First", "headers": ["A"], "rows": [["1"]]},
                {"caption": "Second", "headers": ["B"], "rows": [["2"]]},
            ],
        )
        assert "First" in body
        assert "Second" in body
        assert body.count("<table") == 2

    def test_text_and_table_both_render_together(self):
        body = build_preview_body_html("Some description.", tables=[{"headers": ["A"], "rows": [["1"]]}])
        assert "Some description." in body
        assert "<table" in body

    def test_empty_text_and_no_tables_falls_back_to_placeholder(self):
        assert "(no details)" in build_preview_body_html("", tables=[])
        assert "(no details)" in build_preview_body_html("", tables=None)

    def test_table_alone_does_not_show_no_details_placeholder(self):
        body = build_preview_body_html("", tables=[{"headers": ["A"], "rows": [["1"]]}])
        assert "(no details)" not in body

    def test_table_without_headers_omits_thead(self):
        body = build_preview_body_html("", tables=[{"rows": [["1", "2"]]}])
        assert "<thead>" not in body
        assert "<td>1</td>" in body


class TestPreviewBlocks:
    """blocks (text/field/table, in order) is what makes interleaving
    possible -- text, then a table, then more text -- which a flat
    details_text-then-tables split can't express. Takes full precedence
    over details_text/tables when given."""

    def test_text_block_renders_as_a_paragraph(self):
        body = build_preview_body_html(blocks=[{"type": "text", "text": "Hello world."}])
        assert 'class="pf-preview-paragraph"' in body
        assert "Hello world." in body

    def test_field_block_uses_the_shared_label_font(self):
        body = build_preview_body_html(blocks=[{"type": "field", "label": "Reporter", "value": "Alice"}])
        assert '<span class="pf-preview-label">Reporter:</span>' in body
        assert "Alice" in body

    def test_table_block_renders_as_a_real_table(self):
        body = build_preview_body_html(
            blocks=[{"type": "table", "headers": ["Author", "Comment"], "rows": [["Bob", "ack"]]}],
        )
        assert "<table" in body
        assert "<th>Author</th>" in body

    def test_heading_block_uses_the_shared_label_font_with_no_value(self):
        body = build_preview_body_html(blocks=[{"type": "heading", "label": "Description"}])
        assert '<div class="pf-preview-label"' in body
        assert ">Description</div>" in body

    def test_blocks_render_in_order_interleaved(self):
        body = build_preview_body_html(blocks=[
            {"type": "field", "label": "Reporter", "value": "Alice"},
            {"type": "text", "text": "A long description."},
            {"type": "table", "headers": ["Author"], "rows": [["Bob"]]},
        ])
        assert body.index("Reporter") < body.index("A long description.") < body.index("<table")

    def test_blocks_take_priority_over_details_text_and_tables(self):
        body = build_preview_body_html(
            "should not appear",
            tables=[{"headers": ["should not appear either"], "rows": [["x"]]}],
            blocks=[{"type": "text", "text": "only this"}],
        )
        assert "only this" in body
        assert "should not appear" not in body

    def test_field_and_text_are_escaped(self):
        body = build_preview_body_html(blocks=[
            {"type": "field", "label": "<b>L</b>", "value": "<i>V</i>"},
            {"type": "text", "text": "<script>alert(1)</script>"},
        ])
        assert "<b>" not in body
        assert "<i>" not in body
        assert "<script>alert(1)</script>" not in body

    def test_unknown_block_type_renders_nothing(self):
        body = build_preview_body_html(blocks=[{"type": "mystery"}])
        assert body == ""


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
