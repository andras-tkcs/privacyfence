# Popup formatting catalog

Working document for reviewing/correcting how approval popups present data. Every gated tool call
ends up in one of these popups — some are curated and readable, some leak raw IDs or JSON. This
doc lists what each popup currently shows so you can mark corrections against your screenshots
without having to read the connector code yourself.

Not something to keep in sync forever — once the fixes below are triaged and applied, this file
has done its job and can be deleted or left stale.

## 1. Anatomy of a popup

Every popup ([`approval_window.py`](../src/privacyfence/approval_window.py)) has three content
regions, fed by three arguments every connector call site builds and passes into `gated_call()`
([`gate.py`](../src/privacyfence/gate.py)):

| Region | Source argument | Purpose |
|---|---|---|
| **Title** (bold, top) | `f"PrivacyFence — {tool_name}"` | Human tool name, e.g. "Read Email", "Send Slack Message" |
| **Summary box** (key/value rows) | `preview: dict[str, str]` | 2–6 scannable facts: who/what/where, no prose |
| **Details pane** (scrollable body) | `details_text: str` | The actual content being read/sent/created, or a longer explanation |

A PII warning banner is layered on top when the detector flags content — that part is generic
and not per-connector, out of scope for this doc.

**Important mechanic:** if a call site passes `details_text=""`, `gate.py` does **not** leave the
details pane empty — it falls back to:

```python
def _default_details(raw_data: Any) -> str:
    if hasattr(raw_data, "__dict__"):
        return json.dumps(raw_data.__dict__, default=str, indent=2, ensure_ascii=False)
    return json.dumps(raw_data, default=str, indent=2, ensure_ascii=False)
```

(`gate.py:220`). This is the single biggest source of "technical" popups — several call sites
pass `details_text=""` intending "nothing more to show," and unintentionally get a raw JSON/repr
dump of whatever internal object happened to be passed as `raw_data` (which was chosen for
audit-log purposes, not for display). See §3.1.

## 2. What the well-formatted popups do (the pattern to match)

Looking at the popups that read cleanly (Gmail, Confluence, Contacts, Jira, most of Drive), the
shared conventions are:

- **Preview values are names, never IDs.** Before building `preview`, the connector fetches the
  parent object (message/page/contact/issue) and uses its resolved title/subject/display name —
  the raw `message_id` / `page_id` / `resource_name` never appears as a *value* (it's fine as an
  internal `args` field for the audit log, just not user-facing).
- **Preview is short** — 2 to 5 rows, each a single fact (`From`, `Subject`, `Date`), not
  paragraphs.
- **Details is prose**, built as an f-string of labeled lines followed by the actual body/content,
  e.g. `f"From: {sender}\nSubject: {subject}\n\n{body}"` — never a data-structure dump.
- **Missing data gets a placeholder**, consistently `"(unknown)"` / `"(none)"` / `"(no subject)"`
  / `"(no body)"`, never a blank row or `None`.
- **Long values are truncated** with `…` at the point where they'd otherwise dominate the popup
  (e.g. Jira issue summary at 80 chars).
- **Details text explains consequences in plain language** for actions with no natural "body"
  (e.g. Gmail archive: *"The message will remain in All Mail and is not deleted."*) rather than
  being left empty.

## 3. Known problems, ranked

### 3.1 Empty `details_text` silently becomes a raw dump (root cause, affects many)

Every call site below passes `details_text=""`. Because of the `gate.py` fallback in §1, the user
does **not** see a blank details pane — they see `json.dumps(...)` of whichever object was handed
in as `raw_data`, which is an internal/audit-log value, not something authored for display.

