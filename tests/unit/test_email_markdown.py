"""Tests for email_markdown.py: the Markdown subset (bold, italic,
==highlight==, links, lists, paragraphs) rendered for a rich-text Gmail
draft's text/html part, and stripped back to text/plain for the alternative
part when a caller supplies only body_markdown.
"""
from __future__ import annotations

from privacyfence.email_markdown import markdown_to_html, markdown_to_plain


class TestEmptyInput:
    def test_html_empty_string_returns_empty(self):
        assert markdown_to_html("") == ""

    def test_html_whitespace_only_returns_empty(self):
        assert markdown_to_html("   \n\t  ") == ""

    def test_plain_empty_string_returns_empty(self):
        assert markdown_to_plain("") == ""

    def test_plain_whitespace_only_returns_empty(self):
        assert markdown_to_plain("   \n\t  ") == ""


class TestParagraphs:
    def test_single_paragraph_wrapped_in_p(self):
        assert markdown_to_html("Hello world") == "<p>Hello world</p>"

    def test_blank_line_separates_paragraphs(self):
        assert markdown_to_html("First\n\nSecond") == "<p>First</p><p>Second</p>"

    def test_single_newline_within_paragraph_becomes_br_not_new_paragraph(self):
        assert markdown_to_html("Line one\nLine two") == "<p>Line one<br>Line two</p>"

    def test_plain_preserves_blank_line_between_paragraphs(self):
        assert markdown_to_plain("First\n\nSecond") == "First\n\nSecond"


class TestInlineStyles:
    def test_bold(self):
        assert markdown_to_html("**bold**") == "<p><b>bold</b></p>"

    def test_italic(self):
        assert markdown_to_html("*italic*") == "<p><i>italic</i></p>"

    def test_bold_italic_combined(self):
        assert markdown_to_html("***both***") == "<p><i><b>both</b></i></p>"

    def test_highlight(self):
        html = markdown_to_html("==flagged==")
        assert html == '<p><span style="background-color:#fff59d">flagged</span></p>'

    def test_mixed_inline_styles_in_one_line(self):
        html = markdown_to_html("plain **bold** and ==hi==")
        assert html == '<p>plain <b>bold</b> and <span style="background-color:#fff59d">hi</span></p>'

    def test_plain_strips_bold_italic_highlight_markers(self):
        assert markdown_to_plain("**bold** *italic* ==hi==") == "bold italic hi"


class TestLinks:
    def test_link_renders_as_anchor(self):
        html = markdown_to_html("[click here](https://example.com)")
        assert html == '<p><a href="https://example.com">click here</a></p>'

    def test_mailto_link_allowed(self):
        html = markdown_to_html("[email me](mailto:a@example.com)")
        assert 'href="mailto:a@example.com"' in html

    def test_unsafe_scheme_drops_href_but_keeps_label(self):
        html = markdown_to_html("[click](javascript:alert(1))")
        assert "<a " not in html
        assert "click" in html

    def test_bare_domain_without_scheme_is_unsafe(self):
        html = markdown_to_html("[click](example.com)")
        assert "<a " not in html

    def test_plain_renders_link_as_text_and_url(self):
        assert markdown_to_plain("[click here](https://example.com)") == "click here (https://example.com)"


class TestLists:
    def test_bullet_list(self):
        html = markdown_to_html("- first\n- second")
        assert html == "<ul><li>first</li><li>second</li></ul>"

    def test_numbered_list(self):
        html = markdown_to_html("1. first\n2. second")
        assert html == "<ol><li>first</li><li>second</li></ol>"

    def test_list_items_support_inline_styles(self):
        html = markdown_to_html("- **bold** item")
        assert html == "<ul><li><b>bold</b> item</li></ul>"

    def test_switching_list_kind_starts_a_new_list(self):
        html = markdown_to_html("- bullet\n1. numbered")
        assert html == "<ul><li>bullet</li></ul><ol><li>numbered</li></ol>"

    def test_list_surrounded_by_paragraphs(self):
        html = markdown_to_html("intro\n\n- one\n- two\n\noutro")
        assert html == "<p>intro</p><ul><li>one</li><li>two</li></ul><p>outro</p>"

    def test_plain_bullet_list_uses_dash_marker(self):
        assert markdown_to_plain("- one\n- two") == "- one\n- two"

    def test_plain_numbered_list_renumbers_sequentially(self):
        assert markdown_to_plain("1. one\n2. two") == "1. one\n2. two"


class TestSecurityEscaping:
    def test_literal_html_tags_are_escaped(self):
        html = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_ampersand_in_text_is_escaped(self):
        assert "&amp;" in markdown_to_html("Ben & Jerry's")

    def test_html_in_link_label_is_escaped(self):
        html = markdown_to_html("[<b>bold label</b>](https://example.com)")
        assert "&lt;b&gt;bold label&lt;/b&gt;" in html
        assert "<b>bold label</b>" not in html
