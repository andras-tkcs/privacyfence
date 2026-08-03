"""Markdown -> HTML / plain-text rendering for rich-text email bodies.

Deliberately minimal and independent from drive_client.py's Markdown -> Google
Docs API parser: supports bold, italic, links, lists, paragraphs, and
==highlight== only -- the subset that makes sense in an email body. Not
attempting CommonMark compliance, tables, headings, or nested lists.
"""

from __future__ import annotations

import html as _html
import re as _re
from typing import NamedTuple
from urllib.parse import urlsplit

# Matches drive_client.py's `==text==` highlight syntax for consistency
# across the two tools, though the two parsers are otherwise independent.
_HIGHLIGHT_COLOR = "#fff59d"

_INLINE_RE = _re.compile(
    r"\*\*\*(.+?)\*\*\*"          # bold + italic
    r"|\*\*(.+?)\*\*"             # bold
    r"|\*(.+?)\*"                 # italic
    r"|==(.+?)=="                 # highlight
    r"|\[([^\]]+)\]\(([^)]+)\)"   # link [text](url)
)

# Schemes a mail client will actually open as a link, vs. e.g. "javascript:"
# smuggled in through a [text](url) run -- anything else has its href
# dropped (the link text still renders, just not as a clickable link)
# rather than emitting a link a recipient's mail client might act on.
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


class _InlineRun(NamedTuple):
    text: str
    bold: bool = False
    italic: bool = False
    highlight: bool = False
    url: str = ""


def _is_safe_url(url: str) -> bool:
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    # A bare "example.com" (no scheme) is treated as unsafe rather than
    # guessed at -- callers should write "https://example.com".
    return scheme in _ALLOWED_URL_SCHEMES


def _parse_inline_runs(text: str) -> list[_InlineRun]:
    runs: list[_InlineRun] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            runs.append(_InlineRun(text[last : m.start()]))
        if m.group(1):  # bold + italic
            runs.append(_InlineRun(m.group(1), bold=True, italic=True))
        elif m.group(2):  # bold
            runs.append(_InlineRun(m.group(2), bold=True))
        elif m.group(3):  # italic
            runs.append(_InlineRun(m.group(3), italic=True))
        elif m.group(4):  # highlight
            runs.append(_InlineRun(m.group(4), highlight=True))
        elif m.group(5):  # link
            url = m.group(6)
            runs.append(_InlineRun(m.group(5), url=url if _is_safe_url(url) else ""))
        last = m.end()
    if last < len(text):
        runs.append(_InlineRun(text[last:]))
    return runs


class _Block(NamedTuple):
    kind: str  # "para" or "list"
    ordered: bool
    lines: list[str]


def _parse_blocks(markdown: str) -> list[_Block]:
    """Split markdown source into paragraph and list blocks.

    A blank line always ends the current block. Consecutive non-blank lines
    that aren't list items join one paragraph (rendered with a line break
    between them, not reflowed into one line) -- short emails are usually
    intentionally line-broken, unlike long-form prose. Consecutive list-item
    lines of the same kind (bullet vs numbered) join one list block;
    switching kind starts a new block. Nested/indented lists are not
    supported.
    """
    blocks: list[_Block | None] = []
    for raw_line in markdown.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            blocks.append(None)  # sentinel: ends the current block
            continue

        bullet_match = _re.match(r"^[-*+]\s+(.*)", line)
        numbered_match = _re.match(r"^\d+\.\s+(.*)", line)
        if bullet_match or numbered_match:
            ordered = numbered_match is not None
            item_text = (bullet_match or numbered_match).group(1)
            top = blocks[-1] if blocks else None
            if top is not None and top.kind == "list" and top.ordered == ordered:
                top.lines.append(item_text)
            else:
                blocks.append(_Block("list", ordered, [item_text]))
            continue

        top = blocks[-1] if blocks else None
        if top is not None and top.kind == "para":
            top.lines.append(line)
        else:
            blocks.append(_Block("para", False, [line]))

    return [b for b in blocks if b is not None]


def _render_inline(text: str) -> str:
    chunks = []
    for run in _parse_inline_runs(text):
        rendered = _html.escape(run.text)
        if run.url:
            rendered = f'<a href="{_html.escape(run.url)}">{rendered}</a>'
        if run.bold:
            rendered = f"<b>{rendered}</b>"
        if run.italic:
            rendered = f"<i>{rendered}</i>"
        if run.highlight:
            rendered = f'<span style="background-color:{_HIGHLIGHT_COLOR}">{rendered}</span>'
        chunks.append(rendered)
    return "".join(chunks)


def markdown_to_html(markdown: str) -> str:
    """Render the supported Markdown subset (bold, italic, ==highlight==,
    links, lists, paragraphs) as an HTML fragment for an email's text/html
    part. All literal text is HTML-escaped; only the constructs above ever
    produce markup, and link hrefs are restricted to http/https/mailto.
    """
    if not markdown or not markdown.strip():
        return ""
    parts: list[str] = []
    for block in _parse_blocks(markdown):
        if block.kind == "list":
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{_render_inline(item)}</li>" for item in block.lines)
            parts.append(f"<{tag}>{items}</{tag}>")
        else:
            parts.append(f"<p>{'<br>'.join(_render_inline(line) for line in block.lines)}</p>")
    return "".join(parts)


def _plain_inline(text: str) -> str:
    chunks = []
    for run in _parse_inline_runs(text):
        chunk = run.text
        if run.url:
            chunk = f"{chunk} ({run.url})"
        chunks.append(chunk)
    return "".join(chunks)


def markdown_to_plain(markdown: str) -> str:
    """Strip the same Markdown subset back to readable plain text, for the
    text/plain alternative part when a caller supplies only body_markdown.
    Links render as "text (url)", matching html_to_text.py's convention for
    the same inbound/outbound tradeoff.
    """
    if not markdown or not markdown.strip():
        return ""
    parts: list[str] = []
    for block in _parse_blocks(markdown):
        if block.kind == "list":
            lines = [
                f"{f'{i + 1}.' if block.ordered else '-'} {_plain_inline(item)}"
                for i, item in enumerate(block.lines)
            ]
            parts.append("\n".join(lines))
        else:
            parts.append("\n".join(_plain_inline(line) for line in block.lines))
    return "\n\n".join(parts)
