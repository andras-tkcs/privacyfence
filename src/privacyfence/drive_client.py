"""Google Drive API client.

Handles OAuth2 authorization and read-only access to Google Drive. All file and
folder data is normalized into simple dataclasses so the rest of the
application never has to deal with the raw Drive API payload shape.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.

The Drive client shares the same OAuth client secret as Gmail but caches its
token separately (``drive_token.json``) so the two services can be authorized
independently.
"""

from __future__ import annotations

import base64
import io
import logging
import mimetypes
import os
import re as _re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

# Full Drive scope: read + write + create + move + comment.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Google Workspace MIME types that must be exported (they cannot be downloaded
# directly). We export everything as plain text for review.
_GOOGLE_DOC_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# get_file_content special-cases this one mime type to read the Docs API's
# structured documents().get() and render Markdown instead of using the
# plain-text export above (see _docs_structure_to_markdown) -- it stays in
# _GOOGLE_DOC_EXPORTS too since download_file still exports Docs as a
# plain-text .txt file unchanged; only get_file_content's read path changed.
_GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"

# Metadata fields requested from the Drive API for a single file.
# driveId is populated for files that live inside a Shared Drive. thumbnailLink
# is a short-lived, Google-signed URL to a small preview image Drive already
# generated for this file (not guaranteed to be present for every file type) --
# fetching it is much cheaper than downloading the full file just to preview it.
_FILE_FIELDS = (
    "id, name, mimeType, size, createdTime, modifiedTime, "
    "owners(emailAddress), shared, webViewLink, parents, driveId, thumbnailLink"
)

# Cap on how much we'll read back from a thumbnailLink fetch. It's meant to be
# a small preview image Drive generated, not a full download -- if a response
# somehow exceeds this, something is wrong (not actually a thumbnail) and we'd
# rather fail than buffer an unbounded amount of memory.
_THUMBNAIL_MAX_BYTES = 1_048_576


def resolve_download_name(metadata: Any) -> str:
    """Compute the filename ``download_file`` will save under.

    Google Workspace documents are exported as text (Docs/Slides -> .txt,
    Sheets -> .csv), so the saved name differs from ``metadata.name``. Pure --
    takes already-fetched metadata, makes no API call -- so it can be called
    once to preview the save path before download approval and again inside
    ``download_file`` to actually write the file, and the two can never
    disagree.
    """
    export_mime = _GOOGLE_DOC_EXPORTS.get(metadata.mime_type)
    name = metadata.name or metadata.id
    if export_mime == "text/plain" and not name.endswith(".txt"):
        name = name + ".txt"
    elif export_mime == "text/csv" and not name.endswith(".csv"):
        name = name + ".csv"
    return name


def resolve_download_destination(metadata: Any, destination_dir: str = "") -> str:
    """Compute where ``download_file`` will save this file, without touching disk.

    ``destination_dir`` is mandatory -- there is no default. Callers must
    deliberately choose between a location the user will find (e.g.
    ~/Downloads) and Claude's own working/scratch directory, rather than a
    file silently landing in Downloads just because nobody thought about it.
    ``metadata.name`` comes from Drive and is untrusted -- a file can be
    renamed to anything, including path separators -- so only the basename of
    the resolved download name is kept. This is the same protection
    ``gmail_client.resolve_attachment_destination`` applies to attachment
    names, and for the same reason: it's what stops a file renamed to
    "../../.ssh/authorized_keys" from writing outside ``destination_dir``.
    """
    if not destination_dir.strip():
        raise DriveClientError(
            "download_file requires a non-empty destination_dir -- there is "
            "no default. Pass ~/Downloads (or another location the user "
            "asked for) if this file is a deliverable the user should find "
            "afterward, or your own working/scratch directory if you're "
            "only downloading it to read or process it yourself."
        )
    dest_dir = os.path.expanduser(destination_dir.strip())
    safe_name = os.path.basename(resolve_download_name(metadata)) or metadata.id or "file"
    return os.path.join(dest_dir, safe_name)


# ------------------------------------------------------------------ #
# Markdown → Google Docs API helpers
# ------------------------------------------------------------------ #

_HEADING_PREFIXES = [
    ("###### ", "HEADING_6"),
    ("##### ", "HEADING_5"),
    ("#### ", "HEADING_4"),
    ("### ", "HEADING_3"),
    ("## ", "HEADING_2"),
    ("# ", "HEADING_1"),
]

# CommonMark thematic break: up to 3 leading spaces, then 3+ of the same
# character among -, _, * with only whitespace between/after (e.g. `---`,
# `___`, `* * *`). Rendered as an empty paragraph with a bottom border --
# the Docs API has no native "insert horizontal rule" request (the Docs
# UI's Insert > Horizontal line isn't exposed to batchUpdate), so a
# bottom-bordered empty paragraph is the closest a document built through
# this API can get to one. Previously unrecognized entirely: `---` on its
# own line landed as the literal 3-character string, not a divider and not
# an error either (see test_thematic_break_becomes_bordered_paragraph).
_THEMATIC_BREAK_RE = _re.compile(
    r"^ {0,3}(?:-[ \t]*){3,}$"
    r"|^ {0,3}(?:_[ \t]*){3,}$"
    r"|^ {0,3}(?:\*[ \t]*){3,}$"
)

# Sentinel para_style value (never a real Docs namedStyleType) marking a
# thematic-break line for the bottom-border treatment above instead of the
# updateParagraphStyle/namedStyleType path every heading uses.
_DIVIDER_STYLE = "PRIVACYFENCE_DIVIDER"

# Approximates the Docs UI's own horizontal-line color; there's no native
# element to match exactly (see _THEMATIC_BREAK_RE above).
_DIVIDER_BORDER_COLOR = "#cccccc"

# Fixed highlight color for ==text== spans (Docs UI's default highlighter
# yellow swatch) -- v1 has no per-span color argument, see design doc.
_DEFAULT_HIGHLIGHT_COLOR = "#FFF59D"

# Font substituted for `code` spans so they render monospace like every other
# Markdown renderer's inline code, instead of indistinguishable plain text.
_CODE_FONT_FAMILY = "Courier New"

# Two spaces of leading indent = one nested-list level, capped to what the
# Docs UI itself supports. Callers should indent nested list items by 2
# spaces per level (see drive_write_doc_content's tool description).
_MAX_LIST_NESTING = 8

# NOTE: __underline__ intentionally does not follow CommonMark, where a
# double-underscore is alternate *bold* syntax -- this parser has exactly one
# bold spelling (**) so the double-underscore slot is free to mean something
# else, and dedicated underline syntax is otherwise unavailable in Markdown.
_INLINE_RE = _re.compile(
    r"\*\*\*(.+?)\*\*\*"         # bold + italic
    r"|\*\*(.+?)\*\*"            # bold
    r"|\*(.+?)\*"                # italic
    r"|~~(.+?)~~"                # strikethrough
    r"|__(.+?)__"                # underline
    r"|==(.+?)=="                # highlight
    r"|`(.+?)`"                  # code (monospace font)
    # link [text](url) -- link text may contain literal `[`/`]` if escaped
    # as `\[`/`\]` (an unescaped `]` still closes the label, same as an
    # unescaped `|` closes a table cell below). The two alternatives below
    # are mutually exclusive on their first character (`\\.` only starts on
    # a backslash, `[^\]\\]` excludes one) so a backslash can never be
    # matched two ways -- unlike the earlier `\\[\[\]]|[^\]]` version, where
    # a backslash matched both the escape alternative and the catch-all,
    # this can't blow up into exponential backtracking on a run of `\[`s
    # that's never followed by a closing `](url)`.
    r"|\[((?:\\.|[^\]\\])+)\]\(([^)]+)\)"
)


class InlineRun(NamedTuple):
    text: str
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    underline: bool = False
    highlight: bool = False
    code: bool = False
    url: str = ""


def _parse_inline_runs(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    strikethrough: bool = False,
    underline: bool = False,
    highlight: bool = False,
) -> list[InlineRun]:
    """Return a list of InlineRun from an inline Markdown string.

    bold/italic/*/underline/highlight nest with each other -- a matched
    span's inner text is itself re-parsed for further inline syntax, with
    the enclosing span's style(s) (the keyword-only params below) carried
    down so they combine with whatever style the inner span applies. E.g.
    ``==**Follow-up**==`` yields one run, text "Follow-up", both bold and
    highlight True -- previously the outer ``==...==`` match swallowed the
    inner ``**...**`` as literal, unparsed text (see regression test
    ``test_highlight_wrapping_bold_nests_both_styles``). `code` spans and
    link text are leaves and are inserted verbatim, not re-parsed --
    matching CommonMark's treatment of code spans as literal text.
    """
    runs: list[InlineRun] = []
    last = 0

    def literal(s: str) -> InlineRun:
        return InlineRun(
            s, bold=bold, italic=italic, strikethrough=strikethrough,
            underline=underline, highlight=highlight,
        )

    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            runs.append(literal(text[last : m.start()]))
        if m.group(1):  # bold+italic
            runs.extend(_parse_inline_runs(
                m.group(1), bold=True, italic=True,
                strikethrough=strikethrough, underline=underline, highlight=highlight,
            ))
        elif m.group(2):  # bold
            runs.extend(_parse_inline_runs(
                m.group(2), bold=True, italic=italic,
                strikethrough=strikethrough, underline=underline, highlight=highlight,
            ))
        elif m.group(3):  # italic
            runs.extend(_parse_inline_runs(
                m.group(3), bold=bold, italic=True,
                strikethrough=strikethrough, underline=underline, highlight=highlight,
            ))
        elif m.group(4):  # strikethrough
            runs.extend(_parse_inline_runs(
                m.group(4), bold=bold, italic=italic,
                strikethrough=True, underline=underline, highlight=highlight,
            ))
        elif m.group(5):  # underline
            runs.extend(_parse_inline_runs(
                m.group(5), bold=bold, italic=italic,
                strikethrough=strikethrough, underline=True, highlight=highlight,
            ))
        elif m.group(6):  # highlight
            runs.extend(_parse_inline_runs(
                m.group(6), bold=bold, italic=italic,
                strikethrough=strikethrough, underline=underline, highlight=True,
            ))
        elif m.group(7):  # code -- leaf, not re-parsed
            runs.append(InlineRun(
                m.group(7), bold=bold, italic=italic, strikethrough=strikethrough,
                underline=underline, highlight=highlight, code=True,
            ))
        elif m.group(8):  # link -- leaf, not re-parsed
            link_text = m.group(8).replace("\\[", "[").replace("\\]", "]")
            runs.append(InlineRun(
                link_text, bold=bold, italic=italic, strikethrough=strikethrough,
                underline=underline, highlight=highlight, url=m.group(9),
            ))
        last = m.end()
    if last < len(text):
        runs.append(literal(text[last:]))
    return runs or [literal("")]