| Connector | Tool | `raw_data` passed | What actually renders |
|---|---|---|---|
| Gmail | `gmail_add_label` / `gmail_remove_label` | the full `message` object | JSON dump of every field on the Gmail message model |
| Contacts | `contacts_add_label` / `contacts_remove_label` | `{resource_name, label_name}` dict | JSON dump of those two raw fields |
| Tasks | `tasks_complete_task` / `tasks_uncomplete_task` / `tasks_move_task` | the full `existing` Task object | JSON dump of every field on the Task model |
| Drive | `drive_move_file` | `{"file": drive_file, "destination_folder_id": ...}` | JSON dump where `drive_file` isn't serializable, so it degrades to `default=str` — likely an ugly Python repr, not the file's fields |
| Gmail | `gmail_create_filter` | the `preview` dict itself | Happens to be readable (it's already flat key/value strings) but is redundant with the summary box above it |

**Fix pattern:** these are almost all simple actions ("add this label," "mark this task done,"
"move this file") that genuinely don't need a details pane — the summary box already says
everything. The fix isn't to write more prose, it's to stop `details_text=""` from triggering the
fallback: either make `gate.py` treat an explicitly-empty string as "no details" (show `"(no
details)"` — the placeholder `approval_window.py` already has for `None`) instead of calling
`_default_details`, or have each of these call sites pass a one-line literal like `"Label will be
added; no other content changes."` But it's a decision for whoever fixes this: do we want to keep
default behavior for cases when its called upon or should we just always want an explicit line.
Flagging as the highest-leverage fix since it silently affects five+ call sites at once.

### 3.2 Explicit raw JSON dumps (not the fallback — a deliberate choice in the code)

| Connector | Tool | Current details | Note |
|---|---|---|---|
| Salesforce | `salesforce_get_record` | `f"Object: {object_type}\nRecord ID: {record_id}\n\nFields:\n{json.dumps(record_dict, indent=2, default=str)}"` | Salesforce records have per-object dynamic schemas (`Account` vs. `Contact` vs. custom objects each have different fields), so there's no fixed set of labels to hardcode — this is the one case where "we don't know the shape of the response" is genuinely true. Still fixable: it's a flat-ish dict of scalar fields, so a generic `"Key: Value"` one-per-line pretty-printer (skip nulls, skip nested attribute metadata) would look far less technical than indented JSON while still working for any object type. |
| Salesforce | `salesforce_run_report` | `f"Report: {report_name}\nID: {report_id}\n\n{json.dumps(result_dict, indent=2, default=str)}"` | Same reasoning — Salesforce report result shape varies per report type. Same generic-pretty-printer fix applies, though report results (grouped rows/columns) may need a table-style renderer rather than flat key/value to be genuinely readable. |
| Drive | `drive_sheets_write_range` | `f"Spreadsheet: {name}\nRange: {range_a1}\n\n{values}"` where `values` is the **raw unparsed JSON string argument** the model passed in (e.g. `[["Name","Total"],["Alice","=B2*2"]]`) | Inconsistent with `drive_sheets_get_values`, a few lines away, which formats read rows as comma-joined lines. Fix: format `parsed_values` (already parsed earlier in the function) the same way instead of re-embedding the raw string. |

### 3.3 Raw IDs shown where a friendly name is knowable but not looked up

Unlike the Salesforce case above, these are cases where the connector *has* (or could easily
fetch) a human name and just isn't using it — same category as the well-formatted examples in
§2, just not applied yet.

| Connector | Tool(s) | Current preview value | What's available instead |
|---|---|---|---|
| Slack | `slack_get_channel_history`, `slack_get_thread_replies`, `slack_send_message` | `"Channel": channel_id` (e.g. `C0123ABCD`) | `slack_list_channels` already returns `name` for every channel; these calls could resolve `channel_id → #channel-name` the same way Gmail resolves `message_id → subject`. (`slack_search_messages` results already carry `channel_name` per-message in the details lines, so the data exists on that path too.) |
| Tasks | `tasks_create_task`, `tasks_update_task`, `tasks_complete_task`, `tasks_uncomplete_task`, `tasks_move_task` | `"Task list": task_list_id` (raw list ID) | `tasks_list_task_lists` returns list titles; no per-call lookup currently maps id → title the way Gmail/Jira/Confluence resolve their parent objects. |
| Calendar | `calendar_create_event`, `calendar_update_event` | `"Calendar": calendar_id` | For non-primary calendars this is an opaque ID, not the calendar's display name; `calendar_list_calendars` already returns `.summary` (display name) for this exact purpose but it's not cross-referenced here. |
| Drive | `drive_move_file` | `"Move to folder": destination_folder_id` | The *source* file's name/owner is resolved in the same preview; the destination folder id is not (would need a `get_file_metadata` call on the folder id). |
| Telegram | `telegram_send_message` | `"Chat": str(chat_id)` | `telegram_get_messages` on the same connector resolves `chat_id → chat_name` when available on returned messages; `send_message` doesn't attempt any lookup before sending. |

### 3.4 Smaller inconsistencies

- **Jira `jira_update_issue`**: `details_text=description` always, even when the call only
  changed `summary` or `priority` and `description` is empty — so an update that's *only* a
  priority bump shows an empty/unrelated details pane while the actually-changed field
  (`Priority: → High`) is buried in the summary box. Compare to Calendar's `_update_event`, which
  only puts `description` in details when it's actually one of the changed fields.
- **Gmail `gmail_update_filter`**: has a real explanatory details line (*"Gmail has no
  filter-update API..."*) — good — but `gmail_create_filter` right above it has
  `details_text=""` and inherits the dump issue from §3.1 instead of getting a similar one-liner.

## 4. Full per-connector catalog

Legend: ✅ matches the §2 pattern · ⚠️ flagged above (§3.x reference) · — read-only/no popup (auto-approved, not listed).

### Gmail (`gmail.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `gmail_get_message` | From, To, Date, Subject | `From/To/Date/Subject` header block + body | ✅ |
| `gmail_get_thread` | Subject, Participants, Messages, Dates | Per-message `--- Message N ---` blocks with sender/date/body | ✅ |
| `gmail_download_attachment` | From, Subject, Attachment, Size, Will save to | Header block + attachment info + save path | ✅ |
| `gmail_create_draft` | To, [Cc], [Bcc], Subject | Draft body | ✅ |
| `gmail_reply_draft` | In reply to, To, [Cc], [Bcc] | Reply body | ✅ |
| `gmail_reply_all_draft` | In reply to, To, Also to, [Cc], [Bcc] | Reply body | ✅ |
| `gmail_add_label` | From, Subject, Label | *(empty → JSON dump of message object)* | ⚠️ §3.1 |
| `gmail_remove_label` | From, Subject, Label | *(empty → JSON dump of message object)* | ⚠️ §3.1 |
| `gmail_archive_message` | From, Subject | "Action: Archive... remains in All Mail" | ✅ |
| `gmail_create_filter` | Criteria, Actions | *(empty → JSON dump of preview dict — harmless but redundant)* | ⚠️ §3.1 |
| `gmail_update_filter` | Filter ID, Criteria, Actions | Explains delete+recreate mechanics | ✅ |
| `gmail_create_label` | Label | Nested-label note when the name contains `/`; otherwise `details_text=""` → falls back to `json.dumps({"label_name": ...})` — low-harm (one field) but still inconsistent | ✅ (nested case) / ⚠️ §3.1 (plain label case) |

### Drive (`drive.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `drive_get_file_content` | File, Owner, Size, Modified | Header block + first 2000 chars of content | ✅ |
| `drive_sheets_get_values` | Spreadsheet, Owner, Range | Header block + comma-joined row preview (first 50 rows) | ✅ |
| `drive_download_file` | File, Owner, Size, Saved to | Header block + save path | ✅ |
| `drive_write_doc_content` | File, Owner | Full markdown being written | ✅ |
| `drive_upload_file` | File, Size, Destination | Upload summary line + source + destination | ✅ |
| `drive_write_file_content` | File, Owner | Full new file content | ✅ |
| `drive_move_file` | File, Owner, **Move to folder (raw ID)** | *(empty → JSON dump, degraded via `default=str`)* | ⚠️ §3.1, §3.3 |
| `drive_add_comment` | File, Owner | Comment text | ✅ |
| `drive_sheets_write_range` | Spreadsheet, Owner, Range | `Spreadsheet/Range` header + **raw unparsed JSON string** | ⚠️ §3.2 |
| `drive_sheets_add_sheet` | Spreadsheet, Owner, New tab | One-line summary ("Add tab X (RxC) to Y") | ✅ |
| `drive_sheets_rename_sheet` | Spreadsheet, Owner, Tab id, New title | One-line summary | ✅ |
| `drive_sheets_format_range` | Spreadsheet, Owner, Range, Format | `Range/Format` two-line summary | ✅ |

### Calendar (`calendar.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `calendar_get_event_details` | Title, Time, Organizer, Attendees, [Attachments] | Full header + description + attendee list + attachment list | ✅ |
| `calendar_create_event` | Title, Time, **Calendar (raw ID)**, [Location/Conferencing/Rooms/Attendees] | Event description | ⚠️ §3.3 |
| `calendar_update_event` | Event, **Calendar (raw ID)**, changed fields only | Description only if description itself changed | ✅ (logic) / ⚠️ §3.3 (Calendar field) |

### Confluence (`confluence.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `confluence_get_page` | Title, Space, Author, Last modified | Header block + body | ✅ |
| `confluence_get_page_by_title` | Title, Space, Author, Last modified | Header block + body | ✅ |
| `confluence_create_page` | Space, Title, [Parent page ID] | Page body (HTML storage format — see note below) | ✅ (structure) |
| `confluence_update_page` | Page ID, Space, Title (shows `old → new` when renaming) | New page body | ✅ |

Note: Confluence page bodies are HTML storage format, not plain text — the details pane renders
it as raw markup rather than readable text. Worth deciding whether that's acceptable (power users
may want to see the actual HTML) or whether it should be stripped/rendered before display.

### Contacts (`contacts.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `contacts_update` | Contact (resolved name), [Name/Emails/Phones/Organization/Job title] | Notes field | ✅ |
| `contacts_create` | Name, [Emails/Phones/Organization/Job title] | Notes field | ✅ |
| `contacts_add_label` | Contact (resolved name), Label | *(empty → JSON dump of 2-field dict — low harm, still inconsistent)* | ⚠️ §3.1 |
| `contacts_remove_label` | Contact (resolved name), Label | *(empty → JSON dump)* | ⚠️ §3.1 |

### Jira (`jira.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `jira_get_issue` | Project, Key, Summary (truncated 80 chars), Status, Assignee | Full field block + description + comment thread | ✅ |
| `jira_create_issue` | Project, Type, Summary, [Priority] | Description | ✅ |
| `jira_add_comment` | Issue (`KEY — summary`) | Comment body | ✅ |
| `jira_update_issue` | Issue, changed fields (`Summary`/`Description`/`Priority` with `→`) | Always shows raw `description` arg, even when unrelated to what changed | ⚠️ §3.4 |

### Salesforce (`salesforce.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `salesforce_get_record` | Object type, Name, Record ID | `json.dumps(record_dict, indent=2)` | ⚠️ §3.2 |
| `salesforce_run_report` | Report, Report ID | `json.dumps(result_dict, indent=2)` | ⚠️ §3.2 |

### Slack (`slack.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `slack_get_channel_history` | **Channel (raw ID)**, Messages, First message | `[msg_id] user: text` lines | ⚠️ §3.3 |
| `slack_get_thread_replies` | **Channel (raw ID)**, Thread starter, Replies | `[msg_id] user: text` lines | ⚠️ §3.3 |
| `slack_search_messages` | Query, Results | `[channel_name] user: text` lines (channel name *is* resolved here, per-message) | ✅ (details) |
| `slack_send_message` | **Channel (raw ID)**, [In thread, Mark unread] | Message text | ⚠️ §3.3 |

### Tasks (`tasks.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `tasks_create_task` | **Task list (raw ID)**, Title, [Due] | Notes | ⚠️ §3.3 |
| `tasks_update_task` | **Task list (raw ID)**, Task (resolved title), [New title, New due] | Notes | ⚠️ §3.3 |
| `tasks_complete_task` | **Task list (raw ID)**, Task (resolved title) | *(empty → JSON dump of Task object)* | ⚠️ §3.1, §3.3 |
| `tasks_uncomplete_task` | **Task list (raw ID)**, Task (resolved title) | *(empty → JSON dump)* | ⚠️ §3.1, §3.3 |
| `tasks_move_task` | Task (resolved title), **From list / To list (raw IDs)** | *(empty → JSON dump)* | ⚠️ §3.1, §3.3 |

### Telegram (`telegram.py`)

| Tool | Preview (current) | Details (current) | Status |
|---|---|---|---|
| `telegram_get_messages` | Chat (resolved name when available), Messages | `[date] sender: text` lines | ✅ |
| `telegram_search_messages` | Query, Results | `[chat_name] sender: text` lines | ✅ |
| `telegram_send_message` | **Chat (raw chat_id, not resolved)** | Message text | ⚠️ §3.3 |

## 5. How to use this for review

For each ⚠️ row above, once you've checked the actual popup screenshot against what this doc says
is *currently* rendering:

1. Confirm the flagged field/fallback is indeed what you saw (this doc is derived from reading
   the code paths, not from running the app, so it's worth spot-checking).
2. Note your expected format directly against the row (e.g. "Channel → should show `#general`,
   not `C0123ABCD`").
3. Anything not flagged here but that still looked technical in your screenshots is a gap in this
   catalog, not necessarily a non-issue — flag it back and it can be added to §3.
