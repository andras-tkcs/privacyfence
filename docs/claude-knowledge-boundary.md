# What Claude knows before it ever hits an approval prompt

The other two connector references describe the **review UI**: what a human sees in the popup
([`approval-window-content-reference.md`](approval-window-content-reference.md)) and the
per-tool preview/details text ([`TECHNICAL_REFERENCE.md`'s connector matrix](TECHNICAL_REFERENCE.md#connectors--privacy-matrix)).
This doc answers a different question: **what does Claude itself already know before it ever
calls a gated tool**, purely from prior auto-approved (no-gate) tool calls — and, for each gated
read tool, exactly what *new* information an Allow decision adds on top of that.

Source of truth: each connector file under
[`src/privacyfence/connectors/`](../src/privacyfence/connectors/) — re-derive from there if this
drifts, don't trust it blindly.

## How to read this

- **"Known for free"** — returned by a `read_only`, no-`gate` tool call (labelled "auto" in
  `TECHNICAL_REFERENCE.md`'s tables). Claude can call these itself, at will, with zero human in
  the loop, and the full return value is Claude's to keep and reuse.
- **"New once approved"** — the delta a review-gate tool's return value adds *on top of* whatever
  was already known for free. If a field already came back from an auto tool, re-fetching it via
  a gated tool doesn't "reveal" it again — it was never withheld.
- **Preview vs. return value**: the popup's preview/details text is built *for the human
  reviewer* out of the same `gated_call()` invocation, but it is never sent to Claude. What Claude
  receives is strictly the tool's return value
  (`filtered_data` if set, else `raw_data`, shaped by each connector's `_get_*` method — see
  `gate.py`'s `gated_call` signature). The two often overlap in content, but they are not the same
  channel, and a field can appear in one without appearing in the other (e.g. `drive_get_file_content`'s
  reviewer preview truncates body text to 2,000 characters; the value returned to Claude is not
  truncated to that limit).
- All figures below assume default `settings.yaml` — every category left at `allow`, and
  `calendar.free_busy_full_event_details` left at its default `true`. If the user has redacted or
  blocked a category (`privacy_filter.py`), the corresponding field is replaced by a block marker
  or a length-revealing redaction placeholder — but **only at the specific call sites that
  actually apply the filter**, which is a narrower set than "every field in the tables below." See
  [Category-based redaction: what can actually be blocked or redacted](#category-based-redaction-what-can-actually-be-blocked-or-redacted)
  for the exact scope — this doc otherwise describes the default-`allow` ceiling, not a guarantee.

## Worked example: Gmail

- `gmail_list_messages` (auto) returns exactly `id`, `thread_id`, `subject`, `sender`, `date` —
  **not** the recipient list. So "recipients" is *not* known for free; it's new once
  `gmail_get_message` is approved. `subject`/`sender`/`date` are filtered through the same
  `privacy.metadata` category `gmail_get_message`/`gmail_get_thread` apply to those same fields
  (default `allow`, so unchanged unless configured otherwise) — search no longer bypasses that
  category the way it used to.
- `gmail_list_threads` (auto) returns `id` and `snippet` — and `snippet` is Gmail's own short
  excerpt of the thread's last message body (source: `gmail_client.py`'s `list_threads`, straight
  from the Gmail API's `threads.list` response). Since this is genuine body content, `snippet` is
  filtered through `privacy.body`, the same category that governs the full body on
  `gmail_get_message`.
- Neither auto tool exposes how many messages a thread contains. That count is only known once
  `gmail_get_thread` is approved (its `Messages` preview field / the length of the returned
  `messages` list).
- Attachments are the one field where auto-approval already covers more than list/search alone
  suggests: `gmail_list_message_attachments` is its own **auto** tool — name, MIME type, and size
  for every attachment on a message, with zero approval, specifically so Claude doesn't need to
  gate a whole message just to know what's attached. It's filtered through `privacy.attachments`
  (list-shaped, so a non-`allow` policy empties it entirely), the same category the embedded
  attachments list on `gmail_get_message`/`gmail_get_thread` already used. By the time
  `gmail_download_attachment` gates, none of that metadata is new — approval only adds the
  resolved save path, and even then Claude never gets the file's bytes back (see below).

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `gmail_list_messages` | auto | `id`, `thread_id`, `subject`, `sender`, `date` (last three: `privacy.metadata`) |
| `gmail_list_threads` | auto | `id`, `snippet` (`privacy.body` — short excerpt of last message body) |
| `gmail_list_message_attachments` | auto | `name`, `mime_type`, `size` per attachment (`privacy.attachments`) |
| `gmail_list_filters` | auto | full filter criteria + actions |
| `gmail_list_labels` | auto | full label list (incl. nesting) |
| `gmail_get_message` | review | **new:** full `body_text`, full `recipients`, `labels`; attachments repeated (same fields as the auto tool) |
| `gmail_get_thread` | review | **new:** every message's body, sender, date; message count; attachments per message |
| `gmail_download_attachment` | review | **new:** nothing to Claude — writes the file to disk and returns only `path`/`name`/`size_bytes`, never content bytes |

## Google Drive

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `drive_list_files` | auto | `id`, `name`, `mime_type`, `owners`, sharing status |
| `drive_get_file_metadata` | auto | full metadata record: name, owners, created/modified times, sharing status |
| `drive_list_folder` | auto | same shape as `drive_list_files`, scoped to one folder's direct children |
| `drive_list_shared_drives` | auto | `id`, `name` per Shared Drive |
| `drive_sheets_get_metadata` | auto | per-tab `id`, `title`, `index`, row/column count |
| `drive_get_file_content` | review | **new:** extracted text content (metadata fields were already known for free if `drive_get_file_metadata` was called first) |
| `drive_sheets_get_values` | review | **new:** cell values for the requested range |
| `drive_download_file` | review | **new:** nothing content-wise — returns `path`/`name`/`size_bytes`, never bytes |

`drive_get_file_content`'s reviewer-facing preview truncates the body to ~2,000 characters, but
the value handed to Claude is the full extracted text (subject only to the underlying fetch's own
byte cap, not that 2,000-character UI truncation).

`drive_list_files`'s `file_list` category and `drive_get_file_metadata`'s `file_metadata` category
cover overlapping fields (a file's `name`/`owners`) but gate the two tools independently —
restricting one without the other still lets that information through whichever tool's category
is still `allow`. `privacy_filter.check_consistency_warnings()` logs a startup warning when
they're configured inconsistently; it's advisory only and never changes what either tool actually
returns.

## Slack

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `slack_list_channels` | auto | `id`, `name`, `is_private`, `topic`, `purpose`, `member_count` |
| `slack_list_dms` | auto | `id`, `user_id`, `user_name` per 1:1 DM (who the user DMs with) |
| `slack_list_group_chats` | auto | `id`, `name`, `member_ids`, `member_names` per group DM |
| `slack_get_channel_history` | review | **new:** every message's `text` and `user_name`/`user_id` |
| `slack_get_thread_replies` | review | **new:** same, for a thread |
| `slack_search_messages` | review | **new:** matching messages' `text` and `user_name`/`user_id` across channels |

Unlike Gmail/Drive/Jira, Slack has no metadata-only auto search — `slack_search_messages` itself
returns message text, so it gates like a read, not like a list.

`slack_list_dms`/`slack_list_group_chats` mean *who a user DMs with* is knowable for free, with no
review gate at all — the same "auto tool discloses more than it looks like" pattern Contacts/Tasks
already have for their free-text fields (see below), just for relationship/identity data instead of
free text. Both accept an optional `participant` filter (id, handle, or display name) so Claude can
search by *who's in a conversation* directly, without first listing everything.

## Google Calendar

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `calendar_list_calendars` | auto | `id`, `summary`, `primary`, `access_role` |
| `calendar_list_events` | auto | `id`, `title`, `start_time`, `end_time`, `day_of_week`, `all_day`, `status` — explicitly no attendees/description |
| `calendar_get_free_busy` | auto | **for colleagues the authenticated account has calendar access to: full event `title`, time, and `status`** (not just busy/free blocks) — *unless* `calendar.free_busy_full_event_details` is set to `false`, in which case every entry is downgraded to a busy/free-only block regardless of access (default `true`, preserving the behavior above) |
| `calendar_list_rooms` | auto | room `resource_email`, `resource_name`, building, floor, capacity, description — served from a static directory IT syncs into `org_config.json` (`scripts/sync_room_directory.py`), not a live API call, so it's empty until IT has synced one, and only ever reflects that last sync |
| `calendar_get_event_visibility` | auto | just the `visibility` field |
| `calendar_get_event_details` | review | **new:** `description`, full `attendees` (email, display name, response status), `location`, conferencing link, file attachments (`file_id`, `title`, `mime_type` — not their content; that's a separate `drive_get_file_content` gate) |

`calendar.free_busy_full_event_details` is a single settings.yaml boolean, not a `privacy_filter.py`
category — Calendar has no category schema (see the redaction section below).

## Google Contacts

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `contacts_list` | auto | full `Contact` record: display/given/family name, emails, phones, organization, job title, **notes** (`contacts_privacy.notes`), photo URL, `source` |
| `contacts_search` | auto | same, for matches |
| `contacts_get` | auto | same, for one contact |

Contacts has **no gated read tool at all** — every field except `notes` is available to Claude
with zero approval, unconditionally, the moment it calls any of the three list/search/get tools.
`notes` (a biography field that can hold arbitrary personal detail) is the one field filtered
through `contacts_privacy`'s `notes` category (default `allow`, so unchanged unless configured).

## Telegram

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `telegram_list_chats` | auto | `id`, `name`, `type`, `unread_count`, `is_self` |
| `telegram_get_messages` | review | **new:** every message's `sender_name`, `text`, `date` |
| `telegram_search_messages` | review | **new:** same, across chats |

Like Slack, Telegram's search returns text directly, so it's gated like a read — there's no
metadata-only auto search here.

## Salesforce

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `salesforce_list_reports` | auto | report `name`/`id` list only — no run results |
| `salesforce_get_record` | review | **new:** every field on the record |
| `salesforce_run_report` | review | **new:** all report rows/aggregates |
| `salesforce_search` | review | **new:** lightweight `object_type`/`Name`/`id` per match — no other fields |

Salesforce is the one connector with **no** auto search at all: even `salesforce_search`'s
lightweight Id/Name matches require approval, unlike Gmail/Drive/Jira's metadata-only auto search.

## Jira

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `jira_list_projects` | auto | `key`, `name`, `project_type`, `lead` |
| `jira_search_issues` | auto | per issue: `key`, `summary`, `status`, `issue_type`, `priority`, `assignee`, `reporter`, `labels`, `created`, `updated`, `url` — **no `description`, no comments** (the client explicitly omits description unless fetched via `get_issue`) |
| `jira_get_transitions` | auto | transition `name` and target `to_status` |
| `jira_get_issue` | review | **new:** `description`, all comments (author/body/timestamps); everything else was already knowable via `jira_search_issues` |

## Confluence

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `confluence_list_spaces` | auto | `key`, `name`, `type`, `description` |
| `confluence_search` | auto | matching pages/blog posts **including a content excerpt** (`excerpt` field, straight from Confluence's search API, filtered through `confluence_privacy.search_excerpt`) |
| `confluence_cql_search` | auto | same shape as `confluence_search`, CQL-driven |
| `confluence_list_pages` | auto | `title`, `id`, `version` per page in a space |
| `confluence_get_page` / `confluence_get_page_by_title` | review | **new:** full page `body` (HTML storage format) |

`confluence_search`/`confluence_cql_search` are the only auto tools across every connector that
return actual content excerpts, not just structural metadata — `excerpt` is the one field filtered
through `confluence_privacy`'s `search_excerpt` category (default `allow`, so unchanged unless
configured); title/space/id have no category of their own.

## Google Tasks

| Tool | Gate | Fields Claude gets |
|---|---|---|
| `tasks_list_task_lists` | auto | `id`, `title`, `updated` (a `TaskList` — no `notes` field to filter) |
| `tasks_list_tasks` | auto | full `Task` record per task: `title`, **`notes`** (`tasks_privacy.notes`), `due`, `status`, `completed`, `updated`, `position`, `parent` |
| `tasks_get_task` | auto | same, for one task |

Like Contacts, Tasks has **no gated read tool** — every `Task` field except `notes` is
auto-approved unconditionally. `notes` is filtered through `tasks_privacy`'s `notes` category
(default `allow`, so unchanged unless configured), the one field on a task that can carry
arbitrary personal content.

## Category-based redaction: what can actually be blocked or redacted

The "AI will receive" checklist's three icons (`✓` allow / `◐` redact / `✗` block) are
`privacy_filter.py`'s `category_policy()` made visible (`approval_window.py`'s
`_VISIBILITY_SYMBOL`), configured per category under one of `settings.yaml`'s six category-schema
blocks: `privacy`, `drive_privacy`, `slack_privacy`, `contacts_privacy`, `tasks_privacy`,
`confluence_privacy`. **It does not cover every field in the tables above** — its scope is
deliberately narrow, and it's worth being precise about exactly where it does and doesn't reach.

### Exact mechanics

- **Block (`✗`)**: the whole value is replaced — text becomes the fixed string
  `"[BLOCKED BY PRIVACY FILTER]"`; a list becomes `[]`. Nothing survives.
- **Redact (`◐`)**: for text, the **entire** value is replaced by a placeholder that reveals only
  its character count — `"[REDACTED BY PRIVACY FILTER — N characters withheld]"`. This is a full
  replacement, not partial masking (`settings.yaml.example`'s comment now matches `_redact_text()`'s
  actual implementation). For a list, there's no partial-redaction shape for structured records
  (attachments, files, channels), so `redact` behaves **identically to block**: the list is
  emptied. `privacy_filter.py`'s module docstring calls this out as a known gap.

### Scope: six connectors have a category schema; Calendar has one separate boolean; three have neither

Per `privacy_filter.py`'s own module docstring, its scope is deliberately narrow: only connectors
with a category schema documented in `settings.yaml.example` get one, and it invents none for
others. Six connectors currently qualify — `gmail.py`, `drive.py`, `slack.py`, `contacts.py`,
`tasks.py`, and `confluence.py` all import `privacy_filter` and call `apply_text`/`apply_list` at
specific call sites (the table below is exhaustive for all six). **Calendar is a separate case**:
`calendar_get_free_busy`'s one known leak (full event titles for colleagues the authenticated
account can see) is governed by a single settings.yaml boolean
(`calendar.free_busy_full_event_details`), not a `privacy_filter.py` category — Calendar has no
category schema and never calls `apply_text`/`apply_list`. **Telegram, Salesforce, and Jira have
neither** — grep confirms zero `apply_text`/`apply_list` calls in `telegram.py`, `salesforce.py`,
or `jira.py`. **Every field for those three connectors, listed in the tables above, is delivered in
full, unconditionally — there is no config option that blocks or redacts any of it.** (The
separate, automatic PII detector still scans and can flag a red banner + force a second
confirmation on any connector's content — but it never removes or replaces data, it only warns;
see `approval-window-content-reference.md`'s row 5.)

Within the six schema connectors, the filter is applied field-by-field at specific call sites —
not category-wide. The table below is exhaustive: every row is one `apply_text`/`apply_list` call
site in the connector code, naming the literal key(s) in what Claude actually receives
(`filtered_data`, not the reviewer's preview) that get replaced.

| Category | Tool | Redacted fields (exact keys in Claude's return value) |
|---|---|---|
| `privacy.metadata` | `gmail_list_messages` (auto) | `subject`, `sender`, `date` |
| `privacy.metadata` | `gmail_get_message` | `subject`, `sender`, `recipients`, `date` |
| `privacy.metadata` | `gmail_get_thread` | top-level `subject`; and, per message inside the `messages` list, `subject`, `sender`, `date` |
| `privacy.body` | `gmail_list_threads` (auto) | `snippet` |
| `privacy.body` | `gmail_get_message` | `body_text` |
| `privacy.thread_history` | `gmail_get_thread` | per message inside `messages`, `body_text` |
| `privacy.attachments` | `gmail_list_message_attachments` (auto) | the entire returned `attachments` list |
| `privacy.attachments` | `gmail_get_message` | `attachments` (whole list — redact behaves like block: emptied, not partially redacted) |
| `privacy.attachments` | `gmail_get_thread` | per message inside `messages`, `attachments` (whole list) |
| `drive_privacy.file_list` | `drive_list_files` (auto) | the entire returned list — every file's `id`/`name`/`mime_type`/`owners`/sharing status together |
| `drive_privacy.folder_structure` | `drive_list_folder` (auto) | the entire returned list, same shape as above |
| `drive_privacy.file_metadata` | `drive_get_file_metadata` (auto) | every field except `id` — the whole record collapses to `{"id": ...}` when not `allow`; redact and block are identical here |
| `drive_privacy.file_content` | `drive_get_file_content` | `content` |
| `drive_privacy.file_content` | `drive_sheets_get_values` | the entire returned cell-values array (there's no separate "cell values" category — this reuses `file_content`) |
| `slack_privacy.channel_list` | `slack_list_channels` (auto) | the entire returned list — every channel's `id`/`name`/`is_private`/`topic`/`purpose`/`member_count` together |
| `slack_privacy.dm_list` | `slack_list_dms` (auto) | the entire returned list — every DM's `id`/`user_id`/`user_name` together |
| `slack_privacy.group_chat_list` | `slack_list_group_chats` (auto) | the entire returned list — every group chat's `id`/`name`/`member_ids`/`member_names` together |
| `slack_privacy.message_content` | `slack_get_channel_history`, `slack_search_messages` | per message, `text` |
| `slack_privacy.thread_content` | `slack_get_thread_replies` | per message, `text` |
| `slack_privacy.user_identity` | `slack_get_channel_history`, `slack_get_thread_replies`, `slack_search_messages` | per message, `user_name` and `user_id` |
| `slack_privacy.user_identity` | `slack_list_dms` (auto) | `user_id`, `user_name` (redacted individually, *before* `dm_list` empties the whole list if that category is also non-`allow`) |
| `slack_privacy.user_identity` | `slack_list_group_chats` (auto) | each entry in `member_ids`, `member_names` |
| `contacts_privacy.notes` | `contacts_list`, `contacts_search`, `contacts_get` (all auto) | `notes` |
| `tasks_privacy.notes` | `tasks_list_tasks`, `tasks_get_task` (all auto) | `notes` — `tasks_list_task_lists` has no `notes` field, nothing to redact |
| `confluence_privacy.search_excerpt` | `confluence_search`, `confluence_cql_search` (both auto) | `excerpt` |

Two footnotes the table above can't show inline:

- **`gmail_get_thread`'s `participants` and `date_range`** are computed from the same `metadata`
  category, but only feed the human reviewer's preview box — they're never part of `filtered`, so
  Claude never receives them from this call regardless of policy (nothing to redact; there's
  nothing there in the first place).
- **`drive_privacy.file_metadata` inside `drive_get_file_content`** filters the reviewer's preview
  strings (File/Owner/Size/Modified) but has **no effect on what Claude receives** — that call's
  actual return value is just `{"file_id", "content"}`. The category only actually gates Claude's
  own knowledge via the separate `drive_get_file_metadata` auto tool (the row above it).

Fields with no row in this table (e.g. Slack's `ts`/`channel_id`/`channel_name`/`thread_ts`/
`reply_count`, Gmail's `labels`, Drive's raw file `id`, Contacts'/Tasks' every field but `notes`,
Confluence's `title`/`space`/`id`) aren't part of any category schema at all — they pass through
unconditionally, the same as anything from the three connectors with no schema at all.
