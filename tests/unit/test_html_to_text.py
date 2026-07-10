"""Tests for html_to_text.py: HTML-only email/page bodies must render as
legible plain text in the approval popup's details pane, not raw tag soup.
"""
from __future__ import annotations

from privacyfence.html_to_text import html_to_text


class TestEmptyInput:
    def test_empty_string_returns_empty(self):
        assert html_to_text("") == ""

    def test_whitespace_only_returns_empty(self):
        assert html_to_text("   \n\t  ") == ""


class TestTagStripping:
    def test_simple_paragraph_has_no_tags(self):
        result = html_to_text("<p>Hello world</p>")
        assert result == "Hello world"

    def test_multiple_paragraphs_become_blank_line_separated(self):
        result = html_to_text("<p>First</p><p>Second</p>")
        assert result == "First\n\nSecond"

    def test_br_becomes_single_newline(self):
        result = html_to_text("Line one<br>Line two")
        assert result == "Line one\nLine two"

    def test_nested_divs_and_spans_strip_cleanly(self):
        result = html_to_text("<div><span>Hi <b>Alice</b></span>,</div><div>thanks</div>")
        assert "Hi Alice ," not in result  # no doubled space from tag boundary
        assert "Hi Alice" in result
        assert "thanks" in result


class TestScriptAndStyleDropped:
    def test_script_content_excluded(self):
        result = html_to_text("<p>Visible</p><script>var x = 'not visible';</script>")
        assert "not visible" not in result
        assert "Visible" in result

    def test_style_content_excluded(self):
        result = html_to_text("<style>body{color:red}</style><p>Visible</p>")
        assert "color:red" not in result
        assert result == "Visible"


class TestEntitiesAndWhitespace:
    def test_named_entity_decoded(self):
        assert html_to_text("<p>Ben &amp; Jerry</p>") == "Ben & Jerry"

    def test_nbsp_collapsed_to_space(self):
        result = html_to_text("<p>Hello&nbsp;world</p>")
        assert result == "Hello world"

    def test_source_formatting_whitespace_collapsed(self):
        html = "<p>\n    Hello\n    world\n  </p>"
        assert html_to_text(html) == "Hello world"

    def test_many_blank_lines_collapsed_to_one(self):
        html = "<div>A</div><div></div><div></div><div></div><div>B</div>"
        result = html_to_text(html)
        assert "\n\n\n" not in result


class TestLinks:
    def test_link_text_kept_with_url_appended(self):
        result = html_to_text('<a href="https://example.com/doc">the doc</a>')
        assert result == "the doc (https://example.com/doc)"

    def test_fragment_only_href_not_appended(self):
        result = html_to_text('<a href="#section">jump</a>')
        assert result == "jump"

    def test_link_with_no_href_keeps_text_only(self):
        result = html_to_text("<a>bare</a>")
        assert result == "bare"


class TestLists:
    def test_list_items_prefixed_with_dash(self):
        result = html_to_text("<ul><li>Item one</li><li>Item two</li></ul>")
        assert "- Item one" in result
        assert "- Item two" in result


class TestRealisticEmailBody:
    def test_full_html_email_renders_legibly(self):
        html = (
            "<html><head><style>body{font-family:sans-serif}</style></head>"
            "<body>"
            "<div>Hi Alice,</div>"
            "<div>&nbsp;</div>"
            "<div>Please review the attached report before Friday.</div>"
            "<div>&nbsp;</div>"
            "<div>Best,<br>Bob</div>"
            "</body></html>"
        )
        result = html_to_text(html)
        assert "font-family" not in result
        assert "<div>" not in result
        assert "Hi Alice," in result
        assert "Please review the attached report before Friday." in result
        assert "Best," in result
        assert "Bob" in result
