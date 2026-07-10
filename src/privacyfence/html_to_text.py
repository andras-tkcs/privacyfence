"""Convert HTML to readable plain text for popup display.

Used when a source (e.g. an HTML-only email) has no plain-text body of its
own — the approval popup's details pane is a plain NSTextView, so dumping raw
HTML into it renders as an unreadable wall of tags instead of the message
content the user is being asked to approve.
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
