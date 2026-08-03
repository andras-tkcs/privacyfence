"""Convert HTML to readable plain text (or Markdown) for popup display.

html_to_text() is used when a source (e.g. an HTML-only email) has no
plain-text body of its own — the approval popup's details pane renders plain
text, so dumping raw HTML into it renders as an unreadable wall of tags
instead of the message content the user is being asked to approve.

html_to_markdown() covers the same "don't show raw markup" problem for
sources whose body carries real structure worth keeping -- Confluence's
XHTML storage format, most notably -- so headings, bold/italic, lists,
links, and tables survive as Markdown syntax instead of being flattened to
plain prose. markdown_to_html.py is the corresponding renderer that turns
that Markdown back into HTML for the preview pane.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr",
}
_SKIP_CONTENT_TAGS = {"script", "style", "head", "title"}
_WHITESPACE_RUN = re.compile(r"\s+")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


class _HTMLToTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._link_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "li":
            self._chunks.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith("#"):
                self._link_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._link_href:
            self._chunks.append(f" ({self._link_href})")
            self._link_href = None
        elif tag == "li" or tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        collapsed = _WHITESPACE_RUN.sub(" ", data.replace("\xa0", " "))
        if collapsed:
            self._chunks.append(collapsed)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Strip tags and render HTML as plain text: block elements and list
    items become line breaks, link targets are kept inline as "text (url)",
    script/style content is dropped, and whitespace is collapsed. Not a full
    HTML renderer -- just enough to make an HTML-only body legible in a
    plain-text popup."""
    if not html or not html.strip():
        return ""
    parser = _HTMLToTextParser()
    parser.feed(html)
    parser.close()
    lines = [line.strip() for line in parser.text().split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------- #
# html_to_markdown() -- same "don't dump raw markup" problem, but keeps
# structure (headings, bold/italic, lists, links, tables) as Markdown syntax
# instead of discarding it. Best-effort, same as html_to_text() above: this
# assumes reasonably well-formed input (true of Confluence's own storage
# format) rather than validating nesting -- a stray unclosed tag degrades to
# slightly-off formatting, never a crash. A literal "#"/"*"/"|" character
# already present in the source text is not escaped before being emitted --
# same "best-effort, not a security boundary" posture as the rest of this
# module -- so it could rarely be misread as Markdown syntax by
# markdown_to_html.py's renderer; harmless, since that renderer's own output
# is always HTML-escaped regardless of what it thinks the syntax means.
# ---------------------------------------------------------------------------- #

_MD_NEWLINE_TAGS = {"p", "div", "br", "blockquote", "hr"}
_MD_MARK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i"}


class _HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        # Open h1-6/strong/b/em/i/a spans awaiting their end tag, as
        # (marker, start-index-into-_chunks) -- marker is the tag name for
        # headings/emphasis, or "a:<href>" for a link. Popped on the
        # matching end tag under a well-formed-HTML assumption (see module
        # docstring), not validated against the actual tag name.
        self._marks: list[tuple[str, int]] = []
        self._list_stack: list[dict] = []
        self._table_stack: list[dict] = []
        self._cell_start: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _MD_MARK_TAGS:
            self._marks.append((tag, len(self._chunks)))
        elif tag == "a":
            href = dict(attrs).get("href")
            marker = f"a:{href}" if href and not href.startswith("#") else "a:"
            self._marks.append((marker, len(self._chunks)))
        elif tag in ("ul", "ol"):
            self._list_stack.append({"kind": tag, "index": 0})
        elif tag == "li":
            self._chunks.append("\n")
            top = self._list_stack[-1] if self._list_stack else None
            if top is not None and top["kind"] == "ol":
                top["index"] += 1
                self._chunks.append(f"{top['index']}. ")
            else:
                self._chunks.append("- ")
        elif tag == "table":
            self._table_stack.append({"rows": [], "row": None})
        elif tag == "tr" and self._table_stack:
            self._table_stack[-1]["row"] = []
        elif tag in ("th", "td") and self._table_stack:
            self._cell_start.append(len(self._chunks))
        elif tag in _MD_NEWLINE_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._close_mark(lambda inner: f"\n{'#' * level} {inner.strip()}\n" if inner.strip() else "")
        elif tag in ("strong", "b"):
            self._close_mark(lambda inner: f"**{inner}**" if inner.strip() else inner)
        elif tag in ("em", "i"):
            self._close_mark(lambda inner: f"*{inner}*" if inner.strip() else inner)
        elif tag == "a":
            self._close_link()
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._chunks.append("\n")
        elif tag == "table":
            if self._table_stack:
                table = self._table_stack.pop()
                self._chunks.append(_render_table_markdown(table["rows"]))
        elif tag == "tr" and self._table_stack:
            row = self._table_stack[-1].get("row")
            if row is not None:
                self._table_stack[-1]["rows"].append(row)
                self._table_stack[-1]["row"] = None
        elif tag in ("th", "td") and self._cell_start:
            start = self._cell_start.pop()
            cell_text = "".join(self._chunks[start:])
            del self._chunks[start:]
            if self._table_stack and self._table_stack[-1].get("row") is not None:
                self._table_stack[-1]["row"].append(_WHITESPACE_RUN.sub(" ", cell_text).strip())
        elif tag in _MD_NEWLINE_TAGS:
            self._chunks.append("\n")

    def _close_mark(self, wrap) -> None:
        if not self._marks:
            return
        _, start = self._marks.pop()
        inner = "".join(self._chunks[start:])
        del self._chunks[start:]
        self._chunks.append(wrap(inner))

    def _close_link(self) -> None:
        if not self._marks:
            return
        marker, start = self._marks.pop()
        inner = "".join(self._chunks[start:])
        del self._chunks[start:]
        href = marker[2:] if marker.startswith("a:") else ""
        if href:
            self._chunks.append(f"[{inner.strip() or href}]({href})")
        else:
            self._chunks.append(inner)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        collapsed = _WHITESPACE_RUN.sub(" ", data.replace("\xa0", " "))
        if collapsed:
            self._chunks.append(collapsed)

    def text(self) -> str:
        return "".join(self._chunks)


def _render_table_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header, *body = rows
    lines = [_md_row(header), _md_row(["---"] * len(header))]
    lines.extend(_md_row(row) for row in body)
    return "\n" + "\n".join(lines) + "\n"


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def html_to_markdown(html: str) -> str:
    """Convert ``html`` to Markdown: headings, bold/italic, bullet/numbered
    lists, links, and tables become their Markdown equivalents;
    script/style content is dropped and whitespace is collapsed. Pairs with
    markdown_to_html.py, which renders the result back into HTML for the
    approval window's preview pane."""
    if not html or not html.strip():
        return ""
    parser = _HTMLToMarkdownParser()
    parser.feed(html)
    parser.close()
    lines = [line.strip() for line in parser.text().split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()
