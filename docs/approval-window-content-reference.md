# Approval window content reference

What information each PrivacyFence approval dialog actually shows, and — since most dialogs share
the same layout with only a few optional sections toggled on or off — which tools produce an
**identical dialog shape** ("view"). This is a different cut than
[`TECHNICAL_REFERENCE.md`'s per-connector tool tables](TECHNICAL_REFERENCE.md#connectors--privacy-matrix),
which list exact preview/details text per tool grouped by connector; this doc groups by dialog
*shape* first, tool second, and adds the optional overlay sections (AI-visibility checklist,
sensitivity badges, Gmail/PDF body rendering, etc.) that table doesn't cover. Source of truth for
everything below: [`gate.py`](../src/privacyfence/gate.py),
[`approval_popup.py`](../src/privacyfence/approval_popup.py),
[`approval_window.py`](../src/privacyfence/approval_window.py) — re-derive from there if this
drifts, don't trust it blindly.

## The four dialogs

Every gated tool call resolves through exactly one of these:

| Dialog | Built by | Used for | Buttons |
|---|---|---|---|
| **Review-gate window** | `approval_popup.show_read_popup` | `gate="review"` tools — reads | Deny, Allow once, *Always allow* (conditional) |
| **Popup-gate window** | `approval_popup.show_popup` | `gate="popup"` tools — writes | Deny, Allow once, *Always allow* (conditional — WG-3 only, sixteen tools; see row 10 below), *temp-accept disclosure caption shown above the buttons for WG-2 instead, conditional — see row 9 below; no separate button for that* |
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
| 9 | Temp-accept disclosure caption (plain text, not a control) | `temp_accept_eligible` | – | **yes** | **Automatic** — `gate.py` sets it from `auto_accept.temp_accept_key()` resolving for the six WG-2 tools below. Not offered as a button: clicking Allow once on one of these silently also arms the 5-minute same-file grace window this caption describes — see WG-2 below |
| 10 | Buttons | always | – | – | Always-allow offered when `auto_accept.suggest_rule()` (review-gate) or `auto_accept.suggest_write_rule()` (popup-gate, WG-3 only — see [Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)) can derive a rule from this item. No separate button is ever offered for the temp-accept window (row 9) |

Row 8's body defaults to plain escaped text in a WKWebView. Two read-only tools override that:

- **`content_kind="email"`** renders a structured From/To/Subject/Date header above the body
  instead of plain text, and — since that header is built from the same `preview` dict the row-3
  summary box would otherwise render — suppresses the summary box for that call, so the same
  fields never appear twice on the dialog (`_show_summary_box()`). Only ever set by
  `gmail_get_message` — `gmail_get_thread` deliberately doesn't use it (a thread has several
  messages, each with its own sender, so a single header doesn't fit; it inlines per-message
  `From:`/`Date:` lines in the body text instead, and keeps its own summary box).
- **`pdf_bytes`** (non-empty) renders a native `PDFView` instead of the WKWebView body entirely.
  Only ever set by `drive_get_file_content`, and only when the file is a PDF, wasn't truncated by
  the fetch, and `category_policy(..., "file_content") == "allow"` — the reviewer must never see a
  richer rendering than what the "AI will receive" checklist already discloses for the same call.

## View groups — review-gate (read) tools

Grouped by which of rows 3/4/8 above actually render for that tool. Rows 2, 5, 7, 9 are automatic/
data-driven on every group (any of them can show the seen-count caption, the red PII banner, or
Claude's reason on a given call — that's about the *content* of a specific request, not the tool).

### RG-1 — Plain review popup (summary box only, no AI-visibility checklist)

No `visibility` passed — Confluence, Telegram, Salesforce, and part of Calendar/Jira never wired
into `privacy_filter.py`'s category-policy system, so there's no resolved policy to disclose here.