def _markdown_to_docs_requests(markdown: str, start_index: int = 1) -> list[dict]:
    """Convert simple Markdown to a list of Google Docs batchUpdate requests.

    Text is inserted in a single ``insertText`` call at ``start_index``
    (1-based, like every Docs API index); subsequent requests apply
    paragraph and inline styles by character range relative to it.
    ``start_index`` defaults to 1 (the start of a fresh/cleared document);
    callers replacing one matched span mid-document pass the Docs index
    where that span begins instead.
    """
    lines = markdown.rstrip("\n").split("\n")

    # Parse each line into (inline-runs, paragraph-style, list-bullet-preset,
    # list-nesting-level). list_level is only meaningful when list_preset is set.
    parsed: list[tuple[list[InlineRun], str, str, int]] = []
    for line in lines:
        para_style = "NORMAL_TEXT"
        list_preset = ""
        list_level = 0
        if _THEMATIC_BREAK_RE.match(line):
            para_style = _DIVIDER_STYLE
            line = ""
        else:
            for prefix, style in _HEADING_PREFIXES:
                if line.startswith(prefix):
                    line = line[len(prefix):]
                    para_style = style
                    break
            else:
                stripped = line.lstrip(" \t")
                indent_width = len(line) - len(stripped)
                if _re.match(r"^[-*+] ", stripped):
                    line = stripped[2:]
                    list_preset = "BULLET_DISC_CIRCLE_SQUARE"
                    list_level = min(indent_width // 2, _MAX_LIST_NESTING)
                elif _re.match(r"^\d+\. ", stripped):
                    line = _re.sub(r"^\d+\. ", "", stripped)
                    list_preset = "NUMBERED_DECIMAL_ALPHA_ROMAN"
                    list_level = min(indent_width // 2, _MAX_LIST_NESTING)
        parsed.append((_parse_inline_runs(line), para_style, list_preset, list_level))

    # Build full plain text and record per-line doc positions. A list line's
    # paragraph is prefixed with `list_level` literal tab characters -- the
    # Docs API infers each paragraph's nesting level from its count of
    # leading tabs at createParagraphBullets time, then strips them, so the
    # tabs never end up visible in the final document.
    full_text = ""
    line_spans: list[tuple[int, int, str, list[InlineRun], str, int]] = []
    for runs, para_style, list_preset, list_level in parsed:
        line_start = len(full_text) + start_index
        if list_level:
            full_text += "\t" * list_level
        text_start = len(full_text) + start_index
        for run in runs:
            full_text += run.text
        full_text += "\n"
        line_end = len(full_text) + start_index
        line_spans.append((line_start, line_end, para_style, runs, list_preset, text_start))

    # A lone divider line has no text of its own -- it's still real content
    # (a bordered paragraph), so the "nothing but blank lines" short-circuit
    # below must not also swallow it.
    has_divider = any(para_style == _DIVIDER_STYLE for _, para_style, _, _ in parsed)
    if not full_text.strip("\n\t") and not has_divider:
        return []

    requests: list[dict] = [
        {"insertText": {"location": {"index": start_index}, "text": full_text}}
    ]

    # createParagraphBullets strips the leading nesting tabs it counts as a
    # side effect of inferring nesting level, shrinking the document. Since
    # every line's createParagraphBullets request in this same batchUpdate
    # runs in order against the *live* result of every earlier request, a
    # line's true position by the time its own request fires is offset by
    # however many tabs every preceding list line's own request already
    # stripped -- track that running total and shift every range by it, or
    # later list lines silently land on the wrong paragraph.
    tabs_stripped_so_far = 0
    for line_start, line_end, para_style, runs, list_preset, text_start in line_spans:
        list_level = text_start - line_start
        line_start -= tabs_stripped_so_far
        line_end -= tabs_stripped_so_far
        if para_style == _DIVIDER_STYLE:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": line_start,
                            "endIndex": line_end,
                        },
                        "paragraphStyle": {
                            "borderBottom": {
                                "color": {
                                    "color": {"rgbColor": _hex_to_rgb_dict(_DIVIDER_BORDER_COLOR)}
                                },
                                "width": {"magnitude": 1, "unit": "PT"},
                                "padding": {"magnitude": 1, "unit": "PT"},
                                "dashStyle": "SOLID",
                            }
                        },
                        "fields": "borderBottom",
                    }
                }
            )
        elif para_style != "NORMAL_TEXT":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": line_start,
                            "endIndex": line_end,
                        },
                        "paragraphStyle": {"namedStyleType": para_style},
                        "fields": "namedStyleType",
                    }
                }
            )
        if list_preset:
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": {
                            "startIndex": line_start,
                            "endIndex": line_end,
                        },
                        "bulletPreset": list_preset,
                    }
                }
            )
            tabs_stripped_so_far += list_level
        # Inline styles: by now this line's own leading tabs (if any) have
        # already been stripped by the createParagraphBullets request above,
        # so the real text starts at the (shifted) line start.
        pos = line_start
        for run in runs:
            if not run.text:
                continue
            run_end = pos + len(run.text)
            text_style: dict = {}
            fields: list[str] = []
            if run.bold:
                text_style["bold"] = True
                fields.append("bold")
            if run.italic:
                text_style["italic"] = True
                fields.append("italic")
            if run.strikethrough:
                text_style["strikethrough"] = True
                fields.append("strikethrough")
            if run.underline:
                text_style["underline"] = True
                fields.append("underline")
            if run.code:
                text_style["weightedFontFamily"] = {"fontFamily": _CODE_FONT_FAMILY}
                fields.append("weightedFontFamily")
            if run.highlight:
                text_style["backgroundColor"] = {
                    "color": {"rgbColor": _hex_to_rgb_dict(_DEFAULT_HIGHLIGHT_COLOR)}
                }
                fields.append("backgroundColor")
            if run.url:
                text_style["link"] = {"url": run.url}
                fields.append("link")
            if fields:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": pos,
                                "endIndex": run_end,
                            },
                            "textStyle": text_style,
                            "fields": ",".join(fields),
                        }
                    }
                )
            pos = run_end

    return requests


# ------------------------------------------------------------------ #
# Markdown tables -- handled separately from _markdown_to_docs_requests
# because a Docs table is a structural element (insertTable), not text:
# it can't be produced by the single insertText call every other block
# type shares, and the API doesn't hand back a new table's cell indices
# synchronously, so filling cell content needs a re-fetch after the
# table is created (see DriveClient._insert_table_at_placeholder).
# ------------------------------------------------------------------ #

_TABLE_SEP_CELL_RE = _re.compile(r"^:?-+:?$")


class TableBlock(NamedTuple):
    # rows[0] is the header row; every row (including the header) has the
    # same number of cells as the header. Cell values are raw Markdown,
    # rendered later through the same inline-run machinery as everything else.
    rows: list[list[str]]
    alignments: list[str]  # one of "START"/"CENTER"/"END" per column
    placeholder: str


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _re.split(r"(?<!\\)\|", line)]


def _is_table_separator_row(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(cell) for cell in cells)


def _table_column_alignments(sep_cells: list[str]) -> list[str]:
    alignments = []
    for cell in sep_cells:
        left, right = cell.startswith(":"), cell.endswith(":")
        alignments.append("CENTER" if left and right else "END" if right else "START")
    return alignments


def _extract_tables(
    markdown: str, placeholder_prefix: str = "PRIVACYFENCE_TABLE_PLACEHOLDER_"
) -> tuple[str, list[TableBlock]]:
    """Pull every GFM pipe-table block out of ``markdown``, replacing each
    with a unique placeholder line, and return (text_with_placeholders,
    tables). A block is recognized as a table when a line containing ``|``
    is immediately followed by a separator row (only ``-``, ``:``, ``|`` and
    spaces) -- the same rule GFM itself uses. Body rows are collected while
    they keep containing ``|`` and aren't blank; short/long rows are
    padded/truncated to the header's column count.

    ``placeholder_prefix`` lets a caller that extracts tables from the same
    Markdown more than once (``edit_doc_content``, once per ``find_text``
    occurrence under ``replace_all``) keep every occurrence's placeholders
    unique -- otherwise two copies of the same table Markdown would produce
    identical placeholder text at multiple document locations, and
    ``_insert_table_at_placeholder``'s single-match lookup would find both
    and refuse to guess which one it was asked to fill in.
    """
    lines = markdown.split("\n")
    out_lines: list[str] = []
    tables: list[TableBlock] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and _is_table_separator_row(lines[i + 1]):
            header = _split_table_row(line)
            n_cols = len(header)
            alignments = _table_column_alignments(_split_table_row(lines[i + 1]))
            rows = [header]
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                row = _split_table_row(lines[j])
                rows.append((row + [""] * n_cols)[:n_cols])
                j += 1
            # A control-character-wrapped placeholder (e.g. "\x00...\x00")
            # would be simpler to guarantee uniqueness for, but the Docs
            # API's insertText silently strips control characters
            # (U+0000-U+0008, U+000C-U+001F) and Private Use Area characters
            # (U+E000-U+F8FF) from inserted text, so the placeholder actually
            # written to the document would never match what's searched for
            # here. Plain text it is.
            placeholder = f"{placeholder_prefix}{len(tables)}"
            tables.append(TableBlock(rows=rows, alignments=alignments, placeholder=placeholder))
            out_lines.append(placeholder)
            i = j
        else:
            out_lines.append(line)
            i += 1
    return "\n".join(out_lines), tables


def _table_cell_start_indices(doc: dict, insert_location_index: int) -> list[list[int]]:
    """Return the just-inserted table's cell start indices as doc[row][col],
    read back from a fresh ``documents().get()`` response -- Docs assigns
    these itself and doesn't return them from the insertTable request, so
    this is the only reliable way to know where to insert each cell's text.

    ``insert_location_index`` is the ``location.index`` the insertTable
    request itself used, not the table's own resulting ``startIndex`` --
    the Docs API always inserts a newline immediately before a table (so it
    never merges into the preceding paragraph), so the table structural
    element actually starts one index past where it was requested.
    """
    table_start_index = insert_location_index + 1
    for element in doc.get("body", {}).get("content", []):
        table = element.get("table")
        if table is None or element.get("startIndex") != table_start_index:
            continue
        grid: list[list[int]] = []
        for row in table.get("tableRows", []):
            row_starts = []
            for cell in row.get("tableCells", []):
                cell_content = cell.get("content", [])
                if not cell_content or "startIndex" not in cell_content[0]:
                    raise DriveClientError("write_doc_rich_content: inserted table cell had no content")
                row_starts.append(cell_content[0]["startIndex"])
            grid.append(row_starts)
        return grid
    raise DriveClientError("write_doc_rich_content: could not locate the inserted table")


def _docs_plain_text_with_index_map(doc: dict) -> tuple[str, list[tuple[int, int, int]]]:
    """Concatenate a Doc's body text runs into plain text, alongside a map
    back to Docs API indices.

    Returns ``(plain_text, runs)`` where each entry in ``runs`` is
    ``(plain_start, plain_end, docs_start)`` — the interval
    ``[plain_start, plain_end)`` in ``plain_text`` came from a single text run
    whose first character sits at Docs index ``docs_start``, so any offset
    ``o`` in that interval maps to Docs index ``docs_start + (o - plain_start)``.
    Runs tile ``plain_text`` contiguously with no gaps, even though the
    underlying Docs indices can have gaps between runs (e.g. around a table
    or image) — those simply don't appear in ``plain_text`` at all, the same
    way they're absent from the tools' Markdown-only formatting model.
    """
    plain_text = ""
    runs: list[tuple[int, int, int]] = []
    for element in doc.get("body", {}).get("content", []):
        for para_element in element.get("paragraph", {}).get("elements", []):
            text_run = para_element.get("textRun")
            docs_start = para_element.get("startIndex")
            content = text_run.get("content", "") if text_run else ""
            if not content or docs_start is None:
                continue
            plain_start = len(plain_text)
            plain_text += content
            runs.append((plain_start, plain_start + len(content), docs_start))
    return plain_text, runs


def _offset_to_docs_index(offset: int, runs: list[tuple[int, int, int]]) -> int:
    """Map a plain-text offset (from _docs_plain_text_with_index_map) to a
    Docs API index. Accepts the boundary offset one past the last run too,
    so both the start and the (exclusive) end of a matched span resolve."""
    for plain_start, plain_end, docs_start in runs:
        if plain_start <= offset <= plain_end:
            return docs_start + (offset - plain_start)
    raise DriveClientError(f"Could not map text offset {offset} into the document")


def _find_text_matches(plain_text: str, find_text: str) -> list[tuple[int, int]]:
    """Return every non-overlapping (start, end) span where find_text occurs."""
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = plain_text.find(find_text, start)
        if idx == -1:
            break
        matches.append((idx, idx + len(find_text)))
        start = idx + len(find_text)
    return matches


