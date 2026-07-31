# Approval window content reference

What information each PrivacyFence approval dialog actually shows, and — since most dialogs share
the same layout with only a few optional sections toggled on or off — which tools produce an
**identical dialog shape** ("view"). This is a different cut than
[`TECHNICAL_REFERENCE.md`'s per-connector tool tables](TECHNICAL_REFERENCE.md#connectors--privacy-matrix),
which list exact preview/details text per tool grouped by connector; this doc groups by dialog
*shape* first, tool second, and adds the optional overlay sections (AI-visibility checklist,
sensitivity badges, PDF body rendering, etc.) that table doesn't cover. Neither this doc nor
that table says what Claude *already knew* before a given call, from prior auto-approved calls —
for that cut, see [`claude-knowledge-boundary.md`](claude-knowledge-boundary.md). Source of truth
for everything below: [`gate.py`](../src/privacyfence/gate.py),
[`approval_popup.py`](../src/privacyfence/approval_popup.py),
[`approval_window.py`](../src/privacyfence/approval_window.py),
[`approval_window_html.py`](../src/privacyfence/approval_window_html.py) — re-derive from there if
this drifts, don't trust it blindly.

## The four dialogs

Every gated tool call resolves through exactly one of these:

| Dialog | Built by | Used for | Buttons |
|---|---|---|---|
| **Review-gate window** | `approval_popup.show_read_popup` | `gate="review"` tools — reads | Deny, Allow once, *Always allow* (conditional) |
| **Popup-gate window** | `approval_popup.show_popup` | `gate="popup"` tools — writes | Deny, Allow once, *Always allow* (conditional — WG-2/WG-3 below, 35 tools; see row 9 below), *temp-accept disclosure caption shown above the buttons for WG-3 instead — conditional, see row 8 below; no separate button for that* |
| **PII confirmation** | `approval_popup.show_pii_confirmation_popup` | second-step check after Allow/Always-allow on a review-gate call whose content matched the PII detector | Cancel (default), Proceed |
| **Rule confirmation** | `approval_popup.show_rule_confirmation_popup` | second-step check after clicking Always allow | Cancel (default), Confirm |

The first two are the real estate this doc is about — a custom AppKit window
(`approval_window.py`) with one WKWebView (`approval_window_html.build_card_stack_html`) filling
the content area, native Deny/Allow once/Always allow buttons in a fixed band below it. The last
two are plain `osascript display dialog` prompts: one line of text, two buttons, no
preview/details sections at all, so there's nothing to group — see their docstrings in
`approval_popup.py` for exact wording.

## Anatomy of the main window, top to bottom

Both the review-gate and popup-gate windows are the *same* `ApprovalWindowController`, built from
the same section order (`approval_window.py`'s `_build_content_view`); what differs is which
optional sections a given call populates. In display order:

| # | Section | Appears when | Review-gate only? | Popup-gate only? | Per-tool opt-in, or automatic? |
|---|---|---|---|---|---|
| 1 | Kicker + fence icon + title | always | – | – | Kicker text/color differ by direction: "What Claude already knows" / "Why Claude needs more data" (review-gate) vs. "Action to perform" / "Why Claude is doing this" (popup-gate) — see `is_read` |
| 2 | "Seen N times this week" caption | `seen_count > 0` | no | no | **Automatic** — computed centrally in `gate.py` from the audit log for every call |
| 3 | §1 preview card (the "WHAT") | `preview` dict is non-empty | no | no | Per-tool — each connector call site builds its own `preview` dict |
| 4 | §2 "Claude says" reason card | `claude_reason` non-empty | no | no | **Automatic** — every gated tool's schema requires a `reason` param; self-reported, never verified |
| 5 | §3 disclosure card ("What will be provided to Claude") | `new_info` and/or `visibility` non-empty | **yes** | – | Per-tool — built from `new_info` (real per-tool values) first, then `visibility`-derived policy sentences appended if also set (`_disclosure_rows()`). Never present for a write — `show_popup` never sets either |
| 6 | §4 PII/content-flag risk card | PII detector flagged the scanned content | – | – | **Automatic** — `gate.py` runs `detect_pii_categories()` on every call's content (review-gate: `pii_categories`, forces a second confirmation; popup-gate: `write_content_flags`, informational only — see Cross-cutting below) |
| 7 | Right-hand preview pane (WIDE layout only; reading-time estimate) | `layout == "wide"` for this tool | no | no | Per-tool, fixed — see `_TOOL_LAYOUT` in `gate.py`/`scripts/qa_popup_smoke.py`. NARROW-layout tools have no preview pane at all |
| 8 | Temp-accept disclosure caption (plain text, not a control) | `temp_accept_eligible` | – | **yes** | **Automatic** — `gate.py` sets it from `auto_accept.temp_accept_key()` resolving for the six WG-3 tools below. Not offered as a button: clicking Allow once on one of these silently also arms the 5-minute same-file grace window this caption describes — see WG-3 below |
| 9 | Buttons | always | – | – | Always-allow offered when `auto_accept.suggest_rule()` (review-gate) or `auto_accept.suggest_write_rule()` (popup-gate, WG-2/WG-3 — see [Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)) can derive a rule from this item. The temp-accept caption (row 8) and an Always-allow button are independent and can both appear at once (WG-3). The button's own label names the specific rule it would create (e.g. "Always allow — this folder"), not a plain unspecific "Always allow" — see `gate.py`'s `accept_all_hint`/`auto_accept.describe_rule_short()`; empty for the one unconditional rule (`always_allow`, e.g. `gmail_create_draft`) with no category to name |

Row 7's right pane defaults to plain escaped text. Two tools override that:

- **`pdf_bytes`** (non-empty), on `drive_get_file_content` only, renders an inline `<embed>` PDF
  data URI instead of plain text. Only ever set when the file is a PDF, wasn't truncated by the
  fetch, and `category_policy(..., "file_content") == "allow"` — the reviewer must never see a
  richer rendering than what the "AI will receive" checklist already discloses for the same call.
- **`preview_bytes`/`preview_mime_type`** (image), on `gmail_download_attachment`/
  `drive_download_file`/`drive_upload_file`, render an inline `<img>` data URI instead. Carries no
  AI-visibility parity constraint (unlike `pdf_bytes`) — these three tools' content never reaches
  Claude at all, so there's nothing an "AI will receive" checklist could disclose to stay in parity
  with.

## View groups — review-gate (read) tools

Every review-gate tool renders through one of two dialog shapes, distinguished only by whether the
right pane holds a native PDF embed:

### RG-1 — Review popup

Every review-gate tool except `drive_get_file_content` (see RG-2 below). "AI-visibility checklist
rows" lists the `{label: allow/redact/block}` sentences from `privacy_filter.category_policy()` that
append to §3 when the connector passes a `visibility` dict — blank for tools that never resolve a
category policy for their own content (Telegram and Salesforce entirely, Calendar, Jira, and
Confluence's `get_page`/`get_page_by_title`). §3 still renders for those tools when `new_info` alone
is non-empty; a blank checklist column just means fewer §3 rows, not that the whole card disappears.

"Preview summary fields" below is strictly §1's `preview` dict — the identifying context shown
before any approval. It's deliberately the *slimmer* of the two field lists a connector builds:
fields the human needs to identify *which* item this is (a message's Subject, a record's Object
type) stay here, while everything a connector's own comments call out as "genuinely new" (a
thread's message count, a contact record's notes, a page's author) moves to the separate
`new_info` dict that only renders in §3 — never duplicated in this preview column. Don't confuse
"not in this column" with "never shown to the reviewer": every `new_info` field still renders,
directly below, in §3. For exactly which fields are new vs. already knowable from a prior
auto-approved call — the actual "AI knowledge boundary" question, as opposed to "what renders where
in the dialog" — see [`claude-knowledge-boundary.md`](claude-knowledge-boundary.md), not this table.

| Tool | Preview summary fields | AI-visibility checklist rows |
|---|---|---|
| `gmail_download_attachment` | From, Subject, Attachment, Type, Size | — |
| `drive_download_file` | File, Owner, Size, Modified | — |
| `calendar_get_event_details` | Title, Time | — |
| `jira_get_issue` | Project, Key, Summary (truncated 80 chars), Status, Assignee | — |
| `confluence_get_page` / `confluence_get_page_by_title` | Title, Space | — |
| `telegram_get_messages` | Chat | — |
| `telegram_search_messages` | Query | — |
| `salesforce_get_record` | Object type, Record ID | — |
| `salesforce_run_report` | Report, Report ID | — |
| `salesforce_search` | Search term, Object types, *[Account ID]* | — |
| `gmail_get_thread` | Subject, Participants, Dates | Thread messages, Attachments |
| `drive_sheets_get_values` | Spreadsheet, Owner, Range | Cell values |
| `slack_get_channel_history` | Channel | Message text, Usernames |
| `slack_get_thread_replies` | Channel | Reply text, Usernames |
| `slack_search_messages` | Query | Message text, Usernames |
| `gmail_get_message` | From, Date, Subject | Message body, Attachments |

### RG-2 — Review popup with native PDF body

One tool: **`drive_get_file_content`**. Preview: File, Owner, Size, Modified. Checklist: File
metadata, Document content. Right pane is plain text (first ~2000 chars) *unless* the file is an
unredacted, untruncated PDF, in which case it renders as an inline `<embed>` PDF data URI instead
(see row 7 above).

## View groups — popup-gate (write) tools

All popup-gate dialogs share one shape: §1 preview card, no AI-visibility checklist or §3
disclosure card (ever — see `show_popup`'s docstring: a write is content Claude itself already
drafted, there's nothing extra to disclose), optional amber content-flag card, optional §2
"Claude says" card, plain-text (or, for WIDE-layout tools, right-pane) details, Deny/Allow once
always available. Two things vary independently on top of that: whether **Always allow** renders
(conditional on `suggest_write_rule()` deriving a value — see
[Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)) and whether the
**temp-accept disclosure caption** (row 8) appears above the buttons. Every popup-gate tool falls
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
| `contacts_update` | Name, Emails, Phones (each: current value, or old → new if changing), [Organization], [Job title] |
| `contacts_create` | Name, [Emails], [Phones], [Organization], [Job title] |
| `contacts_add_label` | Name, Label |
| `contacts_remove_label` | Name, Label |
| `telegram_send_message` | Chat |

`calendar_create_out_of_office`/`calendar_set_working_location` support the same unconditional
`always_allow` rule `gmail_create_draft` does (see WG-2 below), but only from **Manage Auto-accept
Rules… → Calendar → Filters** — deliberately no popup-time shortcut, so they stay in this group
rather than WG-2.

### WG-2 — Deny / Allow once, conditionally Always allow

29 tools across `auto_accept.WRITE_RULE_SUGGESTIONS` — the narrow, deliberate exception to "writes
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
| `gmail_create_draft_with_attachments` | To, [Cc], [Bcc], Subject, Attachments (names/sizes) |
| `gmail_reply_draft_with_attachments` | In reply to, To, [Cc], [Bcc], Attachments (names/sizes) |
| `gmail_reply_all_draft_with_attachments` | In reply to, To, Also to, [Cc], [Bcc], Attachments (names/sizes) |
| `gmail_add_label` | From, Subject, Label |
| `gmail_remove_label` | From, Subject, Label |
| `drive_write_doc_content` | File, Owner |
| `drive_upload_file` | File, Source, Size, Folder |
| `drive_write_file_content` | File, Owner |
| `drive_move_file` | File, Owner, Folder (old → new — a move always changes it) |
| `drive_sheets_add_sheet` | Spreadsheet, Owner, New tab, Size |
| `drive_sheets_rename_sheet` | Spreadsheet, Owner, Tab title (old → new) |
| `drive_sheets_delete_dimensions` | Spreadsheet, Owner, Tab (current title), Action (e.g. "Delete 2 COLUMNS starting at index 3") |
| `calendar_create_event` | Title, Time, Calendar, [Location], [Conferencing], [Rooms], [Attendees] |
| `calendar_update_event` | Event, Calendar, Start, End (each: current value, or old → new if changing), + one row per other changed field (Description/Location/Conferencing/Rooms — only fields that actually changed appear) |
| `calendar_set_event_visibility` | Event, Calendar, Visibility (old → new) |
| `jira_create_issue` | Project, Type, Summary, [Priority] — the WIDE right pane also shows Description as a labeled block, when provided |
| `jira_add_comment` | Issue |
| `jira_update_issue` | Issue, + one row per changed field (Summary/Description/Priority/any custom fields — only fields actually being updated appear) |
| `jira_transition_issue` | Issue, Status (old → new) |
| `confluence_create_page` | Space, Title, [Parent page ID] |
| `confluence_update_page` | Page ID, Space, Title |
| `tasks_create_task` | Task list, Title, [Due] — the WIDE right pane also shows Notes as a labeled block, when provided |
| `tasks_update_task` | Task list, Task (current value, or old → new if changing), [Due (old → new)] — the WIDE right pane also shows Notes as a labeled block, when changing |
| `tasks_complete_task` | Task list, Task |
| `tasks_uncomplete_task` | Task list, Task |
| `tasks_move_task` | Task, List (old → new — a move always changes it) |

`gmail_create_draft`/`gmail_reply_draft`/`gmail_reply_all_draft` and their `_with_attachments`
counterparts propose the one unconditional entry in this whole group, `always_allow` (no recipient
check at all) — every other row proposes a rule scoped to the one
folder/label/calendar/project/space/task-list the call touched.

Clicking Always allow here goes through the same second-confirmation dialog
(`show_rule_confirmation_popup`) and persistence path (`add_auto_accept_rule`) the review-gate's own
Always allow uses — described via `describe_rule_change()`, not `describe_rule()`, since these rule
names are shared with a read operation key too (e.g. `jira.read_issue`) and `describe_rule()`'s
canned templates are read-direction-only English.

### WG-3 — Deny / Allow once, conditionally Always allow, *and* the temp-accept disclosure caption

The six operations in `auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS` — repeat calls against the
same file are common enough to warrant a narrower, memory-only 5-minute auto-accept instead of
either a full standing rule or re-approving every single call. Clicking Allow once on one of them
silently arms this grace window, and the dialog only discloses that with a plain caption above the
buttons (row 8), not a distinct control. All six are *also* in `WRITE_RULE_SUGGESTIONS` (they propose
`approved_sandbox_folder` from the file's current parent folder), so this is the one group where the
Always-allow button and the temp-accept caption can both be showing on the same popup at once:

| Tool | Preview summary fields |
|---|---|
| `drive_sheets_write_range` | Spreadsheet, Owner, Range |
| `drive_sheets_format_range` | Spreadsheet, Owner, Range, Format (summary of applied formatting) |
| `drive_sheets_insert_dimensions` | Spreadsheet, Owner, Tab (current title), Action (e.g. "Insert 3 ROWS before index 5") |
| `drive_add_comment` | File, Owner |
| `drive_docs_edit_content` | File, Owner, Match ("every occurrence" / "the one matching occurrence") |
| `drive_docs_format_content` | File, Owner, Format (summary of applied formatting) |

Allow once on one of these auto-accepts further calls of the *same operation against the same
file* for 5 minutes, in memory only — never written to `settings.yaml`, gone on daemon restart.
See `gate.py`'s module docstring for the full write-gate rationale. Clicking Always allow instead
skips the grace window entirely in favor of a standing rule, same confirmation flow as WG-2 above.

## Cross-cutting: what's never in one but is in the other

- **AI-visibility checklist / §3 disclosure card**: review-gate only, never on a write. A write
  already shows exactly what's being sent (Claude's own drafted content); there's no upstream
  filtering step to disclose.
- **§4's read-tinted card (red) vs. write-tinted card (amber)**: both come from the same local
  detector (`pii_detector.py`'s `detect_pii_categories()`), but scan opposite directions and carry
  opposite weight. The read-tinted styling itself is still strictly review-gate only:
  `show_popup`/`approval_window.py`'s controller never receives a `pii_categories` value on the
  popup-gate path, only `write_content_flags` (the amber styling), regardless of tool. What *is*
  shared with one write tool is the underlying **gate behavior** the red tint is a visual cue for,
  not the card's rendering: on Allow/Always-allow, the review-gate's `pii_categories` forces a
  second explicit `show_pii_confirmation_popup` before the decision is final — a trust-boundary
  gate, not just a label — and `drive_upload_file` is the one popup-gate tool whose own scan
  (`gate.py`'s `upload_pii_categories`) triggers that same second dialog and audit-log
  `pii_detected` field. Its first popup's card *does* look different for this one tool: `gate.py`
  passes `upload_forced=True` (derived from `upload_pii_categories` being non-empty) through to
  `show_popup`/`show_native_approval`, which renders the "write-forced" card variant — an interim
  placeholder reusing the read-gate's own red-tinted styling, since no distinct design exists yet
  for this narrow case (see `approval_window_html.py`'s `_risk_section_html` docstring). Every
  other popup-gate tool's amber card stays purely informational end to end: no second dialog, the
  click resolves immediately regardless — there's no "personal data snuck in" scenario for content
  Claude already described in chat. See
  [`TECHNICAL_REFERENCE.md`'s PII detection gate section](TECHNICAL_REFERENCE.md#pii-detection-gate)
  for the full reasoning behind `drive_upload_file`'s exception.
- **Seen-count caption and §2 "Claude says" card**: both, since both are computed centrally in
  `gate.py` for every gated call regardless of direction.
- **Native PDF right pane**: review-gate only, and only for `drive_get_file_content` (RG-2 above)
  — no write tool renders anything but plain text (or, for WIDE-layout tools, its own body content)
  in the right pane.
