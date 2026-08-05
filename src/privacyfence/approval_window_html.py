"""Card-stack HTML template for the approval window.

Renders the *entire* content area of a review-gate or popup-gate dialog as one
self-contained HTML document for a single full-window WKWebView, including its
own Deny/Allow once/Always allow button row (``_button_row_html``) -- see
approval_window.py's module docstring for why these moved off native NSButtons
and into this document, and ``_JS`` below for the click/keyboard-dispatch
bridge (``window.webkit.messageHandlers.pf``) that replaces native
``buttonClicked_`` tag dispatch.

Visual design assets (styles.css, with Source Serif 4 embedded as base64 data
URIs; see that directory's fonts/OFL.txt for licensing) are vendored into
``resources/approval_window/``. Google Fonts' ``@import`` is replaced with the
vendored local ``@font-face`` -- this document must never trigger a network
fetch just to render a popup.

Every section is numbered dynamically (a running counter, not literal
"01"/"02"/"03"/"04" strings) because which sections actually render varies by
tool and by direction: §3 ("What will be provided to Claude") only ever
renders for a review-gate call carrying a ``visibility`` dict today (see
``disclosure_rows`` below), so the §4-equivalent risk card that follows it
lands on "03" instead of "04" whenever §3 is absent -- a write-gate PII card
is numbered "03" (no §3 exists on the write side at all), while a read-gate
one is "04" wherever a §3 card also rendered on that same tool.

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

drive_upload_file's PII card reuses the read-gate's own accent-2 card
styling: gate.py routes its own PII match through the same forced
second-confirmation flow the read-gate case gets, and
``upload_forced=True`` selects that same styling for it -- see that
parameter's docstring.
"""
from __future__ import annotations

from html import escape as _html_escape
from pathlib import Path

from .markdown_to_html import markdown_to_html

_STYLES_PATH = Path(__file__).parent / "resources" / "approval_window" / "styles.css"
_STYLES_CSS = _STYLES_PATH.read_text(encoding="utf-8")

# Narrow (single-column, sections only, no preview pane at all) vs wide
# (two-column, sections + a genuine free-text-body right pane) -- set
# explicitly per call site (see approval_window.py's `layout` param), not a
# length heuristic. See module docstring for exactly which tools get which.
NARROW = "narrow"
WIDE = "wide"

# Public (no leading underscore): approval_window.py's own _WINDOW_WIDTH
# derives from this directly rather than duplicating it, so the native
# window frame and the HTML body rendered inside it can never drift out of
# sync.
CONTENT_WIDTH = {NARROW: 610, WIDE: 980}

# <body>'s own vertical padding (see build_card_stack_html's <style> block,
# "padding-top: {BODY_PADDING_TOP}px ... padding-bottom: {BODY_PADDING_BOTTOM}px").
# box-sizing:border-box means this is carved *out of* body's 100vh before
# any flex child (the .pf-scroll left column, or WIDE's right pane) ever
# sees a pixel of it -- approval_window.py's own window-height estimate
# must reserve this same amount on top of its content-height guess and its
# own WebKit-render-drift margin, or a real render can overflow its
# .pf-scroll container by exactly this much even when the content estimate
# itself was generous. Found via a real scrollHeight/clientHeight
# measurement on a NARROW write dialog (contacts_create) whose estimate's
# usual slack over real content wasn't enough to also absorb this
# previously-nowhere-accounted-for padding.
BODY_PADDING_TOP = 26
BODY_PADDING_BOTTOM = 24
BODY_VERTICAL_PADDING = BODY_PADDING_TOP + BODY_PADDING_BOTTOM

# WIDE's left column width -- deliberately narrower than a full 550px
# single-column tool's content width, giving §1-§4's rows enough room without
# pushing the window's overall width past what comfortably fits on a
# scaled-resolution laptop display (a symmetric 550/550 split would need an
# ~1200px window, too wide for common 1280/1440-logical-point MacBook
# screens).
_WIDE_LEFT_COLUMN_WIDTH = 420

