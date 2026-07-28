"""Card-stack HTML template for the redesigned approval window.

Renders the *entire* content area of a review-gate or popup-gate dialog as one
self-contained HTML document for a single full-window WKWebView, replacing the
hand-laid-out NSTextField/NSBox stack ``approval_window.py`` builds for
``layout="legacy"``. Buttons stay native (see approval_window.py's module
docstring for why) -- nothing here renders Deny/Allow once/Always allow.

Source of truth for the visual design: the "Approval windows design system"
claude.ai/design project (``12d94c54-621e-48ce-b836-a687e0a10ed7``, turns 5
and 6), built on the "Broadsheet" design-system project
(``96120b24-3fd3-4cc7-b48c-109e89968d8e``) for tokens/components -- vendored
into ``resources/approval_window/`` (styles.css, with Source Serif 4 embedded
as base64 data URIs; see that directory's fonts/OFL.txt for licensing). Two
deliberate departures from the design canvas, per the redesign's own
implementation notes: ``.pf-bar`` (the mock's own 3-dot chrome row) is
dropped in favor of the real window's native title bar, and Google Fonts'
``@import`` is replaced with the vendored local ``@font-face`` -- this
document must never trigger a network fetch just to render a popup.

Every section is numbered dynamically (a running counter, not literal
"01"/"02"/"03"/"04" strings) because which sections actually render varies by
tool and by direction: §3 ("What will be provided to Claude") only ever
renders for a review-gate call carrying a ``visibility`` dict today (see
``disclosure_rows`` below), so the §4-equivalent risk card that follows it
lands on "03" instead of "04" whenever §3 is absent -- confirmed against the
design canvas itself, which numbers write-gate PII cards "03" (no §3 exists
on the write side at all) but read-gate ones "04" wherever a §3 card also
rendered on that same tool.

§3's rows are real values (the same rendering as §1's ``.pf-kv`` rows, just
meaning "new to Claude" instead of "already known"), not an abstract policy
summary -- "What will be provided to Claude" should show what will actually
be provided. ``disclosure_rows`` accepts either: literal (label, real value)
pairs a connector builds directly (e.g. calendar_get_event_details's
Attendees/Location/Description), or -- for the handful of tools that also
carry a privacy-category ``visibility`` policy (Gmail/Drive/Slack/Contacts/
Tasks/Confluence) -- rows built by disclosure_rows_from_visibility() below.
Either way this function itself doesn't care; approval_window.py's
controller decides which source to use per call (its ``new_info`` vs
``visibility`` attributes).

NARROW layout has no preview pane at all -- not a smaller version of WIDE's,
genuinely absent. A tool gets WIDE only when it has real free-text body
content §1-§4's fixed-row-per-field format can't represent (email/message
bodies, ticket descriptions, page content, sheet cell values, uploaded file
content); everything else is NARROW. Every row in every section is a fixed
size regardless of actual value length (see styles.css's .pf-kv/.pf-quote
truncation) specifically so a per-tool layout is fully deterministic from
field *counts* alone (known upfront, schema-driven) -- never from how long a
specific call's data happens to be. Rows are never omitted for having an
empty value either (a missing Location still gets its own blank row) --
only the section as a whole disappears when it has no fields at all.

drive_upload_file's PII card is a second known placeholder: gate.py routes
its own PII match through the same forced second-confirmation flow the
read-gate case gets, but no distinct design exists for it yet (a design-canvas
edit is planned). ``upload_forced=True`` reuses the read-gate's own
(unchanged, accent-2) card styling as an interim stand-in -- not a final
answer, see that parameter's docstring.
"""
from __future__ import annotations

from html import escape as _html_escape
from pathlib import Path

_STYLES_PATH = Path(__file__).parent / "resources" / "approval_window" / "styles.css"
_STYLES_CSS = _STYLES_PATH.read_text(encoding="utf-8")