# ------------------------------------------------------------------ #
# Google Docs API structure -> Markdown (read side)
# ------------------------------------------------------------------ #
# Headings, inline styles (bold/italic/strikethrough/underline/code/link/
# highlight), horizontal-rule dividers, lists (including nesting), and real
# GFM table grids all render as the same Markdown dialect
# _markdown_to_docs_requests/_parse_inline_runs/_THEMATIC_BREAK_RE/
# _extract_tables parse on the write side, so a document read this way
# round-trips back through write_doc_rich_content/edit_doc_content
# unchanged (highlight only up to its one fixed default color -- see
# _docs_run_color_notes for the exact-color sidecar Markdown itself can't
# carry). Known, deliberate gaps: a custom numbered-list start value isn't
# preserved (every line reads back "1. ", since Docs auto-numbers by list
# position and the write side's own regex accepts any digit), a soft line
# break (Shift+Enter) reads as a plain space rather than a distinct
# construct, and a <br>-joined multi-paragraph table cell round-trips as
# literal "<br>" text rather than a real line break.

_HEADING_STYLE_TO_PREFIX = {style: prefix for prefix, style in _HEADING_PREFIXES}


def _docs_text_run_to_markdown(text_run: dict, *, suppress_bold: bool = False) -> str:
    """Render one Docs API textRun (its literal content plus textStyle) as
    Markdown, wrapped in the delimiter nesting order _parse_inline_runs
    reads back to the same flags: a link or `code` span innermost (mutually
    exclusive -- see below), then bold/italic, then underline, then
    strikethrough, then highlight outermost.

    ``suppress_bold`` drops the bold wrap even when ``textStyle.bold`` is
    set -- used only for a GFM table's header row:
    write_doc_rich_content always bolds row 0 on write
    (``_insert_table_at_placeholder``), so reading that same bold back as
    literal ``**...**`` would double it up on the next write. GFM's own
    header row already renders visually bold with no markers needed, so
    this is a lossless round trip, not a real loss -- a header cell with
    *additional*, deliberately-chosen styling (italic, a link, ...) still
    keeps everything except bold.

    The paragraph's own trailing "\\n" (only ever present on a paragraph's
    *last* run) is stripped before wrapping and never re-added here --
    wrapping it along with real text would leave a raw newline inside a
    delimited span, e.g. "**bold text\\n**", which corrupts every line
    boundary downstream of it. The caller (_docs_content_elements_to_
    markdown) is what turns one rendered paragraph into its own line.

    A soft line break (Shift+Enter) is a literal U+000B inside a run's
    content, not a new paragraph -- rendered as a plain space for now
    (safe: never corrupts a delimiter the way re-emitting it as "\\n"
    would), a known, deliberate loss with no representation in this dialect.

    Highlight is rendered as a bare ==...== regardless of the run's actual
    color (matching the write side's own single fixed default,
    _DEFAULT_HIGHLIGHT_COLOR) -- Markdown as this parser defines it has
    exactly one highlight color, so that's the most this function alone
    can losslessly represent. The exact hex, when it differs from the
    default, is reported separately -- see _docs_run_color_notes -- rather
    than invented as new inline syntax here, which would break round-trip
    parsing for every other caller of the write side's own Markdown
    dialect.
    """
    content = text_run.get("content", "").replace("\x0b", " ").rstrip("\n")
    if not content:
        return ""
    style = text_run.get("textStyle", {})
    url = style.get("link", {}).get("url", "")
    is_code = style.get("weightedFontFamily", {}).get("fontFamily") == _CODE_FONT_FAMILY
    if url:
        # Link text is opaque on the write side too (_parse_inline_runs
        # never reparses it), so this is exactly reversible. A run that is
        # simultaneously link *and* code-styled has no representation in
        # this dialect -- a code span isn't link-capable here, matching
        # how the parser never recurses into link text -- so it renders as
        # a plain link, dropping the monospace styling; keeping the link
        # is the more useful of the two, and a hyperlinked code span is
        # rare enough in practice not to warrant new syntax for it.
        content = f"[{content}]({url})"
    elif is_code:
        content = f"`{content}`"
    bold = bool(style.get("bold")) and not suppress_bold
    italic = bool(style.get("italic"))
    if bold and italic:
        content = f"***{content}***"
    elif bold:
        content = f"**{content}**"
    elif italic:
        content = f"*{content}*"
    if style.get("underline"):
        content = f"__{content}__"
    if style.get("strikethrough"):
        content = f"~~{content}~~"
    if style.get("backgroundColor"):
        content = f"=={content}=="
    return content


def _docs_run_color_notes(text_run: dict) -> tuple[str, str]:
    """Return (highlight_hex, text_color_hex) for one textRun, each "" when
    absent -- the color sidecar, read alongside (but independently of)
    _docs_text_run_to_markdown.

    highlight_hex is also "" when the run's highlight color exactly matches
    _DEFAULT_HIGHLIGHT_COLOR -- the plain ==...== that function already
    emits represents that case losslessly on its own, so it needs no
    sidecar entry. text_color has no Markdown syntax at all (the write
    side's own dialect can only set it via drive_docs_format_content, never
    through Markdown), so every text color is reported, default or not.
    """
    content = text_run.get("content", "").replace("\x0b", " ").rstrip("\n")
    if not content:
        return "", ""
    style = text_run.get("textStyle", {})
    highlight_hex = ""
    bg_rgb = style.get("backgroundColor", {}).get("color", {}).get("rgbColor")
    if bg_rgb is not None:
        hex_color = _rgb_dict_to_hex(bg_rgb)
        if hex_color.lower() != _DEFAULT_HIGHLIGHT_COLOR.lower():
            highlight_hex = hex_color
    text_color_hex = ""
    fg_rgb = style.get("foregroundColor", {}).get("color", {}).get("rgbColor")
    if fg_rgb is not None:
        text_color_hex = _rgb_dict_to_hex(fg_rgb)
    return highlight_hex, text_color_hex


def _docs_content_elements_color_sidecar(
    content_elements: list[dict], highlights: list[dict], text_colors: list[dict]
) -> None:
    """Walk a Docs API list of structural elements -- mirrors
    _docs_content_elements_to_markdown's traversal (top-level body content,
    or recursing into a table cell's own content) -- appending every run's
    non-default highlight/text color to highlights/text_colors in place.
    See _docs_run_color_notes.
    """
    for element in content_elements:
        paragraph = element.get("paragraph")
        if paragraph is not None:
            for para_element in paragraph.get("elements", []):
                text_run = para_element.get("textRun")
                if text_run is None:
                    continue
                highlight_hex, text_color_hex = _docs_run_color_notes(text_run)
                if not highlight_hex and not text_color_hex:
                    continue
                content = text_run.get("content", "").replace("\x0b", " ").rstrip("\n")
                if highlight_hex:
                    highlights.append({"text": content, "hex": highlight_hex})
                if text_color_hex:
                    text_colors.append({"text": content, "hex": text_color_hex})
            continue
        table = element.get("table")
        if table is not None:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    _docs_content_elements_color_sidecar(
                        cell.get("content", []), highlights, text_colors
                    )
            continue


def _docs_structure_color_sidecar(doc: dict) -> tuple[list[dict], list[dict]]:
    """Return (highlights, text_colors) for a Docs API ``documents().get()``
    response -- each a list of ``{"text": ..., "hex": "#rrggbb"}``, for
    every run whose exact color the plain Markdown from
    _docs_structure_to_markdown can't represent on its own.
    """
    highlights: list[dict] = []
    text_colors: list[dict] = []
    _docs_content_elements_color_sidecar(
        doc.get("body", {}).get("content", []), highlights, text_colors
    )
    return highlights, text_colors


_ORDERED_LIST_GLYPH_TYPES = {
    "DECIMAL", "ZERO_DECIMAL", "UPPER_ALPHA", "ALPHA", "UPPER_ROMAN", "ROMAN",
}


def _docs_list_nesting_is_ordered(doc_lists: dict, list_id: str, nesting_level: int) -> bool:
    """Whether one nesting level of one list (from the document's top-level
    ``lists`` map) is numbered rather than bulleted -- the Docs API records
    this once per level via each ``NestingLevel``'s ``glyphType``
    (``DECIMAL``, ``UPPER_ROMAN``, ...) for a numbered level, versus a
    ``glyphSymbol`` (a literal bullet character) for an unordered one. An
    unrecognized or missing ``list_id``/level defaults to unordered -- the
    same visual default the Docs UI itself uses for a plain bulleted list,
    and the write side's own ``BULLET_DISC_CIRCLE_SQUARE`` default preset.
    """
    nesting_levels = doc_lists.get(list_id, {}).get("listProperties", {}).get("nestingLevels", [])
    if nesting_level >= len(nesting_levels):
        return False
    return nesting_levels[nesting_level].get("glyphType", "") in _ORDERED_LIST_GLYPH_TYPES


def _docs_paragraph_is_divider(paragraph: dict) -> bool:
    """Whether a paragraph is a horizontal-rule divider, read back as a bare
    ``---`` line. Two cases, both real:

    - A ``horizontalRule`` paragraph element -- present when a human
      inserted one through the Docs UI's own Insert > Horizontal line. This
      is a genuine structural element the API can read even though it has
      no way to *write* one (see the next case).
    - An empty paragraph with ``paragraphStyle.borderBottom`` set -- what
      ``write_doc_rich_content`` actually produces for a `---`/`***`/`___`
      line, since the Docs API has no native "insert horizontal rule"
      request (_THEMATIC_BREAK_RE's own comment explains why). Checked only
      when the paragraph is otherwise empty, so a real paragraph of prose
      that happens to carry a bottom border for some unrelated reason keeps
      its text instead of silently losing it.
    """
    para_elements = paragraph.get("elements", [])
    if any("horizontalRule" in pe for pe in para_elements):
        return True
    if not paragraph.get("paragraphStyle", {}).get("borderBottom"):
        return False
    text = "".join(pe["textRun"].get("content", "") for pe in para_elements if "textRun" in pe)
    return not text.strip("\n")