# §3's generic allow/redact/block -> disclosure-sentence mapping. A
# deliberate, generic rule rather than hand-authored per-tool prose -- see
# this module's docstring for why the exact wording isn't tool-specific.
_DISCLOSURE_ALLOW = "Full {label_lower}"
_DISCLOSURE_REDACT = "{label}, with some fields redacted"
_DISCLOSURE_BLOCK = "None — not disclosed to Claude"


def disclosure_rows_from_visibility(visibility: dict[str, str]) -> list[tuple[str, str]]:
    """Translate the existing ``{label: allow/redact/block}`` policy dict
    (privacy_filter.category_policy()'s ground truth, unchanged) into §3's
    plain "what's disclosed" sentence per field (prose, not per-row icons),
    even though the exact wording here is generic rather than hand-tuned
    per tool (see module docstring). Pure function, order-preserving."""
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
# Extend here as more tools need a taller allowance; approval_window.py's
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


def _markdown_block_html(markdown: str) -> str:
    """A block of extracted content that carries real structure -- DOCX/
    PPTX/XLSX/Confluence content run through text_extraction.py or
    html_to_text.py's html_to_markdown(), both of which emit the same
    Markdown syntax markdown_to_html.py renders here. Unlike
    ``_text_block_html``, this is not further HTML-escaped: markdown_to_html.py
    already escapes every literal text span itself and only ever emits
    markup for syntax it actually recognized, so its output is safe to
    embed directly."""
    return f'<div class="pf-md">{markdown_to_html(markdown)}</div>'


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
    if kind == "markdown":
        return _markdown_block_html(block.get("text", ""))
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
    narrow-shape tool). Same escaping/whitespace discipline: ``details_text``
    is already HTML-stripped plain text (see html_to_text.py) and is never
    treated as markup, only escaped and given ``white-space: pre-wrap``.

    ``pdf_data_uri`` takes priority over ``image_data_uri``, which takes
    priority over plain ``details_text``/``tables`` -- rendered inline via
    a standard ``<embed>``/``<img>`` data URI: the whole content area is
    already one WKWebView, so WebKit's own built-in PDF renderer and image
    decoding handle both directly, no native PDFView/NSImageView overlay
    needed.

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
    ``_field_block_html``), ``{"type": "markdown", "text": ...}`` (Markdown
    syntax -- headings, bold/italic, bullet/numbered lists, links, pipe
    tables -- rendered to real HTML via markdown_to_html.py, see
    ``_markdown_block_html``; this is how text_extraction.py's DOCX/PPTX/
    XLSX output and html_to_text.py's html_to_markdown() output get a rich
    preview instead of a flat text dump), or a table dict (same shape as
    one entry of ``tables``, see ``_table_html``). This is what makes
    *interleaving* possible -- text, then a table, then more text -- which
    a flat details_text-then-tables split can't express: e.g.
    jira_get_issue's Reporter field, then its Description paragraph, then
    its Comments table; or a Gmail thread's per-message From/Date fields
    each followed by that message's body. Tools whose right pane is simple
    prose or a simple table-only list don't need this -- ``details_text``/
    ``tables`` alone still cover those without the extra structure.

    No content_kind="email" structured header here: under the §1/§3
    knowledge-boundary split, From/Subject/Date already render as §1 rows
    and To as a §3 row, so
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
        # A native title="..." tooltip, not JS -- WebKit already shows a
        # hover tooltip for any element with a title attribute with no
        # script needed, so this doesn't reach for _JS's bridge even though
        # JavaScript is enabled for this document now (see
        # approval_window.py's module docstring for what _JS is actually
        # for -- the button row's click/keyboard dispatch, not tooltips).
        # Set unconditionally rather than only when a value is actually
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


