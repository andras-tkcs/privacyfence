"""Native macOS approval popups.

Every gated tool call resolves through exactly one blocking dialog here.
There is no separate "show details" step and no pending-approval handshake:
full content is always shown before the decision, so the human always sees
what they're approving before they can click Allow once. The main gate
(show_popup / show_read_popup) renders through approval_window.py's custom
AppKit window; show_rule_confirmation_popup and show_pii_confirmation_popup
are smaller secondary prompts (confirming a standing auto-accept rule, or
confirming approval of content the PII detector flagged) and render through
dialog_window.py's own small AppKit+WKWebView host instead -- the same
bridge/blocking-wait pattern show_native_approval below uses, just for a
1-2-button confirmation rather than the full card-stack layout (issue #145
ported these off the old `osascript display dialog`/`choose from list`
prompts this module used to build directly).
"""
from __future__ import annotations

from .approval_window import show_native_approval
from .approval_window_html import NARROW
from .dialog_window import show_choice_dialog, show_confirmation_dialog


# ---------------------------------------------------------------------------- #
# Write gate (actions: send, create, edit, move, comment)
# ---------------------------------------------------------------------------- #

def show_popup(
    title: str,
    preview: dict[str, str],
    details_text: str,
    temp_accept_eligible: bool = False,
    claude_reason: str = "",
    write_content_flags: list[str] | None = None,
    seen_count: int = 0,
    connector: str = "",
    allow_accept_all: bool = False,
    preview_bytes: bytes = b"",
    preview_mime_type: str = "",
    preview_tables: list[dict] | None = None,
    preview_blocks: list[dict] | None = None,
    table_only: bool = False,
    upload_forced: bool = False,
    layout: str = NARROW,
    accept_all_hint: str = "",
) -> str:
    """Approval popup for write tools. No PII *gate* applies here -- see
    gate.py's module docstring for why the PII confirmation flow is
    read-only. Same reasoning is why this has no "AI will receive"
    visibility checklist (show_read_popup does): a write already shows
    exactly what's being sent, since it's content Claude itself drafted,
    not something read from an external source and potentially filtered
    on the way in.

    ``claude_reason`` (unlike ``visibility``) is shown here too -- Claude's
    self-reported reason for the call applies to writes as much as reads.
    See gate.py's reason_scope docstring: unverified, rendered as such.

    ``write_content_flags`` is a separate, deliberately weaker signal from
    ``pii_categories`` in show_read_popup: the same local detector run over
    Claude's own drafted content, informational only -- it never triggers
    the second "Are you sure?" confirmation show_pii_confirmation_popup
    adds on the read side, because there is no "possible PII flowed in
    from an external source" here to confirm. Rendered with a neutral
    style, not the red tint/banner. See gate.py's gated_call for why this
    is computed separately rather than reusing pii_categories's plumbing.

    ``seen_count`` is the request-fingerprint feature (AuditLogger.
    recent_matches) -- how many times this exact (connector, tool,
    summary) was already approved this week, shown here too since it
    applies to writes as much as reads.

    ``connector`` (e.g. "gmail", "slack") selects the top-left brand icon
    -- see approval_window.py's _connector_icon_path() docstring for the
    silent-skip fallback when no asset exists yet for it.

    ``temp_accept_eligible`` no longer offers a separate button (there used
    to be a distinct "Allow for 5 min" choice here) -- it only adds an
    informational caption above Deny/Allow once. Whether clicking Allow once
    also arms auto_accept.py's 5-minute, same-file grace window is gate.py's
    call, made from the same TEMP_ACCEPT_ELIGIBLE_OPERATIONS check that
    produced this flag.

    ``allow_accept_all`` adds an "Always allow" button, same as
    show_read_popup's -- offered only for the handful of write operations
    with a resource-identity-scoped rule to propose (see auto_accept.py's
    WRITE_RULE_SUGGESTIONS); gate.py sets this from whether
    suggest_write_rule() returned anything for this call. False for every
    other write, identical to today.

    ``preview_bytes``/``preview_mime_type``, when set, render a native image
    view in the details pane instead of the usual plain-text WKWebView --
    unlike show_read_popup's ``pdf_bytes``/``content_kind``, these carry no
    AI-visibility parity constraint (see gate.py's gated_call docstring):
    only ever set by upload-shaped write tools (drive_upload_file) whose
    payload never reaches Claude's context at all.

    ``preview_tables``/``preview_blocks``/``table_only`` are the WIDE
    right-pane preview, same as show_read_popup's own -- e.g.
    drive_sheets_write_range's own values-being-written table, or
    jira_create_issue's Description heading. See gate.py's gated_call
    docstring for the exact shape of each.

    ``upload_forced`` selects the distinct "write-forced" PII card styling
    (an interim placeholder reusing the read-gate's own look -- see
    approval_window_html.py's _risk_section_html docstring) for
    drive_upload_file's own real PII match, which -- unlike every other
    write tool's informational ``write_content_flags`` banner -- forces the
    same second "Are you sure?" confirmation the read side gets. gate.py
    sets this from whether ``upload_pii_categories`` is non-empty, the one
    signal unique to that call site.

    ``layout`` selects the NARROW/WIDE card-stack shape -- gate.py picks
    this per tool from its own _TOOL_LAYOUT table, same as show_read_popup's
    own.

    ``accept_all_hint``, when set, is a short phrase naming the specific
    rule Always allow would create (e.g. "this folder", "if I'm sender") --
    shown right on the button itself, not just in the confirmation dialog
    after clicking. gate.py derives this from the same ``suggest_write_
    rule()`` result that decides ``allow_accept_all`` -- empty whenever
    that's False, and also empty for the one unconditional rule
    (``always_allow``, e.g. gmail_create_draft) that has no category to
    name. See auto_accept.describe_rule_short's own docstring.

    Returns 'accept', 'deny', or 'accept_all' (only offered when
    allow_accept_all is True).
    """
    return show_native_approval(
        title=title, preview=preview, details_text=details_text, allow_accept_all=allow_accept_all,
        temp_accept_eligible=temp_accept_eligible, claude_reason=claude_reason,
        write_content_flags=write_content_flags, seen_count=seen_count, connector=connector,
        preview_bytes=preview_bytes, preview_mime_type=preview_mime_type,
        preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
        upload_forced=upload_forced, layout=layout, accept_all_hint=accept_all_hint,
        # show_popup is unconditionally the write-gate popup -- is_read is a
        # property of which of the two show_* functions was called, not a
        # per-call choice gate.py makes.
        is_read=False,
    )