# Narrow (single-column, sections only, no preview pane at all) vs wide
# (two-column, sections + a genuine free-text-body right pane) -- set
# explicitly per call site (see approval_window.py's `layout` param), not a
# length heuristic. See module docstring for exactly which tools get which.
NARROW = "narrow"
WIDE = "wide"

# Public (no leading underscore): approval_window.py's own _V2_WINDOW_WIDTH
# derives from this directly rather than duplicating it, so the native
# window frame and the HTML body rendered inside it can never drift out of
# sync the way they once did (that dict still said 880 after this one was
# bumped to 980 for a wider right pane).
CONTENT_WIDTH = {NARROW: 610, WIDE: 980}

# WIDE's left column width -- deliberately narrower than a full 550px
# single-column tool's content width (matching the design canvas's own
# flex:0 0 350px would have been even narrower still), bumped to give §1-§4's
# rows more room without pushing the window's overall width past what
# comfortably fits on a scaled-resolution laptop display (see the redesign
# discussion: a symmetric 550/550 split would need an ~1200px window, too
# wide for common 1280/1440-logical-point MacBook screens).
_WIDE_LEFT_COLUMN_WIDTH = 420

# §3's generic allow/redact/block -> disclosure-sentence mapping. A
# deliberate, generic rule rather than hand-authored per-tool prose (compare
# the design canvas's bespoke wording, e.g. "Full values for range") -- see
# this module's docstring for why the exact wording isn't tool-specific yet.
_DISCLOSURE_ALLOW = "Full {label_lower}"
_DISCLOSURE_REDACT = "{label}, with some fields redacted"
_DISCLOSURE_BLOCK = "None — not disclosed to Claude"


def disclosure_rows_from_visibility(visibility: dict[str, str]) -> list[tuple[str, str]]:
    """Translate the existing ``{label: allow/redact/block}`` policy dict
    (privacy_filter.category_policy()'s ground truth, unchanged) into §3's
    plain "what's disclosed" sentence per field -- the structural change
    turn 5 makes (dropping the old checklist's per-row ✓/✗/◐ icons for
    prose), even though the exact wording here is generic rather than
    hand-tuned per tool (see module docstring). Pure function, order-
    preserving, same testability contract as _details_html()."""
    rows = []
    for label, policy in visibility.items():
        if policy == "allow":
            sentence = _DISCLOSURE_ALLOW.format(label_lower=label[:1].lower() + label[1:])
        elif policy == "redact":
            sentence = _DISCLOSURE_REDACT.format(label=label)
        else:
            sentence = _DISCLOSURE_BLOCK
        rows.append((label, sentence))
    return rows


# Per-field max line-clamp before a value truncates with an ellipsis
# (styles.css's .pf-kv default is 2 lines) instead of growing the row or the
# window -- keyed by exact label text, since some fields are known to
# reliably carry longer content than a typical short structured field.
# Provisional: extend here as more tools are built out; approval_window.py's
# own height estimate calls this too, so the two never disagree about how
# tall a given row's worst case is.
DEFAULT_LINE_CLAMP = 2
LINE_CLAMP_BY_LABEL = {
    "Attendees": 3,
    "Description": 4,
}


def line_clamp_for(label: str) -> int:
    return LINE_CLAMP_BY_LABEL.get(label, DEFAULT_LINE_CLAMP)