def _docs_content_elements_to_markdown(
    content_elements: list[dict], doc_lists: dict | None = None, *, suppress_bold: bool = False
) -> str:
    """Render a Docs API list of structural elements -- either
    ``doc["body"]["content"]`` itself, or one table cell's own
    ``["content"]`` -- as Markdown. Shared by both so a table cell's text
    gets the same heading/inline-style/list treatment as top-level document
    content (see _docs_structure_to_markdown). ``doc_lists`` is the whole
    document's top-level ``lists`` map (needed to resolve a list paragraph's
    ordered/unordered glyph type, per-list rather than per-paragraph) --
    defaults to ``None`` (treated as ``{}``) for a caller with no list
    content to worry about, which resolves every list paragraph as
    unordered (see _docs_list_nesting_is_ordered's own default).
    ``suppress_bold`` is passed straight through to every
    _docs_text_run_to_markdown call -- see that function's own docstring;
    used only when rendering a GFM table's header-row cells, never set by
    the top-level document walk.
    """
    doc_lists = doc_lists or {}
    lines: list[str] = []
    for element in content_elements:
        paragraph = element.get("paragraph")
        if paragraph is not None:
            para_elements = paragraph.get("elements", [])
            if _docs_paragraph_is_divider(paragraph):
                # Exact reverse of the write side's _THEMATIC_BREAK_RE
                # handling: a divider paragraph carries no text of its own
                # worth rendering, so this is unambiguous -- render the bare
                # divider line rather than falling through to heading_prefix
                # (a divider paragraph never carries a namedStyleType worth
                # applying either) or being skipped as "no text" the way
                # other non-text elements below still are. See
                # _docs_paragraph_is_divider's own docstring for the two
                # real Docs API shapes this recognizes.
                lines.append("---")
                continue
            line = "".join(
                _docs_text_run_to_markdown(para_element["textRun"], suppress_bold=suppress_bold)
                for para_element in para_elements
                # Non-text paragraph elements (inlineObjectElement/images,
                # a footnote reference, ...) have no text to render in
                # this phase -- see the module comment above.
                if "textRun" in para_element
            )
            bullet = paragraph.get("bullet")
            if bullet is not None:
                # Exact reverse of the write side's 2-spaces-per-nesting-
                # level convention (_MAX_LIST_NESTING) -- a list paragraph
                # never legitimately carries a namedStyleType either, so
                # this takes priority over heading_prefix below the same
                # way the horizontal-rule check above takes priority over
                # both. The literal digit in a numbered marker doesn't
                # matter -- _markdown_to_docs_requests' own numbered-list
                # regex (`^\d+\. `) accepts any digit(s), and Docs
                # auto-numbers by list position, not by what's typed -- so
                # "1. " for every line is exactly as correct as counting.
                nesting_level = bullet.get("nestingLevel", 0)
                list_id = bullet.get("listId", "")
                marker = "1. " if _docs_list_nesting_is_ordered(doc_lists, list_id, nesting_level) else "- "
                lines.append(("  " * nesting_level) + marker + line)
                continue
            heading_prefix = _HEADING_STYLE_TO_PREFIX.get(
                paragraph.get("paragraphStyle", {}).get("namedStyleType", ""), ""
            )
            lines.append(heading_prefix + line)
            continue
        table = element.get("table")
        if table is not None:
            lines.append(_docs_table_to_markdown(table, doc_lists))
            continue
        # tableOfContents, sectionBreak, and anything else Docs can put in
        # body.content has no Markdown-dialect representation yet and no
        # plain-text-export precedent worth preserving either -- skipped.
    return "\n".join(lines)


_ALIGNMENT_TO_SEPARATOR = {"START": "---", "CENTER": ":---:", "END": "---:"}


def _docs_table_column_alignment(table: dict, col_index: int) -> str:
    """Read one column's alignment ("START"/"CENTER"/"END") from its header
    (row 0) cell's first paragraph -- the reverse of
    _table_column_alignments. Defaults to "START" (GFM's own default, a
    plain "---" separator cell) for a missing row/column, an unaligned
    paragraph, or anything CommonMark can't express (e.g. JUSTIFIED).
    _insert_table_at_placeholder applies one alignment per column
    uniformly across every row when writing, so the header row's own
    alignment is representative of the whole column.
    """
    rows = table.get("tableRows", [])
    if not rows:
        return "START"
    cells = rows[0].get("tableCells", [])
    if col_index >= len(cells):
        return "START"
    for element in cells[col_index].get("content", []):
        paragraph = element.get("paragraph")
        if paragraph is None:
            continue
        alignment = paragraph.get("paragraphStyle", {}).get("alignment", "START")
        return alignment if alignment in _ALIGNMENT_TO_SEPARATOR else "START"
    return "START"


def _docs_table_to_markdown(table: dict, doc_lists: dict) -> str:
    """Render a Docs API table element as a GFM pipe table -- the read-side
    mirror of _extract_tables/_insert_table_at_placeholder.

    Each cell's own content renders through _docs_content_elements_to_
    markdown recursively (so headings, inline styles, and even nested
    lists/tables inside a cell still work), with two cell-specific
    adjustments neither prose paragraphs nor list items need:

    - Multiple paragraphs in one cell join with ``<br>`` -- GFM has no
      other way to represent an embedded newline inside a table cell.
    - A literal ``|`` is escaped as ``\\|``, the same direction
      _split_table_row's own unescape (``.replace("\\|", "|")``) expects.

    Row 0 (the header) renders with every run's bold suppressed --
    write_doc_rich_content always bolds row 0 on write
    (_insert_table_at_placeholder), so reading that same bold back as
    literal ``**...**`` would double it up on the next write; see
    _docs_text_run_to_markdown's own docstring.
    """
    rows: list[list[str]] = []
    for r, row in enumerate(table.get("tableRows", [])):
        cells = []
        for cell in row.get("tableCells", []):
            rendered = _docs_content_elements_to_markdown(
                cell.get("content", []), doc_lists, suppress_bold=(r == 0)
            )
            cells.append("<br>".join(line.replace("|", "\\|") for line in rendered.split("\n")))
        rows.append(cells)
    if not rows:
        return ""
    n_cols = len(rows[0])
    separator = [_ALIGNMENT_TO_SEPARATOR[_docs_table_column_alignment(table, c)] for c in range(n_cols)]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(separator) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _docs_structure_to_markdown(doc: dict) -> str:
    """Render a Docs API ``documents().get()`` response as Markdown -- the
    read-side mirror of _markdown_to_docs_requests.
    """
    return _docs_content_elements_to_markdown(
        doc.get("body", {}).get("content", []), doc.get("lists", {})
    )


# ------------------------------------------------------------------ #
# Sheets API helpers
# ------------------------------------------------------------------ #

def _col_letters_to_index(letters: str) -> int:
    """Convert an A1 column reference ('A', 'Z', 'AA', ...) to a 0-based index."""
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _parse_a1_range(range_a1: str) -> dict:
    """Parse a fully-bounded A1 range ('A1:C10') into a 0-indexed GridRange dict
    (without sheetId, which the caller merges in).

    Only the ``<col><row>:<col><row>`` form is supported - no whole-row,
    whole-column, or sheet-name-prefixed references. The caller specifies the
    sheet separately via sheet_id, so no sheet-name prefix is expected here.
    """
    m = _re.match(r"^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$", range_a1.strip())
    if not m:
        raise DriveClientError(
            f"Unsupported range syntax {range_a1!r}; use a fully-bounded "
            "range like 'A1:C10' (no sheet-name prefix, no open-ended rows/columns)."
        )
    c1, r1, c2, r2 = m.groups()
    col1, col2 = _col_letters_to_index(c1), _col_letters_to_index(c2)
    row1, row2 = int(r1) - 1, int(r2) - 1
    return {
        "startRowIndex": min(row1, row2),
        "endRowIndex": max(row1, row2) + 1,
        "startColumnIndex": min(col1, col2),
        "endColumnIndex": max(col1, col2) + 1,
    }


def _hex_to_rgb_dict(hex_color: str) -> dict:
    """Convert '#rrggbb' (or 'rrggbb') to a Sheets API Color dict (0..1 floats)."""
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        raise DriveClientError(f"Invalid hex color {hex_color!r}; expected '#rrggbb'")
    try:
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise DriveClientError(f"Invalid hex color {hex_color!r}: {exc}") from exc
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


