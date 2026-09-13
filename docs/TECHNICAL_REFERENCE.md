# PrivacyFence technical reference

PrivacyFence is a local or organization-hosted MCP privacy gateway. It sits between an MCP client and third-party data providers, applies policy and approval gates, and records auditable decisions.

## Runtime architecture

A PrivacyFence daemon owns connector clients, policy evaluation, approval state, the audit log, and the embedded HTTP application.

The embedded web application serves:

- the approval list and approval cards;
- settings and connector authorization surfaces;
- state/event streams used by the web UI;
- the `/mcp` Streamable HTTP endpoint;
- org-mode identity/authorization routes when org mode is enabled;
- short-lived staged downloads when org-mode delivery requires them.

Claude Code and other HTTP-capable MCP clients can connect to `/mcp` directly. Claude Desktop uses the bundled Node/TypeScript shim in `mcpb/shim/`, which reads the daemon discovery/auth files and proxies stdio MCP traffic to the daemon's HTTP endpoint.

There is no native AppKit approval/settings runtime. The browser-based embedded UI is the approval/settings surface on every supported platform.

## Modes

### Local mode

Local mode represents one user/principal for the life of the daemon. Connector credentials and policy are resolved for that local user. The web server binds locally and uses a bootstrap/session mechanism for the human approval/settings UI plus bearer-token protection for MCP.

### Org mode

Org mode is a centralized Linux/server deployment. Human identity is established through the configured OIDC identity provider, MCP authorization is handled by PrivacyFence's org authorization flow, and connectors are scoped per principal.

`ConnectorRegistry` lazily builds and caches one connector host per authenticated principal, with bounded capacity and idle eviction. User-scoped state paths are resolved inside the active principal scope.