def _card(kicker: str, inner_html: str, *, style: str = "", kicker_color: str = "") -> str:
    style_attr = f' style="{style}"' if style else ""
    kicker_style_attr = f' style="color:{kicker_color}"' if kicker_color else ""
    return (
        f'<div class="card"{style_attr}><div class="card-kicker"{kicker_style_attr}>'
        f'{_html_escape(kicker)}</div>{inner_html}</div>'
    )


# §1/§2's kicker color for a write dialog -- read stays the plain
# .card-kicker default (var(--color-accent), teal); write gets the same
# accent-2 (magenta) family the pill/rail already use, so the two kinds
# of dialog read as visually distinct at the section-header level too,
# not just via the header pill/rail.
_WRITE_KICKER_COLOR = "var(--color-accent-2-700)"


def _section_1_html(number: int, is_read: bool, preview: dict[str, str]) -> str:
    if not preview:
        return ""
    kicker = f"{number:02d} · " + ("What Claude already knows" if is_read else "Action to perform")
    return _card(
        kicker, _kv_rows_html(list(preview.items())),
        kicker_color="" if is_read else _WRITE_KICKER_COLOR,
    )


def _section_2_html(number: int, is_read: bool, claude_reason: str) -> str:
    if not claude_reason:
        return ""
    # §2 always shows Claude's stated *reason* (the quote below), on both
    # read and write. "Why Claude is doing this" matches what's actually
    # on screen -- the real write payload lives in §1/the right pane, not
    # here -- same as read's "Why Claude needs more data".
    kicker = f"{number:02d} · " + ("Why Claude needs more data" if is_read else "Why Claude is doing this")
    # title="..." tooltip, same reasoning as _kv_rows_html's own -- shows
    # the full reason on hover with no JS, harmless when it isn't actually
    # clamped.
    body = (
        f'<p class="pf-quote" title="{_html_escape(claude_reason)}">“{_html_escape(claude_reason)}”</p>'
        f'<div class="card-meta">Claude’s stated reason · unverified</div>'
    )
    return _card(kicker, body, kicker_color="" if is_read else _WRITE_KICKER_COLOR)


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
      - "read": review-gate PII match. Accent-2 tokens -- see module
        docstring, this card's job is to look distinct from "write" below.
      - "write": popup-gate content-flag match, informational only. Uses
        the pii-write-bg amber/ochre tokens.
      - "write-forced": drive_upload_file's own PII match, which forces the
        same second-confirmation flow "read" does despite being a write --
        reuses "read"'s styling. See module docstring.
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