def _rgb_dict_to_hex(color: dict) -> str:
    """Convert a Sheets API Color dict (0..1 floats, missing channel = 0) to
    '#rrggbb' -- the inverse of _hex_to_rgb_dict."""
    r = round(color.get("red", 0) * 255)
    g = round(color.get("green", 0) * 255)
    b = round(color.get("blue", 0) * 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def _cell_format_summary(fmt: dict) -> dict:
    """Compact one cell's userEnteredFormat down to the aspects
    format_sheet_range can set, omitting anything left at its default so an
    unformatted cell summarizes to {}."""
    out: dict = {}
    text_style = fmt.get("textFormat", {})
    if text_style.get("bold"):
        out["bold"] = True
    if text_style.get("italic"):
        out["italic"] = True
    if "foregroundColor" in text_style:
        out["text_color"] = _rgb_dict_to_hex(text_style["foregroundColor"])
    if "backgroundColor" in fmt:
        out["background_color"] = _rgb_dict_to_hex(fmt["backgroundColor"])
    if "numberFormat" in fmt:
        out["number_format"] = fmt["numberFormat"].get("pattern", "")
    if "horizontalAlignment" in fmt:
        out["horizontal_alignment"] = fmt["horizontalAlignment"]
    if "verticalAlignment" in fmt:
        out["vertical_alignment"] = fmt["verticalAlignment"]
    if "wrapStrategy" in fmt:
        out["wrap_strategy"] = fmt["wrapStrategy"]
    return out


class DriveClientError(Exception):
    """Raised for unrecoverable Drive client problems (auth, config, API)."""


@dataclass
class DriveFile:
    """A normalized Drive file (metadata only)."""

    id: str
    name: str
    mime_type: str
    size: int  # bytes, 0 if unknown (Google Docs report no size)
    created_time: str = ""
    modified_time: str = ""
    owners: list[str] = field(default_factory=list)  # owner email addresses
    shared: bool = False
    web_view_link: str = ""
    parent_ids: list[str] = field(default_factory=list)
    drive_id: str = ""  # non-empty when the file lives in a Shared Drive
    thumbnail_link: str = ""  # signed URL to a Drive-generated preview image, if any

    def short_summary(self) -> str:
        """Human-readable one-liner for the review UI / logs."""
        name = self.name or "(unnamed)"
        return f"{name} ({self.mime_type})"


@dataclass
class DriveFileContent:
    """A Drive file's content after fetching.

    ``content_text`` carries exported text for Google Docs/Sheets/Slides and
    decoded text for text-like binaries. ``content_bytes`` carries raw bytes for
    other binary files. Exactly one of them is normally populated.

    ``highlights``/``text_colors`` are the color sidecar -- populated only
    for a Google Doc, and only when there's something to say: a highlight
    whose exact color isn't the tool's own default (the plain ``==...==``
    Markdown already represents that case losslessly), or any text color at
    all (Markdown has no syntax for that whatsoever, so every text color is
    reported). Each entry is ``{"text": <run's own text>, "hex": "#rrggbb"}``.
    Empty for every other file type.
    """

    file: DriveFile
    content_text: str = ""
    content_bytes: bytes = b""
    truncated: bool = False
    highlights: list[dict] = field(default_factory=list)
    text_colors: list[dict] = field(default_factory=list)


class DriveClient:
    """Read-only Google Drive client with OAuth2 token caching."""

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        # googleapiclient service objects (and the httplib2 transport they
        # wrap) are not thread-safe. Requests are dispatched to a thread per
        # call (see connectors/*.py._fetch), so a single shared service can
        # have two threads read/write the same socket concurrently,
        # corrupting the connection (observed as SSL: WRONG_VERSION_NUMBER
        # on a later, unrelated request reusing the same connection). Keep
        # one service per thread instead of one shared instance.
        self._local = threading.local()
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        Opens a local browser window, lets the user grant access, then writes
        the token to ``token_file``. ``client_config`` comes from the
        organization config bundle (installed via the menu bar), not a file
        on disk.
        """
        if not self._client_config:
            raise DriveClientError(
                "No Google organization config installed. Install/Update "
                "Organization Config from the PrivacyFence menu bar first."
            )

        logger.info("Starting interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        """Load cached credentials, refreshing them if expired.

        Raises if no usable token exists - the user must run `--oauth-setup`.
        """
        # Guards concurrent refresh/save of the shared token file when
        # multiple threads hit an expired token at the same time.
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise DriveClientError(
                    f"No OAuth token found at '{self._token_file}'. "
                    "Run the application once with '--oauth-setup' to authorize."
                )

            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

            if creds.valid:
                return creds

            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:  # noqa: BLE001 - surface a clear message
                    raise DriveClientError(
                        f"Failed to refresh OAuth token: {exc}. "
                        "Re-run with '--oauth-setup' to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds

            raise DriveClientError(
                "Cached OAuth token is invalid and cannot be refreshed. "
                "Re-run with '--oauth-setup' to re-authorize."
            )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        # Tighten permissions - this file is a bearer credential.
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:  # pragma: no cover - best effort on non-POSIX
            logger.debug("Could not chmod token file (non-fatal)")

    def _get_service(self):
        """Build (or reuse) the Drive API service resource for this thread."""
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            # cache_discovery=False avoids noisy warnings without a file cache.
            service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
            self._local.service = service
            logger.debug("Drive API service initialized for thread %s", threading.current_thread().name)
        return service

    def get_credentials(self) -> Credentials:
        """Expose the cached OAuth credentials for sibling API clients (Sheets)
        that reuse the Drive OAuth grant instead of requesting their own scope."""
        return self._load_credentials()

    def _get_docs_service(self):
        """Build (or reuse) the Docs API service resource for this thread.

        Reuses the Drive OAuth grant (the Docs API v1 accepts the ``drive``
        scope) the same way ``_get_sheets_service`` does for Sheets.
        """
        service = getattr(self._local, "docs_service", None)
        if service is None:
            creds = self._load_credentials()
            service = build(
                "docs", "v1", credentials=creds, cache_discovery=False
            )
            self._local.docs_service = service
            logger.debug("Docs API service initialized for thread %s", threading.current_thread().name)
        return service

    def check_connection(self) -> str:
        """Verify the credentials work. Returns the authorized email address."""
        try:
            about = self._get_service().about().get(fields="user").execute()
        except HttpError as exc:
            raise DriveClientError(f"Drive connection check failed: {exc}") from exc
        email = about.get("user", {}).get("emailAddress", "unknown")
        logger.info("Connected to Drive as %s", email)
        return email

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #
    def list_files(self, query: str, max_results: int = 20) -> list[DriveFile]:
        """List files matching a Drive search query (the ``q`` parameter).

        See https://developers.google.com/drive/api/guides/search-files for the
        query syntax. Returns normalized ``DriveFile`` metadata.
        """
        max_results = self._clamp_max_results(max_results)
        service = self._get_service()
        try:
            response = (
                service.files()
                .list(
                    q=query or None,
                    pageSize=max_results,
                    fields=f"files({_FILE_FIELDS})",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"list_files failed: {exc}") from exc

        files = [self._parse_file(f) for f in response.get("files", [])]
        logger.info("list_files query=%r returned %d files", query, len(files))
        return files

    def get_file_metadata(self, file_id: str) -> DriveFile:
        """Fetch metadata for a single file."""
        if not file_id:
            raise DriveClientError("get_file_metadata requires a non-empty file_id")
        service = self._get_service()
        try:
            raw = (
                service.files()
                .get(fileId=file_id, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(
                f"get_file_metadata({file_id}) failed: {exc}"
            ) from exc
        drive_file = self._parse_file(raw)
        logger.info("get_file_metadata %s: %s", file_id, drive_file.short_summary())
        return drive_file

    def get_file_content(
        self, file_id: str, max_bytes: int = 102400
    ) -> DriveFileContent:
        """Fetch a file's content, capped at ``max_bytes``.

        A Google Doc is fetched through the Docs API's structured
        ``documents().get()`` (the same call the ``docs_*`` write tools
        already use) and rendered to Markdown -- headings, inline styles
        (bold/italic/strikethrough/underline/code/link/highlight),
        horizontal-rule dividers, lists (including nesting), and GFM pipe
        tables (a real grid, with column alignment) all round-trip through
        the same dialect ``_markdown_to_docs_requests``/
        ``_parse_inline_runs``/``_extract_tables`` parse on the write side.
        A non-default highlight/text color also populates
        ``highlights``/``text_colors`` (Markdown alone can't carry an exact
        color). Slides are still exported as plain text and Sheets as CSV.
        Other files are downloaded as raw bytes. If the
        content exceeds ``max_bytes`` it is truncated and ``truncated``
        is set to True -- for a Doc, truncation lands on the last complete
        line at or before the cap rather than an arbitrary byte offset, so a
        truncated result never lands mid-delimiter and stays safe to feed
        back into ``edit_doc_content``.
        """
        if not file_id:
            raise DriveClientError("get_file_content requires a non-empty file_id")
        if max_bytes <= 0:
            max_bytes = 102400

        metadata = self.get_file_metadata(file_id)

        if metadata.mime_type == _GOOGLE_DOC_MIME_TYPE:
            content = self._get_doc_content_as_markdown(file_id, metadata, max_bytes)
            logger.info(
                "get_file_content %s: %d bytes (truncated=%s, text=True, format=markdown)",
                file_id,
                len(content.content_text.encode("utf-8")),
                content.truncated,
            )
            return content

        service = self._get_service()
        export_mime = _GOOGLE_DOC_EXPORTS.get(metadata.mime_type)
        try:
            if export_mime is not None:
                request = service.files().export_media(
                    fileId=file_id, mimeType=export_mime
                )
            else:
                request = service.files().get_media(
                    fileId=file_id, supportsAllDrives=True
                )
            data = self._download(request, max_bytes)
        except HttpError as exc:
            raise DriveClientError(
                f"get_file_content({file_id}) failed: {exc}"
            ) from exc

        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]

        # Workspace exports are always text; for downloads, only treat clearly
        # text-like MIME types as text, otherwise keep raw bytes.
        is_text = export_mime is not None or metadata.mime_type.startswith("text/")
        content = DriveFileContent(file=metadata, truncated=truncated)
        if is_text:
            content.content_text = data.decode("utf-8", errors="replace")
        else:
            content.content_bytes = data

        logger.info(
            "get_file_content %s: %d bytes (truncated=%s, text=%s)",
            file_id,
            len(data),
            truncated,
            is_text,
        )
        return content

    def _get_doc_content_as_markdown(
        self, file_id: str, metadata: DriveFile, max_bytes: int
    ) -> DriveFileContent:
        """Fetch a Google Doc's structure and render it to Markdown, then
        truncate at a line boundary rather than an arbitrary byte offset --
        a byte-exact cut isn't safe here the way it is for the plain byte
        stream every other branch of ``get_file_content`` truncates: it
        could land mid-delimiter (e.g. right after an opening ** with no
        closing pair), corrupting every subsequent parse of the truncated
        Markdown.

        Unlike the streaming byte download the other branches use, the Docs
        API always returns the whole document in one response -- there's no
        way to stop fetching partway through and still have a parseable
        structure, so a very large Doc costs one full fetch here regardless
        of ``max_bytes``. Accepted tradeoff; no fix proposed for it.
        """
        docs_service = self._get_docs_service()
        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"get_file_content({file_id}) failed: {exc}"
            ) from exc

        markdown = _docs_structure_to_markdown(doc)
        highlights, text_colors = _docs_structure_color_sidecar(doc)
        data = markdown.encode("utf-8")
        truncated = len(data) > max_bytes
        if truncated:
            cut = data[:max_bytes]
            last_newline = cut.rfind(b"\n")
            if last_newline > 0:
                cut = cut[:last_newline]
            markdown = cut.decode("utf-8", errors="replace")
            # A sidecar entry for text that fell outside the truncated
            # Markdown would misleadingly describe content the caller can
            # no longer see -- keep only entries whose text still appears
            # in the truncated result.
            highlights = [h for h in highlights if h["text"] in markdown]
            text_colors = [c for c in text_colors if c["text"] in markdown]

        return DriveFileContent(
            file=metadata,
            content_text=markdown,
            truncated=truncated,
            highlights=highlights,
            text_colors=text_colors,
        )

    def download_file(
        self, file_id: str, destination_dir: str = ""
    ) -> dict[str, Any]:
        """Download a file to a local directory and return the saved path.

        ``destination_dir`` is mandatory -- see ``resolve_download_destination``.
        Google Workspace documents are exported as text (Docs/Slides → .txt,
        Sheets → .csv). Binary files are saved with their original extension.
        Returns a dict with ``path``, ``name``, ``size_bytes``, and
        ``truncated`` (always False for full downloads).
        """
        if not file_id:
            raise DriveClientError("download_file requires a non-empty file_id")

        metadata = self.get_file_metadata(file_id)
        export_mime = _GOOGLE_DOC_EXPORTS.get(metadata.mime_type)
        dest_path = resolve_download_destination(metadata, destination_dir)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        try:
            creds = self._load_credentials()
            session = AuthorizedSession(creds)
            if export_mime is not None:
                url = (
                    "https://www.googleapis.com/drive/v3/files/"
                    f"{file_id}/export?mimeType={urllib.parse.quote(export_mime)}"
                )
            else:
                url = (
                    f"https://www.googleapis.com/drive/v3/files/{file_id}"
                    "?alt=media&supportsAllDrives=true"
                )
            with session.get(url, stream=True) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:
            raise DriveClientError(
                f"download_file({file_id}) failed: {exc}"
            ) from exc

        size = os.path.getsize(dest_path)
        logger.info("download_file %s → %s (%d bytes)", file_id, dest_path, size)
        return {
            "path": dest_path,
            "name": os.path.basename(dest_path),
            "size_bytes": size,
            "truncated": False,
        }

    def fetch_thumbnail(
        self, thumbnail_link: str, max_bytes: int = _THUMBNAIL_MAX_BYTES
    ) -> dict:
        """Fetch a Drive-generated preview image from its signed thumbnailLink URL.

        Much cheaper than a full ``download_file`` when a caller only wants
        something to show a human, not the file itself -- but not every file
        has one (``DriveFile.thumbnail_link`` is empty when Drive hasn't
        generated a thumbnail for it). Capped at ``max_bytes``: this is meant
        to be a small preview image, so a response that large indicates
        something unexpected rather than a legitimately big thumbnail.

        Returns a dict with ``data`` (bytes) and ``mime_type`` (str, from the
        response's own Content-Type header) -- Drive's thumbnails are
        commonly JPEG, but that's not a documented guarantee, so this reports
        whatever the response actually says rather than assuming.
        """
        if not thumbnail_link:
            raise DriveClientError("fetch_thumbnail requires a non-empty thumbnail_link")
        try:
            creds = self._load_credentials()
            session = AuthorizedSession(creds)
            with session.get(thumbnail_link, stream=True) as resp:
                resp.raise_for_status()
                mime_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise DriveClientError(
                            f"fetch_thumbnail: response exceeded {max_bytes} bytes"
                        )
                    chunks.append(chunk)
        except DriveClientError:
            raise
        except Exception as exc:
            raise DriveClientError(f"fetch_thumbnail failed: {exc}") from exc

        data = b"".join(chunks)
        logger.info("fetch_thumbnail: %d bytes, mime_type=%s", len(data), mime_type)
        return {"data": data, "mime_type": mime_type}

    def list_folder(self, folder_id: str, max_results: int = 50) -> list[DriveFile]:
        """List the direct children of a folder."""
        if not folder_id:
            raise DriveClientError("list_folder requires a non-empty folder_id")
        max_results = self._clamp_max_results(max_results)
        query = f"'{folder_id}' in parents and trashed = false"
        service = self._get_service()
        try:
            response = (
                service.files()
                .list(
                    q=query,
                    pageSize=max_results,
                    fields=f"files({_FILE_FIELDS})",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"list_folder({folder_id}) failed: {exc}") from exc

        files = [self._parse_file(f) for f in response.get("files", [])]
        logger.info("list_folder %s returned %d children", folder_id, len(files))
        return files

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #
    def create_blank_file(
        self, name: str, mime_type: str, parent_folder_id: str = ""
    ) -> dict:
        """Create a new blank file and return its metadata dict."""
        body: dict = {"name": name, "mimeType": mime_type}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        service = self._get_service()
        try:
            result = (
                service.files()
                .create(body=body, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"create_blank_file failed: {exc}") from exc
        logger.info("create_blank_file: id=%s name=%s", result.get("id"), name)
        return {"id": result.get("id", ""), "name": name, "mime_type": mime_type}

    def upload_file(
        self,
        local_path: str = "",
        name: str = "",
        parent_folder_id: str = "",
        content_base64: str = "",
    ) -> dict:
        """Upload a file as a new Drive file, either from disk or inline bytes.

        Exactly one of ``local_path`` or ``content_base64`` must be given.
        Both paths use a resumable Google API media upload instead of the
        ``write_file_content`` path, which always encodes as UTF-8 text and
        uploads with a hardcoded ``text/plain`` media type — arbitrary binary
        files (PDFs, images, …) can't round-trip through that.

        ``local_path`` reads straight from disk via ``MediaFileUpload`` and is
        preferred when the file already lives on the same machine as
        PrivacyFence. ``content_base64`` lets a caller that only has the bytes
        in hand (no shared filesystem) hand them over directly — PrivacyFence
        decodes the base64 itself via ``MediaIoBaseUpload``.
        """
        from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

        if bool(local_path.strip()) == bool(content_base64.strip()):
            raise DriveClientError(
                "upload_file: provide exactly one of local_path or content_base64"
            )

        if local_path.strip():
            path = os.path.expanduser(local_path.strip())
            if not os.path.isfile(path):
                raise DriveClientError(f"upload_file: no such file: {local_path!r}")
            resolved_name = name.strip() or os.path.basename(path)
            mime_type = mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"
            media = MediaFileUpload(path, mimetype=mime_type, resumable=True)
            size_bytes = os.path.getsize(path)
        else:
            resolved_name = name.strip()
            if not resolved_name:
                raise DriveClientError("upload_file: name is required with content_base64")
            try:
                data = base64.b64decode(content_base64, validate=True)
            except (base64.binascii.Error, ValueError) as exc:
                raise DriveClientError(f"upload_file: invalid content_base64: {exc}") from exc
            mime_type = mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)
            size_bytes = len(data)

        body: dict = {"name": resolved_name}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]

        service = self._get_service()
        try:
            result = (
                service.files()
                .create(body=body, media_body=media, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"upload_file({resolved_name}) failed: {exc}") from exc

        parsed = self._parse_file(result)
        logger.info("upload_file: id=%s name=%s mime=%s", parsed.id, parsed.name, mime_type)
        return {
            "id": parsed.id,
            "name": parsed.name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }

    def write_file_content(self, file_id: str, content: str) -> dict:
        """Write (overwrite) the content of a file."""
        import io
        from googleapiclient.http import MediaIoBaseUpload

        if not file_id:
            raise DriveClientError("write_file_content requires a non-empty file_id")
        service = self._get_service()
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")), mimetype="text/plain"
        )
        try:
            result = (
                service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    fields="id,name,modifiedTime",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"write_file_content({file_id}) failed: {exc}") from exc
        logger.info("write_file_content: file_id=%s", file_id)
        return {"file_id": result.get("id", file_id), "modified_time": result.get("modifiedTime", "")}

    def write_doc_rich_content(self, file_id: str, markdown: str) -> dict:
        """Write Markdown to a Google Doc with rich formatting via the Docs API.

        Supports: headings (# through ######), **bold**, *italic*,
        ***bold-italic***, ~~strikethrough~~, __underline__, `code`,
        ==highlight== (these five nest freely with each other, e.g.
        ==**bold and highlighted**==), [link](url) (escape a literal `[` or
        `]` in the link text as `\\[`/`\\]`), unordered/numbered lists (nest
        a level by indenting 2 spaces per level), GFM pipe tables, `---` /
        `***` / `___` on their own line as a horizontal-rule divider
        (rendered as a bottom-bordered empty paragraph -- the Docs API has
        no native horizontal-rule element), and plain paragraphs.
        Clears existing document content before writing.
        Requires the ``drive`` or ``documents`` OAuth scope (already granted).
        """
        if not file_id:
            raise DriveClientError(
                "write_doc_rich_content requires a non-empty file_id"
            )
        docs_service = self._get_docs_service()
        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"write_doc_rich_content get({file_id}) failed: {exc}"
            ) from exc

        # Find end index so we can delete existing content
        end_index = 1
        for element in doc.get("body", {}).get("content", []):
            if "endIndex" in element:
                end_index = element["endIndex"]

        text_markdown, tables = _extract_tables(markdown)

        requests: list[dict] = []
        if end_index > 2:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": 1, "endIndex": end_index - 1}
                    }
                }
            )
            # deleteContentRange collapses the body to a single empty
            # paragraph at [1, 2), but that paragraph can still carry bullet
            # formatting left over from whatever was there before. The Docs
            # API's createParagraphBullets silently merges a new list into
            # an immediately-preceding paragraph with a matching bullet --
            # useful for continuing one list across the per-line calls below,
            # but it means a leftover bullet here would make the new
            # document's first list item continue numbering/nesting from the
            # content we just deleted instead of starting fresh. Clearing it
            # unconditionally is a no-op when there was nothing to clear.
            requests.append(
                {"deleteParagraphBullets": {"range": {"startIndex": 1, "endIndex": 2}}}
            )
        requests.extend(_markdown_to_docs_requests(text_markdown))

        if requests:
            try:
                docs_service.documents().batchUpdate(
                    documentId=file_id, body={"requests": requests}
                ).execute()
            except HttpError as exc:
                raise DriveClientError(
                    f"write_doc_rich_content batchUpdate({file_id}) failed: {exc}"
                ) from exc

        # Tables are structural elements the Docs API can't create from plain
        # text, and it doesn't hand back a new table's cell indices
        # synchronously -- each one needs its own insert-then-re-fetch cycle
        # to find out where its cells actually ended up before it can fill
        # them in. Processed one at a time and in original document order,
        # each re-fetch naturally accounts for every earlier table's index
        # shift, so nothing here needs to guess at cumulative offsets.
        for table in tables:
            self._insert_table_at_placeholder(docs_service, file_id, table)

        logger.info(
            "write_doc_rich_content: file_id=%s tables=%d", file_id, len(tables)
        )
        return {"file_id": file_id}

    def _insert_table_at_placeholder(
        self,
        docs_service: Any,
        file_id: str,
        table: TableBlock,
        caller: str = "write_doc_rich_content",
    ) -> None:
        """Replace ``table``'s placeholder text (already written to the
        document by ``caller``) with a real Docs table, fetching the
        document in between steps to get ground-truth indices rather than
        computing a table's index footprint by hand.

        ``caller`` names the public method this was invoked from
        (``write_doc_rich_content`` or ``edit_doc_content``) purely for
        error-message prefixes, so a failure here is traceable back to the
        tool call that triggered it.
        """
        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"{caller} table lookup({file_id}) failed: {exc}"
            ) from exc
        plain_text, runs = _docs_plain_text_with_index_map(doc)
        matches = _find_text_matches(plain_text, table.placeholder)
        if len(matches) != 1:
            raise DriveClientError(
                f"{caller}: expected exactly one placeholder for a "
                f"table, found {len(matches)}"
            )
        docs_start = _offset_to_docs_index(matches[0][0], runs)
        docs_end = _offset_to_docs_index(matches[0][1], runs)

        n_rows = len(table.rows)
        n_cols = len(table.rows[0]) if table.rows else 0
        structure_requests = [
            {"deleteContentRange": {"range": {"startIndex": docs_start, "endIndex": docs_end}}},
            {"insertTable": {"rows": n_rows, "columns": n_cols, "location": {"index": docs_start}}},
        ]
        try:
            docs_service.documents().batchUpdate(
                documentId=file_id, body={"requests": structure_requests}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"{caller} table insert({file_id}) failed: {exc}"
            ) from exc

        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"{caller} table lookup({file_id}) failed: {exc}"
            ) from exc
        cell_starts = _table_cell_start_indices(doc, docs_start)
        if len(cell_starts) != n_rows or any(len(row) != n_cols for row in cell_starts):
            raise DriveClientError(
                f"{caller}: inserted table shape did not match the "
                f"requested {n_rows}x{n_cols} grid"
            )

        # Fill from the last cell back to the first: every cell_starts index
        # comes from one snapshot, and inserting text only shifts indices
        # that come *after* it, so working backwards keeps every
        # not-yet-filled cell's captured index valid (same trick
        # edit_doc_content uses for multiple find_text matches).
        fill_requests: list[dict] = []
        for r in range(n_rows - 1, -1, -1):
            for c in range(n_cols - 1, -1, -1):
                cell_start = cell_starts[r][c]
                cell_markdown = table.rows[r][c]
                if r == 0 and cell_markdown:  # bold the header row
                    cell_markdown = f"**{cell_markdown}**"
                alignment = table.alignments[c] if c < len(table.alignments) else "START"
                if alignment != "START":
                    fill_requests.append(
                        {
                            "updateParagraphStyle": {
                                "range": {"startIndex": cell_start, "endIndex": cell_start + 1},
                                "paragraphStyle": {"alignment": alignment},
                                "fields": "alignment",
                            }
                        }
                    )
                fill_requests.extend(
                    _markdown_to_docs_requests(cell_markdown, start_index=cell_start)
                )

        if fill_requests:
            try:
                docs_service.documents().batchUpdate(
                    documentId=file_id, body={"requests": fill_requests}
                ).execute()
            except HttpError as exc:
                raise DriveClientError(
                    f"{caller} table fill({file_id}) failed: {exc}"
                ) from exc

    def edit_doc_content(
        self, file_id: str, find_text: str, replace_markdown: str, replace_all: bool = False
    ) -> dict:
        """Replace one (or, with ``replace_all``, every) occurrence of
        ``find_text`` in a Google Doc with newly rendered Markdown, touching
        only the matched span(s) rather than the whole document.

        ``find_text`` is matched against the document's plain, unformatted
        text (``_docs_plain_text_with_index_map``'s raw textRun
        concatenation) -- *not* the Markdown ``get_file_content`` now
        renders for a Doc; a caller piping that Markdown straight into
        ``find_text`` needs to strip any formatting markers back out first.
        Must match exactly once unless ``replace_all`` is set — an ambiguous
        match raises rather than
        guessing which occurrence was meant, the same contract a
        unique-match text editor enforces.

        Supports the same Markdown as ``write_doc_rich_content``, including
        GFM pipe tables: each occurrence's tables are written the same
        placeholder-then-insertTable way, one at a time, after the plain-text
        replacement lands.

        Known limitation: unlike ``write_doc_rich_content``, this doesn't
        guard against a new list introduced by ``replace_markdown`` merging
        into an untouched, pre-existing list paragraph that happens to sit
        immediately before or after the matched span (see the
        deleteParagraphBullets note in ``write_doc_rich_content``) — doing
        that safely here would require inspecting the surrounding
        paragraphs' existing bullet state, which risks stripping bullet
        formatting from document content the caller never asked to change.
        """
        if not file_id:
            raise DriveClientError("edit_doc_content requires a non-empty file_id")
        if not find_text:
            raise DriveClientError("edit_doc_content requires a non-empty find_text")

        docs_service = self._get_docs_service()
        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(f"edit_doc_content get({file_id}) failed: {exc}") from exc

        plain_text, runs = _docs_plain_text_with_index_map(doc)
        matches = _find_text_matches(plain_text, find_text)
        if not matches:
            raise DriveClientError(f"edit_doc_content: find_text {find_text!r} not found in the document")
        if len(matches) > 1 and not replace_all:
            raise DriveClientError(
                f"edit_doc_content: find_text {find_text!r} matches {len(matches)} locations; "
                "add more surrounding context to make it unique, or set replace_all=true"
            )

        # replace_markdown is re-extracted once per match rather than once
        # overall so that, under replace_all, every occurrence's tables get
        # their own uniquely-prefixed placeholders -- otherwise inserting the
        # same table Markdown at two locations would leave two identical
        # placeholder strings in the document, and _insert_table_at_placeholder
        # (a single-match lookup) couldn't tell which one it was asked to fill.
        per_match = [
            _extract_tables(replace_markdown, placeholder_prefix=f"PRIVACYFENCE_TABLE_PLACEHOLDER_M{m}_")
            for m in range(len(matches))
        ]

        # Apply from the last match to the first so an earlier edit's index
        # shift (deleting/inserting text changes every index after it) never
        # invalidates an already-computed later range.
        requests: list[dict] = []
        for (plain_start, plain_end), (text_markdown, _tables) in zip(
            reversed(matches), reversed(per_match)
        ):
            docs_start = _offset_to_docs_index(plain_start, runs)
            docs_end = _offset_to_docs_index(plain_end, runs)
            requests.append(
                {"deleteContentRange": {"range": {"startIndex": docs_start, "endIndex": docs_end}}}
            )
            requests.extend(_markdown_to_docs_requests(text_markdown, start_index=docs_start))

        try:
            docs_service.documents().batchUpdate(
                documentId=file_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"edit_doc_content batchUpdate({file_id}) failed: {exc}") from exc

        # Same reasoning as write_doc_rich_content: each table is a
        # structural element the API can't produce from insertText, and
        # filling its cells needs a re-fetch to learn where they landed, so
        # every table (across every match) is processed one at a time.
        for _text_markdown, tables in per_match:
            for table in tables:
                self._insert_table_at_placeholder(docs_service, file_id, table, caller="edit_doc_content")

        logger.info(
            "edit_doc_content: file_id=%s matches=%d replace_all=%s", file_id, len(matches), replace_all
        )
        return {"file_id": file_id, "occurrences_replaced": len(matches)}

    def format_doc_content(
        self,
        file_id: str,
        find_text: str,
        bold: str = "",
        italic: str = "",
        highlight_color: str = "",
        text_color: str = "",
        replace_all: bool = False,
    ) -> dict:
        """Apply text styling to existing text in a Google Doc, located the
        same way as ``edit_doc_content``, without changing the text itself.

        Every styling parameter is opt-in like ``format_sheet_range``: its
        default (empty string) means "leave that aspect unchanged", so a call
        that only sets ``highlight_color`` never touches bold/italic already
        on the matched text.
        """
        if not file_id:
            raise DriveClientError("format_doc_content requires a non-empty file_id")
        if not find_text:
            raise DriveClientError("format_doc_content requires a non-empty find_text")

        text_style: dict = {}
        fields: list[str] = []
        if bold:
            text_style["bold"] = bold.strip().lower() == "true"
            fields.append("bold")
        if italic:
            text_style["italic"] = italic.strip().lower() == "true"
            fields.append("italic")
        if highlight_color:
            text_style["backgroundColor"] = {"color": {"rgbColor": _hex_to_rgb_dict(highlight_color)}}
            fields.append("backgroundColor")
        if text_color:
            text_style["foregroundColor"] = {"color": {"rgbColor": _hex_to_rgb_dict(text_color)}}
            fields.append("foregroundColor")
        if not fields:
            return {"file_id": file_id, "occurrences_formatted": 0}

        docs_service = self._get_docs_service()
        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(f"format_doc_content get({file_id}) failed: {exc}") from exc

        plain_text, runs = _docs_plain_text_with_index_map(doc)
        matches = _find_text_matches(plain_text, find_text)
        if not matches:
            raise DriveClientError(f"format_doc_content: find_text {find_text!r} not found in the document")
        if len(matches) > 1 and not replace_all:
            raise DriveClientError(
                f"format_doc_content: find_text {find_text!r} matches {len(matches)} locations; "
                "add more surrounding context to make it unique, or set replace_all=true"
            )

        # Styling doesn't change text length, so match order doesn't matter
        # the way it does in edit_doc_content.
        requests = [
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": _offset_to_docs_index(plain_start, runs),
                        "endIndex": _offset_to_docs_index(plain_end, runs),
                    },
                    "textStyle": text_style,
                    "fields": ",".join(fields),
                }
            }
            for plain_start, plain_end in matches
        ]

        try:
            docs_service.documents().batchUpdate(
                documentId=file_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"format_doc_content batchUpdate({file_id}) failed: {exc}") from exc

        logger.info(
            "format_doc_content: file_id=%s matches=%d replace_all=%s", file_id, len(matches), replace_all
        )
        return {"file_id": file_id, "occurrences_formatted": len(matches)}

    def move_file(self, file_id: str, destination_folder_id: str) -> dict:
        """Move a file to a different folder."""
        if not file_id or not destination_folder_id:
            raise DriveClientError("move_file requires file_id and destination_folder_id")
        service = self._get_service()
        # Get current parents
        try:
            file_meta = service.files().get(
                fileId=file_id, fields="parents", supportsAllDrives=True
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"move_file get_parents({file_id}) failed: {exc}") from exc
        current_parents = ",".join(file_meta.get("parents", []))
        try:
            service.files().update(
                fileId=file_id,
                addParents=destination_folder_id,
                removeParents=current_parents,
                fields="id,parents",
                supportsAllDrives=True,
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"move_file({file_id}) failed: {exc}") from exc
        logger.info("move_file: file_id=%s dest=%s", file_id, destination_folder_id)
        return {"file_id": file_id, "new_parent": destination_folder_id}

    def add_comment(self, file_id: str, comment: str) -> dict:
        """Add a comment to a file."""
        if not file_id:
            raise DriveClientError("add_comment requires a non-empty file_id")
        service = self._get_service()
        try:
            result = (
                service.comments()
                .create(fileId=file_id, body={"content": comment}, fields="id,content")
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"add_comment({file_id}) failed: {exc}") from exc
        logger.info("add_comment: file_id=%s comment_id=%s", file_id, result.get("id"))
        return {"file_id": file_id, "comment_id": result.get("id", ""), "content": comment}

    def list_shared_drives(self, max_results: int = 50) -> list[dict]:
        """Return a list of Shared Drives the authorized user can access."""
        max_results = self._clamp_max_results(max_results)
        service = self._get_service()
        try:
            response = (
                service.drives()
                .list(pageSize=max_results, fields="drives(id,name,kind)")
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"list_shared_drives failed: {exc}") from exc
        drives = response.get("drives", [])
        logger.info("list_shared_drives returned %d drives", len(drives))
        return [{"id": d.get("id", ""), "name": d.get("name", "")} for d in drives]

    # ------------------------------------------------------------------ #
    # Sheets operations
    #
    # These reuse the Drive OAuth grant (the Sheets API v4 accepts the
    # ``drive`` scope) the same way write_doc_rich_content() above reuses it
    # for the Docs API - no separate consent screen or token file.
    # ------------------------------------------------------------------ #
    def _get_sheets_service(self):
        service = getattr(self._local, "sheets_service", None)
        if service is None:
            creds = self._load_credentials()
            service = build(
                "sheets", "v4", credentials=creds, cache_discovery=False
            )
            self._local.sheets_service = service
            logger.debug("Sheets API service initialized for thread %s", threading.current_thread().name)
        return service

    def create_spreadsheet(
        self, name: str, sheet_titles: list[str] | None = None, parent_folder_id: str = ""
    ) -> dict:
        """Create a new spreadsheet, optionally with named tabs (defaults to one
        tab named 'Sheet1' if ``sheet_titles`` is empty). Returns id/name/web link.

        The Sheets API always creates in "My Drive" root; if a parent folder is
        given we move the resulting file there via the Drive API afterward.
        """
        if not name.strip():
            raise DriveClientError("create_spreadsheet requires a non-empty name")
        body: dict = {"properties": {"title": name}}
        if sheet_titles:
            body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles]
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().create(
                body=body, fields="spreadsheetId,properties.title,spreadsheetUrl"
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"create_spreadsheet({name}) failed: {exc}") from exc

        spreadsheet_id = result.get("spreadsheetId", "")
        if parent_folder_id:
            self.move_file(spreadsheet_id, parent_folder_id)
        logger.info("create_spreadsheet: id=%s name=%s", spreadsheet_id, name)
        return {
            "id": spreadsheet_id,
            "name": result.get("properties", {}).get("title", name),
            "web_view_link": result.get("spreadsheetUrl", ""),
        }

    def list_sheets(self, spreadsheet_id: str) -> list[dict]:
        """List the tabs (sheets) within a spreadsheet."""
        if not spreadsheet_id:
            raise DriveClientError("list_sheets requires a non-empty spreadsheet_id")
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().get(
                spreadsheetId=spreadsheet_id, fields="sheets.properties"
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"list_sheets({spreadsheet_id}) failed: {exc}") from exc
        sheets = []
        for s in result.get("sheets", []):
            props = s.get("properties", {})
            grid = props.get("gridProperties", {})
            sheets.append({
                "sheet_id": props.get("sheetId"),
                "title": props.get("title", ""),
                "index": props.get("index"),
                "row_count": grid.get("rowCount"),
                "column_count": grid.get("columnCount"),
                "hidden": bool(props.get("hidden", False)),
            })
        logger.info("list_sheets %s returned %d tab(s)", spreadsheet_id, len(sheets))
        return sheets

    def get_sheet_values(
        self, spreadsheet_id: str, range_a1: str, value_render_option: str = "FORMATTED_VALUE"
    ) -> list[list]:
        """Read a range of cell values (A1 notation, e.g. 'Sheet1!A1:C10').

        ``value_render_option`` controls what each cell's value represents:
          - FORMATTED_VALUE (default): the displayed string, e.g. "$1.00".
          - UNFORMATTED_VALUE: the underlying value with no formatting applied,
            e.g. 1 instead of "$1.00".
          - FORMULA: the formula text itself, e.g. "=A1+A2", instead of its
            computed result -- use this to read formulas rather than values.
        """
        if not spreadsheet_id or not range_a1:
            raise DriveClientError("get_sheet_values requires spreadsheet_id and range")
        render_option = (value_render_option or "FORMATTED_VALUE").strip().upper()
        if render_option not in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"):
            raise DriveClientError(
                f"Invalid value_render_option {value_render_option!r}; use "
                "FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA"
            )
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=range_a1, valueRenderOption=render_option
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"get_sheet_values({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        values = result.get("values", [])
        logger.info(
            "get_sheet_values %s %s (%s): %d row(s)",
            spreadsheet_id, range_a1, render_option, len(values),
        )
        return values

    def get_sheet_formatting(self, spreadsheet_id: str, range_a1: str) -> list[list[dict]]:
        """Read per-cell formatting for a range (A1 notation, sheet-name-
        prefixed, e.g. 'Sheet1!A1:C10'), shaped as a 2D grid lined up with
        get_sheet_values's own rows/columns for the same range.

        Each cell is a dict covering the same aspects format_sheet_range can
        set -- bold, italic, text_color, background_color, number_format,
        horizontal_alignment, vertical_alignment, wrap_strategy -- as
        '#rrggbb' hex for colors, present only when set (an empty dict means
        no non-default formatting on that cell). Rows/trailing cells with no
        formatting at all may be shorter than the sheet's full extent, same
        as get_sheet_values.
        """
        if not spreadsheet_id or not range_a1:
            raise DriveClientError("get_sheet_formatting requires spreadsheet_id and range")
        service = self._get_sheets_service()
        fields = (
            "sheets.data.rowData.values.userEnteredFormat"
            "(textFormat(bold,italic,foregroundColor),backgroundColor,numberFormat,"
            "horizontalAlignment,verticalAlignment,wrapStrategy)"
        )
        try:
            result = service.spreadsheets().get(
                spreadsheetId=spreadsheet_id, ranges=[range_a1], fields=fields
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"get_sheet_formatting({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        sheets = result.get("sheets", [])
        row_data = sheets[0].get("data", [{}])[0].get("rowData", []) if sheets else []
        grid = [
            [_cell_format_summary(cell.get("userEnteredFormat", {})) for cell in row.get("values", [])]
            for row in row_data
        ]
        logger.info("get_sheet_formatting %s %s: %d row(s)", spreadsheet_id, range_a1, len(grid))
        return grid

    def write_sheet_values(
        self, spreadsheet_id: str, range_a1: str, values: list[list], value_input_option: str = "USER_ENTERED"
    ) -> dict:
        """Write a 2D array of values into a range (A1 notation, e.g.
        'Sheet1!A1:C10'). With the default ``USER_ENTERED`` option, cell strings
        starting with '=' are evaluated as formulas, exactly as if typed into
        the Sheets UI - there is no separate "set formula" operation.
        """
        if not spreadsheet_id or not range_a1:
            raise DriveClientError("write_sheet_values requires spreadsheet_id and range")
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption=value_input_option,
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"write_sheet_values({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        logger.info(
            "write_sheet_values %s %s: updated %s cell(s)",
            spreadsheet_id, range_a1, result.get("updatedCells", 0),
        )
        return {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": result.get("updatedRange", range_a1),
            "updated_cells": result.get("updatedCells", 0),
        }

    def add_sheet(self, spreadsheet_id: str, title: str, rows: int = 1000, cols: int = 26) -> dict:
        """Add a new tab to an existing spreadsheet."""
        if not spreadsheet_id or not title.strip():
            raise DriveClientError("add_sheet requires spreadsheet_id and a non-empty title")
        service = self._get_sheets_service()
        request = {
            "addSheet": {
                "properties": {
                    "title": title,
                    "gridProperties": {"rowCount": max(1, rows), "columnCount": max(1, cols)},
                }
            }
        }
        try:
            result = service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [request]}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"add_sheet({spreadsheet_id}, {title}) failed: {exc}") from exc
        props = result["replies"][0]["addSheet"]["properties"]
        logger.info("add_sheet: spreadsheet=%s sheet_id=%s title=%s", spreadsheet_id, props.get("sheetId"), title)
        return {"sheet_id": props.get("sheetId"), "title": props.get("title", title), "index": props.get("index")}

    def rename_sheet(self, spreadsheet_id: str, sheet_id: int, new_title: str) -> dict:
        """Rename an existing tab. Also the sanctioned way to mark a tab for
        deletion (rename it, e.g. to 'TO BE DELETED - <original title>') since
        this client intentionally has no delete-sheet operation."""
        if not spreadsheet_id or not new_title.strip():
            raise DriveClientError("rename_sheet requires spreadsheet_id and a non-empty new_title")
        service = self._get_sheets_service()
        request = {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "title": new_title},
                "fields": "title",
            }
        }
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [request]}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"rename_sheet({spreadsheet_id}, {sheet_id}) failed: {exc}"
            ) from exc
        logger.info("rename_sheet: spreadsheet=%s sheet_id=%s new_title=%s", spreadsheet_id, sheet_id, new_title)
        return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "title": new_title}

    def insert_dimensions(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        dimension: str,
        start_index: int,
        count: int,
        inherit_from_before: bool = True,
    ) -> dict:
        """Insert blank rows or columns, shifting existing content after the
        insertion point. Values/formulas are untouched, only their position
        shifts; the Sheets API adjusts formula references automatically.

        ``dimension`` is 'ROWS' or 'COLUMNS'. ``start_index`` is 0-based, the
        index the new rows/columns are inserted before. ``inherit_from_before``
        matches the Sheets UI default: the inserted rows/columns copy the
        formatting of the row/column immediately before the insertion point.
        """
        if not spreadsheet_id:
            raise DriveClientError("insert_dimensions requires a non-empty spreadsheet_id")
        if dimension not in ("ROWS", "COLUMNS"):
            raise DriveClientError(f"insert_dimensions: dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}")
        if count < 1:
            raise DriveClientError("insert_dimensions requires count >= 1")
        request = {
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": dimension,
                    "startIndex": start_index,
                    "endIndex": start_index + count,
                },
                "inheritFromBefore": inherit_from_before,
            }
        }
        service = self._get_sheets_service()
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [request]}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"insert_dimensions({spreadsheet_id}, {sheet_id}) failed: {exc}"
            ) from exc
        logger.info(
            "insert_dimensions: spreadsheet=%s sheet_id=%s dimension=%s start=%d count=%d",
            spreadsheet_id, sheet_id, dimension, start_index, count,
        )
        return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "dimension": dimension, "inserted": count}

    def delete_dimensions(
        self, spreadsheet_id: str, sheet_id: int, dimension: str, start_index: int, count: int
    ) -> dict:
        """Delete rows or columns, including any values, formulas, and
        formatting they contain. Remaining rows/columns shift to close the
        gap. ``dimension`` is 'ROWS' or 'COLUMNS'; ``start_index`` is 0-based
        and inclusive of the first row/column removed.
        """
        if not spreadsheet_id:
            raise DriveClientError("delete_dimensions requires a non-empty spreadsheet_id")
        if dimension not in ("ROWS", "COLUMNS"):
            raise DriveClientError(f"delete_dimensions: dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}")
        if count < 1:
            raise DriveClientError("delete_dimensions requires count >= 1")
        request = {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": dimension,
                    "startIndex": start_index,
                    "endIndex": start_index + count,
                },
            }
        }
        service = self._get_sheets_service()
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [request]}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"delete_dimensions({spreadsheet_id}, {sheet_id}) failed: {exc}"
            ) from exc
        logger.info(
            "delete_dimensions: spreadsheet=%s sheet_id=%s dimension=%s start=%d count=%d",
            spreadsheet_id, sheet_id, dimension, start_index, count,
        )
        return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "dimension": dimension, "deleted": count}

    def format_sheet_range(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        range_a1: str,
        bold: str = "",
        italic: str = "",
        background_color: str = "",
        text_color: str = "",
        number_format: str = "",
        horizontal_alignment: str = "",
        vertical_alignment: str = "",
        wrap_strategy: str = "",
        freeze_rows: int = -1,
        freeze_cols: int = -1,
        column_width: int = -1,
        merge_type: str = "KEEP",
    ) -> dict:
        """Apply formatting to a range. Every parameter is opt-in: its "unset"
        value (empty string / -1 / 'KEEP') means "leave that aspect unchanged" -
        a format call only ever touches the aspects it's explicitly given, so
        e.g. changing a background color never silently clears bold text or
        un-merges cells set by an earlier call.

        ``range_a1`` is plain A1 notation scoped to ``sheet_id`` (e.g. 'A1:C10',
        no sheet-name prefix - only fully-bounded ranges are supported).
        ``horizontal_alignment`` is one of LEFT / CENTER / RIGHT.
        ``vertical_alignment`` is one of TOP / MIDDLE / BOTTOM.
        ``wrap_strategy`` is one of OVERFLOW_CELL (overflow into empty
        neighboring cells) / CLIP (cut off at the cell boundary) / WRAP (line
        break to fit the cell) - matches the Sheets UI's "Overflow"/"Clip"/
        "Wrap" text-wrapping options.
        ``merge_type`` is one of KEEP / NONE (unmerge) / MERGE_ALL /
        MERGE_COLUMNS / MERGE_ROWS.
        """
        if not spreadsheet_id:
            raise DriveClientError("format_sheet_range requires a non-empty spreadsheet_id")
        grid_range = {"sheetId": sheet_id, **_parse_a1_range(range_a1)}

        requests: list[dict] = []

        cell_format: dict = {}
        fields: list[str] = []
        text_style: dict = {}
        text_fields: list[str] = []
        if bold:
            text_style["bold"] = bold.strip().lower() == "true"
            text_fields.append("bold")
        if italic:
            text_style["italic"] = italic.strip().lower() == "true"
            text_fields.append("italic")
        if text_color:
            text_style["foregroundColor"] = _hex_to_rgb_dict(text_color)
            text_fields.append("foregroundColor")
        if text_fields:
            cell_format["textFormat"] = text_style
            fields.append("userEnteredFormat.textFormat(" + ",".join(text_fields) + ")")
        if background_color:
            cell_format["backgroundColor"] = _hex_to_rgb_dict(background_color)
            fields.append("userEnteredFormat.backgroundColor")
        if number_format:
            cell_format["numberFormat"] = {"type": "NUMBER", "pattern": number_format}
            fields.append("userEnteredFormat.numberFormat")
        if horizontal_alignment:
            cell_format["horizontalAlignment"] = horizontal_alignment.upper()
            fields.append("userEnteredFormat.horizontalAlignment")
        if vertical_alignment:
            cell_format["verticalAlignment"] = vertical_alignment.upper()
            fields.append("userEnteredFormat.verticalAlignment")
        if wrap_strategy:
            cell_format["wrapStrategy"] = wrap_strategy.upper()
            fields.append("userEnteredFormat.wrapStrategy")
        if fields:
            requests.append({
                "repeatCell": {
                    "range": grid_range,
                    "cell": {"userEnteredFormat": cell_format},
                    "fields": ",".join(fields),
                }
            })

        if column_width >= 0:
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": grid_range["startColumnIndex"],
                        "endIndex": grid_range["endColumnIndex"],
                    },
                    "properties": {"pixelSize": column_width},
                    "fields": "pixelSize",
                }
            })

        if freeze_rows >= 0 or freeze_cols >= 0:
            grid_properties: dict = {}
            sheet_fields: list[str] = []
            if freeze_rows >= 0:
                grid_properties["frozenRowCount"] = freeze_rows
                sheet_fields.append("gridProperties.frozenRowCount")
            if freeze_cols >= 0:
                grid_properties["frozenColumnCount"] = freeze_cols
                sheet_fields.append("gridProperties.frozenColumnCount")
            requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": grid_properties},
                    "fields": ",".join(sheet_fields),
                }
            })

        merge_type = merge_type.upper()
        if merge_type == "NONE":
            requests.append({"unmergeCells": {"range": grid_range}})
        elif merge_type in ("MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"):
            requests.append({"mergeCells": {"range": grid_range, "mergeType": merge_type}})
        elif merge_type != "KEEP":
            raise DriveClientError(
                f"format_sheet_range: invalid merge_type {merge_type!r}; "
                "use KEEP, NONE, MERGE_ALL, MERGE_COLUMNS, or MERGE_ROWS"
            )

        if not requests:
            return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "requests_applied": 0}

        service = self._get_sheets_service()
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"format_sheet_range({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        logger.info(
            "format_sheet_range: spreadsheet=%s sheet_id=%s range=%s requests=%d",
            spreadsheet_id, sheet_id, range_a1, len(requests),
        )
        return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "requests_applied": len(requests)}

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp_max_results(max_results: int) -> int:
        """Defensive bounds on caller-supplied result counts."""
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            value = 20
        return max(1, min(value, 1000))

    @staticmethod
    def _download(request, max_bytes: int) -> bytes:
        """Stream a media request, stopping once we have more than max_bytes.

        We read one extra byte's worth of chunks beyond the cap so the caller
        can reliably detect truncation.
        """
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=max(8192, min(max_bytes, 1048576)))
        done = False
        while not done:
            _status, done = downloader.next_chunk()
            if buffer.tell() > max_bytes:
                break
        return buffer.getvalue()

    @staticmethod
    def _parse_file(raw: dict[str, Any]) -> DriveFile:
        owners = [
            o.get("emailAddress", "")
            for o in raw.get("owners", []) or []
            if o.get("emailAddress")
        ]
        try:
            size = int(raw.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        return DriveFile(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            mime_type=raw.get("mimeType", ""),
            size=size,
            created_time=raw.get("createdTime", ""),
            modified_time=raw.get("modifiedTime", ""),
            owners=owners,
            shared=bool(raw.get("shared", False)),
            web_view_link=raw.get("webViewLink", ""),
            parent_ids=list(raw.get("parents", []) or []),
            drive_id=raw.get("driveId", ""),
            thumbnail_link=raw.get("thumbnailLink", ""),
        )