Org mode is normally deployed behind the configured HTTPS reverse proxy. See [`org-mode-setup-guide.md`](org-mode-setup-guide.md) and [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## Local state and discovery

`src/privacyfence/paths.py` is the source of truth for application paths.

A source/unbundled development run keeps its normal configuration, credentials, and logs in the repository-local development locations. A bundled release keeps user state under the user's PrivacyFence home directory.

MCP discovery/auth files under the user's PrivacyFence home include the daemon MCP URL and token so the Desktop shim can find the running daemon independently of the checkout/install location.

The daemon enforces a single-instance lock with `portalocker`.

## MCP endpoint

The daemon exposes the MCP protocol over Streamable HTTP at `/mcp` using the official MCP Python SDK. Local mode protects MCP with the generated bearer token. Org mode uses its own authorization/identity path and principal-aware request handling.

The MCP tool registry is built from the configured connectors. Tool calls are routed through the PrivacyFence gate before connector execution where policy requires review or confirmation.

## Meta-tools

Alongside the connector-derived tools, the daemon exposes six `privacyfence_`-prefixed meta-tools over the same `/mcp` endpoint (`web/mcp_tools.py`'s `META_TOOLS`), dispatched by `routes_mcp.py`'s `_dispatch_meta_tool` to `McpDispatcher` methods (`web/mcp_dispatch.py`) that call back into `gate.py`/`auto_accept.py`. Each takes a `reason` string, logged the same self-reported, unverified way as every gated connector tool's own `reason` param.

- `privacyfence_check_policy` — asks whether a specific `(connector, tool, args)` call would auto-accept or need a human, without making the call or having any side effects. Returns one of `auto_accept`, `requires_review`, or `unknown` (whether it auto-accepts can depend on fetched content this can't see in advance); for `review`-gated tools, `pii_gate_may_apply` is always `true`, since the PII gate scans real content and can never be predicted ahead of time. Safe to call as often as needed while planning a task.
- `privacyfence_list_auto_accept_rules` — read-only listing of the current `auto_accept_rules` and `auto_accept_grants` from `settings.yaml`. Call this before `privacyfence_propose_auto_accept_rule_change` so an update/remove targets an entry that actually exists rather than a guessed identifier.
- `privacyfence_propose_auto_accept_rule_change` — proposes adding, updating, or removing a rule (`target: "rule"`) or a resource-scoped grant (`target: "grant"`). Always blocks on a native confirmation dialog a human must approve — there is no way to change this config without one, even for an entry that already exists — and throws if declined, or outright if the connection is in an unattended session.
- `privacyfence_begin_unattended_session` / `privacyfence_end_unattended_session` — see "Scheduled / unattended Cowork tasks" below.
- `privacyfence_await_approval` — long-polls one or more `approval_id`s from a gated call's `{status: "approval_pending", approval_id, ...}` result and reports status only (`pending`, `approved`, `denied`, `expired`, or `unknown`), never content — a re-issue of the original gated call with the same arguments is still what actually retrieves data once `approved`.

Every tool advertised over `/mcp`, meta-tools included, carries the same uniform read-only/non-destructive/idempotent annotations regardless of its real effect (`_UNIFORM_READ_ONLY_ANNOTATIONS` in `web/mcp_tools.py`) — those are MCP UI hints, not a security boundary. The real authorization is the gate itself, enforced here in the daemon.

### Scheduled / unattended Cowork tasks

`privacyfence_begin_unattended_session` tells PrivacyFence that the rest of this MCP connection is a scheduled/unattended run — a Cowork Routine firing on a schedule with no human necessarily watching — rather than an interactive conversation. It errors unless an administrator has opted the install into this: `unattended_sessions.enabled` in `org/org_config.json`, a deliberate per-organization setting, not a per-user one.

Once set, the flag is tracked per MCP session (`McpDispatcher._unattended_sessions`) and read by `gate.py`'s `is_unattended()` through every gated call on that connection. It changes exactly one thing: a call that isn't already covered by a configured auto-accept rule is denied immediately (audited as `denied_unattended`) instead of PrivacyFence opening a native approval dialog nobody is there to answer. It never changes what auto-accepts, only what happens when nothing does. `privacyfence_propose_auto_accept_rule_change` is likewise refused outright in an unattended session, since a config change always requires a human confirmation.

`privacyfence_end_unattended_session` clears the flag, restoring normal interactive approval behavior — not strictly required, since it also clears when the connection closes, but useful if the connection might be reused afterward for something interactive. Pairing `privacyfence_check_policy` with a scheduled run lets it plan around steps that would otherwise need a human who isn't there.

## Approval model

A gated request becomes a `PendingApproval` managed by `PendingApprovalRegistry`.

The approval list at `/approvals` can contain multiple pending requests. Each row exposes **Deny** and **Review**; there is deliberately no one-click Allow on the list. Review opens the full approval card, where the user can inspect the operation and make the decision.

Pending approvals update in the browser through the state/event stream. Decisions are idempotent: an approval that is no longer pending cannot be approved again as a fresh request.

Approval content is built by `approval_window_html.py` and confirmation content by `dialog_window_html.py`; the web routes inject the browser decision bridge and enforce the current session/CSRF/CSP controls. See [`approval-window-content-reference.md`](approval-window-content-reference.md).

## Gate behavior

Connector tools declare their gate behavior through the shared connector/tool machinery. The important policy outcomes are:

- **auto** — the operation can run without a human decision under the current policy;
- **review** — PrivacyFence shows the read/retrieval operation before releasing protected data;
- **popup/write confirmation** — PrivacyFence requires an explicit decision before a write or other sensitive action;
- **PII confirmation** — detected sensitive content can require an additional confirmation before release/delivery.

Always-allow rules can bypass a matching future approval only within the rule shape and scope the user approved. See [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

## Connectors & privacy matrix

This section lists preview/details text per tool, grouped by connector. For a cut across *what
Claude already knows from prior auto-approved calls* before it ever reaches a given gated tool —
i.e. how much of a "review" tool's return value is actually new information — see
[`claude-knowledge-boundary.md`](claude-knowledge-boundary.md). For the approval dialog's own
layout and optional sections (AI-visibility checklist, PII banner, etc.), see
[`approval-window-content-reference.md`](approval-window-content-reference.md).

### Gmail

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `gmail_list_messages` | read | auto | — | — |
| `gmail_list_threads` | read | auto | — | — |
| `gmail_get_message` | read | review | from, recipients, date, subject | Full body text |
| `gmail_get_thread` | read | review | subject, all participants, message count, date range | All messages in thread |
| `gmail_list_message_attachments` | read | auto | — | — |
| `gmail_download_attachment` | read | review | from, subject, attachment name, size, save path | — |
| `gmail_create_draft` | write | popup | — | To, cc, subject, full body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_draft` | write | popup | — | In reply to, to, cc/bcc, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_all_draft` | write | popup | — | In reply to, to, also-to (expanded participants), cc/bcc, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_create_draft_with_attachments` | write | popup | — | To, cc, subject, attachment names/sizes, full body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_draft_with_attachments` | write | popup | — | In reply to, to, cc/bcc, attachment names/sizes, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_reply_all_draft_with_attachments` | write | popup | — | In reply to, to, also-to (expanded participants), cc/bcc, attachment names/sizes, full reply body (or Markdown source, if `body_markdown` given) |
| `gmail_add_label` | write | popup | — | From, subject, label name |
| `gmail_remove_label` | write | popup | — | From, subject, label name |
| `gmail_archive_message` | write | popup | — | From, subject, confirmation that message stays in All Mail |
| `gmail_list_filters` | read | auto | — | — |
| `gmail_list_labels` | read | auto | — | — |
| `gmail_create_filter` | write | popup | — | Criteria, actions |
| `gmail_update_filter` | write | popup | — | Filter ID, criteria, actions, note that this deletes + recreates under a new id |
| `gmail_create_label` | write | popup | — | Label name, note when a parent segment will also be created |

### Google Drive

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `drive_list_files` | read | auto | — | — |
| `drive_get_file_metadata` | read | auto | — | — |
| `drive_list_folder` | read | auto | — | — |
| `drive_list_shared_drives` | read | auto | — | — |
| `drive_create_blank_file` | write | auto | — | — |
| `drive_get_file_content` | read | review | file name, owner, size, modified date | First ~500 chars of content (a Google Doc renders as Markdown — headings, bold/italic/strikethrough/underline/code/link/highlight, horizontal-rule dividers, nested lists, real GFM tables; a non-default highlight/text color also adds `highlights`/`text_colors`) |
| `drive_download_file` | read | review | file name, owner, size, save path | File name, owner, size, modified date, save path |
| `drive_write_file_content` | write | popup | — | File name, owner, new content (plain text) |
| `drive_upload_file` | write | popup | — | File name, size, destination folder |
| `drive_write_doc_content` | write | popup | — | File name, owner, Markdown preview (headings, bold/italic/strikethrough/underline/code, ==highlight== (styles nest, e.g. bold+highlight together), links, nested lists, a `---`/`***`/`___` horizontal-rule divider, tables rendered as rich formatting in the Google Doc) |
| `drive_docs_edit_content` | write | popup | — | File name, owner; find/replace text goes in the details pane, not the preview |
| `drive_docs_format_content` | write | popup | — | File name, owner, formatting summary; the located text goes in the details pane |
| `drive_move_file` | write | popup | — | File name, from folder → to folder |
| `drive_add_comment` | write | popup | — | File name, full comment text |
| `drive_sheets_create` | write | auto | — | — |
| `drive_sheets_get_metadata` | read | auto | — | — |
| `drive_sheets_get_values` | read | review | spreadsheet name, owner, range | Cell values (or formulas, or +formatting) in the range |
| `drive_sheets_write_range` | write | popup | — | Spreadsheet name, owner, range, values/formulas being written |
| `drive_sheets_add_sheet` | write | popup | — | Spreadsheet name, owner, new tab title/dimensions |
| `drive_sheets_rename_sheet` | write | popup | — | Spreadsheet name, owner, tab id, new title |
| `drive_sheets_format_range` | write | popup | — | Spreadsheet name, owner, range, formatting being applied |
| `drive_sheets_insert_dimensions` | write | popup | — | Spreadsheet name, owner, tab id, rows/columns being inserted |
| `drive_sheets_delete_dimensions` | write | popup | — | Spreadsheet name, owner, tab id, rows/columns being deleted (data-loss warning) |

Google Sheets is not a separate connector — the `drive_sheets_*` tools live on the Drive
connector and reuse its OAuth grant (the Sheets API accepts the same `drive` scope). There is
intentionally no delete-sheet tool: `drive_sheets_rename_sheet` is the sanctioned way to mark a
tab for removal (e.g. rename it to `TO BE DELETED - <original title>`) — you delete it by hand
in the Sheets UI. `drive_sheets_write_range` has no separate "set formula" tool either — a cell
string starting with `=` is evaluated as a formula, exactly like typing it into the Sheets UI.

`drive_sheets_get_values` defaults to displayed cell values (`value_render_option=FORMATTED_VALUE`,
e.g. `"$1.00"`); pass `UNFORMATTED_VALUE` for the raw underlying value (e.g. `1`) or `FORMULA` to
read formula text (e.g. `"=A1+A2"`) instead of its computed result — there's no separate
"get formulas" tool, same reasoning as writing them. `include_formatting=true` additionally fetches
a same-shaped grid of per-cell formatting (bold/italic/text color/background color/number
format/horizontal/vertical alignment/text wrap — the same aspects `drive_sheets_format_range` can
set), returned alongside the values as `{"values": [...], "formatting": [...]}` instead of a bare
values grid. Formatting doesn't carry the same PII risk as cell text, so it isn't run through the
privacy filter/PII scan the way values are.

`drive_docs_edit_content` and `drive_docs_format_content` locate existing text in a Google Doc by
exact match against its **plain, unformatted** text — `find_text` must match exactly one location
unless `replace_all` is set, so an ambiguous match raises rather than guessing which occurrence was
meant. This is *not* the same representation `drive_get_file_content` returns for a Google Doc —
that one renders Markdown (`**bold**`, `# Heading`, etc.), so `find_text` for these two tools must
be a substring of the doc's real words, with any Markdown formatting markers
`drive_get_file_content` added stripped back out first (e.g. search for `bold text`, not
`**bold text**`). Unlike `drive_write_doc_content`, they
touch only the matched span, not the whole document. `drive_sheets_insert_dimensions`/
`drive_sheets_delete_dimensions` insert or remove whole rows/columns (not just cell content) in a
tab, shifting everything after the insertion/deletion point; there is no undo path through
PrivacyFence for a delete.

### Slack

**Auth:** OAuth2 (browser sign-in), user token scope. Sees exactly what you see — no bot to invite. See [slack-setup.md](slack-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `slack_list_channels` | read | auto | — | — (optional `participant` filter matches channel membership by user id, handle, or display name, comma-separated for multiple; one extra `conversations.members` call per channel, plus one `users.info` call per unresolved member if no needle matches by id alone) |
| `slack_list_dms` | read | auto | — | — (each entry is `id` + the other participant; optional `participant` filter matches by user id, handle, or display name) |
| `slack_list_group_chats` | read | auto | — | — (each entry is `id`, `name`, and resolved `member_ids`/`member_names`; optional `participant` filter matches by user id, handle, or display name, comma-separated to require all of them as members of the same group chat; one extra `conversations.members` call per group chat) |
| `slack_resolve_permalink` | read | auto | — | — (parses a pasted Slack message permalink into `channel_id`/`channel_name`/`ts`/`thread_ts`, ready for `slack_get_channel_history`/`slack_get_thread_replies`; no Slack API call beyond a best-effort channel-name lookup) |
| `slack_refresh_user_cache` | read | auto | — | — (forces an immediate `users.list` re-sync of the on-disk, weekly-refreshed user name/email cache that `slack_get_channel_history`/`slack_get_thread_replies`/`slack_search_messages` use to resolve message authors without a per-message `users.info` call; call after a teammate joins mid-week so they resolve correctly before the next automatic refresh) |
| `slack_refresh_channel_cache` | read | auto | — | — (forces an immediate re-sync of the on-disk, weekly-refreshed channel/DM/group-DM name cache those same read tools use to resolve which conversation a message belongs to without a per-message `conversations.info` call; call after a new channel is created so it resolves by name right away. On a workspace with enough channels that one call can't finish before the calling MCP client's own tool-call timeout, the result's `has_more` flag comes back `true` — call the tool again to resume from where it left off) |
| `slack_get_channel_history` | read | review | channel name, message count, first message (80 chars) | All messages |
| `slack_get_thread_replies` | read | review | channel name, thread starter (80 chars), reply count | All replies |
| `slack_search_messages` | read | review | query and/or participant, result count | All results |
| `slack_create_group_chat` | write | popup | — | Resolved participant names (or raw user ids when unresolvable); returns the new/reopened conversation's `id`, ready for `slack_send_message` |
| `slack_send_message` | write | popup | — | Channel name, full message text (optional `mark_unread=true` leaves the message unread after sending; requires `mark` scope) |

`slack_list_group_chats` only lists group DMs that already exist — there's nothing to list until one
has been opened at least once. To start a brand-new group chat, call `slack_create_group_chat` with
2+ participant user ids (from `slack_list_dms`/`slack_list_group_chats`, or a message's `user_id`
field — it does not resolve email addresses or handles), then pass the returned `id` to
`slack_send_message` as `channel_id`.

`slack_list_channels`/`slack_list_group_chats` both accept the same comma-separated `participant`
matching (user id, handle, or display name; comma-separated to require all of them as members of the
same channel/chat) — the tool to reach for when a lookup is "the group chat/channel with Alice and
Bob", as opposed to `slack_search_messages`'s participant matching below, which is for message
content once the right conversation is already known.

`slack_search_messages` accepts an optional `participant` (user id, handle, or display name;
comma-separated to require a group chat containing all of them) alongside or instead of `query`.
When given, it skips Slack's own search index — whose `from:`/`in:` modifiers need exact handle
syntax and don't reliably index every message — and instead reads the matching DM/group-chat
conversation(s) directly via the same matching `slack_list_dms`/`slack_list_group_chats` use,
optionally narrowed by `query` as a client-side text filter. Prefer `participant` over a
text-only `query` for "messages from Bob" or "messages with Bob and Jane" style lookups.

`slack_resolve_permalink` decodes a message permalink (Slack's "Copy link" on any message) into the
`channel_id`/`ts` (and, for a link to a threaded reply, the thread root's `thread_ts`) that
`slack_get_channel_history`/`slack_get_thread_replies` need — the permalink already carries both, so
this needs no `slack_list_channels`/`slack_search_messages` call at all to resolve a link a human
pasted directly.

`slack_refresh_user_cache`/`slack_refresh_channel_cache` refresh the on-disk snapshots that
`get_user_info`/`resolve_channel_name`/`resolve_is_group_dm` check before falling back to a live,
per-item Slack call — without them, resolving every message author/channel in a `slack_search_messages`
result costs one `users.info` and one `conversations.info` call per unique sender/channel, every time.
Both snapshots refresh automatically about once a week — checked once the IPC server comes up on every
daemon restart (so a snapshot that went stale while the app was closed is caught then, not on whatever
tool call happens to run first), in the background so the refresh can't delay startup completing,
and, in between restarts, lazily on first use once seven days have passed. These two tools
exist purely for the exception: someone new (a hire, a channel) needs to resolve correctly *before* the
next automatic refresh. Neither tool reads any message content; both are auto-approved.
`slack_refresh_channel_cache` specifically bounds each call to a fixed number of `conversations.list`
pages so a large workspace can't run one call past the calling MCP client's own timeout — see the table
above for the `has_more`/resume behavior; the eager background refresh at startup isn't subject to this
bound, since it isn't racing anyone's timeout.

### Google Calendar

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `calendar_list_calendars` | read | auto | — | — |
| `calendar_list_events` | read | auto | — | — |
| `calendar_get_free_busy` | read | auto | — | — (returns full events when calendar access is available; falls back to busy-slot list otherwise — set `calendar.free_busy_full_event_details: false` in `settings.yaml` to always fall back regardless of access) |
| `calendar_list_rooms` | read | auto | — | — (lists meeting rooms — name, email, building, floor, capacity — from a static directory IT syncs into `org_config.json` via `scripts/sync_room_directory.py`; not a live lookup, so it may be empty until IT has synced one; the Calendar connector's own OAuth client never holds Workspace admin directory access) |
| `calendar_get_event_details` | read | review | title, time, organizer, attendee count | Description, full attendee list, conferencing link, file attachments (e.g. Gemini meeting notes/transcript) |
| `calendar_get_event_visibility` | read | auto | — | — |
| `calendar_create_event` | write | popup | — | Title, time, attendees, description, location, Google Meet flag, room bookings |
| `calendar_update_event` | write | popup | — | Title, time, fields changing (old → new), Google Meet flag, room bookings |
| `calendar_set_event_visibility` | write | popup | — | Event title, calendar, visibility change (old → new) |
| `calendar_create_out_of_office` | write | popup | — | Title, time, fixed "auto-decline new conflicts only" note, decline message |
| `calendar_set_working_location` | write | popup | — | Date, location (office/home), building/label if given |

`calendar_create_out_of_office` and `calendar_set_working_location` are only supported on the
primary calendar (a Google Calendar API restriction) and always create the event there regardless
of any `calendar_id` used elsewhere. The out-of-office auto-decline behavior is fixed to "decline
new conflicting invitations only" — Calendar also supports declining all conflicts or none, but
that isn't exposed here. Working-location presence only offers "office" or "home" (Calendar's third
"custom location" option isn't exposed either).

`calendar_get_event_visibility` returns just the `visibility` field ("default", "public",
"private", or "confidential") without the full attendee/description/attachment fetch
`calendar_get_event_details` does — cheap enough to be auto-approved on its own, the same way
`calendar_list_events` is. `calendar_set_event_visibility` changes only that one field; every other
property of the event is left untouched. There's no separate `calendar_create_event`/
`calendar_update_event` visibility parameter — set it via `calendar_set_event_visibility` after
creating or alongside updating the event.

### Google Contacts

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `contacts_list` | read | auto | — | — |
| `contacts_search` | read | auto | — | — |
| `contacts_get` | read | auto | — | — |
| `contacts_update` | write | popup | — | Contact name, fields changing (old → new) |
| `contacts_create` | write | popup | — | Name, fields being set |
| `contacts_add_label` | write | popup | — | Contact name, label (creates the label if it doesn't exist) |
| `contacts_remove_label` | write | popup | — | Contact name, label |

Contact deletion is not supported by this connector.

Google's People API blends personally-saved contacts together with Workspace
directory profiles (colleagues) into a single response by default. `contacts_list`,
`contacts_search`, and `contacts_get` each accept a `source` parameter
(`personal`, `directory`, or `both` — default `both`) to split them apart, and
every returned contact carries a `source` field (`personal`, `directory`, `both`
if it's a saved contact who's also a colleague, or `other` for unclassifiable
entries) plus the raw `source_types` it was derived from. `contacts_get` fails
if the fetched resource doesn't match the requested `source`. Directory search
(`contacts_search` with `source="directory"`) is limited to directory profiles
you already have some contact history with — there is no full company-directory
search under this connector's OAuth scope.

### Telegram

**Auth:** Telethon (MTProto). Reads your chats as you, not as a bot.

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `telegram_list_chats` | read | auto | — | — |
| `telegram_refresh_chat_cache` | read | auto | — | — (forces an immediate `get_dialogs` re-sync of the on-disk, weekly-refreshed chat/group/channel name cache that `telegram_get_messages`/`telegram_search_messages` use to resolve which chat a message belongs to; call after a new chat starts so it resolves by name right away) |
| `telegram_get_messages` | read | review | chat name, message count | All messages |
| `telegram_search_messages` | read | review | query, result count | All results |
| `telegram_send_message` | write | popup | — | Chat name, full message text |

`telegram_refresh_chat_cache` refreshes the on-disk snapshot that `get_chat_name`/`get_messages`/
`search_messages` check before falling back to a bare numeric chat id — without it, any chat not
already primed by a recent `telegram_list_chats` call (in particular, any chat surfaced only via
`telegram_search_messages`, or after a daemon restart) shows up unresolved. The snapshot refreshes
automatically about once a week — same as Slack's directory caches, checked once the IPC server comes
up on every daemon restart, in the background so a large account's re-sync can't delay startup
completing — and, in between restarts, lazily on first use once seven days have passed. This does
mean a daemon restart now connects to Telegram eagerly rather than waiting for the first Telegram tool
call, an accepted tradeoff now that the connection happens off the startup critical path. Reads no
message content; auto-approved.

### Salesforce

**Auth:** OAuth2 (browser sign-in via a Connected App). See [salesforce-setup.md](salesforce-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `salesforce_list_reports` | read | auto | — | — |
| `salesforce_get_record` | read | review | object type, record name, record ID | All field values |
| `salesforce_run_report` | read | review | report name, report ID | All report rows |
| `salesforce_search` | read | review | search term, object types, result count | One line per match: object type, name, id |

`salesforce_search` is the same mechanism (SOSL) behind the search bar at the top of the
Salesforce UI — search by name or id across one or more object types, optionally scoped to one
Account's related records (`account_id`, requires `object_types` to be set). Results are
lightweight Id/Name matches, not full records — call `salesforce_get_record` for full field
details on a match, the same search-then-drill-in split `jira_search_issues`/`jira_get_issue`
already use.

### Jira

**Auth:** OAuth2 (browser sign-in, Atlassian 3LO). Shared with Confluence — one sign-in covers both. See [atlassian-setup.md](atlassian-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `jira_list_projects` | read | auto | — | — |
| `jira_search_issues` | read | auto | — | — |
| `jira_get_issue` | read | review | project name, key, summary, status, assignee | Description, comments, all fields |
| `jira_get_transitions` | read | auto | — | — |
| `jira_create_issue` | write | popup | — | Project, type, summary, full description |
| `jira_add_comment` | write | popup | — | Issue key + summary, full comment |
| `jira_update_issue` | write | popup | — | Issue key + summary, fields (old → new), including custom fields |
| `jira_transition_issue` | write | popup | — | Issue key + summary, status (old → new) |

`jira_update_issue`'s `custom_fields` parameter takes a JSON object keyed by each custom field's
**display name** exactly as shown in the Jira UI (e.g. `{"Story Points": 5}`) — never the internal
`customfield_NNNNN` id. The connector resolves the name via Jira's field metadata and shapes the
value for select-list (single- and multi-option) fields automatically; fields needing a structured
reference the name alone can't supply (e.g. a user-picker field, which needs an `accountId`) are
passed through as-is and surface Jira's own validation error if the shape is wrong.
`jira_transition_issue` moves an issue by transition name (e.g. "Done") — call
`jira_get_transitions` first to see which names are valid from the issue's current status.

### Confluence

**Auth:** OAuth2 (browser sign-in, Atlassian 3LO), shared with Jira — one sign-in covers both. See [atlassian-setup.md](atlassian-setup.md).

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `confluence_list_spaces` | read | auto | — | — |
| `confluence_search` | read | auto | — | — |
| `confluence_cql_search` | read | auto | — | — |
| `confluence_list_pages` | read | auto | — | — |
| `confluence_get_page` | read | review | title, space, author, last modified | Full page body |
| `confluence_get_page_by_title` | read | review | title, space, author, last modified | Full page body |
| `confluence_list_attachments` | read | auto | — | — |
| `confluence_download_attachment` | read | review | title, space, attachment name, type, size, save path | — |
| `confluence_create_page` | write | popup | — | Space, title, parent page, full body |
| `confluence_update_page` | write | popup | — | Title, space, full new body |

### Google Tasks

**Auth:** OAuth2

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `tasks_list_task_lists` | read | auto | — | — |
| `tasks_list_tasks` | read | auto | — | — |
| `tasks_get_task` | read | auto | — | — |
| `tasks_create_task` | write | popup | — | Task list, title, due date, full notes |
| `tasks_update_task` | write | popup | — | Task list, task, new title/due date, full notes |
| `tasks_complete_task` | write | popup | — | Task list, task |
| `tasks_uncomplete_task` | write | popup | — | Task list, task |
| `tasks_move_task` | write | popup | — | Task, from list, to list |

### Apps Script

**Auth:** OAuth2. Reads/writes script *source* only — PrivacyFence never runs a script. There is
deliberately no execute/run tool: see issue #154's "Non-goals" and `apps_script_client.py`'s
module docstring. The user runs a script themselves in the Apps Script editor (or via its own
triggers), under their own Google account, through Apps Script's own separate consent screen —
untouched by PrivacyFence.

| Tool | Dir | Gate | Preview | Details popup |
|------|-----|------|----------------|---------------|
| `apps_script_list_projects` | read | auto | — | — |
| `apps_script_get_content` | read | review | project name, file count | Full source of every file |
| `apps_script_write_content` | write | popup | — | Project name, file names/types, full new source of every file |
| `apps_script_get_execution_log` | read | review | project name, execution count | Function, status, start time, duration per recent run |

`apps_script_get_execution_log` surfaces the result of a run the **user** triggered outside
PrivacyFence (via the Processes API's `listScriptProcesses`) — status/duration/which function ran,
not a live `console.log` transcript; see `apps_script_client.py`'s module docstring for why.
`apps_script_write_content` always replaces a project's entire file set (there is no
single-file/partial update in the underlying API), the same "show full resulting content, not a
diff" precedent `drive_write_doc_content` set. `apps_script_write_content` has no configurable
auto-accept rule yet — Allow-once-only, like most new write tools at first cut.

### The `auto` tier, across all connectors

The tables above gate 41 tools `auto` — allowed to proceed with no human in the loop, but still
recorded in the audit log as `auto_accepted` (see
[Audit integrity and forwarding](security-and-compliance.md#audit-integrity-and-forwarding): the
`auto` gate is a logged, IT-and-user-configured exception, never a default absence of control).
This section gives that tier its own documented view rather than leaving it implicit across eleven
separate per-connector tables.

**What qualifies a tool for `auto`, as a rule rather than a case-by-case judgment call:** every tool
below is either (a) a listing/metadata operation whose result names *what exists* (message subjects
lists, channel names, calendar names, project keys) without returning a message body, document
content, or any other free-text personal data, or (b) a narrow administrative action with no data
disclosure of its own (creating a blank spreadsheet, refreshing a name-resolution cache). Nothing
that returns full message/document/record content is ever `auto` — that boundary is what keeps this
tier's risk bounded regardless of how many tools sit in it; see
[claude-knowledge-boundary.md](claude-knowledge-boundary.md) for the exact fields each `review`-gated
tool discloses once a human does approve it.

| Connector | Auto tools | Count |
|---|---|---|
| Gmail | `gmail_list_messages`, `gmail_list_threads`, `gmail_list_message_attachments`, `gmail_list_filters`, `gmail_list_labels` | 5 |
| Google Drive (incl. Sheets) | `drive_list_files`, `drive_get_file_metadata`, `drive_list_folder`, `drive_list_shared_drives`, `drive_create_blank_file`, `drive_sheets_create`, `drive_sheets_get_metadata` | 7 |
| Slack | `slack_list_channels`, `slack_list_dms`, `slack_list_group_chats`, `slack_resolve_permalink`, `slack_refresh_user_cache`, `slack_refresh_channel_cache` | 6 |
| Google Calendar | `calendar_list_calendars`, `calendar_list_events`, `calendar_get_free_busy`, `calendar_list_rooms`, `calendar_get_event_visibility` | 5 |
| Google Contacts | `contacts_list`, `contacts_search`, `contacts_get` | 3 |
| Telegram | `telegram_list_chats`, `telegram_refresh_chat_cache` | 2 |
| Salesforce | `salesforce_list_reports` | 1 |
| Jira | `jira_list_projects`, `jira_search_issues`, `jira_get_transitions` | 3 |
| Confluence | `confluence_list_spaces`, `confluence_search`, `confluence_cql_search`, `confluence_list_pages`, `confluence_list_attachments` | 5 |
| Google Tasks | `tasks_list_task_lists`, `tasks_list_tasks`, `tasks_get_task` | 3 |
| Apps Script | `apps_script_list_projects` | 1 |
| **Total** | | **41** |

A few things worth calling out explicitly about this tier as a whole, rather than tool by tool:

- **`auto` is not "unauditable" or "silent."** Every one of these 41 calls still writes an
  `auto_accepted` entry to the same hash-chained audit log a `review`/`popup` decision writes to
  (`audit_log.py`) — the difference from `review`/`popup` is *when* the call proceeds (immediately,
  vs. after a human decision), not *whether* it's recorded.
- **`auto` here means "IT and the review model decided this category is safe by design," not
  "unconfigurable."** Nothing in this tier can be moved to `review`/`popup` by a user today — the
  gate each tool goes through is fixed in code (`auto_accept.py`'s `TOOL_TO_GATE`, the single source
  of truth the connector tables above are checked against —
  `tests/unit/connectors/test_readme_manifest_alignment.py`), not a `settings.yaml` setting, so an
  organization that wants a *narrower* `auto` tier than what ships today has no way to configure
  that yet; this is a real, current limitation worth naming rather than leaving implicit.
- **Two tools carry a search/filter parameter that narrows results without changing their gate.**
  `slack_list_channels`/`slack_list_group_chats`/`slack_list_dms`'s `participant` matching and
  `calendar_get_free_busy`'s access-fallback behavior (`calendar.free_busy_full_event_details`) both
  still return only metadata-shaped results — narrowing *what's listed* never crosses into returning
  message/document content, so neither changes the `auto` classification.
- **Some `auto` tools are themselves prerequisites for a later `review`-gated call**, not endpoints
  in their own right — e.g. `gmail_list_messages` (auto) is how a message id reaches
  `gmail_get_message` (review); the list operation discloses subjects/senders/dates but never a body,
  and the body-returning call is exactly where the gate steps up. See
  [claude-knowledge-boundary.md](claude-knowledge-boundary.md) for this pattern worked through in
  detail across every connector.

---

## Auto-accept grants

Trusting a specific resource — a Drive folder, a Google Tasks list, a Slack channel, a Jira
project, ... — is configured **once per resource**, under `auto_accept_grants` in
`config/settings.yaml`, rather than by adding the same ID to every operation key that resource
happens to touch (see [Auto-accept rules](#auto-accept-rules) below for the older, still-supported
per-operation form). This is also what PrivacyFence Settings' **Auto-accept Rules → \<Connector\> →
Trusted \<Resource\>** sections read and write — editing the YAML directly and editing from that
window are equivalent.

```yaml
auto_accept_grants:
  drive:
    sandbox_folders:
      - id: "1CdeFghIJKLmnoPQRstuVWxyz0123456789AbCdEfGh"
        name: "Claude scratch space"   # cosmetic — see below
        write: true
    folders:
      - id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        name: "Shared Reports"
        read: true
  tasks:
    task_lists:
      - id: "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow"
        name: "Personal"
        create: true
        edit: true
        complete: true
        move: true
```

Each grant entry is keyed by `id` (or `key` for Jira/Confluence, which already address resources
that way) plus a small set of capability booleans. A freshly added grant starts with every
capability `false` — adding a resource does nothing until a capability is explicitly turned on,
from PrivacyFence Settings or by hand. `name` is a cosmetic cache of the resource's last-resolved
display name; the evaluator never reads it, only `id`/`key` and the capability booleans decide what
auto-accepts.

### What each resource type covers

| Connector | Resource type (`config_key`) | Capabilities → what they auto-accept |
|---|---|---|
| `drive` | `folders` | `read` → reading file contents/downloads in that folder, and `sheets.read_values` for spreadsheets in it |
| `drive` | `sandbox_folders` | `write` → writing files/Docs in that folder (including `docs.edit_content`/`docs.format_content`), every `sheets.*` write operation for spreadsheets in it, commenting on a file already there, uploading into it, and moving a file out of it |
| `tasks` | `task_lists` | `create`, `edit`, `complete` (covers complete + uncomplete), `move` — one per Tasks write tool |
| `slack` | `channels` | `read` → reading channel/thread history and search results in that channel; `send` → sending messages there |
| `telegram` | `chats` | `read` → reading/searching that chat; `send` → sending messages there |
| `jira` | `projects` (by `key`) | `read`, `create`, `comment`, `update`, `transition` — one per Jira tool |
| `confluence` | `spaces` (by `key`) | `read` → reading a page or downloading its attachments in that space, `create`, `update` |
| `calendar` | `calendars` | `read` → reading event details on that calendar; `write` → creating/updating events there |
| `salesforce` | `reports` | `run` → running that specific report |

`drive.upload_file`'s destination-folder allowlist (`parent_folder_allowlist`) and
`drive.move_file`'s move-approval (`move_within_approved_folders`) are targets of the
`sandbox_folders` grant's `write` capability too, alongside the rest — one trusted sandbox folder
now covers writing into it, uploading into it, and moving a file out of it, not only writing to a
file already there. They use their own rule names rather than `approved_sandbox_folder` since their
underlying checks differ (a destination-folder arg for uploads; the file's current parent folder,
not the move's destination, for moves — see [Auto-accept rules](#auto-accept-rules) below), but take
the same plain folder-id-list value the grant already compiles.

### Settings page UX

On the settings page's **Auto-accept Rules** page, selecting a connector shows each resource type
above as its own **Trusted \<Resource\>** section: every currently-granted resource is its own row,
with a **Name** field and a **Resource ID** field (plain text inputs, committed on blur/Enter —
pasting a Drive/Sheets URL into a Drive folder's ID field extracts the ID automatically; every other
connector's ID field takes the raw ID/key as typed), one toggle (rendered as a chip) per capability,
and its own **✕ Remove**. Once an ID is entered and the connector is authenticated, its display name
is resolved in the background and shown in the Name field (see [Name resolution](#name-resolution)
below). Adding one is a single **+ Add \<resource\>…** action that appends a blank row to fill in by
hand — an earlier, pre-#120 native menu-bar version of this page had a native "pick from a list of
everything visible to this connector" picker for connectors with a cheap listing call; that pass
dropped it in favor of the same manual Name/Resource-ID entry for every connector, and it stayed
dropped through the move to the web (P4/P10).

Every existing rule under `auto_accept_rules` that isn't a resource grant (domain trust, label
matching, file-type allowlists, and similar — see [Auto-accept rules](#auto-accept-rules)) lives on
that same connector's page as a `rule_type` / `value` row. `rule_type` is a dropdown listing only
the rule names that operation actually supports (`RULES_BY_OPERATION` in settings_controller.py),
committed immediately on selection rather than requiring the rule name to be typed by hand; `value`
stays a plain text field, committed on blur/Enter. A list-valued rule's `value` field takes a
comma-separated list directly (e.g. `domain1.com, domain2.com`) in one field, rather than an earlier
native menu-bar version's one-value-at-a-time **+ Add value…** / **✕ Remove** treatment.

**Sheets** and **Docs** get their own top-level sidebar pages (neither is a real connector — both
ride on Drive's OAuth grant, see [Auto-accept rules](#auto-accept-rules)'s Drive section), but the
`folders`/`sandbox_folders` grants above are Drive-page-only sections — a folder trusted there
silently also covers `sheets.read_values` and every `sheets.*`/`docs.*` write. So each of those two
pages opens with a read-only **Governed by Drive** section summarizing the currently-granted
folder(s) for read/write and a **Manage in Drive →** link that jumps the sidebar selection there —
no checkboxes of its own; the one editable copy of these grants stays on the Drive page.

### Web surfaces (`/approvals`, `/settings`)

Every approval card and the settings page above are served over the embedded web server
(`web/server.py`) — this used to run
alongside a native macOS menu bar/approval dialogs/settings window; that native UI
layer was later deleted entirely ("two approval surfaces means two places for a security fix to
land"), so the web surface is now the only one, on every platform this daemon runs on. Two config
keys under `web:` in `settings.yaml`:

- `web.mcp.enabled: true` (default) — turns on the `/mcp` Streamable HTTP endpoint Claude talks to.
- `web.settings.enabled: true` (default) — turns on `GET /settings` and its
  `POST /api/settings/{action}` dispatcher. `web.settings.allow_quit` (default `true`) gates whether
  the About page's Quit button works from a browser at all — always behind an in-page confirmation
  either way. The approval surface itself (`/approvals`) has no such switch — P10 is the phase with
  no rollback, since it deleted the fallback.

Both pages share one origin, one session (the same local `web_token` §10 of the refactor plan
already describes), and one shared chrome (`web_shell.py`): a header with Approvals/Settings
navigation and a live-connection indicator bound to `GET /api/state/stream` — one SSE channel
carrying both a `settings` event (`SettingsController.snapshot()`, pushed the moment something
changes it from anywhere — a rule edited over MCP, a background OAuth flow finishing) and an
`approvals` event (the pending-approval list), so an open tab never needs a manual refresh.

`/settings`'s own action dispatcher is an **explicit allowlist** — an unlisted or misspelled action
name is a 404 before any lookup happens at all, and every argument is validated against the
controller method's own type annotations (a bad `idx` is a 400, not a 500). Four actions that don't
fit "POST an action, get a snapshot back" get their own routes instead: uploading an organization
config bundle (multipart, JSON/`version`-validated, written `0600`), downloading the current week's
audit log export (`Content-Disposition: attachment`), an in-page "update available" banner
(Download/Remind Me Later/Skip), and the repo link (a plain `<a href>`, opened client-side — never a
`subprocess.run(["open", ...])` reachable from an HTTP request, which nothing under `web/` does at
all, by design).

The `/approvals` list (`docs/approval-list-ui-ux.md`) shows every currently-pending card as its own
row — connector icon, title, a relative timestamp, a **Deny** button right on the row, and a
**Review →** link to the full card at `/approvals/{id}`. There is deliberately no **Allow** on the
row: denying without reading the card can't leak anything, and putting an "Allow" button on a
one-line summary is exactly the habituation failure the full card exists to prevent. Deciding a card
navigates back to the list (not a dead "close this tab" page) with a toast saying what happened,
including the 409 case where a rule created elsewhere already resolved it first.

Desktop notifications (`web.notifications.enabled`, default `true`) are tier 0/1 only — a title-bar
`(N)` badge and an `aria-live` announcement need no permission at all; `registration.showNotification
()` (via `resources/sw.js`, a service worker with no `push` handler and no cache) fires while a tab
is open but unfocused, after the browser's own permission prompt, itself only ever offered once,
right after a person's first decision (never on page load). The notification body is always the bare
pending count — never a connector, tool, or row title, several of which can carry real gated content
(an event title, a contact name) — until a real per-field allowlist for the richer `standard`/
`detailed` levels ships. Push notifications for a closed tab (tier 2) are `org`-mode work, not built
yet.

### Name resolution

Grant rows show the resource's real name, resolved via the same connector API calls used
elsewhere in the daemon (e.g. `drive_get_file_metadata`, `tasks_list_task_lists`), cached
in-memory (short TTL) and on disk (`resource_name_cache.json` next to the rest of PrivacyFence's
data) so a name is available immediately even before a connector has reconnected this session.
Resolution never blocks or changes an auto-accept decision — a row falls back to the ID itself,
annotated "(resolving…)" or "(connect \<Connector\> to see its name)", if a name isn't available
yet or the connector isn't currently authenticated.

### Relationship to `auto_accept_rules`

`auto_accept_grants` and `auto_accept_rules` are both read every time rules are (re)loaded — a
grant's enabled capabilities compile into the exact same `{rule, value}` shape a hand-written entry
under `auto_accept_rules` already used, so the evaluator itself has no separate code path for
grants. Existing hand-written `auto_accept_rules` entries keep working unmodified.

On first startup after upgrading to a version with this feature, PrivacyFence looks for
`auto_accept_rules` entries that exactly match what a grant's capability would already produce —
i.e. the same rule value repeated identically across *every* operation key that capability covers
— and folds those into `auto_accept_grants` automatically, removing the now-redundant
`auto_accept_rules` entries. This runs once (tracked by a `migrated_to_grants_v1` marker) and is
logged at `INFO` level. A **partial** match (the value present on some but not all of a
capability's operation keys) is deliberately left alone rather than migrated, since folding it in
would silently widen auto-accept to operation keys never explicitly configured — those stay under
`auto_accept_rules`, visible and removable from the connector's page in PrivacyFence Settings, but no
longer offered as something "+ Add rule…" creates fresh (steering new configuration toward the
grants model without breaking what's already there).

---

## Auto-accept rules

Beyond the connector/resource-scoped [grants](#auto-accept-grants) above, routine, low-risk
requests can also be approved automatically based on an *attribute* of the request rather than a
specific resource's identity — sender domain, label, file type, and similar, where there's no
single resource ID to grant trust to once. These stay configured per operation in
`config/settings.yaml` under `auto_accept_rules`. When a rule matches, the gate is bypassed and the
request is logged as `auto_accepted`.

### Available rules

**Gmail**

| Rule | Matches when… |
|------|--------------|
| `i_am_sender` | The authenticated account is the sender |
| `i_am_sole_recipient` | The only recipient is the authenticated account |
| `trusted_sender_domain` | Sender's domain is in the allowlist, including subdomains (e.g. `mail.trusted.com` matches an allowlisted `trusted.com`) |
| `label_match` | Message carries one of the specified labels |
| `age_threshold_days` | Message is older than N days |
| `no_attachments` | Message has no attachments |

These apply to Gmail's read tools. Gmail's write tools (`gmail_create_draft`, `gmail_reply_draft`,
`gmail_reply_all_draft` and their `_with_attachments` counterparts, `gmail_add_label`,
`gmail_remove_label`, `gmail_create_label`) have their own rules:

| Rule | Matches when… |
|------|--------------|
| `to_is_myself` | Every recipient of the draft/reply is the authenticated account itself |
| `approved_recipient_domain` | Every recipient's domain is in the allowlist |
| `label_name_allowlist` | The label being added/removed/created is in the allowlist |
| `always_allow` | Unconditional — matches every call, regardless of recipient |

`always_allow` (`gmail.create_draft` only, of these three) is deliberately broader than
`to_is_myself`/`approved_recipient_domain`: a draft never sends itself, so "always auto-accept
drafting, I review before it sends anyway" is a coherent policy independent of who the draft is
addressed to. It's the same value-less rule shape as `i_am_owner`/`dm_with_myself` — presence under
an operation key is the whole condition — see [Google Calendar](#google-calendar) below for its
other two uses.

`gmail_create_filter` and `gmail_update_filter` have no built-in rule and always prompt — a
filter's criteria/action combination is too open-ended for a simple allowlist match.

**Google Drive**

| Rule | Matches when… |
|------|--------------|
| `i_am_owner` / `created_by_me` | Authenticated account owns the file |
| `approved_folder` | File is in an approved folder (by Drive folder ID) |
| `approved_sandbox_folder` | File is in an approved sandbox folder |
| `move_within_approved_folders` | Move operation stays within approved folders |
| `file_type_allowlist` | File MIME type is in the allowlist |
| `created_this_session` | File was created by Claude in the current session |
| `shared_drive_exclusion` | File is NOT on a shared drive |

`drive_upload_file` additionally supports `parent_folder_allowlist` (matches when the upload's
destination folder ID is in the allowlist).

> **`approved_folder`, `approved_sandbox_folder`, `parent_folder_allowlist`, and
> `move_within_approved_folders` are all grant-managed** — see
> [Auto-accept grants](#auto-accept-grants) → `drive.folders` / `drive.sandbox_folders`. Add the
> folder there once (from PrivacyFence Settings' **Trusted Folders** / **Sandbox Folders** sections
> under **Auto-accept Rules → Drive**, or by hand under `auto_accept_grants`) and it applies across
> every operation key below automatically, instead of needing the same folder ID added to each one
> separately — including
> `drive_upload_file`'s destination-folder check and `drive_move_file`'s move-approval, which use
> their own rule names (different underlying check — see below) but the same sandbox-folder grant.

The same rules apply to the `drive_sheets_*` tools, under their own operation keys so they can be
configured independently of plain-file Drive operations: `sheets.read_values` (`i_am_owner`,
`created_by_me`, `approved_folder`, `created_this_session`, `shared_drive_exclusion`) and
`sheets.write_range` / `sheets.add_sheet` / `sheets.rename_sheet` / `sheets.format_range` /
`sheets.insert_dimensions` / `sheets.delete_dimensions`
(`i_am_owner`, `approved_sandbox_folder`, `created_this_session`). A spreadsheet is a Drive file,
so e.g. `created_this_session` fires for a spreadsheet `drive_sheets_create` made earlier in the
same conversation. `approved_folder`/`approved_sandbox_folder` on these seven operation keys
(`sheets.read_values` plus the six `sheets.*` writes) are the same grant-managed rules as above —
one `drive.folders`/`drive.sandbox_folders` grant covers all of plain Drive reads/writes and every
one of these `sheets.*` operations at once, instead of needing the same folder ID added to each one
separately (the old, still-fully-supported way — configure each rule independently under
`auto_accept_rules`, as before grants existed).

Clicking **Always allow** on a "Read Sheet Values" prompt proposes the same `i_am_owner`/
`approved_folder` candidate(s) as `drive.read_file_contents`/`download_file` — see
[Multiple matching candidates](always-allow-rules-reference.md#multiple-matching-candidates) for how
the popup renders one button per candidate when both apply.

`drive.comment_file` (`drive_add_comment` — also used for comments on Docs and Sheets, since those
ride the Drive connector's OAuth grant) supports `i_am_owner`, `approved_sandbox_folder`, and
`created_this_session` the same way plain Drive files do. `docs.edit_content` and
`docs.format_content` (`drive_docs_edit_content`/`drive_docs_format_content`) support the same rules
`drive.write_doc` does — `i_am_owner`, `approved_sandbox_folder`, `created_this_session` — under
their own operation keys. `approved_sandbox_folder` here is the same `drive.sandbox_folders` grant
covered above — enabling its `write` capability auto-accepts `drive.comment_file`,
`docs.edit_content`/`docs.format_content`, `drive.upload_file`, and `drive.move_file` too, alongside
`drive.write_file`/`drive.write_doc` and every `sheets.*` write.

**Every one of Drive's write ops offers Always allow** — see
[Write tools](always-allow-rules-reference.md#write-tools) for the full table; most propose
`approved_sandbox_folder` from the file's current parent folder(s), `drive.upload_file` proposes
`parent_folder_allowlist` from the upload's destination folder, and `drive.move_file` proposes
`move_within_approved_folders` from the file's folder *before* the move. Some also still get a
temp-accept grace window on top (see
[Related but distinct mechanisms](always-allow-rules-reference.md#related-but-distinct-mechanisms)).
`sheets.write_range`, `sheets.format_range`,
`sheets.insert_dimensions`, `drive.comment_file`, `docs.edit_content`, and `docs.format_content`
are the exception: clicking Allow once on one of these also arms an in-memory, non-persisted
acceptance scoped to one spreadsheet/file for 5 minutes — disclosed in the popup with a plain
caption, not a separate button — see
[Related but distinct mechanisms](always-allow-rules-reference.md#related-but-distinct-mechanisms).
`sheets.add_sheet` and `sheets.rename_sheet`
get neither; they're one-shot per file rather than something called repeatedly in a burst, so a
standing rule (configured as above) is the only way to skip their popup. `sheets.delete_dimensions`
also deliberately gets neither, despite being called in the same kind of burst
`sheets.insert_dimensions` is: unlike insert/format, deleting rows or columns removes cell content
with no undo path through PrivacyFence, so it only ever gets the standing-rule treatment — see
[Related but distinct mechanisms](always-allow-rules-reference.md#related-but-distinct-mechanisms)
for the reasoning.

**Slack**

| Rule | Matches when… |
|------|--------------|
| `dm_with_myself` / `send_to_myself` | Target channel is a self-DM |
| `group_dm` | Target channel is a group DM (Slack's "mpim" type — a private multi-person conversation, distinct from a 1:1 DM and from a private channel) |
| `approved_channel` / `approved_recipient` | Channel ID is in the allowlist |
| `approved_channel_all_results` | **Every** message returned is from a channel in the allowlist |
| `public_channels_only` | All messages are from public channels |
| `no_file_attachments` | Messages have no file attachments |
| `reply_in_existing_thread` | Message is a reply (has `thread_ts`) |

`group_dm` recognizes the group-DM *shape* itself as a trustable category, rather than requiring
each group's channel ID to be individually allowlisted under `approved_channel` the way a regular
channel is. Channel type isn't derivable from the ID alone (a private channel can share the same
`G`-prefixed shape a group DM uses), so `slack_get_channel_history`/
`slack_get_thread_replies` resolve it via `SlackClient.resolve_is_group_dm()` (a cached
`conversations.info` lookup) before the call reaches the gate, alongside the channel-name lookup
`slack.py`'s preview text already does.

> **`approved_channel`/`approved_recipient` are grant-managed** — see
> [Auto-accept grants](#auto-accept-grants) → `slack.channels`. One channel grant's `read`/`send`
> capabilities cover both rules above.

`approved_channel` reads a single `channel_id` out of the call's own arguments, which
`slack_get_channel_history`/`slack_get_thread_replies` always provide but `slack_search_messages`
never does — a search can match messages across any number of channels, so there's no one channel
to check against the allowlist. `approved_channel_all_results` is the counterpart for that case: it
reads every message actually returned and only matches when **all** of them are on the allowlist,
gating the whole search if even one result isn't. Configuring it (or `approved_channel`, since both
share `slack.read_messages`) once covers reads, thread reads, *and* searches of the approved
channel(s) alike.

**Google Calendar**

| Rule | Matches when… |
|------|--------------|
| `i_am_organizer` | Authenticated account is the event organizer |
| `no_external_attendees` | All attendees share the same email domain |
| `personal_calendar` | Event is from a specified calendar ID |
| `past_event` | Event end time is in the past |
| `time_window_days` | Event starts within the next N days |
| `no_conferencing_link` | Event has no video conferencing link |
| `non_private_event` | The event's visibility is not `private` |
| `always_allow` | Unconditional — `calendar.out_of_office`/`calendar.working_location` only (see below) |

> **`personal_calendar` is grant-managed** — see [Auto-accept grants](#auto-accept-grants) →
> `calendar.calendars`. One calendar grant's `read`/`write` capabilities cover
> `calendar.read_event_details`, `calendar.create_modify_event`, and `calendar.set_visibility`.

`calendar_create_out_of_office` (`calendar.out_of_office`) and `calendar_set_working_location`
(`calendar.working_location`) each have their own operation key, but none of the rules above apply
to either — both always act on your own primary calendar with no organizer/attendee/other-calendar
concept for these rules to check. Like `gmail.create_draft` above, their only configurable
auto-accept is the unconditional `always_allow` — there's no narrower resource identity to scope a
rule to, so it's a plain yes/no rather than the organizer/calendar-scoped rules
`calendar_create_event`/`calendar_update_event` support.

`calendar_set_event_visibility` (`calendar.set_visibility`) is a write like
`calendar_create_event`/`calendar_update_event`, so it shares `calendar.create_modify_event`'s
rule set (`i_am_organizer`, `no_external_attendees`, `personal_calendar`) rather than getting a
rule of its own — `non_private_event` only applies to `calendar.read_event_details`. Clicking
**Always allow** on a "Read Calendar Event" prompt proposes `non_private_event` when the event
isn't private and neither `i_am_organizer` nor `no_external_attendees` apply.

**Salesforce**

| Rule | Matches when… |
|------|--------------|
| `approved_object_types` | Object type (Account, Contact, …) is in the allowlist — for `salesforce_search` (`salesforce.search`), every object type in its comma-separated `object_types` must be on the allowlist, not just one |
| `approved_report_ids` | Report ID is in the approved list |

> **`approved_report_ids` is grant-managed** — see [Auto-accept grants](#auto-accept-grants) →
> `salesforce.reports`. `approved_object_types` is a small fixed vocabulary (not a resource
> identity) and stays a plain rule.

`salesforce_search` with no `object_types` given reaches Salesforce's whole default set of
globally-searchable objects — too broad for `approved_object_types` to ever match, so an unscoped
search always prompts (or needs a differently-shaped rule, none of which exist yet).

**Google Contacts**

| Rule | Matches when… |
|------|--------------|
| `no_contact_info_change` | The update doesn't touch `emails` or `phones` (name/organization/notes-only edits) |

**Jira**

| Rule | Matches when… |
|------|--------------|
| `i_am_reporter` | Authenticated account is the issue's reporter |
| `i_am_assignee` | Authenticated account is the issue's assignee |
| `approved_project_keys` | Issue's project key is in the allowlist |

`jira_transition_issue` (`jira.transition_issue`) also accepts `approved_project_keys` — it derives
the project from `issue_key` the same way `jira_get_issue`/`jira_update_issue` do. `i_am_reporter` /
`i_am_assignee` don't apply to it, since a transition call doesn't carry the issue's reporter/assignee.

> **`approved_project_keys` is grant-managed** — see [Auto-accept grants](#auto-accept-grants) →
> `jira.projects`. One project grant's `read`/`create`/`comment`/`update`/`transition`
> capabilities cover all five rules above at once, instead of adding the same project key
> separately to `jira.read_issue`, `jira.create_issue`, `jira.add_comment`, `jira.update_issue`,
> and `jira.transition_issue`.

**Confluence**

| Rule | Matches when… |
|------|--------------|
| `i_am_author` | Authenticated account is the page's author |
| `approved_space_keys` | Page's space key is in the allowlist |

> **`approved_space_keys` is grant-managed** — see [Auto-accept grants](#auto-accept-grants) →
> `confluence.spaces`. One space grant's `read`/`create`/`update` capabilities cover
> `confluence.read_page`/`confluence.download_attachment`, `confluence.create_page`, and
> `confluence.update_page` at once.

**Telegram**

| Rule | Matches when… |
|------|--------------|
| `approved_chats` | Chat ID is in the allowlist |
| `approved_chats_all_results` | **Every** message returned is from a chat in the allowlist |
| `no_media_attachments` | Messages have no media attachments |

> **`approved_chats` is grant-managed** — see [Auto-accept grants](#auto-accept-grants) →
> `telegram.chats`. One chat grant's `read`/`send` capabilities cover both
> `telegram.read_chat_messages` and `telegram.send_message`.

`telegram_search_messages` shares the `telegram.read_chat_messages` operation key with
`telegram_get_messages` (an upgrade from an older release with a separate `telegram.search_messages`
key migrates any existing rules onto the shared key automatically, see
`auto_accept.migrate_telegram_search_operation_key()`), the same way `slack_search_messages`
already shares `slack.read_messages`. `approved_chats` reads a single `chat_id` out of the call's
arguments, which a search never provides (it can match across any number of chats); configuring it
also covers `approved_chats_all_results`, the counterpart evaluated against every result a search
actually returns, matching only when **all** of them are on the allowlist.

**Google Tasks**

| Rule | Matches when… |
|------|--------------|
| `approved_task_list` | Task list is in the allowlist — for `tasks_move_task`, both the source and destination list must be |

`approved_task_list` applies independently to each of `tasks.create_task`, `tasks.update_task`,
`tasks.complete_task`, `tasks.uncomplete_task`, and `tasks.move_task`, so you can e.g. auto-accept
edits within a personal list while still requiring review for creates.

> **`approved_task_list` is grant-managed** — see [Auto-accept grants](#auto-accept-grants) →
> `tasks.task_lists`. One task-list grant's `create`/`edit`/`complete`/`move` capabilities cover
> all five task-write operations at once (`complete` covers both complete and uncomplete).

> **Google Contacts**: `contacts_list`, `contacts_search`, and `contacts_get` are unconditionally auto-accepted. `contacts_update`, `contacts_create`, `contacts_add_label`, and `contacts_remove_label` are all `popup`-gated; `no_contact_info_change` above is the only configurable auto-accept rule, and it applies only to `contacts_update`. Contact deletion is not supported. **Google Tasks**: all three read tools plus `tasks_list_task_lists` are unconditionally auto-accepted; the five write tools (`tasks_create_task`, `tasks_update_task`, `tasks_complete_task`, `tasks_uncomplete_task`, `tasks_move_task`) are `popup`-gated, each independently configurable via `approved_task_list` above. **Telegram**: `telegram_list_chats` is unconditionally auto-accepted; `telegram_get_messages` and `telegram_search_messages` are `review`-gated by default but configurable via the rules above (sharing one operation key, `telegram.read_chat_messages`); `telegram_send_message` is `popup`-gated with no configurable rule. **Jira and Confluence** read tools (`jira_get_issue`, `confluence_get_page`, `confluence_get_page_by_title`, `confluence_download_attachment`) are `review`-gated by default but configurable via the rules above; their write tools remain `popup`-gated with no configurable rule, except `jira_transition_issue`, which accepts `approved_project_keys` as noted above. **Apps Script**: `apps_script_list_projects` is unconditionally auto-accepted; `apps_script_get_content` and `apps_script_get_execution_log` are `review`-gated with no configurable rule; `apps_script_write_content` is `popup`-gated with no configurable rule (Allow-once-only at first cut — see issue #154 open question 2).

## Privacy filtering and PII

Privacy filtering runs before protected content is released to the MCP client. Organization policy can allow, redact, or block configured PII categories. Invalid configured policy values fail closed at startup/config validation rather than silently becoming permissive.

The PII detector and privacy filter are implemented in `pii_detector.py` and `privacy_filter.py`. Tool-specific preview/extraction limits are documented in [`file-type-support.md`](file-type-support.md).

## Audit logging

PrivacyFence records tool/gate decisions to the audit log, including the connector/tool, decision, request metadata, and principal information where applicable. Security-sensitive audit integrity/forwarding behavior is implemented in the audit modules and described in [`security-and-compliance.md`](security-and-compliance.md).

Treat the audit log as security-relevant state: protect its directory, include it in operational backup decisions where required, and do not expose it through connector content paths.

## Web authentication and CSRF

Local browser sessions are established from the one-time bootstrap exchange and then represented by the HttpOnly session cookie. Mutating browser requests require same-origin/session checks plus the CSRF value carried by the page/request flow.

CSP nonces are generated and applied to the inline scripts/styles required by the rendered application. Security headers are applied by the web-server middleware.

Org-mode routes use the org session/identity machinery and apply principal-aware authorization rather than the local single-user session model.

## Browser notifications

The shared web shell maintains the state stream, pending-approval count, and optional browser notifications. Notification detail is controlled by the web notification configuration:

- `minimal` exposes only a pending-count style notification;
- `standard` can include safe operation metadata such as connector/direction;
- `detailed` may include the approval summary and therefore can expose gated content in the OS/browser notification surface.

Notification permission is requested only after a user interaction/decision path, not automatically on initial page load.

## Connector lifecycle

Local mode builds one `ConnectorHost` at daemon startup. Org mode uses `ConnectorRegistry` to build connector sets lazily per principal and evict idle hosts.

Connector setup documentation:

- [`google-cloud-setup.md`](google-cloud-setup.md)
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md)
- [`telegram-setup.md`](telegram-setup.md)

The definitive tool surface lives in `src/privacyfence/connectors/` and the connector registry/daemon construction code.

## File and download handling

PrivacyFence extracts/normalizes supported attachment types for preview and PII inspection using the bounded extraction paths documented in [`file-type-support.md`](file-type-support.md).

Local-mode downloads can be written on the user's machine. Org-mode downloads are delivered inline or through encrypted short-lived staged downloads as documented in [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Configuration

`config/settings.yaml` (and the packaged example under resources) defines local/web/connector behavior. Org deployments additionally use the signed/validated organization configuration bundle and org-specific identity/settings.

Configuration that affects security boundaries is validated strictly; invalid values should stop startup rather than silently widen access.

## Installation and packaging

Current packaging paths are documented in [`platform-support.md`](platform-support.md):

- macOS signed/notarized DMG;
- Windows Inno Setup installer;
- Debian/Ubuntu self-contained `.deb` for local desktop mode;
- Python package/system-service path for Linux/server deployments.

### Windows

`installer/privacyfence.iss` (built by `scripts/build_installer.ps1`) installs the PyInstaller
onedir output under `%ProgramFiles%\PrivacyFence\` (or a per-user-writable location instead, when
the installer runs without admin elevation — `PrivilegesRequired=lowest`), the bundled `.mcpb`
alongside it, and a Start Menu entry pointing at the embedded web settings UI rather than at the
daemon executable directly.

Autostart is a Task Scheduler task (`PrivacyFence`), not a Startup-folder shortcut, registered from
`installer/privacyfence.iss`'s `[Code]` section (`CurStepChanged(ssPostInstall)` calling
`RegisterAutostartTask`) rather than a plain `[Run]` entry, and removed by the uninstaller's
`[UninstallRun]` section (`schtasks /delete`) — visible and removable through normal Windows
install/uninstall UI, the same way the macOS LaunchAgent plist and the Linux `.deb`'s XDG autostart
entry are. Registration is a real Task Scheduler XML task definition
(`installer/privacyfence-task.xml.tmpl`, extracted at install time, `__EXEC_PATH__` substituted for
the real installed path, registered via `schtasks /create /xml`), not plain `schtasks /create` CLI
flags — an earlier CLI-flag-only version of this mechanism shipped briefly with two real bugs
(invalid `/ri`/`/du` flags for an `ONLOGON` schedule, then a trigger scoped to only the installing
account), both superseded by this XML-based rewrite rather than patched in place; see that
template's own header comment and `platform-support.md`'s "Known open items" for the full history.
`<LogonTrigger>` with no `<UserId>` fires for any interactive logon, `<Principal>` uses `GroupId`
(`Builtin\Users`) rather than a specific account so the task runs as whichever user just signed in,
in their own session, at the non-elevated `LeastPrivilege` run level, and
`<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>` gives it real
crash-restart behavior — parity with the macOS LaunchAgent's `KeepAlive`/`SuccessfulExit=false` and
the Linux `.deb`'s systemd restart policy. `<DisallowStartIfOnBatteries>` and
`<StopIfGoingOnBatteries>` are both set to `false`, inverting Task Scheduler's own defaults: left at
the defaults, a laptop on battery power would not start PrivacyFence at sign-in and would stop it
when unplugged — a privacy gate that quietly isn't running, with the MCP client simply finding no
daemon. This closes
[`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) Phase 13. See
[`platform-support.md`](platform-support.md)'s "Known open items" for this mechanism's current
verification status. In short: `windows-graphical-session.yml` verifies the definition Task
Scheduler itself stored, that Task Scheduler really starts the daemon for an account that installed
nothing, and that it really relaunches it after a crash; the `<LogonTrigger>`'s own firing is a
human check on a real machine (`release-testing.md`), because a hosted runner cannot produce the
Terminal Services session logon the trigger subscribes to.

Per-user state (credentials, settings, the audit log) lives under `%USERPROFILE%\.privacyfence\`,
created by the app on first run — the installer never touches it, and uninstalling removes only the
program files and the scheduled task.

**File-permissions caveat, accepted for v1**: elsewhere on this codebase, credential/token files are
written with `chmod(0o600/0o700)` to lock them down to the owning user. On Windows, `chmod` is a
silent no-op — there is no POSIX permission bit to set — so those files rely on the default NTFS ACLs
a per-user Windows profile already has (restricted to that user and Administrators) rather than an
explicit lock-down step. This is a deliberate, accepted gap, not an oversight: a single-user Windows
profile's own default ACLs already provide the same practical protection the `chmod` calls give on
POSIX, and tightening it further (e.g. via `icacls`/`pywin32`) is out of scope unless a security
review finds the default insufficient.

### Linux

Two distinct install paths, not one — see `platform-support.md`'s support matrix for the full
comparison:

- **Local desktop mode**: a self-contained `.deb` (`PrivacyFenceApp.linux.spec`,
  `scripts/build_deb.sh`, `debian/`) installing the PyInstaller onedir output under
  `/opt/privacyfence`, exposing `/usr/bin/privacyfence-app`, and registering an XDG autostart entry
  under `/etc/xdg/autostart/` — the Linux analogue of the macOS LaunchAgent/Windows Task Scheduler
  task. Package removal does not delete per-user state from the home directory. Currently `amd64`
  only; `arm64` is a deliberate, undecided follow-up rather than a gap (see `platform-support.md`'s
  "Architecture and CPU constraints").
- **Org mode / server deployments**: `pip`/`pipx install privacyfence`, walked end to end by
  [`org-mode-setup-guide.md`](org-mode-setup-guide.md) (dedicated system user, OIDC registration, org
  config bundle, reverse proxy, a **system** systemd unit distinct from the repo-root
  `privacyfence.service` below), or the repo-root `privacyfence.service` — a systemd **`--user`** unit
  for a single-user Linux desktop install via the same `pip`/`pipx` path, requiring
  `loginctl enable-linger` or a graphical session to autostart at login the way the `.deb`'s XDG entry
  does. **Unverified on a real install, both paths**: nothing in `src/privacyfence/` imports a
  platform-specific module any more, and the full suite runs headlessly on Linux CI on every PR
  (`org-mode-smoke` exercises the daemon's own startup/authz/audit contract against a real subprocess
  and a mocked IdP) — but neither a real `pip`/`pipx install privacyfence` nor a live third-party
  IdP's actual OIDC round-trip has been run against a real server or desktop install yet. See
  [`platform-support.md`](platform-support.md)'s "Known open items" and `privacyfence.service`'s own
  header comment — the one place this status has stayed accurate throughout.

## Testing

[`testing-policy.md`](testing-policy.md) describes the checks that currently run. [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) is the only plan document and tracks automation gaps that still exist.

The source files, tests, workflow definitions, build scripts, and configuration examples are authoritative if this reference drifts.
