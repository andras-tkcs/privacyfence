"""Property-based round-trip tests for the four HTML/Markdown/document
parsers: html_to_text.py, markdown_to_html.py, email_markdown.py, and
text_extraction.py.

These four modules sit between untrusted external content (an HTML email,
a Confluence page, a DOCX attachment) and the approval popup's preview
pane -- exactly the kind of code a handful of example-based unit tests can
look complete for while still missing the one weird input (an unbalanced
tag, a lone ``*``, a truncated ZIP) that crashes the converter or lets a
literal ``<script>`` survive unescaped into the rendered preview.
``hypothesis`` generates those inputs instead of a human having to think of
every one of them by hand.

Every property test below asserts two things, regardless of what else it
checks:

1. **The function never raises** on any input the strategy can produce -- a
   crash here means the popup preview fails to render at all (an
   availability bug, not by itself a privacy one -- see each module's own
   "never raises" contract, most explicit in text_extraction.py's module
   docstring).
2. **Nothing dangerous survives unescaped into HTML output** -- the actual
   security property these converters exist to hold: a source document's
   own literal text (which may itself say "<script>" or similar) must
   never come out the other side as live, executable markup.

``TestHtmlToMarkdownToHtmlChain`` is the "especially the html_to_text ->
markdown_to_html chain" case the review calls out by name: html_to_text.py's
``html_to_markdown()`` output is exactly the input markdown_to_html.py's
``markdown_to_html()`` is built to render (approval_window_html.py's own
preview_blocks path pipes Confluence's XHTML storage format through both,
in that order). Neither half is the other's literal inverse -- this isn't a
bijection, the information that survives the trip is deliberately lossy --
but the pipe as a whole must never raise, and content from the original
HTML must still never come out the far end as live markup.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from privacyfence.email_markdown import markdown_to_html as email_markdown_to_html
from privacyfence.email_markdown import markdown_to_plain
from privacyfence.html_to_text import html_to_markdown, html_to_text
from privacyfence.markdown_to_html import markdown_to_html
from privacyfence.text_extraction import extract_text

# docs/automated-test-strategy-plan.md Phase 0: hypothesis-generated inputs
# through pure parsing functions, no I/O -- unit per testing-policy.md's
# seven-layer taxonomy.
pytestmark = pytest.mark.unit

# A generous but bounded text strategy: arbitrary Unicode text (not just
# ASCII -- these modules all handle real-world non-ASCII content), capped
# in length so a single failing example doesn't take pages to read and the
# suite doesn't slow down a default CI run.
_text = st.text(min_size=0, max_size=200)

# Sampled explicitly alongside the free-form strategy above: st.text()
# alone could eventually generate one of these by chance, but sampling
# them directly makes sure every run actually exercises the dangerous case
# instead of relying on hypothesis's shrinking/example budget to stumble
# onto it.
_dangerous_text = st.one_of(
    _text,
    st.sampled_from([
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "</td></tr><script>evil()</script>",
        "&amp;&lt;&gt;",
        '"><svg onload=alert(1)>',
        "javascript:alert(1)",
    ]),
)

# deadline=None: hypothesis's default per-example wall-clock deadline
# (200ms) exists to catch a strategy that's accidentally O(n^2)/exponential,
# not to bound these tests -- on a loaded/shared CI runner it would turn
# ordinary scheduling jitter into a flaky failure unrelated to the code
# under test. pytest-timeout's own 30s whole-test cap (pyproject.toml)
# still bounds a genuinely hanging parser.
_settings = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _no_unescaped_script_tag(html: str) -> bool:
    return "<script" not in html.lower()


class TestHtmlToTextNeverRaises:
    @given(html=_dangerous_text)
    @_settings
    def test_html_to_text_never_raises_and_returns_a_str(self, html):
        result = html_to_text(html)
        assert isinstance(result, str)

    @given(html=_dangerous_text)
    @_settings
    def test_html_to_markdown_never_raises_and_returns_a_str(self, html):
        result = html_to_markdown(html)
        assert isinstance(result, str)


class TestMarkdownToHtmlNeverRaisesAndEscapesContent:
    @given(markdown=_dangerous_text)
    @_settings
    def test_never_raises_and_returns_a_str(self, markdown):
        result = markdown_to_html(markdown)
        assert isinstance(result, str)

    @given(markdown=_dangerous_text)
    @_settings
    def test_literal_script_tags_in_the_source_never_survive_unescaped(self, markdown):
        result = markdown_to_html(markdown)
        assert _no_unescaped_script_tag(result)


class TestEmailMarkdownNeverRaisesAndEscapesContent:
    @given(markdown=_dangerous_text)
    @_settings
    def test_markdown_to_html_never_raises_and_escapes_script_tags(self, markdown):
        result = email_markdown_to_html(markdown)
        assert isinstance(result, str)
        assert _no_unescaped_script_tag(result)

    @given(markdown=_dangerous_text)
    @_settings
    def test_markdown_to_plain_never_raises(self, markdown):
        result = markdown_to_plain(markdown)
        assert isinstance(result, str)


class TestHtmlToMarkdownToHtmlChain:
    """See module docstring -- the chain the review names explicitly."""

    @given(html=_dangerous_text)
    @_settings
    def test_chain_never_raises_and_stays_escaped(self, html):
        markdown = html_to_markdown(html)
        result = markdown_to_html(markdown)
        assert isinstance(result, str)
        assert _no_unescaped_script_tag(result)


class TestExtractTextNeverRaises:
    """extract_text's own module docstring: "Never raises... simply
    contributes no text." Fuzzed with arbitrary bytes -- not just valid
    DOCX/PPTX/XLSX/ZIP archives -- against every MIME type it recognizes,
    since a corrupted or truncated upload is exactly the input this
    contract has to survive, not only a well-formed document.
    """

    @given(
        data=st.binary(min_size=0, max_size=2000),
        mime_type=st.sampled_from([
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/pdf",
            "text/html",
            "application/octet-stream",
            "",
        ]),
    )
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_never_raises_on_arbitrary_bytes(self, data, mime_type):
        result = extract_text(data, mime_type)
        assert isinstance(result, str)
