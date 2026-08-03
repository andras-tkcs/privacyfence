"""Tests for markdown_to_html.py: renders the constrained Markdown subset
text_extraction.py/html_to_text.py produce back into HTML for the approval
window's preview pane.

The one invariant that matters most: every literal text span is
HTML-escaped, and only recognized Markdown syntax produces real tags --
unrecognized or malicious-looking input degrades to safely-escaped plain
text, never raw markup.
"""
from __future__ import annotations

from privacyfence.markdown_to_html import markdown_to_html


class TestEmptyInput:
    def test_empty_string_returns_empty(self):
        assert markdown_to_html("") == ""

    def test_whitespace_only_returns_empty(self):
        assert markdown_to_html("   \n\n  ") == ""


class TestHeadings:
    def test_h1_through_h6(self):
        for level in range(1, 7):
            md = f"{'#' * level} Heading"
            assert markdown_to_html(md) == f"<h{level}>Heading</h{level}>"

    def test_heading_requires_space_after_hashes(self):
        # "#no-space" isn't a heading -- falls through to a plain paragraph.
        assert markdown_to_html("#no-space") == "<p>#no-space</p>"


class TestParagraphsAndLineBreaks:
    def test_single_line_paragraph(self):
        assert markdown_to_html("Hello world") == "<p>Hello world</p>"

    def test_blank_line_separates_paragraphs(self):
        result = markdown_to_html("First paragraph.\n\nSecond paragraph.")
        assert result == "<p>First paragraph.</p><p>Second paragraph.</p>"

    def test_single_newline_within_a_block_becomes_br(self):
        result = markdown_to_html("Line one\nLine two")
        assert result == "<p>Line one<br>Line two</p>"


class TestInlineEmphasis:
    def test_bold(self):
        assert markdown_to_html("**bold**") == "<p><strong>bold</strong></p>"

    def test_italic(self):
        assert markdown_to_html("*italic*") == "<p><em>italic</em></p>"

    def test_bold_and_italic_combined(self):
        result = markdown_to_html("**bold** and *italic*")
        assert result == "<p><strong>bold</strong> and <em>italic</em></p>"

    def test_bold_processed_before_italic_so_double_star_is_not_two_italics(self):
        # A naive single-"*" italic regex applied first would misread "**"
        # as two empty italic spans -- pin that bold wins.
        assert markdown_to_html("**bold**") != "<p><em></em><em>bold</em></p>"

    def test_inline_code(self):
        assert markdown_to_html("`code`") == "<p><code>code</code></p>"

    def test_link(self):
        result = markdown_to_html("[the doc](https://example.com/doc)")
        assert result == '<p><a href="https://example.com/doc">the doc</a></p>'


class TestLists:
    def test_unordered_list(self):
        result = markdown_to_html("- one\n- two")
        assert result == "<ul><li>one</li><li>two</li></ul>"

    def test_unordered_list_with_asterisk_marker(self):
        result = markdown_to_html("* one\n* two")
        assert result == "<ul><li>one</li><li>two</li></ul>"

    def test_ordered_list(self):
        result = markdown_to_html("1. one\n2. two")
        assert result == "<ol><li>one</li><li>two</li></ol>"

    def test_list_item_text_is_inline_rendered(self):
        result = markdown_to_html("- **bold** item")
        assert result == "<ul><li><strong>bold</strong> item</li></ul>"


class TestTables:
    def test_simple_table(self):
        md = "| Name | Size |\n| --- | --- |\n| a.txt | 100 |\n| b.txt | 200 |"
        result = markdown_to_html(md)
        assert result == (
            '<table class="pf-table"><thead><tr><th>Name</th><th>Size</th></tr></thead>'
            "<tbody><tr><td>a.txt</td><td>100</td></tr><tr><td>b.txt</td><td>200</td></tr></tbody>"
            "</table>"
        )

    def test_table_cells_are_inline_rendered(self):
        md = "| Name |\n| --- |\n| **Alice** |"
        result = markdown_to_html(md)
        assert "<td><strong>Alice</strong></td>" in result

    def test_two_line_block_without_separator_row_is_not_a_table(self):
        # Two pipe-shaped lines with no "---" separator row don't qualify --
        # falls through to a plain paragraph instead of a malformed table.
        result = markdown_to_html("| a | b |\n| c | d |")
        assert "<table" not in result
        assert "<p>" in result


class TestEscaping:
    def test_html_special_characters_are_escaped(self):
        result = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_ampersand_in_plain_text_is_escaped(self):
        assert markdown_to_html("Ben & Jerry") == "<p>Ben &amp; Jerry</p>"

    def test_unrecognized_syntax_degrades_to_escaped_text(self):
        result = markdown_to_html("just | some # random *punctuation")
        assert "<table" not in result
        assert "<h1>" not in result
        assert "just | some # random" in result


class TestMultiBlockDocument:
    def test_heading_paragraph_list_and_table_together(self):
        md = (
            "# Report\n\n"
            "**Q3 results** are in.\n\n"
            "- point one\n- point two\n\n"
            "| Region | Total |\n| --- | --- |\n| EU | 100 |"
        )
        result = markdown_to_html(md)
        assert result.startswith("<h1>Report</h1>")
        assert "<p><strong>Q3 results</strong> are in.</p>" in result
        assert "<ul><li>point one</li><li>point two</li></ul>" in result
        assert '<table class="pf-table">' in result