def _button_row_html(accept_all_labels: list[str]) -> str:
    """Deny/Allow once/Always allow -- rendered as part of this document's
    own content now (see module docstring), not native NSButtons in a fixed
    band below the webview. ``accept_all_labels`` is the already-formatted
    "Always allow" / "Always allow — {hint}" string per matching candidate
    (approval_window.py's ApprovalWindowController computes each one, same
    as it always computed the single one -- this function just renders
    whatever strings it's given, one button per entry).

    Zero entries: no Always allow button at all, same as
    ``allow_accept_all=False`` used to render. Exactly one entry: rendered
    inline in ``.pf-btn-row-left`` alongside Deny -- pixel-identical to
    today's single-candidate layout, so the ~46 single-candidate operations
    get zero visual change. Two or more entries (only the four
    ``auto_accept.SUGGESTION_FAMILIES`` operations can ever produce this):
    rendered as their own left-aligned, wrapping button row *above* the
    Deny/Allow once band instead, which keeps its fixed position and drops
    its own inline Always-allow link -- see module docstring's decisions.

    Every button starts fully disabled -- ``aria-disabled="true"``, no
    ``tabindex`` -- exactly like settings_window_html.py's own disabled
    ``toggleHtml()`` state (same "omit the interactive affordances entirely,
    don't rely on a browser default disabled semantic no plain ``<div>``
    gets for free" reasoning). ``_JS``'s ``enableButtons()`` is what clears
    this once the page is actually ready to be looked at -- see that
    function's own comment for why that's a DOMContentLoaded-driven, fully
    in-page signal now, not something approval_window.py's
    WKNavigationDelegate methods drive directly the way they used to.

    Each Always-allow button carries ``data-pf-choice="{index}"`` (its
    index into ``accept_all_labels``) alongside ``data-pf-action="accept_all"``
    -- ``_JS``'s bridge includes this in the resolve message so
    approval_window.py/gate.py know *which* candidate rule was picked, not
    just that some "Always allow" button was clicked.

    ``data-pf-primary`` marks Allow once specifically: ``_JS``'s keydown
    handler activates a *focused* Deny/Always-allow control on Enter/Space
    the same way a click would, but deliberately excludes anything carrying
    this attribute -- hitting Enter/Space must never be able to approve a
    request nobody has actually reviewed yet, the same guarantee the native
    button's missing ``"\\r"`` keyEquivalent used to give (see
    approval_window.py's own module docstring). Escape still resolves Deny
    regardless of focus (``_JS``'s own document-level handler), matching the
    native Deny button's ``"\\x1b"`` keyEquivalent -- declining via a
    reflexive keypress stays the safe direction.
    """
    deny_html = (
        '<div class="pf-btn pf-btn-deny" role="button" aria-disabled="true" '
        'aria-label="Deny" data-pf-action="deny">Deny</div>'
    )
    allow_once_html = (
        '<div class="pf-btn pf-btn-primary" role="button" aria-disabled="true" '
        'data-pf-primary="1" aria-label="Allow once" data-pf-action="accept">Allow once</div>'
    )

    def _candidate_html(index: int, label: str) -> str:
        return (
            '<div class="pf-btn-link" role="button" aria-disabled="true" '
            f'aria-label="{_html_escape(label)}" data-pf-action="accept_all" '
            f'data-pf-choice="{index}">{_html_escape(label)}</div>'
        )

    if len(accept_all_labels) <= 1:
        always_allow_html = _candidate_html(0, accept_all_labels[0]) if accept_all_labels else ""
        return (
            '<div class="pf-btn-row">'
            f'<div class="pf-btn-row-left">{deny_html}{always_allow_html}</div>'
            f'{allow_once_html}'
            '</div>'
        )

    candidates_html = "".join(_candidate_html(i, label) for i, label in enumerate(accept_all_labels))
    return (
        f'<div class="pf-btn-row-candidates">{candidates_html}</div>'
        '<div class="pf-btn-row">'
        f'<div class="pf-btn-row-left">{deny_html}</div>'
        f'{allow_once_html}'
        '</div>'
    )