| Tool | Preview summary fields |
|---|---|
| `gmail_download_attachment` | From, Subject, Attachment name, Type, Size, Will save to |
| `drive_download_file` | File, Owner, Size, Modified, Saved to |
| `calendar_get_event_details` | Title, Time, Organizer, Attendees, *Attachments (if any)* |
| `jira_get_issue` | Project, Key, Summary (truncated 80 chars), Status, Assignee |
| `confluence_get_page` / `confluence_get_page_by_title` | Title, Space, Author, Last modified |
| `telegram_get_messages` | Chat, Messages (count) |
| `telegram_search_messages` | Query, Results (count) |
| `salesforce_get_record` | Object type, Name, Record ID |
| `salesforce_run_report` | Report, Report ID |
| `salesforce_search` | Search term, Object types, Results, *Account ID (if scoped)* |

### RG-2 — Review popup + "AI will receive" checklist, plain body

Same as RG-1 plus row 4: a checklist of `{label: allow/redact/block}` from
`privacy_filter.category_policy()`, one row per privacy category the connector defines.

| Tool | Preview summary fields | AI-visibility checklist rows |
|---|---|---|
| `gmail_get_thread` | Subject, Participants, Messages (count), Dates (range) | Sender & metadata, Thread messages, Attachments |
| `drive_sheets_get_values` | Spreadsheet, Owner, Range | Cell values |
| `slack_get_channel_history` | Channel, Messages (count), First message (80 chars) | Message text, Usernames |
| `slack_get_thread_replies` | Channel, Thread starter (80 chars), Replies (count) | Reply text, Usernames |
| `slack_search_messages` | Query, Results (count) | Message text, Usernames |

### RG-3 — Review popup + checklist + Gmail-style email header body (no summary box)

One tool: **`gmail_get_message`**. No summary box — its preview dict (From, To, Date, Subject) is
rendered once, as the structured header at the top of the details pane, instead of also appearing
as row-3 label/value pairs (see row 3/row 8 in the anatomy table above). Checklist: Sender &
metadata, Message body, Attachments.

### RG-4 — Review popup + checklist + optional native PDFView body

One tool: **`drive_get_file_content`**. Preview: File, Owner, Size, Modified. Checklist: File
metadata, Document content. Body pane is plain text (first ~2000 chars) *unless* the file is an
unredacted, untruncated PDF, in which case it's a scrollable native PDF render instead (see row 8
above).

## View groups — popup-gate (write) tools

All popup-gate dialogs share one shape: summary box, no AI-visibility checklist (ever — see
`show_popup`'s docstring: a write is content Claude itself already drafted, there's nothing extra
to disclose), optional amber content-flag banner, optional "Claude says" box, plain-text details
pane. Every popup-gate dialog also shares the same **button set** now — Deny, Allow once, that's
it. The only structural difference between write tools is whether the temp-accept disclosure
caption (row 9 in the anatomy table above) appears above those buttons.

### WG-1 — Deny / Allow once (never Always allow, not temp-accept eligible)

