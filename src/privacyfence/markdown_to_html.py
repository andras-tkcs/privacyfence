"""Render a constrained Markdown subset to safe HTML for the approval
window's preview pane.

Counterpart to html_to_text.py's html_to_markdown() -- that function turns
markup (Confluence's XHTML storage format) into Markdown; text_extraction.py
turns other formats (DOCX/PPTX/XLSX) into the same Markdown syntax directly.
Either way, this module is the one place that turns that Markdown back into
HTML for display, so a heading, list, or table extracted from any of those
sources renders identically in the preview pane (headings, bold/italic,
links, bullet/numbered lists, and pipe tables reuse the same ``.pf-table``
class approval_window_html.py's own ``_table_html`` already uses).

Deliberately not a full CommonMark implementation -- just the subset the
extraction/conversion side of this codebase actually produces. All literal
text is HTML-escaped before any markup is applied, so unrecognized syntax
(or content that happens to contain a stray ``*`` or ``|``) degrades to
plain, safely-escaped text rather than broken markup or an injection risk.
"""
from __future__ import annotations

import html
import re

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")
_UL_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")
_OL_ITEM_RE = re.compile(r"^\d+\.\s+(.*)$")

# Applied in this order: code first (so a code span's contents are never
# re-processed by link/bold/italic below), bold before italic (a leading
# "**" must be consumed as bold before the single-"*" italic pattern gets a
# chance to misread it as two empty italic spans).
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")


def markdown_to_html(markdown: str) -> str:
    """Convert ``markdown`` into an HTML fragment: headings, bullet/numbered
    lists, pipe tables, and paragraphs (blank-line separated), with inline
    bold/italic/code/link spans. Never raises -- empty or unparseable input
    just yields an empty string or a single escaped paragraph.
    """
    if not markdown or not markdown.strip():
        return ""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in markdown.replace("\r\n", "\n").split("\n"):
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return "".join(_render_block(block) for block in blocks)


def _render_block(lines: list[str]) -> str:
    if len(lines) == 1:
        m = _HEADING_RE.match(lines[0])
        if m:
            level = len(m.group(1))
            return f"<h{level}>{_inline(m.group(2).strip())}</h{level}>"

    if (
        len(lines) >= 2
        and _TABLE_ROW_RE.match(lines[0])
        and _TABLE_ROW_RE.match(lines[1])
        and _is_table_separator(lines[1])
    ):
        return _render_table(lines)

    if all(_UL_ITEM_RE.match(line) for line in lines):
        items = "".join(f"<li>{_inline(_UL_ITEM_RE.match(line).group(1))}</li>" for line in lines)
        return f"<ul>{items}</ul>"

    if all(_OL_ITEM_RE.match(line) for line in lines):
        items = "".join(f"<li>{_inline(_OL_ITEM_RE.match(line).group(1))}</li>" for line in lines)
        return f"<ol>{items}</ol>"

    text = "<br>".join(_inline(line.strip()) for line in lines)
    return f"<p>{text}</p>"


def _is_table_separator(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table(lines: list[str]) -> str:
    header_html = "".join(f"<th>{_inline(c)}</th>" for c in _split_row(lines[0]))
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _split_row(line)) + "</tr>"
        for line in lines[2:]
    )
    return f'<table class="pf-table"><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table>'


def _inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
    return escaped
