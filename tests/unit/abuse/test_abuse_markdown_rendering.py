"""Adversarial (SEC-01 / TST-01a) coverage for the two Markdown -> HTML
renderers that build a clickable ``<a href>`` from externally-influenced
text: markdown_to_html.py (the approval window's preview pane, fed by
html_to_text.py's html_to_markdown() and text_extraction.py) and
email_markdown.py (rich-text Gmail draft bodies).

The invariant under test: no unsafe URL scheme -- "javascript:", "data:",
"vbscript:", or an obfuscated (percent-encoded, entity-encoded, mixed-case)
spelling of one of those -- ever reaches an emitted ``href`` attribute. An
unsafe link degrades to its plain, escaped label text; it never becomes an
`<a ...>` at all. This is the regression test for SEC-01: markdown_to_html.py
previously emitted `m.group(2)` as a raw href with no scheme check.
"""
from __future__ import annotations

import pytest

from privacyfence import email_markdown
from privacyfence import markdown_to_html as markdown_to_html_mod
from privacyfence.html_to_text import html_to_markdown

# One payload per obfuscation category the review calls out: a plain unsafe
# scheme, a second unsafe scheme, percent-encoding two characters of the
# scheme name, an HTML numeric-character-reference-encoded scheme, and a
# mixed-case spelling relying on a case-sensitive check somewhere upstream.
# None contain "(" / ")" in the URL itself -- markdown_to_html.py's link
# regex (`[^)\s]+`) stops at the first ")", which would otherwise truncate
# the match independently of the scheme check this test is pinning; that's
# a separate, pre-existing parsing quirk, not part of SEC-01.
UNSAFE_URL_PAYLOADS = [
    pytest.param("javascript:alert(document.cookie)", id="raw-javascript-scheme"),
    pytest.param("vbscript:msgbox(1)", id="raw-vbscript-scheme"),
    pytest.param("data:text/html,evil", id="raw-data-scheme"),
    pytest.param("java%73cript:alert(1)", id="percent-encoded-scheme"),
    pytest.param("&#106;avascript:alert(1)", id="entity-encoded-scheme"),
    pytest.param("JaVaScRiPt:alert(1)", id="mixed-case-scheme"),
]

SAFE_URL_PAYLOADS = [
    pytest.param("https://example.com/path", id="https"),
    pytest.param("http://example.com", id="http"),
    pytest.param("mailto:a@example.com", id="mailto"),
]


class TestMarkdownToHtmlUnsafeSchemes:
    """markdown_to_html.py's `_inline`: the approval window preview pane."""

    @pytest.mark.parametrize("url", UNSAFE_URL_PAYLOADS)
    def test_unsafe_scheme_never_reaches_an_href(self, url):
        result = markdown_to_html_mod.markdown_to_html(f"[click here]({url})")
        assert "<a " not in result
        assert "href=" not in result

    @pytest.mark.parametrize("url", UNSAFE_URL_PAYLOADS)
    def test_unsafe_scheme_keeps_the_link_label(self, url):
        result = markdown_to_html_mod.markdown_to_html(f"[click here]({url})")
        assert "click here" in result

    @pytest.mark.parametrize("url", SAFE_URL_PAYLOADS)
    def test_safe_scheme_still_renders_as_a_link(self, url):
        result = markdown_to_html_mod.markdown_to_html(f"[click here]({url})")
        assert f'<p><a href="{url}">click here</a></p>' == result

    def test_bare_domain_without_scheme_is_unsafe(self):
        # No scheme at all -- must not be guessed at as "https://...".
        result = markdown_to_html_mod.markdown_to_html("[click](example.com/evil)")
        assert "<a " not in result

    def test_unsafe_link_inside_a_heading(self):
        result = markdown_to_html_mod.markdown_to_html("# [click](javascript:alert(1))")
        assert "<a " not in result
        assert result.startswith("<h1>")

    def test_unsafe_link_inside_a_list_item(self):
        result = markdown_to_html_mod.markdown_to_html("- [click](javascript:alert(1))")
        assert "<a " not in result

    def test_unsafe_link_inside_a_table_cell(self):
        md = "| Link |\n| --- |\n| [click](javascript:alert(1)) |"
        result = markdown_to_html_mod.markdown_to_html(md)
        assert "<a " not in result


class TestEmailMarkdownUnsafeSchemes:
    """email_markdown.py: rich-text Gmail draft bodies (the module the
    shared is_safe_url() helper was originally lifted from)."""

    @pytest.mark.parametrize("url", UNSAFE_URL_PAYLOADS)
    def test_unsafe_scheme_never_reaches_an_href(self, url):
        result = email_markdown.markdown_to_html(f"[click here]({url})")
        assert "<a " not in result
        assert "click here" in result

    @pytest.mark.parametrize("url", SAFE_URL_PAYLOADS)
    def test_safe_scheme_still_renders_as_a_link(self, url):
        result = email_markdown.markdown_to_html(f"[click here]({url})")
        assert f'<p><a href="{url}">click here</a></p>' == result

    def test_unsafe_scheme_dropped_in_plain_text_variant_too(self):
        # markdown_to_plain() never emits markup at all, but an unsafe link
        # still shouldn't be rendered as "(url)" the way a safe one is --
        # it degrades to the label alone, same as the HTML path.
        result = email_markdown.markdown_to_plain("[click](javascript:evil)")
        assert result == "click"


class TestFullChainHtmlToMarkdownToHtml:
    """The full inbound/outbound round trip: html_to_text.py's
    html_to_markdown() (e.g. a Confluence page's XHTML storage format) feeds
    markdown_to_html.py (the approval preview pane) directly -- an unsafe
    href surviving that trip is exactly the SEC-01 exploit path."""

    @pytest.mark.parametrize(
        "href",
        [
            pytest.param("javascript:alert(document.cookie)", id="raw-javascript-scheme"),
            pytest.param("vbscript:msgbox(1)", id="raw-vbscript-scheme"),
            pytest.param("data:text/html,evil", id="raw-data-scheme"),
            pytest.param("java%73cript:alert(1)", id="percent-encoded-scheme"),
            pytest.param("JaVaScRiPt:alert(1)", id="mixed-case-scheme"),
        ],
    )
    def test_unsafe_href_dropped_after_the_full_chain(self, href):
        source_html = f'<p><a href="{href}">click here</a></p>'
        rendered = markdown_to_html_mod.markdown_to_html(html_to_markdown(source_html))
        assert "<a " not in rendered
        assert "click here" in rendered

    def test_entity_encoded_scheme_decoded_by_the_html_parser_is_still_caught(self):
        # The upstream HTML parser (convert_charrefs=True) decodes
        # "&#106;avascript:" back to "javascript:" while extracting the
        # href -- so by the time markdown_to_html.py sees it, it's the
        # plain scheme, not the obfuscated spelling. Pin that the chain
        # still catches it rather than relying on the obfuscation itself.
        source_html = '<p><a href="&#106;avascript:alert(1)">click here</a></p>'
        markdown = html_to_markdown(source_html)
        assert "javascript:alert(1)" in markdown
        rendered = markdown_to_html_mod.markdown_to_html(markdown)
        assert "<a " not in rendered
        assert "click here" in rendered

    def test_safe_href_survives_the_full_chain(self):
        source_html = '<p><a href="https://example.com/doc">click here</a></p>'
        rendered = markdown_to_html_mod.markdown_to_html(html_to_markdown(source_html))
        assert '<p><a href="https://example.com/doc">click here</a></p>' == rendered
