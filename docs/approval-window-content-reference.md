# Approval window content reference

What information each PrivacyFence approval dialog actually shows, and — since most dialogs share
the same layout with only a few optional sections toggled on or off — which tools produce an
**identical dialog shape** ("view"). This is a different cut than
[`TECHNICAL_REFERENCE.md`'s per-connector tool tables](TECHNICAL_REFERENCE.md#connectors--privacy-matrix),
which list exact preview/details text per tool grouped by connector; this doc groups by dialog
*shape* first, tool second, and adds the optional overlay sections (AI-visibility checklist,
sensitivity badges, Gmail/PDF body rendering, etc.) that table doesn't cover. Neither this doc nor
that table says what Claude *already knew* before a given call, from prior auto-approved calls —
for that cut, see [`claude-knowledge-boundary.md`](claude-knowledge-boundary.md). Source of truth
for everything below: [`gate.py`](../src/privacyfence/gate.py),
[`approval_popup.py`](../src/privacyfence/approval_popup.py),
[`approval_window.py`](../src/privacyfence/approval_window.py) — re-derive from there if this
drifts, don't trust it blindly.

## The four dialogs

Every gated tool call resolves through exactly one of these:

| Dialog | Built by | Used for | Buttons |
|---|---|---|---|
| **Review-gate window** | `approval_popup.show_read_popup` | `gate="review"` tools — reads | Deny, Allow once, *Always allow* (conditional) |
| **Popup-gate window** | `approval_popup.show_popup` | `gate="popup"` tools — writes | Deny, Allow once, *Always allow* (conditional — WG-2/WG-3 below, 32 tools; see row 10 below), *temp-accept disclosure caption shown above the buttons for WG-3 instead — conditional, see row 9 below; no separate button for that* |
| **PII confirmation** | `approval_popup.show_pii_confirmation_popup` | second-step check after Allow/Always-allow on a review-gate call whose content matched the PII detector | Cancel (default), Proceed |
| **Rule confirmation** | `approval_popup.show_rule_confirmation_popup` | second-step check after clicking Always allow | Cancel (default), Confirm |

The first two are the real estate this doc is about — a custom AppKit window
(`approval_window.py`) with a WKWebView body. The last two are plain `osascript display dialog`
prompts: one line of text, two buttons, no preview/details sections at all, so there's nothing to
group — see their docstrings in `approval_popup.py` for exact wording.

## Anatomy of the main window, top to bottom

Both the review-gate and popup-gate windows are the *same* `ApprovalWindowController`, built from
the same section order (`approval_window.py`'s `_compute_layout`/`_build_content_view`); what
differs is which optional sections a given call populates. In display order:

| # | Section | Appears when | Review-gate only? | Popup-gate only? | Per-tool opt-in, or automatic? |
|---|---|---|---|---|---|
| 1 | Kicker + fence icon + title | always | – | – | – |
| 2 | "Seen N times this week" caption | `seen_count > 0` | no | no | **Automatic** — computed centrally in `gate.py` from the audit log for every call |
| 3 | Preview summary box (the "WHAT") | `preview` dict is non-empty *and* `content_kind != "email"` | no | no | Per-tool — each connector call site builds its own `preview` dict. Suppressed for `content_kind="email"` (`_show_summary_box()`) — see row 8: the email header already renders those same fields, so showing both would put them on screen twice |
| 4 | "AI will receive" checklist | `visibility` dict passed | **yes** | – | Per-tool opt-in — only Gmail/Drive/Slack read tools pass this (see View groups below) |
| 5 | PII banner (red tint + sensitivity badges) | PII detector flagged the scanned content | **yes** | – | **Automatic** — `gate.py` runs `detect_pii_categories()` on every review-gate call's content, not opt-in per tool |
| 6 | Content-flag banner (amber, informational) | local PII-pattern detector flagged Claude's own drafted content | – | **yes** | **Automatic** — same detector run over every popup-gate call's `details_text` |
| 7 | "Claude says (unverified)" reason box | `claude_reason` non-empty | no | no | **Automatic** — every gated tool's schema requires a `reason` param (Phase 1b); self-reported, never verified |
| 8 | Details/"Preview" pane (reading-time estimate + Show more/less) | always | no | no | Body rendering varies — see `content_kind`/`pdf_bytes` below |
| 9 | Temp-accept disclosure caption (plain text, not a control) | `temp_accept_eligible` | – | **yes** | **Automatic** — `gate.py` sets it from `auto_accept.temp_accept_key()` resolving for the six WG-3 tools below. Not offered as a button: clicking Allow once on one of these silently also arms the 5-minute same-file grace window this caption describes — see WG-3 below |
| 10 | Buttons | always | – | – | Always-allow offered when `auto_accept.suggest_rule()` (review-gate) or `auto_accept.suggest_write_rule()` (popup-gate, WG-2/WG-3 — see [Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)) can derive a rule from this item. The temp-accept caption (row 9) and an Always-allow button are independent and can both appear at once (WG-3) |

Row 8's body defaults to plain escaped text in a WKWebView. Two read-only tools override that:

- **`content_kind="email"`**, on **legacy only**, renders a structured From/To/Subject/Date header
  above the body instead of plain text, and — since that header is built from the same `preview`
  dict the row-3 summary box would otherwise render — suppresses the summary box for that call, so
  the same fields never appear twice on the dialog (`_show_summary_box()`). Only ever set by
  `gmail_get_message` — `gmail_get_thread` deliberately doesn't use it (a thread has several
  messages, each with its own sender, so a single header doesn't fit; it inlines per-message
  `From:`/`Date:` lines in the body text instead, and keeps its own summary box). **v2 ignores
  `content_kind` entirely** — `gmail_get_message` gets an ordinary row-3 card there, no header, no
  suppression (see the "View groups" section below).
- **`pdf_bytes`** (non-empty) renders a native `PDFView` instead of the WKWebView body entirely.
  Only ever set by `drive_get_file_content`, and only when the file is a PDF, wasn't truncated by
  the fetch, and `category_policy(..., "file_content") == "allow"` — the reviewer must never see a
  richer rendering than what the "AI will receive" checklist already discloses for the same call.

## View groups — review-gate (read) tools

`approval_window.py` renders every review-gate call through one of two `layout` variants:
**legacy** (the original hand-laid-out `NSTextField`/`NSBox` stack, still the default today) and
**v2** (the redesigned card-stack `WKWebView` template, `approval_window_html.py` — opt in today via
`show_native_approval(layout="v2")`/`qa_popup_smoke.py --layout v2`, pending cutover per the redesign
project). The two variants disagree about how many distinct dialog *shapes* exist:

- **Legacy** still has three real shapes among the tools below, driven by whether row 4 (the "AI
  will receive" checklist, a separate box) renders at all, and whether `content_kind="email"`
  suppresses row 3's summary box in favor of a structured header. That's the RG-1/RG-2/RG-3 split
  this section used to document as three groups.
- **v2** collapses all three: its §3 ("What will be provided to Claude") is built from `new_info`
  first and the checklist policy sentences *appended* to that same list only if `visibility` is set
  (see `approval_window.py`'s `_v2_disclosure_rows()`) — so §3 renders for every tool below
  regardless of whether it carries a checklist, not just the ones that used to get their own row-4
  box. And v2 has no `content_kind="email"` special case at all (`gmail_get_message` gets an
  ordinary §1 card there, same as everything else) — see `build_preview_body_html()`'s docstring.
  The one dialog-shape difference that survives into v2 is body-content-type, not checklist
  presence: whether the right pane renders a native PDF instead of text (formerly RG-4, now RG-2
  below).

This section documents **v2's** two remaining shapes, since v2 is what the current redesign work
targets. Legacy's extra RG-2/RG-3 distinction (checklist box present, and `gmail_get_message`'s
suppressed summary box + email header) still applies unchanged on the legacy path today — noted
inline below, not as separate groups, since it goes away entirely at cutover.

### RG-1 — Review popup

Every review-gate tool except `drive_get_file_content` (see RG-2 below). "AI-visibility checklist
rows" lists the `{label: allow/redact/block}` sentences from `privacy_filter.category_policy()` that
append to §3 when the connector passes a `visibility` dict — blank for tools that never resolve a
category policy for their own content (Telegram and Salesforce entirely, Calendar, Jira, and
Confluence's `get_page`/`get_page_by_title`). On **legacy**, a blank checklist column means no row-4
box renders at all for that tool (the old RG-1 group); a non-blank one means it does (the old RG-2
group) — on **v2**, §3 renders either way, just with fewer rows when blank.

| Tool | Preview summary fields | AI-visibility checklist rows |
|---|---|---|
| `gmail_download_attachment` | From, Subject, Attachment name, Type, Size, Will save to | — |
| `drive_download_file` | File, Owner, Size, Modified, Saved to | — |
| `calendar_get_event_details` | Title, Time, Organizer, Attendees, *Attachments (if any)* | — |
| `jira_get_issue` | Project, Key, Summary (truncated 80 chars), Status, Assignee | — |
| `confluence_get_page` / `confluence_get_page_by_title` | Title, Space, Author, Last modified | — |
| `telegram_get_messages` | Chat, Messages (count) | — |
| `telegram_search_messages` | Query, Results (count) | — |
| `salesforce_get_record` | Object type, Name, Record ID | — |
| `salesforce_run_report` | Report, Report ID | — |
| `salesforce_search` | Search term, Object types, Results, *Account ID (if scoped)* | — |
| `gmail_get_thread` | Subject, Participants, Messages (count), Dates (range) | Thread messages, Attachments |
| `drive_sheets_get_values` | Spreadsheet, Owner, Range | Cell values |
| `slack_get_channel_history` | Channel, Messages (count) | Message text, Usernames |
| `slack_get_thread_replies` | Channel, Replies (count) | Reply text, Usernames |
| `slack_search_messages` | Query, Results (count) | Message text, Usernames |
| `gmail_get_message` | From, Date, Subject | Message body, Attachments |

`gmail_get_message` is the one row worth a callout: on **legacy only**, its preview dict (From, To,
Date, Subject) renders as a structured header above the body instead of in a row-3 summary box,
which is suppressed for that call (`_show_summary_box()`, `content_kind="email"`). On **v2** it's an
ordinary row like every other tool above — no header, no suppression, To/Labels land in §3 like any
other tool's `new_info`.

### RG-2 — Review popup with native PDF body

One tool: **`drive_get_file_content`**. Preview: File, Owner, Size, Modified. Checklist: File
metadata, Document content. Body pane is plain text (first ~2000 chars) *unless* the file is an
unredacted, untruncated PDF, in which case it's a scrollable native PDF render instead (a native
`PDFView` on legacy; an inline `<embed>` data URI on v2 — see row 8 above and
`_build_content_view_v2()`).

## View groups — popup-gate (write) tools

All popup-gate dialogs share one shape: summary box, no AI-visibility checklist (ever — see
`show_popup`'s docstring: a write is content Claude itself already drafted, there's nothing extra
to disclose), optional amber content-flag banner, optional "Claude says" box, plain-text details
pane, Deny/Allow once always available. Two things vary independently on top of that: whether
**Always allow** renders (conditional on `suggest_write_rule()` deriving a value — see
[Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)) and whether the
**temp-accept disclosure caption** (row 9) appears above the buttons. Every popup-gate tool falls
into exactly one of the three groups below — WG-3 is the one group where both can be true at once.

### WG-1 — Deny / Allow once only (never Always allow, not temp-accept eligible)

12 tools. Preview fields are tool-specific; `[brackets]` mark a field that's only added to the dict
when the corresponding argument was actually provided (empty/default arguments don't produce an
empty row).

| Tool | Preview summary fields |
|---|---|
| `gmail_archive_message` | From, Subject |
| `gmail_create_filter` | Criteria, Actions |
| `gmail_update_filter` | Filter ID, Criteria, Actions |
| `gmail_create_label` | Label |
| `slack_send_message` | Channel, [In thread], [Mark unread] |
| `calendar_create_out_of_office` | Title, Time, Auto-decline |
| `calendar_set_working_location` | Date, Location, [Building], [Label] |
| `contacts_update` | Contact, [Name], [Emails], [Phones], [Organization], [Job title] |
| `contacts_create` | Name, [Emails], [Phones], [Organization], [Job title] |
| `contacts_add_label` | Contact, Label |
| `contacts_remove_label` | Contact, Label |
| `telegram_send_message` | Chat |

`calendar_create_out_of_office`/`calendar_set_working_location` support the same unconditional
`always_allow` rule `gmail_create_draft` does (see WG-2 below), but only from **Manage Auto-accept
Rules… → Calendar → Filters** — deliberately no popup-time shortcut, so they stay in this group
rather than WG-2.

### WG-2 — Deny / Allow once, conditionally Always allow

26 tools across `auto_accept.WRITE_RULE_SUGGESTIONS` — the narrow, deliberate exception to "writes
never get Always allow" (see [Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)).
The button only renders when `suggest_write_rule()` can actually derive a value from this call's own
args or current state (e.g. `jira_create_issue` always can; `jira_add_comment` can't if `issue_key`
has no `-` to parse a project out of; a Drive write can't if the file has no parent folder) — same
"never propose a rule broader than what the item supports" contract `suggest_rule()` already holds
on the read side.

| Tool | Preview summary fields |
|---|---|
| `gmail_create_draft` | To, [Cc], [Bcc], Subject |
| `gmail_reply_draft` | In reply to, To, [Cc], [Bcc] |
| `gmail_reply_all_draft` | In reply to, To, Also to, [Cc], [Bcc] |
| `gmail_add_label` | From, Subject, Label |
| `gmail_remove_label` | From, Subject, Label |
| `drive_write_doc_content` | File, Owner |
| `drive_upload_file` | File, Source, Size, Destination |
| `drive_write_file_content` | File, Owner |
| `drive_move_file` | File, Owner, Move to folder |
| `drive_sheets_add_sheet` | Spreadsheet, Owner, New tab, Size |
| `drive_sheets_rename_sheet` | Spreadsheet, Owner, Tab id, New title |
| `drive_sheets_delete_dimensions` | Spreadsheet, Owner, Tab id, Action (e.g. "Delete 2 COLUMNS starting at index 3") |
| `calendar_create_event` | Title, Time, Calendar, [Location], [Conferencing], [Rooms], [Attendees] |
| `calendar_update_event` | Event, Calendar, + one row per changed field (Title/Start/End/Description/Location/Conferencing/Rooms — only fields that actually changed appear) |
| `calendar_set_event_visibility` | Event, Calendar, Visibility (old → new) |
| `jira_create_issue` | Project, Type, Summary, [Priority] |
| `jira_add_comment` | Issue |
| `jira_update_issue` | Issue, + one row per changed field (Summary/Description/Priority/any custom fields — only fields actually being updated appear) |
| `jira_transition_issue` | Issue, Status (old → new) |
| `confluence_create_page` | Space, Title, [Parent page ID] |
| `confluence_update_page` | Page ID, Space, Title |
| `tasks_create_task` | Task list, Title, [Due] |
| `tasks_update_task` | Task list, Task, [New title], [New due] |
| `tasks_complete_task` | Task list, Task |
| `tasks_uncomplete_task` | Task list, Task |
| `tasks_move_task` | Task, From list, To list |

`gmail_create_draft`/`gmail_reply_draft`/`gmail_reply_all_draft` propose the one unconditional
entry in this whole group, `always_allow` (no recipient check at all) — every other row proposes a
rule scoped to the one folder/label/calendar/project/space/task-list the call touched.

Clicking Always allow here goes through the same second-confirmation dialog
(`show_rule_confirmation_popup`) and persistence path (`add_auto_accept_rule`) the review-gate's own
Always allow uses — described via `describe_rule_change()`, not `describe_rule()`, since these rule
names are shared with a read operation key too (e.g. `jira.read_issue`) and `describe_rule()`'s
canned templates are read-direction-only English.

### WG-3 — Deny / Allow once, conditionally Always allow, *and* the temp-accept disclosure caption

The six operations in `auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS` — repeat calls against the
same file are common enough to warrant a narrower, memory-only 5-minute auto-accept instead of
either a full standing rule or re-approving every single call. There used to be a separate "Allow
for 5 min" button for these; clicking Allow once on one of them now silently arms the same grace
window instead, and the dialog only discloses that with a plain caption above the buttons (row 9),
not a distinct control. All six are *also* in `WRITE_RULE_SUGGESTIONS` (they propose
`approved_sandbox_folder` from the file's current parent folder), so this is the one group where the
Always-allow button and the temp-accept caption can both be showing on the same popup at once:

| Tool | Preview summary fields |
|---|---|
| `drive_sheets_write_range` | Spreadsheet, Owner, Range |
| `drive_sheets_format_range` | Spreadsheet, Owner, Range, Format (summary of applied formatting) |
| `drive_sheets_insert_dimensions` | Spreadsheet, Owner, Tab id, Action (e.g. "Insert 3 ROWS before index 5") |
| `drive_add_comment` | File, Owner |
| `drive_docs_edit_content` | File, Owner, Match ("every occurrence" / "the one matching occurrence") |
| `drive_docs_format_content` | File, Owner, Format (summary of applied formatting) |

Allow once on one of these auto-accepts further calls of the *same operation against the same
file* for 5 minutes, in memory only — never written to `settings.yaml`, gone on daemon restart.
See `gate.py`'s module docstring for the full write-gate rationale. Clicking Always allow instead
skips the grace window entirely in favor of a standing rule, same confirmation flow as WG-2 above.

## Cross-cutting: what's never in one but is in the other

- **AI-visibility checklist**: review-gate only, never on a write. A write already shows exactly
  what's being sent (Claude's own drafted content); there's no upstream filtering step to
  disclose.
- **PII banner (red) vs. content-flag banner (amber)**: both come from the same local detector
  (`pii_detector.py`'s `detect_pii_categories()`), but scan opposite directions and carry opposite
  weight. The red banner itself (row 5 in the anatomy table — the visual tint on the *first*
  dialog) is still strictly review-gate only: `show_popup`/`approval_window.py`'s controller never
  receives a `pii_categories` value on the popup-gate path, only `write_content_flags` (the amber
  banner), regardless of tool. What *is* shared with one write tool is the underlying **gate
  behavior** the red banner is a visual cue for, not the banner's rendering: on Allow/Always-allow,
  the review-gate's `pii_categories` forces a second explicit `show_pii_confirmation_popup` before
  the decision is final — a trust-boundary gate, not just a label — and `drive_upload_file` is the
  one popup-gate tool whose own scan (`gate.py`'s `upload_pii_categories`) triggers that same
  second dialog and audit-log `pii_detected` field, without changing what the first popup itself
  shows (still just the ordinary amber banner, informational only, from `write_content_flags`).
  Every other popup-gate tool's amber banner stays purely informational end to end: no tint beyond
  it, no second dialog, the click resolves immediately regardless — there's no "personal data
  snuck in" scenario for content Claude already described in chat. See
  [`TECHNICAL_REFERENCE.md`'s PII detection gate section](TECHNICAL_REFERENCE.md#pii-detection-gate)
  for the full reasoning behind `drive_upload_file`'s exception.
- **Seen-count caption and "Claude says" reason box**: both, since both are computed centrally in
  `gate.py` for every gated call regardless of direction.
- **content_kind email header / native PDFView**: review-gate only, and only for the two specific
  Gmail/Drive tools named above — no write tool renders anything but plain text in the details
  pane. The email header (legacy only, see row 8 above) is also the one case where row 3 (the
  summary box) is deliberately suppressed rather than shown alongside it — see row 3 in the anatomy
  table above. The native-PDF body, unlike the email header, is not legacy-only — it's RG-2 in the
  "View groups" section below, on both layouts.