# ---------------------------------------------------------------------------- #
# Review gate (reads)
# ---------------------------------------------------------------------------- #

def show_read_popup(
    title: str,
    preview: dict[str, str],
    details_text: str,
    allow_accept_all: bool,
    pii_categories: list[str] | None = None,
    visibility: dict[str, str] | None = None,
    claude_reason: str = "",
    seen_count: int = 0,
    content_kind: str = "generic",
    pdf_bytes: bytes = b"",
    connector: str = "",
    preview_bytes: bytes = b"",
    preview_mime_type: str = "",
    new_info: dict[str, str] | None = None,
    preview_tables: list[dict] | None = None,
    preview_blocks: list[dict] | None = None,
    table_only: bool = False,
    layout: str = NARROW,
    accept_all_hint: str = "",
) -> str:
    """Approval popup for read tools. Full content is always shown before the
    decision, in a scrollable pane — the user never has to click through to
    a second "show details" step.

    ``visibility`` is the "AI will receive" checklist (label -> resolved
    allow/redact/block policy from privacy_filter.category_policy()) --
    write (popup-gate) approvals never carry this, see show_popup's
    docstring for why. ``claude_reason`` is Claude's self-reported reason
    for the call -- unverified, see gate.py's reason_scope docstring.
    ``seen_count`` is the request-fingerprint feature (AuditLogger.
    recent_matches) -- how many times this exact (connector, tool, summary)
    was already approved this week. ``content_kind``/``pdf_bytes`` are a
    legacy-layout-only body rendering hint and native-PDFView payload,
    respectively -- ``pdf_bytes`` still renders inline via an <embed> data
    URI (see approval_window.py's _build_content_view), but
    ``content_kind`` has no effect on the current rendering (see
    build_preview_body_html's docstring).
    ``connector`` (e.g. "gmail", "drive") selects the top-left brand icon
    -- see approval_window.py's _connector_icon_path() docstring for the
    silent-skip fallback when no asset exists yet for it.
    ``preview_bytes``/``preview_mime_type``, when set, render a native image
    view instead -- see gate.py's gated_call docstring for why these carry
    no AI-visibility parity constraint, unlike ``pdf_bytes``.
    ``new_info``, when given, is §3's ("What will be provided to Claude")
    real (label, value) pairs -- see gate.py's gated_call docstring.
    ``preview_tables``, when given, renders the WIDE right-pane preview as
    structured table(s) instead of plain text -- see gate.py's gated_call
    docstring.
    ``preview_blocks``, when given, takes full precedence over both
    ``details_text`` and ``preview_tables`` for the WIDE right pane --
    an ordered list of text/field/table blocks, letting them interleave.
    See gate.py's gated_call docstring.
    ``table_only``, when True (and ``preview_tables`` is non-empty, and
    ``preview_blocks`` isn't set), shows only the table(s) in the WIDE
    right pane, not ``details_text`` too -- for tools whose details_text
    fully duplicates the table's own data. See gate.py's gated_call
    docstring.
    ``layout`` selects the NARROW/WIDE card-stack shape -- gate.py picks
    this per tool from its own _TOOL_LAYOUT table.

    ``accept_all_hint``, when set, is a short phrase naming the specific
    rule Always allow would create (e.g. "this folder", "if I'm sender") --
    shown right on the button itself, not just in the confirmation dialog
    after clicking. gate.py derives this from the same ``suggest_rule()``
    result that decides ``allow_accept_all``. See show_popup's matching
    docstring and auto_accept.describe_rule_short.

    Returns 'accept', 'deny', or 'accept_all' (only offered when
    allow_accept_all is True).
    """
    return show_native_approval(
        title=title, preview=preview, details_text=details_text, allow_accept_all=allow_accept_all,
        pii_categories=pii_categories, visibility=visibility, claude_reason=claude_reason,
        seen_count=seen_count, content_kind=content_kind, pdf_bytes=pdf_bytes, connector=connector,
        preview_bytes=preview_bytes, preview_mime_type=preview_mime_type, new_info=new_info,
        preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
        layout=layout, accept_all_hint=accept_all_hint,
        # show_read_popup is unconditionally the review-gate popup -- see
        # show_popup's own matching comment.
        is_read=True,
    )


