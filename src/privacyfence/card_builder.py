"""Pure (no AppKit/PyObjC) translation from gate.py's show_popup/
show_read_popup argument shapes into approval_window_html.
build_card_stack_html()'s own argument shape.

This mirrors what approval_window.py's ApprovalWindowController does for
the native host (reading-time estimate, the "Seen N times this week"
caption, §3's disclosure rows, each accept_all candidate's "Always allow —
{hint}" label, the pdf/image data-URI precedence for the WIDE preview pane)
-- ported here framework-free, with the native-only window-sizing math left
out, so web_approval_ui.py can render an identical card without importing
anything AppKit-only. See that controller's own docstring for the
field-by-field reasoning this mirrors; keep the two in sync if either one's
translation logic changes.
"""
from __future__ import annotations

import base64

from . import approval_icons, approval_window_html

# Shown above the button row for operations
# auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS lists -- same copy as
# approval_window.py's own _TEMP_ACCEPT_DISCLOSURE_TEXT, deliberately kept
# as a second literal rather than a shared import: it's UI copy, not logic,
# and the two hosts are allowed to drift here without either being "wrong".
TEMP_ACCEPT_DISCLOSURE_TEXT = (
    "Approving this also allows further calls like this to the same file "
    "for a few minutes without asking again."
)


def _estimate_reading_seconds(text: str) -> int:
    """~200 words/minute silent-reading estimate, floored at 1 second so an
    empty/tiny body still renders a sane label rather than "~0 sec read"."""
    words = len(text.split())
    return max(1, round(words / 200 * 60))


def _reading_time_label(text: str) -> str:
    seconds = _estimate_reading_seconds(text)
    if seconds < 60:
        return f"~{seconds} sec read"
    return f"~{round(seconds / 60)} min read"


def _seen_count_text(seen_count: int) -> str:
    return f"Seen {seen_count} time{'s' if seen_count != 1 else ''} this week"


def _disclosure_rows(
    is_read: bool, new_info: dict[str, str] | None, visibility: dict[str, str] | None,
) -> list[tuple[str, str]]:
    """§3's rows -- see ApprovalWindowController._disclosure_rows's own
    docstring for the same (new_info first, then visibility-derived policy
    sentences) merge this mirrors."""
    if not is_read:
        return []
    rows = list((new_info or {}).items())
    if visibility:
        rows += approval_window_html.disclosure_rows_from_visibility(visibility)
    return rows


def build_card_html(
    *,
    title: str,
    preview: dict[str, str],
    details_text: str,
    is_read: bool,
    layout: str,
    accept_all_choices: list[tuple[str, str]] | None = None,
    pii_categories: list[str] | None = None,
    visibility: dict[str, str] | None = None,
    claude_reason: str = "",
    write_content_flags: list[str] | None = None,
    seen_count: int = 0,
    pdf_bytes: bytes = b"",
    connector: str = "",
    preview_bytes: bytes = b"",
    preview_mime_type: str = "",
    new_info: dict[str, str] | None = None,
    preview_tables: list[dict] | None = None,
    preview_blocks: list[dict] | None = None,
    table_only: bool = False,
    upload_forced: bool = False,
    temp_accept_eligible: bool = False,
) -> str:
    """Build the full card-stack HTML document for one approval -- the web
    host's counterpart to ApprovalWindowController._build_content_view,
    minus that method's native-only window-sizing math (nothing here needs
    to guess a window height; the browser lays out a real page).

    ``content_kind`` is accepted by show_read_popup's own signature but
    (like the native host) has no effect on rendering -- see that
    function's docstring -- so it's deliberately not a parameter here.
    """
    pdf_data_uri = ""
    if pdf_bytes:
        pdf_data_uri = f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode('ascii')}"
    image_data_uri = ""
    if not pdf_data_uri and preview_bytes and preview_mime_type.startswith("image/"):
        image_data_uri = (
            f"data:{preview_mime_type};base64,{base64.b64encode(preview_bytes).decode('ascii')}"
        )

    # table_only suppresses details_text only when there's a real table to
    # show instead -- and never when preview_blocks is set, which already
    # controls exactly what renders on its own. Same rule as the native
    # host's own _build_content_view.
    body_text = "" if table_only and preview_tables and not preview_blocks else details_text
    preview_body_html = approval_window_html.build_preview_body_html(
        body_text, image_data_uri=image_data_uri, pdf_data_uri=pdf_data_uri,
        tables=preview_tables, blocks=preview_blocks,
    )

    accept_all_labels = [
        f"Always allow — {hint}" if hint else "Always allow"
        for _rule_name, hint in (accept_all_choices or [])
    ]

    return approval_window_html.build_card_stack_html(
        layout=layout,
        title=title,
        connector_icon_data_uri=approval_icons.icon_data_uri(approval_icons.connector_icon_path(connector)),
        shield_icon_data_uri=approval_icons.icon_data_uri(approval_icons.shield_icon_path()),
        is_read=is_read,
        seen_count_text=_seen_count_text(seen_count) if seen_count > 0 else "",
        preview=preview or {},
        claude_reason=claude_reason or "",
        disclosure_rows=_disclosure_rows(is_read, new_info, visibility),
        pii_categories=pii_categories or [],
        write_content_flags=write_content_flags or [],
        upload_forced=upload_forced,
        temp_accept_text=TEMP_ACCEPT_DISCLOSURE_TEXT if temp_accept_eligible else "",
        preview_kicker=f"Preview ({_reading_time_label(details_text)})",
        preview_body_html=preview_body_html,
        accept_all_labels=accept_all_labels,
    )
