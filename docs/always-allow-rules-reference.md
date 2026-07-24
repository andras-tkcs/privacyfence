# "Always allow" — per-tool reference

What clicking **Always allow** does, tool by tool. Source of truth is
[`src/privacyfence/auto_accept.py`](../src/privacyfence/auto_accept.py) — specifically
`TOOL_TO_GATE`, `TOOL_TO_OPERATION`, `suggest_rule()`, and `suggest_write_rule()` — cross-checked
against [`docs/TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md)'s
[Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules) section. If this drifts from either,
they're authoritative, not this doc.

## How this doc is organized

Every gated tool has a **gate** (`TOOL_TO_GATE`): `auto`, `review`, or `popup`. `auto` tools never
show a popup or any button — nothing to allow — so they're **left out of this doc entirely**. What
remains splits into two sections:

- **[Read tools](#read-tools)** (`review` gate) — the popup offers **Always allow** whenever
  `suggest_rule()` can derive a plausible rule from the specific item being read; otherwise the
  button doesn't appear, and the row is empty.
- **[Write tools](#write-tools)** (`popup` gate) — most write popups never offer Always allow at
  all; 32 tools across 29 operation keys are a narrow exception (`suggest_write_rule()`), each
  proposing a rule scoped to the one folder/label/calendar/project/space/task-list the call just
  touched (`gmail_create_draft` and its two reply variants are the one exception — see their row
  below).

Where a tool can produce more than one rule, they're checked in priority order and the first match
wins — clicking Always allow on a message where you're the sender proposes `i_am_sender`, not
`trusted_sender_domain`, even though the domain would also match. When more than one candidate
actually matches the item you're looking at, the popup asks which rule to create instead of always
silently picking the top-priority one — see [Multiple matching candidates](#multiple-matching-candidates)
below.

**Always allow always writes a plain `auto_accept_rules` entry scoped to one operation key** — even
for rules TECHNICAL_REFERENCE.md marks "grant-managed" (`approved_folder`, `approved_channel`,
`approved_project_keys`, etc.). The equivalent `auto_accept_grants` entry, which covers every
operation key a resource touches from one place, is a separate mechanism set up by hand or from the
menu bar's Trusted-\* submenus — the popup button itself never writes there. See
[Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants) for that alternative.

---

## Read tools

### Gmail

| Tool | Always allow rule created |
|---|---|
| `gmail_get_message` | `i_am_sender` (you're the sender), else `trusted_sender_domain` (sender's domain) |
| `gmail_get_thread` | `i_am_sender` (you're the sender), else `trusted_sender_domain` (sender's domain) |
| `gmail_download_attachment` | `i_am_sender` (you're the sender), else `trusted_sender_domain` (sender's domain) |

### Google Drive (incl. Sheets)

| Tool | Always allow rule created |
|---|---|
| `drive_get_file_content` | `i_am_owner` (you own the file), else `approved_folder` (file's parent folder) |
| `drive_download_file` | `i_am_owner`, else `approved_folder` |
| `drive_sheets_get_values` | `i_am_owner`, else `approved_folder` — same family as the two rows above |

The `i_am_owner` / `approved_folder` priority is configurable, not fixed — from **Manage
Auto-accept Rules… → Drive → Always-allow Suggestion Order** (**↑ Move up** / **↓ Move down** /
**✕ Never suggest** / **+ Re-include**), or by hand under `rule_suggestion_priority.drive_read` in
`settings.yaml`. Listing only `approved_folder` there makes Always allow propose it even when
`i_am_owner` would also match — excluded from consideration entirely, not just deprioritized. See
[Always-allow suggestion priority](TECHNICAL_REFERENCE.md#always-allow-suggestion-priority). When a
file is both owned by you *and* in an approved folder, Always allow asks which rule to create
instead of silently picking whichever one is first in that order — see
[Multiple matching candidates](#multiple-matching-candidates) below.

### Slack

| Tool | Always allow rule created |
|---|---|
| `slack_get_channel_history` | `dm_with_myself` (channel is your self-DM), else `group_dm` (channel is a group DM), else `approved_channel` (that channel) |
| `slack_get_thread_replies` | `dm_with_myself`, else `group_dm`, else `approved_channel` |
| `slack_search_messages` | `approved_channel_all_results` (matches only when **every** result in the search is from a channel on the allowlist) |

`group_dm` recognizes a Slack group DM (the "mpim" conversation type — a private multi-person
conversation, distinct from a 1:1 DM and from a private channel, even though both can share the
same `G`-prefixed channel-id shape) as its own always-allow-able category, instead of requiring
each group's ID to be individually allowlisted under `approved_channel`.

### Google Calendar

| Tool | Always allow rule created |
|---|---|
| `calendar_get_event_details` | `i_am_organizer` (you organize it), else `no_external_attendees` (every attendee shares your domain), else `non_private_event` (event isn't marked private) |

This priority order is configurable via **Calendar → Always-allow Suggestion Order** /
`rule_suggestion_priority.calendar_read_event` — e.g. requiring `no_external_attendees` even when
you're the organizer, instead of `i_am_organizer` always winning outright. When 2+ of these actually
match the event, Always allow asks which one to create — see
[Multiple matching candidates](#multiple-matching-candidates).

### Telegram

| Tool | Always allow rule created |
|---|---|
| `telegram_get_messages` | `approved_chats` (that chat) |
| `telegram_search_messages` | `approved_chats_all_results` (matches only when **every** result in the search is from a chat on the allowlist) |

`telegram_search_messages` shares the `telegram.read_chat_messages` operation key with
`telegram_get_messages` — one "trusted chats" rule or grant covers both.

### Salesforce

| Tool | Always allow rule created |
|---|---|
| `salesforce_get_record` | `approved_object_types` (that object type) |
| `salesforce_run_report` | `approved_report_ids` (that report) |
| `salesforce_search` | `approved_object_types` (every object type the search touches) — only offered when the call specifies `object_types`; an unscoped search (Salesforce's whole default searchable set) never offers Always allow |

### Jira

| Tool | Always allow rule created |
|---|---|
| `jira_get_issue` | `i_am_reporter` (you filed it), else `i_am_assignee` (you're assigned), else `approved_project_keys` (issue's project) |

This priority order is configurable via **Jira → Always-allow Suggestion Order** /
`rule_suggestion_priority.jira_read_issue`. When 2+ of these actually match the issue, Always allow
asks which one to create — see [Multiple matching candidates](#multiple-matching-candidates).

### Confluence

| Tool | Always allow rule created |
|---|---|
| `confluence_get_page` | `i_am_author` (you wrote it), else `approved_space_keys` (page's space) |
| `confluence_get_page_by_title` | `i_am_author`, else `approved_space_keys` |

This priority order is configurable via **Confluence → Always-allow Suggestion Order** /
`rule_suggestion_priority.confluence_read_page`. When 2+ of these actually match the page, Always
allow asks which one to create — see [Multiple matching candidates](#multiple-matching-candidates).

> Google Contacts and Google Tasks have no `review`-gate tools at all — their only reads
> (`contacts_list`/`contacts_search`/`contacts_get`, `tasks_list_task_lists`/`tasks_list_tasks`/
> `tasks_get_task`) are unconditionally `auto`, so neither connector has a Read tools table here.

---

### Conditional gating for search tools

`slack_search_messages` and `telegram_search_messages` return matches from any number of
channels/chats, so there's no single `channel_id`/`chat_id` to check against an allowlist the way
`slack_get_channel_history`/`telegram_get_messages` do. `approved_channel_all_results` and
`approved_chats_all_results` evaluate every result the search actually returned instead of a single
arg:

```python
def _rule_approved_channel_all_results(self, value, ctx):
    if not value:
        return False
    allowed = set(value if isinstance(value, list) else [value])
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    return bool(items) and all(getattr(m, "channel_id", None) in allowed for m in items)
```

(and the `chat_id` mirror for Telegram). In practice:

1. Every result is from an approved channel/chat → auto-accepted, no popup.
2. A partial match (some results approved, one not) → gated for the *entire* call, not just the
   unapproved result — PrivacyFence doesn't split one response into an approved half and a gated
   half.
3. No results are from an approved channel/chat → gated.

`suggest_rule()` proposes the *union* of channel/chat ids present across the current search's
results, the same way `approved_folder`'s suggestion is the file's own `parent_ids`.

---

### Multiple matching candidates

Four of the tables above (Drive, Calendar, Jira, Confluence) list more than one possible rule per
row because the priority order only decides which one wins *by default* — it doesn't mean the
others never apply. When you click Always allow on an item where 2+ of a row's candidates actually
match (e.g. a file you own that's also in an approved folder), a "choose from list" popup names
each matching candidate and asks which one to create, instead of always silently creating the
top-priority match. Picking one both selects and creates it — there's no separate confirmation
dialog afterward, unlike the everyday single-candidate case. Cancelling accepts the item once
without creating any rule, same as cancelling the single-candidate confirmation does. If only one
candidate matches, nothing about today's flow changes.

---

## Write tools

Most write tools never offer **Always allow** — auto-accepting a write silently is a materially
bigger blast radius than auto-accepting a read. 32 tools across 29 operation keys are a narrow,
deliberate exception (`auto_accept.WRITE_RULE_SUGGESTIONS`): all but one propose an already-existing
rule scoped to the one folder/label/calendar/project/space/task-list the call just touched — never
a bare "accept every future write of this type" toggle (`gmail_create_draft` and its two reply
variants are the deliberate exception to that — see below). Every other write tool below offers
exactly Deny / Allow once, with an empty **Always allow rule created** column. A handful of tools
also have a separate, non-persisted grace-window behavior tucked into their "Allow once" instead —
see [Related but distinct mechanisms](#related-but-distinct-mechanisms) for what that is; it isn't
an Always-allow rule and doesn't belong in this column.

### Gmail

| Tool | Always allow rule created |
|---|---|
| `gmail_create_draft` | `always_allow` (unconditional) |
| `gmail_reply_draft` | `always_allow` (unconditional) |
| `gmail_reply_all_draft` | `always_allow` (unconditional) |
| `gmail_add_label` | `label_name_allowlist` (that label) |
| `gmail_remove_label` | `label_name_allowlist` (that label) |
| `gmail_archive_message` | |
| `gmail_create_filter` | |
| `gmail_update_filter` | |
| `gmail_create_label` | |

`gmail_create_draft`/`gmail_reply_draft`/`gmail_reply_all_draft` (`gmail.create_draft`) propose a
plain, unconditional `always_allow` rule — no recipient check at all, broader than
`to_is_myself`/`approved_recipient_domain`, which are both conditional on who the draft goes to.
It's the one entry in `WRITE_RULE_SUGGESTIONS` that isn't resource-identity-scoped: drafting has no
recipient sent yet, unlike `gmail_send_message` (still reviewed via `to_is_myself`/
`approved_recipient_domain` before it goes out, and not in this table at all), so an unconditional
rule for drafting alone doesn't carry the blast radius a bare toggle would for an operation that
actually delivers something. Also configurable directly from **Manage Auto-accept Rules… → Gmail →
Filters** without ever clicking Always allow.

### Google Drive (incl. Sheets and Docs)

| Tool | Always allow rule created |
|---|---|
| `drive_write_file_content` | `approved_sandbox_folder` (file's current parent folder) |
| `drive_upload_file` | `parent_folder_allowlist` (upload's destination folder) |
| `drive_write_doc_content` | `approved_sandbox_folder` (file's current parent folder) |
| `drive_move_file` | `move_within_approved_folders` (file's parent folder **before** the move, not the destination) |
| `drive_add_comment` | `approved_sandbox_folder` (file's current parent folder) |
| `drive_sheets_write_range` | `approved_sandbox_folder` (spreadsheet's current parent folder) |
| `drive_sheets_add_sheet` | `approved_sandbox_folder` (spreadsheet's current parent folder) |
| `drive_sheets_rename_sheet` | `approved_sandbox_folder` (spreadsheet's current parent folder) |
| `drive_sheets_format_range` | `approved_sandbox_folder` (spreadsheet's current parent folder) |
| `drive_sheets_insert_dimensions` | `approved_sandbox_folder` (spreadsheet's current parent folder) |
| `drive_sheets_delete_dimensions` | `approved_sandbox_folder` (spreadsheet's current parent folder) |
| `drive_docs_edit_content` | `approved_sandbox_folder` (doc's current parent folder) |
| `drive_docs_format_content` | `approved_sandbox_folder` (doc's current parent folder) |

A single trusted-folder grant (`auto_accept_grants` → `drive.sandbox_folders`) covers all of them at
once: writing into the folder, uploading into it, commenting on a file already there, and moving a
file out of it — `drive_upload_file`/`drive_move_file` use their own rule names
(`parent_folder_allowlist`/`move_within_approved_folders`, checking the upload's destination folder
and the file's current parent folder respectively) rather than `approved_sandbox_folder`, but all
three are compiled from the same grant, and all three now also offer the popup-time Always-allow
shortcut above. See [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants).

### Slack

| Tool | Always allow rule created |
|---|---|
| `slack_send_message` | |

### Google Calendar

| Tool | Always allow rule created |
|---|---|
| `calendar_create_event` | `personal_calendar` (that calendar) |
| `calendar_update_event` | `personal_calendar` (that calendar) |
| `calendar_create_out_of_office` | |
| `calendar_set_working_location` | |
| `calendar_set_event_visibility` | `personal_calendar` (that calendar) |

`calendar_create_out_of_office`/`calendar_set_working_location` are a separate case: neither tool
takes a `calendar_id` (both always act on your own primary calendar), so `personal_calendar` has
nothing to check against and they aren't resource-identity-scoped the way `WRITE_RULE_SUGGESTIONS`
requires. Both instead support the same unconditional `always_allow` rule Gmail drafts use,
configurable from **Manage Auto-accept Rules… → Calendar → Filters** — no popup-time shortcut,
deliberately (see [Related but distinct mechanisms](#related-but-distinct-mechanisms)).

### Google Contacts

| Tool | Always allow rule created |
|---|---|
| `contacts_update` | |
| `contacts_create` | |
| `contacts_add_label` | |
| `contacts_remove_label` | |

### Telegram

| Tool | Always allow rule created |
|---|---|
| `telegram_send_message` | |

### Jira

| Tool | Always allow rule created |
|---|---|
| `jira_create_issue` | `approved_project_keys` (that project) |
| `jira_add_comment` | `approved_project_keys` (issue's project) |
| `jira_update_issue` | `approved_project_keys` (issue's project) |
| `jira_transition_issue` | `approved_project_keys` (issue's project) |

The project is derived from `project_key` directly (`jira_create_issue`) or parsed from
`issue_key`'s `"PROJ-123"` prefix (the other three).

### Confluence

| Tool | Always allow rule created |
|---|---|
| `confluence_create_page` | `approved_space_keys` (that space) |
| `confluence_update_page` | `approved_space_keys` (page's space) |

### Google Tasks

| Tool | Always allow rule created |
|---|---|
| `tasks_create_task` | `approved_task_list` (that list) |
| `tasks_update_task` | `approved_task_list` (that list) |
| `tasks_complete_task` | `approved_task_list` (that list) |
| `tasks_uncomplete_task` | `approved_task_list` (that list) |
| `tasks_move_task` | `approved_task_list` (**both** source and destination lists) |

`tasks_move_task`'s suggestion covers **both** ends of the move — a rule scoped to only one list
would let a future move smuggle a task out of (or into) a list never approved.

> `drive_create_blank_file` and `drive_sheets_create` are writes too, but both are unconditionally
> `auto` — an empty file/spreadsheet with no content yet carries no disclosure risk — so they're
> omitted from this table rather than shown with an empty column.

---

## Related but distinct mechanisms

These are easy to conflate with Always allow because they sit in the same popups or touch the same
config, but none of them are the "Always allow" button covered above.

**Temp-accept grace window** — an in-memory, non-persisted acceptance for six `popup`-gate writes
expected to fire repeatedly against the same file in a burst (`drive_sheets_write_range`,
`drive_sheets_format_range`, `drive_sheets_insert_dimensions`, `drive_add_comment`,
`drive_docs_edit_content`, `drive_docs_format_content` — `auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`),
scoped to one file/spreadsheet for 5 minutes and gone on daemon restart. There's no separate button
for it: these six popups show only Deny / Allow once, with a plain disclosure caption above the
buttons explaining that Allow once also arms the grace window. Deliberately *not* offered on
`drive_sheets_delete_dimensions` (no undo path) or on `drive_sheets_add_sheet`/
`drive_sheets_rename_sheet` (one-shot per file, not called in a burst) — those get a plain
Deny/Allow once with no caption at all.

**The `always_allow` rule is deliberately excluded from `WRITE_RULE_SUGGESTIONS` everywhere except
`gmail.create_draft`** — it's the one unconditional, non-resource-scoped rule in the whole engine.
Calendar out-of-office/working-location also use it but stay out of `WRITE_RULE_SUGGESTIONS`
entirely: menu-bar-configured only, with no popup-time shortcut. Gmail drafting is the sole,
deliberate exception (see its row above) — every other entry in `WRITE_RULE_SUGGESTIONS` keeps the
table's safety property of being scoped to one specific folder/label/calendar/project/space/list.

**Bridge-proposed rule/grant changes** (`privacyfence_propose_auto_accept_rule_change`) — lets Claude
itself propose adding/updating/removing an `auto_accept_rules` or `auto_accept_grants` entry for
*any* operation, including the dozen or so write tools that never get an Always-allow button of
their own.
Every call still blocks on the same confirmation dialog Always allow uses
(`show_rule_confirmation_popup`) — there's no way for a rule to land without a human confirming it.
See [Reading and proposing auto-accept changes from the bridge](TECHNICAL_REFERENCE.md#reading-and-proposing-auto-accept-changes-from-the-bridge).

**Auto-accept grants** (`auto_accept_grants` in `settings.yaml`, and the menu bar's **Manage
Auto-accept Rules… → Trusted \*** submenus) — the resource-scoped alternative to a narrow
`auto_accept_rules` entry: grant one folder/channel/project/etc. once and it covers every operation
key that resource touches. Always allow never writes here directly; these are set up separately.
See [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants).