# Click/keyboard dispatch for the button row above, plus the "content is
# actually ready" gate that used to be a Python-side concern
# (webView_didFinishNavigation_ enabling native NSButtons). DOMContentLoaded
# is the right in-page equivalent specifically because this document has
# nothing left to fetch by the time it fires -- fonts/icons/images are all
# already-inlined base64 data URIs, never a network request (see module
# docstring) -- so there's no meaningful gap between "DOM built" and
# "everything that was ever going to render has rendered" the way there
# would be for a document with real external resources.
#
# window.__pfEnableButtons is exposed specifically so approval_window.py's
# WKNavigationDelegate fail-safes (webView_didFail(Provisional)Navigation_
# withError_) can still force button click-ability in the one case
# DOMContentLoaded itself might never fire: an outright load failure. Safe
# to call more than once (removeAttribute/setAttribute are idempotent), so
# those fail-safes can call it unconditionally without checking whether the
# page's own handler already ran.
_JS = """
(function () {
  function post(result, choice) {
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.pf) {
      var payload = { action: 'resolve', result: result };
      if (choice !== null && choice !== undefined) payload.choice = choice;
      window.webkit.messageHandlers.pf.postMessage(payload);
    }
  }

  function resolveFrom(el) {
    if (!el || el.getAttribute('aria-disabled') === 'true') return;
    var action = el.getAttribute('data-pf-action');
    if (!action) return;
    // Only the accept_all buttons carry data-pf-choice -- see
    // _button_row_html's own comment for why this identifies *which*
    // matching candidate rule was picked, not just that some
    // "Always allow" button was clicked.
    var choiceAttr = el.getAttribute('data-pf-choice');
    post(action, choiceAttr !== null ? parseInt(choiceAttr, 10) : null);
  }

  function enableButtons() {
    // Not scoped to .pf-btn-row alone -- 2+ candidates render their own
    // .pf-btn-row-candidates row above it (see _button_row_html), which
    // needs the same enable treatment. data-pf-action is never used
    // outside these two button rows, so this selector is unambiguous.
    var buttons = document.querySelectorAll('[data-pf-action]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].removeAttribute('aria-disabled');
      buttons[i].setAttribute('tabindex', '0');
    }
  }
  window.__pfEnableButtons = enableButtons;

  document.addEventListener('DOMContentLoaded', function () {
    enableButtons();

    document.body.addEventListener('click', function (e) {
      resolveFrom(e.target.closest('[data-pf-action]'));
    });

    document.body.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        resolveFrom(document.querySelector('[data-pf-action="deny"]'));
        return;
      }
      // See _button_row_html's own docstring for why [data-pf-primary]
      // (Allow once) is deliberately excluded here.
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest) {
        var interactive = e.target.closest('[data-pf-action]:not([data-pf-primary])');
        if (interactive) {
          e.preventDefault();
          resolveFrom(interactive);
        }
      }
    });
  });
})();
"""


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
    accept_all_labels: list[str],
) -> str:
    """Build the full HTML document for one approval window's content area.

    Pure function -- no AppKit, no filesystem access beyond the module-level
    styles.css already read at import time -- directly unit-testable.

    ``layout`` is ``NARROW`` (§1-§4 only, no preview pane at all --
    ``preview_kicker``/``preview_body_html`` are ignored entirely) or
    ``WIDE`` (§1-§4 in a fixed-width left column, plus a genuine
    independently-scrolling right-hand preview pane). Callers decide which
    per tool -- see module docstring for the criterion (real free-text body
    content vs. everything else) and approval_window.py's ``layout``
    parameter. No "Show more"/"Show less" control anywhere: progressive
    disclosure by area-expansion doesn't apply once every row has a fixed,
    truncated size (see styles.css's ``.pf-kv``/``.pf-quote``).

    Containment is pure CSS flexbox, not a Python-computed pixel cap --
    ``<body>`` is ``height:100vh`` (the WKWebView's own real native frame,
    not an estimate of it) and ``display:flex;flex-direction:column``. The
    left column (header/§1/§2/risk-card/§3, all of it) is one
    ``flex:1;min-height:0;overflow-y:auto`` region -- a single shared
    scrollbar spans the whole column when its content is taller than the
    real available height, rather than only §3 growing its own internal
    one below an always-visible pinned block. For WIDE, the right-hand
    preview pane gets the identical treatment (its own independent
    ``flex:1;min-height:0;overflow-y:auto``) as a sibling of the left
    column inside a row that itself fills the same real 100vh via
    ``flex:1;min-height:0``. Containment is pure CSS flexbox, not a
    Python-computed pixel cap on each region: a Python-estimated
    worst-case row/section-count guess (see approval_window.py's
    ``_rows_height`` comment) is never real text measurement, so a pixel
    cap based on it would leave an uncapped region nowhere to grow but the
    whole-page ``html, body`` fallback scroll the moment WebKit's real
    render of any single row came out even a few pixels taller than
    guessed -- dragging the *entire* window (including the right pane)
    along with it. Flexbox has no such estimate to be wrong about: the
    left column and the right pane always get exactly "100vh," each with
    its own contained scroll, so a real render coming out larger than any
    Python guess just means that column's own scrollbar engages a little
    sooner -- the whole-page scroll path is never reached, regardless of
    which row is off or in which direction. Python's own height estimate
    (``_estimate_left_column_height`` et al.) still exists, but purely to
    pick a reasonable *initial* native window size -- it doesn't have to
    be exactly right for containment to hold.

    Trade-off worth knowing: because the left column is one shared scroll
    region, §1/§2/the PII-or-content-flag risk card are *not* guaranteed to
    stay on screen if the column's total content is taller than the
    window -- scrolling to read the rest of §3 also scrolls them out of
    view. The alternative (pinning those cards and only letting §3 scroll
    internally) trades this for a different problem: a short,
    visually-inconsistent internal scrollbar confined to §3's own card
    whenever the pinned block above it takes up most of the available
    height. This module takes the one-shared-scrollbar trade-off instead,
    matching the right pane's own full-height scrollbar treatment. The
    risk card still renders *before* §3 (not after) for the same reason as
    always: it's the highest-consequence card, so it's the first thing
    scrolled past on the way down, not the last.

    Exactly one of ``pii_categories``/``write_content_flags`` is ever
    non-empty for a given call (gate.py never populates both at once), and
    ``upload_forced`` only ever accompanies a non-empty ``write_content_flags``
    -- see _risk_section_html()'s docstring for what each combination
    renders.

    ``accept_all_labels`` controls the Always allow button(s) (see
    ``_button_row_html``) -- Deny and Allow once always render; an empty
    list renders no Always allow button at all (same as the old
    ``allow_accept_all=False``), one entry renders a single Always allow
    button inline with Deny (pixel-identical to today's single-candidate
    layout), and 2+ entries render their own button row above Deny/Allow
    once instead -- one button per matching auto-accept rule candidate.
    Each entry is already the fully-formatted label string (plain "Always
    allow", or "Always allow — {hint}"; approval_window.py's controller
    decides which per entry, same as it always did for the single case).
    The whole button row is appended last, after ``temp_accept_text``'s own
    caption when present.
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

    header_html = _header_html(title, connector_icon_data_uri, shield_icon_data_uri, seen_count_text, is_read)
    pinned_joined = "".join(pinned_html)
    scrollable_joined = "".join(scrollable_html)
    # The whole left column -- header/§1/§2/risk-card *and* §3 together --
    # is one shared scroll region, not split into an always-visible pinned
    # part plus a separately-scrolling §3. See this function's own
    # docstring for the trade-off this accepts (PII/§1/§2 can scroll out of
    # view alongside §3 in an extreme case) in exchange for one scrollbar
    # that visually spans the whole column, matching the right pane's own
    # full-height one, instead of a short one confined to just §3's card.
    left_column_content = header_html + pinned_joined + scrollable_joined

    if layout == WIDE:
        # Fixed left column width (_WIDE_LEFT_COLUMN_WIDTH, see that
        # constant's own comment) regardless of the overall window width --
        # a fixed flex-basis. overflow-y:auto + min-height:0 here (not
        # split across a pinned/scrollable pair) is what makes this one
        # shared scroll region.
        left_column = (
            f'<div class="pf-scroll" style="flex:0 0 {_WIDE_LEFT_COLUMN_WIDTH}px;min-width:0;'
            f'overflow-y:auto;min-height:0">{left_column_content}</div>'
        )
        right_pane_style = (
            'flex:1;min-width:0;border-left:1px solid var(--color-divider);padding-left:24px'
            ';overflow-y:auto;min-height:0'
        )
        # The outer row is flex:1;min-height:0 (fills the real 100vh body
        # below temp_accept_text, if present -- see the returned document's
        # <body> below) so align-items:stretch (default, kept deliberately)
        # gives both the left column and the right pane that same real
        # height -- not "whichever child is naturally taller," as a
        # content-sized row would give them.
        body_html = (
            '<div style="display:flex;gap:28px;flex:1;min-height:0">'
            f'{left_column}'
            f'<div class="pf-scroll" style="{right_pane_style}">'
            f'<div class="card-kicker" style="margin-bottom:8px">{_html_escape(preview_kicker)}</div>'
            f'{preview_body_html}'
            '</div></div>'
        )
    else:
        # NARROW: no preview pane at all -- preview_kicker/preview_body_html
        # are simply not used. See module docstring. This whole block itself
        # is the flex:1;min-height:0 child of <body> below (same shared
        # scroll-region treatment as WIDE's left column).
        body_html = (
            '<div class="pf-scroll" style="flex:1;min-height:0;overflow-y:auto">'
            f'{left_column_content}</div>'
        )

    if temp_accept_text:
        # flex:none -- a sibling of the row/column above inside <body>'s own
        # flex column, not part of the scrollable region, always visible
        # just above the button row (.pf-btn-row, appended next, also
        # flex:none).
        body_html += (
            f'<div style="flex:none;margin-top:16px;font-size:11px;'
            f'color:color-mix(in srgb, var(--color-text) 55%, transparent)">'
            f'{_html_escape(temp_accept_text)}</div>'
        )

    # Always present (unlike temp_accept_text above) -- every dialog has a
    # Deny/Allow once button row, see _button_row_html.
    body_html += _button_row_html(accept_all_labels)

    # Read/write side rail, paired with the header's same-colored pill
    # above -- 6px, on the window's left edge, cyan/accent for reads and
    # magenta/accent-2 for writes. Left padding is reduced by the rail's
    # own width so the total left inset (rail + padding) still matches the
    # 30px used everywhere else.
    rail_color = "var(--color-accent-500)" if is_read else "var(--color-accent-2-500)"
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<style>
{_STYLES_CSS}
html {{ height: 100%; }}
/* overflow-y:auto here is now a last-resort fallback only, not the
   containment mechanism -- see this function's own docstring. It should
   be effectively unreachable: <body> is exactly 100vh (the WKWebView's
   real native frame) and every region within it is a flex:1;min-height:0
   overflow-y:auto child of its own, so any content taller than expected
   grows that region's own internal scrollbar instead of this one. */
html, body {{ overflow-y: auto; }}
body {{
  box-sizing: border-box; width: {width}px; height: 100vh;
  padding-top: {BODY_PADDING_TOP}px; padding-right: 30px;
  padding-bottom: {BODY_PADDING_BOTTOM}px; padding-left: 24px;
  border-left: 6px solid {rail_color};
  display: flex; flex-direction: column;
}}
</style>
</head>
<body>{body_html}<script>{_JS}</script></body>
</html>
"""


def _header_html(
    title: str, connector_icon_data_uri: str, shield_icon_data_uri: str, seen_count_text: str, is_read: bool,
) -> str:
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
    # Read/Write pill, paired with the same-colored rail on <body> below --
    # cyan/accent for reads, magenta/accent-2 for writes, the same two
    # token families the rest of this template already uses (e.g. the read
    # vs write PII/content-flag card variants), visible on every dialog,
    # not only ones carrying a PII match.
    pill_bg = "var(--color-accent-100)" if is_read else "var(--color-accent-2-100)"
    pill_color = "var(--color-accent-700)" if is_read else "var(--color-accent-2-700)"
    pill_label = "Read" if is_read else "Write"
    pill_html = f'<span class="pf-pill" style="background:{pill_bg};color:{pill_color}">{pill_label}</span>'
    return (
        '<div class="pf-head">'
        '<div style="min-width:0">'
        f'<div class="pf-kicker">{connector_img}<span>PrivacyFence</span></div>'
        f'{seen_html}'
        f'<div style="display:flex;align-items:center;gap:10px">'
        f'<h2>{_html_escape(title)}</h2>{pill_html}</div>'
        '</div>'
        f'{shield_img}'
        '</div>'
    )
