# "Always allow" — per-tool reference

What clicking **Always allow** on the review-gate popup does, tool by tool. Source of truth is
[`src/privacyfence/auto_accept.py`](../src/privacyfence/auto_accept.py) — specifically
`TOOL_TO_GATE`, `TOOL_TO_OPERATION`, and `suggest_rule()` — cross-checked against
[`docs/TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md)'s
[Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules) section. If this drifts from either,
they're authoritative, not this doc.

## How this doc is organized

Every gated tool has a **gate** (`TOOL_TO_GATE`): `auto`, `review`, or `popup`. `auto` tools never
show a popup or any button — nothing to allow — so they're **left out of this doc entirely**. What
remains splits into the two sections below, by what the tool actually does:

- **[Read tools](#read-tools)** (`review` gate) — the popup offers **Always allow** only when
  `suggest_rule()` can derive a plausible rule from the specific item being read; otherwise the
  button doesn't appear on that popup at all, and the row is empty.
- **[Write tools](#write-tools)** (`popup` gate) — **never** offer Always allow, by design (see
  [`gate.py`](../src/privacyfence/gate.py)'s module docstring: auto-accepting writes silently is a
  materially bigger blast radius than auto-accepting reads). Every write popup offers only Deny /
  Allow once — there is no third button — but for a handful of tools, clicking **Allow once** also
  silently arms a narrower, non-persisted 5-minute same-file grace window, disclosed with a plain
  caption above the buttons rather than a separate control. Flagged per tool below — see
  [Related but distinct mechanisms](#related-but-distinct-mechanisms) for what that actually does.

Where a read tool can produce more than one rule, they're checked in the listed order and the first
match wins — clicking Always allow on a message where you're the sender proposes `i_am_sender`, not
`trusted_sender_domain`, even though the domain would also match.

**Always allow always writes a plain `auto_accept_rules` entry scoped to one operation key** — even
for rules TECHNICAL_REFERENCE.md marks "grant-managed" (`approved_folder`, `approved_channel`,
`approved_project_keys`, etc.). The equivalent `auto_accept_grants` entry, which covers every
operation key a resource touches from one place, is a separate mechanism you set up by hand or from
the menu bar's Trusted-\* submenus — the popup button itself never writes there. See
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
| `drive_sheets_get_values` | `approved_spreadsheet` (scoped to that spreadsheet, and its tab if identifiable) |

> 📋 **Proposed design change:** `i_am_owner` and `approved_folder` are mutually exclusive today —
> owning the file always wins, so Always allow never offers folder-scoping for a file you happen to
> own. Make this user-configurable: a setting the user picks (not a fixed priority order) for
> whether Always allow should still propose `approved_folder` — narrower, tied to a specific folder
> — even when `i_am_owner` would also match, for users who'd rather scope auto-accept to folders
> than to "everything I own, wherever it lives." Applies to all three rows above
> (`drive_get_file_content`, `drive_download_file`, `drive_sheets_get_values`). Needs a UI change: a
> toggle in the connector's rule-editor settings, not just a one-off popup-time choice.

### Slack

| Tool | Always allow rule created |
|---|---|
| `slack_get_channel_history` | `dm_with_myself` (channel is your self-DM), else `group_dm` (channel is a group DM), else `approved_channel` (that channel) |
| `slack_get_thread_replies` | `dm_with_myself`, else `group_dm`, else `approved_channel` |
| `slack_search_messages` | `approved_channel_all_results` (union of channels across every result — see [Conditional gating for search tools](#conditional-gating-for-search-tools) below for why this is a different rule from `approved_channel`) |

**Shipped:** `group_dm` recognizes a Slack group DM (the "mpim" conversation type — a private
multi-person conversation, distinct from a 1:1 DM and from a private channel, even though both can
share the same `G`-prefixed channel-id shape) as its own always-allow-able category, rather than
requiring each group's ID to be individually allowlisted under `approved_channel`. Channel type
isn't derivable from the ID alone, so `slack_get_channel_history`/`slack_get_thread_replies` resolve
it via a cached `conversations.info` lookup (`SlackClient.resolve_is_group_dm()`) before the call
reaches the gate — `slack_search_messages` isn't covered, since resolving channel type for every
distinct channel across a search's results would need one lookup per channel per search.

### Google Calendar

| Tool | Always allow rule created |
|---|---|
| `calendar_get_event_details` | `i_am_organizer` (you organize it), else `no_external_attendees` (every attendee shares your domain), else `non_private_event` (event isn't marked private) |

> 📋 **Proposed design change:** similarly to the Drive proposal above, make the
> `i_am_organizer` / `no_external_attendees` / `non_private_event` priority order
> user-configurable instead of fixed — e.g. let a user require `no_external_attendees` even when
> they're the organizer, rather than `i_am_organizer` always winning outright. Same UI-change
> requirement as the Drive proposal.

### Telegram

| Tool | Always allow rule created |
|---|---|
| `telegram_get_messages` | `approved_chats` (that chat) |
| `telegram_search_messages` | `approved_chats_all_results` (union of chats across every result — shares the `telegram.read_chat_messages` operation key with `telegram_get_messages`; see [Conditional gating for search tools](#conditional-gating-for-search-tools) below) |

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

> 📋 **Proposed design change:** same as Drive/Calendar above — make the `i_am_reporter` /
> `i_am_assignee` / `approved_project_keys` priority order user-configurable, e.g. letting a user
> require `approved_project_keys` even when they're the reporter or assignee, instead of one
> criterion always taking priority over the other two.

### Confluence

| Tool | Always allow rule created |
|---|---|
| `confluence_get_page` | `i_am_author` (you wrote it), else `approved_space_keys` (page's space) |
| `confluence_get_page_by_title` | `i_am_author`, else `approved_space_keys` |

> 📋 **Proposed design change:** same as Jira above — make the `i_am_author` / `approved_space_keys`
> priority order user-configurable rather than fixed.

> Google Contacts and Google Tasks have no `review`-gate tools at all — their only reads
> (`contacts_list`/`contacts_search`/`contacts_get`, `tasks_list_task_lists`/`tasks_list_tasks`/
> `tasks_get_task`) are unconditionally `auto`, so neither connector has a Read tools table here.

---

### Conditional gating for search tools

**Shipped.** `slack_search_messages`/`telegram_search_messages` used to be **ungatable by any rule
at all**, even ones already configured — this section is the retrospective on that bug and the fix.

#### The bug: search results carried no single "the channel/chat" arg

Every other read tool passes the one resource it's reading as an arg —
`slack_get_channel_history`/`slack_get_thread_replies` pass `channel_id`
([`slack.py`](../src/privacyfence/connectors/slack.py)'s `_get_channel_history`/
`_get_thread_replies`), `telegram_get_messages` passes `chat_id`
([`telegram.py`](../src/privacyfence/connectors/telegram.py)'s `_get_messages`). The rules that
gate them — `approved_channel`, `dm_with_myself`, `approved_chats` — are all `ARGS_ONLY_RULES` in
[`auto_accept.py`](../src/privacyfence/auto_accept.py): they read `ctx.args.get("channel_id", "")` /
`ctx.args.get("chat_id", "")` and check it against the allowlist.

`slack_search_messages` and `telegram_search_messages` don't have "the channel/chat" — a search
spans however many the query matches — so both connectors call `gated_call(..., args={"query":
query})` with **no channel/chat id in args at all** (`slack.py::_search_messages`,
`telegram.py::_search_messages`). Plugged into the old rules, `_rule_approved_channel`/
`_rule_dm_with_myself`/`_rule_approved_chats` all read an empty string out of args for a search
call, so **a rule you'd already configured, by hand or via a grant, for a channel/chat a search
result actually came from still never fired** — every search call was unconditionally gated,
independent of `auto_accept_rules` content. Not a deliberate stricter policy, a plumbing gap.

#### The fix: a per-result rule, evaluated against every match

Three rules already evaluated a whole list of results at once instead of a single arg —
`_rule_public_channels_only`, `_rule_no_file_attachments` (Slack), and `_rule_no_media_attachments`
(Telegram) all follow the same `items = raw if isinstance(raw, list) else [raw]; return
all(<condition>(m) for m in items)` shape. `should_auto_accept()` only ever needed a rule that
returns `True`/`False` to bypass or gate the popup — no new gate machinery, just a rule of that same
shape checking each result's own channel/chat identity:

```python
def _rule_approved_channel_all_results(self, value, ctx):
    if not value:
        return False
    allowed = set(value if isinstance(value, list) else [value])
    items = ctx.raw_data if isinstance(ctx.raw_data, list) else [ctx.raw_data]
    return bool(items) and all(getattr(m, "channel_id", None) in allowed for m in items)
```

(and the `chat_id` mirror, `_rule_approved_chats_all_results`, for Telegram) — both
`DATA_DEPENDENT_RULES`, alongside `public_channels_only`, a **new, separate rule name** rather than
a change to `_rule_approved_channel`/`_rule_approved_chats` in place, so the existing single-channel
case keeps its `ARGS_ONLY_RULES` classification and `preflight_from_args()` behavior unchanged. This
means, as designed:

1. Every result in an approved channel/chat → `all(...)` is `True` → auto-accepted, no popup.
2. A partial match (some results approved, one not) → `all(...)` is `False` → gated for the *entire*
   call, not just the unapproved result — PrivacyFence doesn't do partial/per-item filtering within
   one tool response, and this preserves that invariant rather than splitting one search result into
   an approved and a gated half.
3. No results in an approved channel/chat → gated, same as before.

`suggest_rule()` gained a branch for each so the popup's own Always allow button proposes the
*union* of channel/chat ids present across the current search's results, the same way
`approved_folder`'s suggestion is the file's own `parent_ids`.

#### Telegram's operation-key merge

`telegram_search_messages` used to map to its own `telegram.search_messages` operation key, separate
from `telegram_get_messages`'s `telegram.read_chat_messages` — so even a fixed rule would've needed
configuring twice, and `resource_grants.py`'s chat grant only ever compiled its `read` capability
onto `telegram.read_chat_messages`, despite [TECHNICAL_REFERENCE.md](TECHNICAL_REFERENCE.md)'s
grants table describing that capability as "reading/searching that chat" — the "searching" half
wasn't actually wired up. Fixed by merging `telegram_search_messages` onto
`telegram.read_chat_messages` in `TOOL_TO_OPERATION`, matching Slack's already-shared
`slack.read_messages` pattern — one "trusted chats" rule or grant now covers both Telegram read
tools. Renaming a live `settings.yaml` key needed a one-time migration:
`auto_accept.migrate_telegram_search_operation_key()` (mirroring
`resource_grants.migrate_rules_to_grants()`'s own marker/idempotency shape), run once from
`daemon_main.py::run_app()` alongside the existing grants migration.

The Slack/Telegram chat-grant `read` capabilities in `resource_grants.py` were also each extended
with a second target — `approved_channel_all_results`/`approved_chats_all_results` on the same
operation key — so a single "Trusted Channel"/"Trusted Chat" grant compiles *both* the single-item
and all-results rule entries; enabling "read" on a trusted channel/chat now covers direct reads and
searches of it alike, without a separate toggle.

---

## Write tools

None of these ever offer **Always allow** — that button doesn't exist on a write's popup, and as of
[the temp-accept redesign](#related-but-distinct-mechanisms) there's no third button of any kind on
a write popup anymore, full stop. Every write tool below offers exactly Deny / Allow once, so the
**Always allow rule created** column is empty for every row in this section — kept only so every
table in this doc shares the same column, not because any write tool has one. A handful of these
tools have a separate, non-persisted grace-window behavior tucked into their "Allow once" instead —
see [Related but distinct mechanisms](#related-but-distinct-mechanisms) for what that is; it isn't
an Always-allow rule and doesn't belong in this column.

> 📋 **Cross-cutting finding, checked against `menu_bar.py`'s `RULES_BY_OPERATION`:** most of the
> proposals below aren't missing rules — the rule already exists and is already configurable today,
> from **Manage Auto-accept Rules…** in the menu bar (either as a plain filter or as a resource
> grant). The actual, common gap is that the write popup itself never offers a one-click shortcut to
> create one on the spot, by design (`gate.py`'s module docstring: silently auto-accepting a write is
> a materially bigger blast radius than a read, so that shortcut was deliberately left off every
> write popup, not just the ones below). Marked **✅ already supported** below wherever that's the
> case, vs. **🆕 genuinely new** where the rule doesn't exist yet at all. Every **✅ already
> supported** item below now also carries its own **📋 proposed design change**: surface *creating*
> that already-existing rule from the approval window itself, the same Always-allow-style flow reads
> already get, instead of leaving the only path through the menu bar's Filters/grant editor.

### Gmail

| Tool | Always allow rule created |
|---|---|
| `gmail_create_draft` | |
| `gmail_reply_draft` | |
| `gmail_reply_all_draft` | |
| `gmail_add_label` | |
| `gmail_remove_label` | |
| `gmail_archive_message` | |
| `gmail_create_filter` | |
| `gmail_update_filter` | |
| `gmail_create_label` | |

> ✅ **Shipped — drafts:** a plain, unconditional `always_allow` rule (a yes/no toggle, no recipient
> check at all) now exists for `gmail_create_draft`/`gmail_reply_draft`/`gmail_reply_all_draft`
> (`gmail.create_draft`), configurable from **Manage Auto-accept Rules… → Gmail → Filters**. It's
> deliberately broader than `to_is_myself`/`approved_recipient_domain` (both *conditional* on who
> the draft goes to) — closer in spirit to "Claude drafting is fine, I always review before it sends
> anyway" (a draft never sends itself). Popup-time creation (an Always-allow-style button on the
> draft popup itself) is still a separate, not-yet-built proposal — see the cross-cutting note
> above; `always_allow` is deliberately excluded from that mechanism since it has no resource
> identity to scope to.
>
> ✅ **Already supported — add/remove label:** `label_name_allowlist` already exists for both
> `gmail.add_label` and `gmail.remove_label` (`menu_bar.py`'s `RULES_BY_OPERATION`), i.e. "auto
> accept specific labels" can already be configured today from **Manage Auto-accept Rules… → Gmail →
> Filters**. The only gap is the one in the cross-cutting note above: no shortcut to add it from the
> write popup itself.
>
> 📋 **Proposed design change:** it exists, but surface *creating* it from the approval window
> itself — an Always-allow-style flow on the `gmail_add_label`/`gmail_remove_label` write popup
> (propose `label_name_allowlist` scoped to the label just clicked, confirmed the same way
> `show_rule_confirmation_popup` already confirms a read's Always allow), not only from the menu
> bar days later.

### Google Drive (incl. Sheets and Docs)

| Tool | Always allow rule created |
|---|---|
| `drive_write_file_content` | |
| `drive_upload_file` | |
| `drive_write_doc_content` | |
| `drive_move_file` | |
| `drive_add_comment` | |
| `drive_sheets_write_range` | |
| `drive_sheets_add_sheet` | |
| `drive_sheets_rename_sheet` | |
| `drive_sheets_format_range` | |
| `drive_sheets_insert_dimensions` | |
| `drive_sheets_delete_dimensions` | |
| `drive_docs_edit_content` | |
| `drive_docs_format_content` | |

> ✅ **Shipped — parent folder treated as an approved sandbox folder everywhere:** coverage used to
> be inconsistent per tool — `drive_write_file_content`/`drive_write_doc_content`/all six
> `drive_sheets_*` writes/`drive_docs_edit_content`/`drive_docs_format_content` had
> `approved_sandbox_folder` (grant-managed), `drive_upload_file` had its own, separate,
> non-grant-managed `parent_folder_allowlist` (checking the upload's *destination* folder, an arg,
> since the file doesn't exist yet), `drive_move_file` had its own `move_within_approved_folders`
> (checking the file's *current* parent folder, the same way `approved_folder`/
> `approved_sandbox_folder` do — **not** the move's destination, despite the name), and
> `drive_add_comment` had no folder-based rule at all. All three are now targets of the same
> `sandbox_folders` grant's `write` capability (`resource_grants.py`) — one trusted folder covers
> writing into it, uploading into it, commenting on a file already there, and moving a file out of
> it, all from a single grant. `parent_folder_allowlist`/`move_within_approved_folders` keep their
> own rule names (their underlying checks are genuinely different from `approved_sandbox_folder`'s),
> just compiled from the same grant now, alongside it.

### Slack

| Tool | Always allow rule created |
|---|---|
| `slack_send_message` | |

### Google Calendar

| Tool | Always allow rule created |
|---|---|
| `calendar_create_event` | |
| `calendar_update_event` | |
| `calendar_create_out_of_office` | |
| `calendar_set_working_location` | |
| `calendar_set_event_visibility` | |

> Status is split down the middle:
>
> - ✅ **Already supported** for `calendar_create_event`/`calendar_update_event` (`calendar.
>   create_modify_event`) and `calendar_set_event_visibility` (`calendar.set_visibility`) —
>   `personal_calendar` already exists for both operation keys and is grant-managed (see
>   [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants) → `calendar.calendars`). Same
>   popup-exposure gap as everywhere else in this section.
>   📋 **Proposed design change:** it exists, but surface *creating* it from the approval window
>   itself — an Always-allow-style flow on these three write popups (propose `personal_calendar`
>   scoped to the event's own calendar), not only from the menu bar. Not yet built.
> - ✅ **Shipped** for `calendar_create_out_of_office`/`calendar_set_working_location`: neither tool
>   even takes a `calendar_id` (`calendar.py`'s `_create_out_of_office`/`_set_working_location` —
>   both always act on your own primary calendar), so `personal_calendar` had nothing to check
>   against here — genuinely new. Both now support the same unconditional `always_allow` toggle
>   shipped for Gmail drafts, configurable from **Manage Auto-accept Rules… → Calendar → Filters**.

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
| `jira_create_issue` | |
| `jira_add_comment` | |
| `jira_update_issue` | |
| `jira_transition_issue` | |

> ✅ **Already supported — allow project:** Jira doesn't have "spaces" (that's Confluence — see
> below); its equivalent is a **project**, and `approved_project_keys` already exists for all four
> write tools above, grant-managed via `jira.projects` (see
> [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants)) — one project grant's
> `create`/`comment`/`update`/`transition` capabilities already cover all four at once. Same
> popup-exposure gap as everywhere else in this section.
>
> 📋 **Proposed design change:** it exists, but surface *creating* it from the approval window
> itself — an Always-allow-style flow on all four Jira write popups (propose `approved_project_keys`
> scoped to the issue's own project), not only from the menu bar.

### Confluence

| Tool | Always allow rule created |
|---|---|
| `confluence_create_page` | |
| `confluence_update_page` | |

> ✅ **Already supported — allow space:** `approved_space_keys` already exists for both write tools
> above, grant-managed via `confluence.spaces` — one space grant's `create`/`update` capabilities
> cover both. Same popup-exposure gap as everywhere else in this section.
>
> 📋 **Proposed design change:** it exists, but surface *creating* it from the approval window
> itself — an Always-allow-style flow on both Confluence write popups (propose `approved_space_keys`
> scoped to the page's own space), not only from the menu bar.

### Google Tasks

| Tool | Always allow rule created |
|---|---|
| `tasks_create_task` | |
| `tasks_update_task` | |
| `tasks_complete_task` | |
| `tasks_uncomplete_task` | |
| `tasks_move_task` | |

> ✅ **Already supported — allow task list:** `approved_task_list` already exists for all five write
> tools above, grant-managed via `tasks.task_lists` — one task-list grant's
> `create`/`edit`/`complete`/`move` capabilities cover all five at once (`complete` covers both
> `tasks_complete_task` and `tasks_uncomplete_task`). Same popup-exposure gap as everywhere else in
> this section.
>
> 📋 **Proposed design change:** it exists, but surface *creating* it from the approval window
> itself — an Always-allow-style flow on all five Tasks write popups (propose `approved_task_list`
> scoped to the task's own list), not only from the menu bar.

> `drive_create_blank_file` and `drive_sheets_create` are writes too, but both are unconditionally
> `auto` — an empty file/spreadsheet with no content yet carries no disclosure risk — so, per the
> rule above, they're omitted from this table rather than shown with two empty columns.

---

## Related but distinct mechanisms

These are easy to conflate with Always allow because they sit in the same popups or touch the same
config, but none of them are the "Always allow" button covered above.

**Temp-accept grace window** (formerly a separate "Allow for 5 min" button — redesigned in commit
`b47d777`, "Fold 'Allow for 5 min' into Allow once, disclosed via caption not a button") — an
in-memory, non-persisted acceptance for six `popup`-gate writes expected to
fire repeatedly against the same file in a burst (`drive_sheets_write_range`,
`drive_sheets_format_range`, `drive_sheets_insert_dimensions`, `drive_add_comment`,
`drive_docs_edit_content`, `drive_docs_format_content` — `auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS`),
scoped to one file/spreadsheet for 5 minutes and gone on daemon restart. **There is no longer a
separate button for it.** The popup for these six tools now shows only Deny / Allow once, same as
every other write, with a plain disclosure caption above the buttons explaining that Allow once
here also arms the grace window; clicking Allow once both approves this call and silently arms it
(`gate.py`'s popup-gate branch, `decision == "accept"` path). The underlying mechanism is unchanged
— same six operations, same 5-minute TTL, same in-memory-only scope, same audit decision codes
(`accepted_via_temp_session` / `session_temp_accept`) — only the UI surface changed: a duration is
no longer something the user picks up front via a distinct control. Deliberately *not* offered on
`drive_sheets_delete_dimensions` (no undo path) or on `drive_sheets_add_sheet`/
`drive_sheets_rename_sheet` (one-shot per file, not called in a burst) — those still get a plain
Deny/Allow once with no caption at all.

**Bridge-proposed rule/grant changes** (`privacyfence_propose_auto_accept_rule_change`) — lets Claude
itself propose adding/updating/removing an `auto_accept_rules` or `auto_accept_grants` entry for
*any* operation, including write tools that never get an Always-allow button on their own popup.
Every call still blocks on the same confirmation dialog Always allow uses
(`show_rule_confirmation_popup`) — there's no way for a rule to land without a human confirming it —
so writes can end up with a standing rule this way even though the popup itself never offers one
directly. See [Reading and proposing auto-accept changes from the bridge](TECHNICAL_REFERENCE.md#reading-and-proposing-auto-accept-changes-from-the-bridge).

**Auto-accept grants** (`auto_accept_grants` in `settings.yaml`, and the menu bar's **Manage
Auto-accept Rules… → Trusted \*** submenus) — the resource-scoped alternative to a narrow
`auto_accept_rules` entry: grant one folder/channel/project/etc. once and it covers every operation
key that resource touches. Always allow never writes here directly; you set these up separately. See
[Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants).