def show_pii_confirmation_popup(categories: list[str]) -> bool:
    """Second-step confirmation shown when the PII detector flagged possible
    personal data in the content just approved.

    Defaults to Cancel, same rationale as show_rule_confirmation_popup:
    hitting Enter shouldn't silently let flagged content through -- see
    dialog_window.show_confirmation_dialog's own docstring for how that's
    enforced now.
    """
    cats = ", ".join(categories) if categories else "personal data"
    return show_confirmation_dialog(
        title="PrivacyFence — Possible PII Detected",
        message_lines=[
            f"PrivacyFence detected possible personal data in this content: {cats}.",
            "Are you sure you want to proceed?",
        ],
        cancel_label="Cancel",
        confirm_label="Proceed",
    )


def show_rule_choice_popup(descriptions: list[str]) -> int | None:
    """Chooser shown after "Always allow" is clicked when more than one rule
    could be created from the same item (see auto_accept.py's
    suggest_rule_choices()) -- e.g. a Drive file you own that also lives in
    an approved folder could become either an i_am_owner or an
    approved_folder rule. Returns the chosen index into ``descriptions``, or
    None if cancelled.

    Picking an option here doubles as the "yes, create this" confirmation --
    there's no separate confirm step afterward, unlike the single-candidate
    case (show_rule_confirmation_popup), since choosing from an explicit list
    is already as deliberate an action as clicking Confirm.
    """
    return show_choice_dialog(
        title="PrivacyFence — Choose Auto-Accept Rule",
        prompt="More than one rule could be created from this item — choose one:",
        options=descriptions,
    )


def show_rule_confirmation_popup(description: str) -> bool:
    """Second-step confirmation shown after "Always allow" is clicked.

    Defaults to Cancel — unlike the main gate, hitting Enter here shouldn't
    silently create a standing rule that skips future approvals.
    """
    return show_confirmation_dialog(
        title="PrivacyFence — Confirm Auto-Accept Rule",
        message_lines=[
            "PrivacyFence will create an auto-accept rule:",
            description,
            "Future matching requests will be approved automatically, without a popup.",
        ],
        cancel_label="Cancel",
        confirm_label="Confirm",
    )