def _table_html(table: dict) -> str:
    """One ``<table>`` for the right-hand preview pane -- record field
    lists, search results, message lists, report rows all read better as
    an actual table than a plain-text dump (drive_upload_file's own
    preview already established the precedent of a structured, non-prose
    disclosure for this pane). ``table`` is ``{"caption": str (optional),
    "headers": list[str], "rows": list[list[str]], "footer": str
    (optional)}`` -- every cell individually escaped, header/footer text
    included, same discipline as everywhere else in this module."""
    caption = table.get("caption", "")
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    footer = table.get("footer", "")
    parts = []
    if caption:
        parts.append(f'<div class="pf-table-caption">{_html_escape(caption)}</div>')
    thead_html = ""
    if headers:
        header_html = "".join(f"<th>{_html_escape(str(h))}</th>" for h in headers)
        thead_html = f"<thead><tr>{header_html}</tr></thead>"
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{_html_escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    parts.append(f'<table class="pf-table">{thead_html}<tbody>{rows_html}</tbody></table>')
    if footer:
        parts.append(f'<div class="pf-table-footer">{_html_escape(footer)}</div>')
    return "".join(parts)


def _field_block_html(label: str, value: str) -> str:
    """A standalone ``Label: value`` line, e.g. Jira's "Reporter" or a
    Gmail thread message's "From"/"Date" -- the label uses
    ``.pf-preview-label``, the same font as a table's ``<th>``/caption, so
    field names read identically everywhere in the right pane."""
    return (
        '<div class="pf-preview-field">'
        f'<span class="pf-preview-label">{_html_escape(label)}:</span>'
        f'<span class="pf-preview-field-value">{_html_escape(value)}</span>'
        '</div>'
    )


def _text_block_html(text: str) -> str:
    return f'<div class="pf-preview-paragraph">{_html_escape(text)}</div>'


def _heading_block_html(label: str) -> str:
    """A standalone section heading with no value on the same line, e.g.
    Jira's "Description" heading above its (often multi-line) paragraph --
    ``_field_block_html`` fits a short label:value pair on one line, but a
    long paragraph needs its label on its own line first. Same
    ``.pf-preview-label`` font as everywhere else in the right pane."""
    return f'<div class="pf-preview-label" style="margin-bottom:6px">{_html_escape(label)}</div>'


def _render_block(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return _text_block_html(block.get("text", ""))
    if kind == "field":
        return _field_block_html(block.get("label", ""), block.get("value", ""))
    if kind == "heading":
        return _heading_block_html(block.get("label", ""))
    if kind == "table":
        return _table_html(block)
    return ""


def build_preview_body_html(
    details_text: str = "", *,
    image_data_uri: str = "",
    pdf_data_uri: str = "",
    tables: list[dict] | None = None,
    blocks: list[dict] | None = None,
) -> str:
    """The inner-HTML fragment for ``WIDE`` layout's right-hand preview pane
    (``NARROW`` has no preview at all -- callers never need this for a
    narrow-shape tool) -- the ``build_card_stack_html()``-embeddable
    counterpart to approval_window.py's ``_details_html()``,
    which builds a *full standalone document* for its own separate small
    WKWebView instead. Same escaping/whitespace discipline: ``details_text``
    is already HTML-stripped plain text (see html_to_text.py) and is never
    treated as markup, only escaped and given ``white-space: pre-wrap``.

    ``pdf_data_uri`` takes priority over ``image_data_uri``, which takes
    priority over plain ``details_text``/``tables`` -- the same precedence
    ``_build_details_view()`` already holds for the legacy layout's
    pdf_bytes-before-preview_bytes-before-text dispatch, just rendered
    inline via a standard ``<embed>``/``<img>`` data URI here instead of a
    separate native ``PDFView``/``NSImageView`` overlay: v2's whole content
    area is already one WKWebView, so there's no separate small pane for a
    native view to stand in for -- WebKit's own built-in PDF renderer and
    image decoding handle both directly, no extra native code needed.

    ``tables`` (see ``_table_html``) render after ``details_text`` --
    together, not either/or, since some tools need both. Neither is
    required; an empty details_text with one table is the normal shape for
    tools whose entire "new" content is inherently record/list-shaped
    (Salesforce record fields, Salesforce search results, Telegram
    message lists).

    ``blocks``, when given, takes full precedence over both
    ``details_text`` and ``tables`` -- an ordered list of ``{"type":
    "text", "text": ...}`` (a plain paragraph), ``{"type": "field",
    "label": ..., "value": ...}`` (a standalone "Label: value" line, font-
    matched to a table header/caption via ``.pf-preview-label`` -- see
    ``_field_block_html``), or a table dict (same shape as one entry of
    ``tables``, see ``_table_html``). This is what makes *interleaving*
    possible -- text, then a table, then more text -- which a flat
    details_text-then-tables split can't express: e.g. jira_get_issue's
    Reporter field, then its Description paragraph, then its Comments
    table; or a Gmail thread's per-message From/Date fields each followed
    by that message's body. Tools whose right pane is simple prose or a
    simple table-only list don't need this -- ``details_text``/``tables``
    alone still cover those without the extra structure.

    No content_kind="email" structured header here (unlike the legacy
    layout's ``_details_html()``): under the §1/§3 knowledge-boundary split,
    From/Subject/Date already render as §1 rows and To as a §3 row, so
    repeating them a second time atop the body would just be duplication --
    the right pane is plain body text for every WIDE tool, email included.
    """
    if pdf_data_uri:
        return (
            f'<embed src="{pdf_data_uri}" type="application/pdf" '
            'style="width:100%;height:100%;min-height:400px;border:none">'
        )
    if image_data_uri:
        return f'<img src="{image_data_uri}" style="max-width:100%;display:block">'
    if blocks:
        return "".join(_render_block(b) for b in blocks)
    tables_html = "".join(_table_html(t) for t in (tables or []))
    if not details_text and not tables_html:
        return _escaped_text_fragment(details_text)  # "(no details)" placeholder
    text_html = _escaped_text_fragment(details_text) if details_text else ""
    return text_html + tables_html


def _escaped_text_fragment(text: str) -> str:
    escaped = _html_escape(text or "(no details)")
    return f'<div style="white-space:pre-wrap;word-wrap:break-word;font-size:13px;line-height:1.6">{escaped}</div>'


def _kv_rows_html(pairs: list[tuple[str, str]]) -> str:
    rows = []
    for k, v in pairs:
        clamp = line_clamp_for(str(k))
        # Inline override only when it actually differs from the CSS
        # default -- keeps the common case's markup uncluttered.
        style_attr = f' style="-webkit-line-clamp:{clamp}"' if clamp != DEFAULT_LINE_CLAMP else ""
        # A native title="..." tooltip, not JS -- this document runs with
        # JavaScript disabled (see build_card_stack_html's caller,
        # approval_window.py's config.preferences().setJavaScriptEnabled_
        # (False)), and WebKit already shows a hover tooltip for any
        # element with a title attribute with no script needed. Set
        # unconditionally rather than only when a value is actually
        # clamped: knowing in advance whether a given string will exceed N
        # lines at the rendered column width/font would need real text
        # measurement, which this whole layout deliberately avoids (see
        # this module's docstring) -- harmless on an untruncated value,
        # since hovering it just repeats what's already fully visible.
        v_str = str(v)
        rows.append(
            f'<div class="pf-kv"><span>{_html_escape(str(k))}</span>'
            f'<span{style_attr} title="{_html_escape(v_str)}">{_html_escape(v_str)}</span></div>'
        )
    return "".join(rows)


def _card(kicker: str, inner_html: str, *, style: str = "") -> str:
    style_attr = f' style="{style}"' if style else ""
    return f'<div class="card"{style_attr}><div class="card-kicker">{_html_escape(kicker)}</div>{inner_html}</div>'


def _section_1_html(number: int, is_read: bool, preview: dict[str, str]) -> str:
    if not preview:
        return ""
    kicker = f"{number:02d} · " + ("What Claude already knows" if is_read else "Action to perform")
    return _card(kicker, _kv_rows_html(list(preview.items())))


def _section_2_html(number: int, is_read: bool, claude_reason: str) -> str:
    if not claude_reason:
        return ""
    kicker = f"{number:02d} · " + ("Why Claude needs more data" if is_read else "Details — data to write")
    # title="..." tooltip, same reasoning as _kv_rows_html's own -- shows
    # the full reason on hover with no JS, harmless when it isn't actually
    # clamped.
    body = (
        f'<p class="pf-quote" title="{_html_escape(claude_reason)}">“{_html_escape(claude_reason)}”</p>'
        f'<div class="card-meta">Claude’s stated reason · unverified</div>'
    )
    return _card(kicker, body)


def _section_3_html(number: int, disclosure_rows: list[tuple[str, str]]) -> str:
    # Read-gate only. Absent (not just empty) when a tool has nothing new to
    # disclose -- see module docstring for where disclosure_rows comes from.
    if not disclosure_rows:
        return ""
    kicker = f"{number:02d} · What will be provided to Claude"
    return _card(kicker, _kv_rows_html(disclosure_rows))


def _tag_html(label: str, *, bg: str, color: str) -> str:
    return f'<span class="tag" style="background:{bg};color:{color}">{_html_escape(label)}</span>'


def _risk_section_html(
    number: int, categories: list[str], *, variant: str,
) -> str:
    """§4 (or §3, if §3 above didn't render): the PII/content-flag card.
    ``variant`` is one of:
      - "read": review-gate PII match. Unchanged from earlier design turns
        (accent-2 tokens) -- see module docstring, this card's job is to
        look distinct from "write" below, which it already does.
      - "write": popup-gate content-flag match, informational only. Uses
        the new pii-write-bg amber/ochre tokens.
      - "write-forced": drive_upload_file's own PII match, which forces the
        same second-confirmation flow "read" does despite being a write --
        no distinct design exists yet (a design-canvas edit is planned), so
        this reuses "read"'s styling as an interim placeholder. See module
        docstring.
    """
    if not categories:
        return ""
    kicker = f"{number:02d} · Possible PII detected"
    if variant == "write":
        card_style = "background:var(--pii-w-bg);border:1px solid var(--pii-w-border)"
        ink = "var(--pii-w-ink)"
        tag_bg, tag_color = "var(--pii-w-tagbg)", "var(--pii-w-ink)"
        message = "This message appears to contain"
    else:  # "read" and the "write-forced" placeholder
        card_style = "background:var(--color-accent-2-100);border:1px solid var(--color-accent-2-300)"
        ink = "var(--color-accent-2-800)"
        tag_bg, tag_color = "var(--color-accent-2-200)", "var(--color-accent-2-800)"
        message = "Review carefully before approving"
    tags = "".join(_tag_html(c, bg=tag_bg, color=tag_color) for c in categories)
    body = (
        f'<div style="display:flex;align-items:center;gap:8px;color:{ink};'
        f'font-weight:600;font-size:14px;margin-bottom:8px">⚠️ {_html_escape(message)}</div>'
        f'{tags}'
    )
    kicker_html = f'<div class="card-kicker" style="color:{ink}">{_html_escape(kicker)}</div>'
    return f'<div class="card" style="{card_style}">{kicker_html}{body}</div>'


def build_card_stack_html(
    *,
    layout: str,
    title: str,
    connector_icon_data_uri: str,
    shield_icon_data_uri: str,
    is_read: bool,
    seen_count_text: str,
    preview: dict[str, str],
    claude_reason: str,
    disclosure_rows: list[tuple[str, str]],
    pii_categories: list[str],
    write_content_flags: list[str],
    upload_forced: bool,
    temp_accept_text: str,
    preview_kicker: str,
    preview_body_html: str,
    columns_max_height: float = 0.0,
    right_pane_max_height: float = 520.0,
) -> str:
    """Build the full HTML document for one approval window's content area.

    Pure function -- no AppKit, no filesystem access beyond the module-level
    styles.css already read at import time -- directly unit-testable, same
    contract ``_details_html()`` already holds in approval_window.py.

    ``layout`` is ``NARROW`` (§1-§4 only, no preview pane at all --
    ``preview_kicker``/``preview_body_html`` are ignored entirely) or
    ``WIDE`` (§1-§4 in a fixed-width left column, plus a genuine
    independently-scrolling right-hand preview pane). Callers decide which
    per tool -- see module docstring for the criterion (real free-text body
    content vs. everything else) and approval_window.py's ``layout``
    parameter. No "Show more"/"Show less" control anywhere: progressive
    disclosure by area-expansion doesn't apply once every row has a fixed,
    truncated size (see styles.css's ``.pf-kv``/``.pf-quote``).

    ``columns_max_height``, when non-zero (approval_window.py's
    ApprovalWindowController._columns_max_height -- 0 in the common case,
    only set once the screen-height cap has actually trimmed the window
    below what everything needs), caps §3 alone (the one card whose row
    count genuinely varies per tool/call) below the always-visible header/
    §1/§2/risk-card stack, and, for WIDE, also caps the right-hand preview
    pane -- independently of whether §3 needed capping, since a WIDE
    tool's preview content (an email/document/report) is unrelated in
    length to the left column and needs its own bound regardless. Zero
    still means "no §3 cap" (§1/§2/risk fit comfortably in virtually every
    case, so an artificial cap there would only risk clipping content over
    a few pixels of Python-estimate-vs-WebKit-actual rendering drift) --
    but the right pane, for WIDE, is *always* capped via
    ``right_pane_max_height`` -- the real caller (approval_window.py)
    always passes the actual webview_height there, since the right pane
    never had the "already fits" guarantee §1-§4 do. The 520.0 default
    here only matters for callers (tests, or a future WIDE caller) that
    don't have a real webview_height on hand.

    §1, §2, and the PII/content-flag risk card are always fully visible,
    never inside the scrollable region -- only §3 ("What will be provided
    to Claude") scrolls, and only once the cap above actually applies.
    This also means the risk card renders *before* §3 now (previously
    after it): the highest-consequence card is never one scroll away from
    being missed.

    Exactly one of ``pii_categories``/``write_content_flags`` is ever
    non-empty for a given call (gate.py never populates both at once), and
    ``upload_forced`` only ever accompanies a non-empty ``write_content_flags``
    -- see _risk_section_html()'s docstring for what each combination
    renders.
    """
    width = CONTENT_WIDTH[layout]
    # A plain running counter, advanced only when a section actually
    # renders -- not itertools.count()'d speculatively, since §1/§2 are
    # effectively always present in production but §3/§4 aren't, and this
    # must reflect exactly what's on screen (see module docstring on why
    # §4's number is dynamic).
    next_number = 1
    pinned_html = []  # header, §1, §2, risk card -- always fully visible
    scrollable_html = []  # §3 alone -- the only card that ever scrolls

    sec1 = _section_1_html(next_number, is_read, preview)
    if sec1:
        pinned_html.append(sec1)
        next_number += 1

    sec2 = _section_2_html(next_number, is_read, claude_reason)
    if sec2:
        pinned_html.append(sec2)
        next_number += 1

    # Pinned, and numbered *before* §3 -- the highest-consequence card
    # must never end up scrolled out of view, and reads that way too:
    # right after "why Claude needs this," before the disclosure detail.
    if pii_categories:
        risk_html = _risk_section_html(next_number, pii_categories, variant="read")
    elif write_content_flags:
        variant = "write-forced" if upload_forced else "write"
        risk_html = _risk_section_html(next_number, write_content_flags, variant=variant)
    else:
        risk_html = ""
    if risk_html:
        pinned_html.append(risk_html)
        next_number += 1

    if is_read:
        # Write-gate calls never get §3 at all -- the counter simply never
        # advances for one.
        sec3 = _section_3_html(next_number, disclosure_rows)
        if sec3:
            scrollable_html.append(sec3)
            next_number += 1

    header_html = _header_html(title, connector_icon_data_uri, shield_icon_data_uri, seen_count_text)
    pinned_joined = "".join(pinned_html)
    scrollable_joined = "".join(scrollable_html)
    # Only §3 ever gets this cap -- header/§1/§2/risk are pinned above it,
    # always fully visible (see this function's own docstring).
    scroll_style = f'max-height:{columns_max_height:.0f}px;overflow-y:auto' if columns_max_height else ""
    scrollable_capped = (
        f'<div class="pf-scroll" style="{scroll_style}">{scrollable_joined}</div>'
        if scroll_style else scrollable_joined
    )
    left_column = header_html + pinned_joined + scrollable_capped

    if layout == WIDE:
        # Fixed left column width (_WIDE_LEFT_COLUMN_WIDTH) regardless of
        # the overall window width -- the design canvas's own two-column
        # cards used a fixed flex-basis the same way (350px there; widened
        # since, see that constant's own comment).
        #
        # right_pane_max_height is unconditional (unlike columns_max_height
        # above) -- a WIDE tool's preview content (an email/document/report)
        # is unrelated in length to the left column and virtually always
        # needs its own scroll bound, regardless of whether §3 needed
        # capping. Without an explicit cap here, a long preview would
        # stretch the whole flex row (align-items:stretch) past the
        # webview's own fixed native frame, falling back to the body's own
        # overflow-y:auto -- a whole-page scroll instead of the right
        # pane's own contained one.
        right_pane_style = (
            f'flex:1;min-width:0;border-left:1px solid var(--color-divider);padding-left:24px'
            f';max-height:{right_pane_max_height:.0f}px;overflow-y:auto'
        )
        body_html = (
            # align-items defaults to "stretch" (deliberately not
            # overridden to "flex-start") so both columns match the height
            # of the taller one -- otherwise the right pane's border-left
            # divider only extends as far as its own (often shorter)
            # content, instead of running the window's full height.
            '<div style="display:flex;gap:28px">'
            f'<div style="flex:0 0 {_WIDE_LEFT_COLUMN_WIDTH}px;min-width:0">{left_column}</div>'
            f'<div class="pf-scroll" style="{right_pane_style}">'
            f'<div class="card-kicker" style="margin-bottom:8px">{_html_escape(preview_kicker)}</div>'
            f'{preview_body_html}'
            '</div></div>'
        )
    else:
        # NARROW: no preview pane at all -- preview_kicker/preview_body_html
        # are simply not used. See module docstring.
        body_html = left_column

    if temp_accept_text:
        body_html += (
            f'<div style="margin-top:16px;font-size:11px;'
            f'color:color-mix(in srgb, var(--color-text) 55%, transparent)">'
            f'{_html_escape(temp_accept_text)}</div>'
        )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light">
<style>
{_STYLES_CSS}
html, body {{ overflow-y: auto; }}
body {{ padding: 26px 30px 24px; width: {width}px; }}
</style>
</head>
<body>{body_html}</body>
</html>
"""


def _header_html(title: str, connector_icon_data_uri: str, shield_icon_data_uri: str, seen_count_text: str) -> str:
    connector_img = (
        f'<img src="{connector_icon_data_uri}" style="width:20px;height:20px;object-fit:contain">'
        if connector_icon_data_uri else ""
    )
    shield_img = (
        f'<img src="{shield_icon_data_uri}" style="width:51px;height:51px;object-fit:contain;opacity:.9">'
        if shield_icon_data_uri else ""
    )
    seen_html = (
        f'<div style="font-size:12px;color:var(--color-neutral-700);margin-bottom:6px">'
        f'{_html_escape(seen_count_text)}</div>'
        if seen_count_text else ""
    )
    return (
        '<div class="pf-head">'
        '<div style="min-width:0">'
        f'<div class="pf-kicker">{connector_img}<span>PrivacyFence</span></div>'
        f'{seen_html}'
        f'<h2>{_html_escape(title)}</h2>'
        '</div>'
        f'{shield_img}'
        '</div>'
    )