Every popup-gate tool *except* the six in WG-2 and the sixteen in WG-3 below — 22 tools. Preview
fields are tool-specific; `[brackets]` mark a field that's only added to the dict when the
corresponding argument was actually provided (empty/default arguments don't produce an empty row).

| Tool | Preview summary fields |
|---|---|
| `gmail_create_draft` | To, [Cc], [Bcc], Subject |
| `gmail_reply_draft` | In reply to, To, [Cc], [Bcc] |
| `gmail_reply_all_draft` | In reply to, To, Also to, [Cc], [Bcc] |
| `gmail_archive_message` | From, Subject |
| `gmail_create_filter` | Criteria, Actions |
| `gmail_update_filter` | Filter ID, Criteria, Actions |
| `gmail_create_label` | Label |
| `drive_write_doc_content` | File, Owner |
| `drive_upload_file` | File, Source, Size, Destination |
| `drive_write_file_content` | File, Owner |
| `drive_move_file` | File, Owner, Move to folder |
| `drive_sheets_add_sheet` | Spreadsheet, Owner, New tab, Size |
| `drive_sheets_rename_sheet` | Spreadsheet, Owner, Tab id, New title |
| `drive_sheets_delete_dimensions` | Spreadsheet, Owner, Tab id, Action (e.g. "Delete 2 COLUMNS starting at index 3") |
| `slack_send_message` | Channel, [In thread], [Mark unread] |
| `calendar_create_out_of_office` | Title, Time, Auto-decline |
| `calendar_set_working_location` | Date, Location, [Building], [Label] |
| `contacts_update` | Contact, [Name], [Emails], [Phones], [Organization], [Job title] |
| `contacts_create` | Name, [Emails], [Phones], [Organization], [Job title] |
| `contacts_add_label` | Contact, Label |
| `contacts_remove_label` | Contact, Label |
| `telegram_send_message` | Chat |

### WG-2 — Deny / Allow once, with the temp-accept disclosure caption

The six operations in `auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS` — repeat calls against the
same file are common enough to warrant a narrower, memory-only 5-minute auto-accept instead of
either a full standing rule or re-approving every single call. There used to be a separate "Allow
for 5 min" button for these; clicking Allow once on one of them now silently arms the same grace
window instead, and the dialog only discloses that with a plain caption above the buttons (row 9),
not a distinct control:

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
See `gate.py`'s module docstring for the full write-gate rationale.

### WG-3 — Deny / Allow once, conditionally Always allow

The sixteen tools across five operation keys in `auto_accept.WRITE_RULE_SUGGESTIONS` — the narrow,
deliberate exception to "writes never get Always allow" (see
[Always allow for writes](TECHNICAL_REFERENCE.md#always-allow-for-writes)). The button only renders
when `suggest_write_rule()` can actually derive a value from this call's own args (e.g. `jira_
create_issue` always can; `jira_add_comment` can't if `issue_key` has no `-` to parse a project out
of) — same "never propose a rule broader than what the item supports" contract `suggest_rule()`
already holds on the read side.

| Tool | Preview summary fields |
|---|---|
| `gmail_add_label` | From, Subject, Label |
| `gmail_remove_label` | From, Subject, Label |
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

Clicking Always allow here goes through the same second-confirmation dialog
(`show_rule_confirmation_popup`) and persistence path (`add_auto_accept_rule`) the review-gate's own
Always allow uses — described via `describe_rule_change()`, not `describe_rule()`, since these rule
names are shared with a read operation key too (e.g. `jira.read_issue`) and `describe_rule()`'s
canned templates are read-direction-only English.

## Cross-cutting: what's never in one but is in the other

- **AI-visibility checklist**: review-gate only, never on a write. A write already shows exactly
  what's being sent (Claude's own drafted content); there's no upstream filtering step to
  disclose.
- **PII banner (red) vs. content-flag banner (amber)**: both come from the same local detector
  (`pii_detector.py`'s `detect_pii_categories()`), but scan opposite directions and carry opposite
  weight. The red banner (review-gate only) scans content flowing **in** from an external source
  (`gate.py`'s `pii_categories`) and, on Allow/Always-allow, forces a second explicit
  `show_pii_confirmation_popup` before the decision is final — it's a trust-boundary gate, not
  just a label. The amber banner (popup-gate only) scans Claude's own drafted content going **out**
  (`gate.py`'s `write_content_flags`) and is purely informational: no full-window tint, no second
  dialog, the click resolves immediately regardless. There's no "personal data snuck in" scenario
  on a write — it's content Claude already described in chat — so it only ever gets a heads-up,
  never a gate.
- **Seen-count caption and "Claude says" reason box**: both, since both are computed centrally in
  `gate.py` for every gated call regardless of direction.
- **content_kind email header / native PDFView**: review-gate only, and only for the two specific
  Gmail/Drive tools named above — no write tool renders anything but plain text in the details
  pane. The email header is also the one case where row 3 (the summary box) is deliberately
  suppressed rather than shown alongside it — see row 3 in the anatomy table above.
