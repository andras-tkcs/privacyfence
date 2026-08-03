"""Tests for html_to_text.py: HTML-only email/page bodies must render as
legible plain text in the approval popup's details pane, not raw tag soup.

Also covers html_to_markdown() -- the same problem, but for sources (mainly
Confluence's XHTML storage format) whose structure is worth keeping as
Markdown syntax rather than flattening to plain prose. markdown_to_html.py
(see test_markdown_to_html.py) is what turns that Markdown back into HTML
for the approval window's preview pane.
"""
from __future__ import annotations

from privacyfence.html_to_text import html_to_markdown, html_to_text


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


class TestHtmlToMarkdownEmptyInput:
    def test_empty_string_returns_empty(self):
        assert html_to_markdown("") == ""

    def test_whitespace_only_returns_empty(self):
        assert html_to_markdown("   \n\t  ") == ""


class TestHtmlToMarkdownHeadings:
    def test_h1_becomes_hash_prefix(self):
        assert html_to_markdown("<h1>Title</h1>") == "# Title"

    def test_h3_becomes_three_hashes(self):
        assert html_to_markdown("<h3>Subsection</h3>") == "### Subsection"

    def test_heading_above_h6_is_capped(self):
        # Not a real HTML tag, but the parser only special-cases h1-h6 by
        # name anyway -- included to document there's no h7+ to worry about.
        assert html_to_markdown("<h6>Deepest</h6>") == "###### Deepest"


class TestHtmlToMarkdownEmphasis:
    def test_bold_becomes_double_star(self):
        assert html_to_markdown("<p><b>bold</b></p>") == "**bold**"

    def test_strong_becomes_double_star(self):
        assert html_to_markdown("<p><strong>bold</strong></p>") == "**bold**"

    def test_italic_becomes_single_star(self):
        assert html_to_markdown("<p><em>italic</em></p>") == "*italic*"

    def test_mixed_emphasis_in_one_paragraph(self):
        result = html_to_markdown("<p>This is <strong>critical</strong> and <em>urgent</em>.</p>")
        assert result == "This is **critical** and *urgent*."


class TestHtmlToMarkdownLinks:
    def test_link_becomes_markdown_link(self):
        result = html_to_markdown('<a href="https://example.com/doc">the doc</a>')
        assert result == "[the doc](https://example.com/doc)"

    def test_fragment_only_href_kept_as_plain_text(self):
        assert html_to_markdown('<a href="#section">jump</a>') == "jump"


class TestHtmlToMarkdownLists:
    def test_unordered_list_items_prefixed_with_dash(self):
        result = html_to_markdown("<ul><li>First</li><li>Second</li></ul>")
        assert result == "- First\n- Second"

    def test_ordered_list_items_numbered(self):
        result = html_to_markdown("<ol><li>First</li><li>Second</li></ol>")
        assert result == "1. First\n2. Second"


class TestHtmlToMarkdownTables:
    def test_table_becomes_pipe_table_with_separator_row(self):
        html = (
            "<table><tbody>"
            "<tr><th>Name</th><th>Status</th></tr>"
            "<tr><td>Alice</td><td>Done</td></tr>"
            "</tbody></table>"
        )
        result = html_to_markdown(html)
        lines = result.split("\n")
        assert lines[0] == "| Name | Status |"
        assert lines[1] == "| --- | --- |"
        assert lines[2] == "| Alice | Done |"


class TestHtmlToMarkdownScriptAndStyleDropped:
    def test_script_and_style_content_excluded(self):
        result = html_to_markdown(
            "<style>body{color:red}</style><p>Visible</p><script>var x = 1;</script>"
        )
        assert result == "Visible"


class TestHtmlToMarkdownRoundTrip:
    def test_realistic_page_round_trips_through_markdown_to_html(self):
        # The actual point of html_to_markdown(): its output must be
        # something markdown_to_html.py can render back into real HTML
        # (see test_markdown_to_html.py), not just readable Markdown source.
        from privacyfence.markdown_to_html import markdown_to_html

        html = (
            "<h1>Project Overview</h1>"
            "<p>This is a <strong>critical</strong> project.</p>"
            "<ul><li>Write spec</li><li>Review with legal</li></ul>"
            "<table><tbody><tr><th>Name</th><th>Status</th></tr>"
            "<tr><td>Alice</td><td>Done</td></tr></tbody></table>"
        )
        markdown = html_to_markdown(html)
        rendered = markdown_to_html(markdown)
        assert "<h1>Project Overview</h1>" in rendered
        assert "<strong>critical</strong>" in rendered
        assert "<li>Write spec</li>" in rendered
        assert '<table class="pf-table">' in rendered
        assert "<td>Alice</td>" in rendered
