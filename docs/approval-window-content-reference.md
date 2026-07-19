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
| **Popup-gate window** | `approval_popup.show_popup` | `gate="popup"` tools — writes | Deny, Allow once, *Allow for 5 min* (conditional) |
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
| 3 | Preview summary box (the "WHAT") | `preview` dict is non-empty | no | no | Per-tool — each connector call site builds its own `preview` dict |
| 4 | "AI will receive" checklist | `visibility` dict passed | **yes** | – | Per-tool opt-in — only Gmail/Drive/Slack read tools pass this (see View groups below) |
| 5 | PII banner (red tint + sensitivity badges) | PII detector flagged the scanned content | **yes** | – | **Automatic** — `gate.py` runs `detect_pii_categories()` on every review-gate call's content, not opt-in per tool |
| 6 | Content-flag banner (amber, informational) | local PII-pattern detector flagged Claude's own drafted content | – | **yes** | **Automatic** — same detector run over every popup-gate call's `details_text` |
| 7 | "Claude says (unverified)" reason box | `claude_reason` non-empty | no | no | **Automatic** — every gated tool's schema requires a `reason` param (Phase 1b); self-reported, never verified |
| 8 | Details/"Preview" pane (reading-time estimate + Show more/less) | always | no | no | Body rendering varies — see `content_kind`/`pdf_bytes` below |
| 9 | Buttons | always | – | – | Always-allow offered only when `auto_accept.suggest_rule()` can derive a rule from this item; Allow-for-5-min only for the six tools in View WG-2 below |

Row 8's body defaults to plain escaped text in a WKWebView. Two read-only tools override that:

- **`content_kind="email"`** renders a structured From/To/Subject/Date header above the body
  instead of plain text. Only ever set by `gmail_get_message` — `gmail_get_thread` deliberately
  doesn't use it (a thread has several messages, each with its own sender, so a single header
  doesn't fit; it inlines per-message `From:`/`Date:` lines in the body text instead).
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

### RG-3 — Review popup + checklist + Gmail-style email header body

One tool: **`gmail_get_message`**. Preview: From, To, Date, Subject. Checklist: Sender & metadata,
Message body, Attachments. Body pane renders a structured From/To/Subject/Date header (built from
the same preview fields) above the message text, instead of plain text alone.

### RG-4 — Review popup + checklist + optional native PDFView body

One tool: **`drive_get_file_content`**. Preview: File, Owner, Size, Modified. Checklist: File
metadata, Document content. Body pane is plain text (first ~2000 chars) *unless* the file is an
unredacted, untruncated PDF, in which case it's a scrollable native PDF render instead (see row 8
above).

## View groups — popup-gate (write) tools

All popup-gate dialogs share one shape: summary box, no AI-visibility checklist (ever — see
`show_popup`'s docstring: a write is content Claude itself already drafted, there's nothing extra
to disclose), optional amber content-flag banner, optional "Claude says" box, plain-text details
pane. The only structural difference between write tools is the **button set**.

### WG-1 — Deny / Allow once (no time-boxed accept)

Every popup-gate tool *except* the six in WG-2 below — 38 tools across Gmail (create/reply drafts,
labels, archive, filters), Drive (write/upload/move/comment, sheets add/rename/delete-dimensions),
Slack, Calendar, Contacts, Telegram, Jira, Confluence, and Tasks. Preview fields are tool-specific
— see `TECHNICAL_REFERENCE.md`'s per-connector tables for the exact field list per tool, they're
not repeated here since there's no shared shape beyond "Deny / Allow once".

### WG-2 — Deny / Allow once / Allow for 5 min

The six operations in `auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS` — repeat calls against the
same file are common enough to warrant a narrower, memory-only 5-minute auto-accept instead of
either a full standing rule or re-approving every single call:

| Tool | Preview summary fields |
|---|---|
| `drive_sheets_write_range` | Spreadsheet, Owner, Range |
| `drive_sheets_format_range` | Spreadsheet, Owner, Range, Format (summary of applied formatting) |
| `drive_sheets_insert_dimensions` | Spreadsheet, Owner, Tab id, Action (e.g. "Insert 3 ROWS before index 5") |
| `drive_add_comment` | File, Owner |
| `drive_docs_edit_content` | File, Owner, Match ("every occurrence" / "the one matching occurrence") |
| `drive_docs_format_content` | File, Owner, Format (summary of applied formatting) |

"Allow for 5 min" auto-accepts further calls of the *same operation against the same file* for 5
minutes, in memory only — never written to `settings.yaml`, gone on daemon restart. See
`gate.py`'s module docstring for the full write-gate rationale.

## Cross-cutting: what's never in one but is in the other

- **AI-visibility checklist**: review-gate only, never on a write. A write already shows exactly
  what's being sent (Claude's own drafted content); there's no upstream filtering step to
  disclose.
- **PII banner (red)**: review-gate only. Triggers the PII confirmation second-step dialog.
- **Content-flag banner (amber)**: popup-gate only. Purely informational — never triggers a
  second-step confirmation, unlike the red PII banner.
- **Seen-count caption and "Claude says" reason box**: both, since both are computed centrally in
  `gate.py` for every gated call regardless of direction.
- **content_kind email header / native PDFView**: review-gate only, and only for the two specific
  Gmail/Drive tools named above — no write tool renders anything but plain text in the details
  pane.
