# PrivacyFence

**PrivacyFence** is a macOS privacy proxy that sits between Claude (via MCP) and your personal data sources. Every time Claude tries to read an email, open a file, or fetch a Slack message, PrivacyFence intercepts the request and requires your approval before any data reaches the AI.

---

## How it works

```
Claude ──MCP stdio──▶ privacyfence-bridge ──Unix socket──▶ privacyfence-app (daemon)
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Auto-accept rules   │
                                                   │  (skip review gate)  │
                                                   └──────────┬──────────┘
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Review gate         │
                                                   │  Cowork / popup      │
                                                   └──────────┬──────────┘
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Audit log           │
                                                   │  (JSONL + Excel)     │
                                                   └─────────────────────┘
```

**`privacyfence-bridge`** — an ephemeral MCP server spawned by Claude on each session. It auto-starts the daemon if it is not already running, fetches the connector manifest, and forwards every tool call over a Unix socket. Claude only ever talks to the bridge; the bridge carries no credentials.

**`privacyfence-app`** — the persistent daemon that owns all credentials, connectors, the review gate, and the audit log. Only one instance runs at a time (enforced via a lock file). It starts automatically at login via a LaunchAgent.

---

## Review model

Every tool call passes through one of three gate values:

| Gate | Behaviour |
|------|-----------|
| `auto` | Passed through immediately, logged as `auto_accepted` |
| `review` | Approval requested in Claude Cowork (see below) |
| `popup` | Approval requested via PrivacyFence native popup |

### Two flows by direction

**Tool → Claude (reads)** — annotated `readOnlyHint = true` in MCP.

When the gate is `review`, a prompt appears in Claude Cowork showing a minimal preview of the request:

- **Accept** — data is returned to Claude
- **Deny** — request is blocked; Claude receives an error
- **Show Details** — PrivacyFence opens a scrollable native popup with the full content (e.g. the email body), which then offers **Accept** or **Deny**

**Claude → Tool (writes / actions)** — annotated `destructiveHint = true` where relevant.

Claude already describes the action it is about to take in the chat. When the gate is `popup`, PrivacyFence opens a native popup showing the full action details with **Accept** or **Deny**. There is no intermediate Cowork step.

---

## Connectors & privacy matrix

### Gmail

**Auth:** OAuth2

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `gmail_list_messages` | read | auto | — | — |
| `gmail_list_threads` | read | auto | — | — |
| `gmail_get_message` | read | review | from, recipients, date, subject | Full body text |
| `gmail_get_thread` | read | review | subject, all participants, message count, date range | All messages in thread |
| `gmail_list_message_attachments` | read | review | from, recipients, date, subject | Attachment names & sizes |
| `gmail_create_draft` | write | popup | — | To, cc, subject, full body |
| `gmail_add_label` | write | popup | — | From, subject, label name |
| `gmail_remove_label` | write | popup | — | From, subject, label name |
| `gmail_archive_message` | write | popup | — | From, subject, confirmation that message stays in All Mail |

### Google Drive

**Auth:** OAuth2

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `drive_list_files` | read | auto | — | — |
| `drive_get_file_metadata` | read | auto | — | — |
| `drive_list_folder` | read | auto | — | — |
| `drive_list_shared_drives` | read | auto | — | — |
| `drive_create_blank_file` | write | auto | — | — |
| `drive_get_file_content` | read | review | file name, owner, size, modified date | First ~500 chars of content |
| `drive_download_file` | read | popup | — | File name, owner, size, save path |
| `drive_write_file_content` | write | popup | — | File name, owner, new content (plain text) |
| `drive_write_doc_content` | write | popup | — | File name, owner, Markdown preview (headings, bold, italic, links, lists rendered as rich formatting in the Google Doc) |
| `drive_move_file` | write | popup | — | File name, from folder → to folder |
| `drive_add_comment` | write | popup | — | File name, full comment text |

### Slack

**Auth:** OAuth2 (browser sign-in), user token scope. Sees exactly what you see — no bot to invite. See [docs/slack-setup.md](docs/slack-setup.md).

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `slack_list_channels` | read | auto | — | — |
| `slack_get_channel_history` | read | review | channel name, message count, first message (80 chars) | All messages |
| `slack_get_thread_replies` | read | review | channel name, thread starter (80 chars), reply count | All replies |
| `slack_search_messages` | read | review | query, result count | All results |
| `slack_send_message` | write | popup | — | Channel name, full message text (optional `mark_unread=true` leaves the message unread after sending; requires `mark` scope) |

### Google Calendar

**Auth:** OAuth2

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `calendar_list_calendars` | read | auto | — | — |
| `calendar_list_events` | read | auto | — | — |
| `calendar_get_free_busy` | read | auto | — | — (returns full events when calendar access is available; falls back to busy-slot list otherwise) |
| `calendar_list_rooms` | read | auto | — | — (lists Google Workspace meeting rooms with name, email, building, floor, capacity; requires Workspace admin directory access) |
| `calendar_get_event_details` | read | review | title, time, organizer, attendee count | Description, full attendee list, conferencing link |
| `calendar_create_event` | write | popup | — | Title, time, attendees, description, location, Google Meet flag, room bookings |
| `calendar_update_event` | write | popup | — | Title, time, fields changing (old → new), Google Meet flag, room bookings |

### Google Contacts

**Auth:** OAuth2

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `contacts_list` | read | auto | — | — |
| `contacts_search` | read | auto | — | — |
| `contacts_get` | read | auto | — | — |
| `contacts_update` | write | popup | — | Contact name, fields changing (old → new) |

### Telegram

**Auth:** Telethon (MTProto). Reads your chats as you, not as a bot.

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `telegram_list_chats` | read | auto | — | — |
| `telegram_get_messages` | read | review | chat name, message count | All messages |
| `telegram_search_messages` | read | review | query, result count | All results |
| `telegram_send_message` | write | popup | — | Chat name, full message text |

### Salesforce

**Auth:** OAuth2 (browser sign-in via a Connected App). See [docs/salesforce-setup.md](docs/salesforce-setup.md).

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `salesforce_list_reports` | read | auto | — | — |
| `salesforce_get_record` | read | review | object type, record name, record ID | All field values |
| `salesforce_run_report` | read | review | report name, report ID | All report rows |

### Jira

**Auth:** OAuth2 (browser sign-in, Atlassian 3LO). Shared with Confluence — one sign-in covers both. See [docs/atlassian-setup.md](docs/atlassian-setup.md).

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `jira_list_projects` | read | auto | — | — |
| `jira_search_issues` | read | auto | — | — |
| `jira_get_issue` | read | review | project name, key, summary, status, assignee | Description, comments, all fields |
| `jira_create_issue` | write | popup | — | Project, type, summary, full description |
| `jira_add_comment` | write | popup | — | Issue key + summary, full comment |
| `jira_update_issue` | write | popup | — | Issue key + summary, fields (old → new) |

### Confluence

**Auth:** OAuth2 (browser sign-in, Atlassian 3LO), shared with Jira — one sign-in covers both.

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `confluence_list_spaces` | read | auto | — | — |
| `confluence_search` | read | auto | — | — |
| `confluence_cql_search` | read | auto | — | — |
| `confluence_list_pages` | read | auto | — | — |
| `confluence_get_page` | read | review | title, space, author, last modified | Full page body |
| `confluence_get_page_by_title` | read | review | title, space, author, last modified | Full page body |
| `confluence_create_page` | write | popup | — | Space, title, parent page, full body |
| `confluence_update_page` | write | popup | — | Title, space, full new body |

### Google Tasks

**Auth:** OAuth2

| Tool | Dir | Gate | Cowork preview | Details popup |
|------|-----|------|----------------|---------------|
| `tasks_list_task_lists` | read | auto | — | — |
| `tasks_list_tasks` | read | auto | — | — |
| `tasks_get_task` | read | auto | — | — |
| `tasks_create_task` | write | auto | — | — |
| `tasks_update_task` | write | auto | — | — |
| `tasks_complete_task` | write | auto | — | — |
| `tasks_uncomplete_task` | write | auto | — | — |
| `tasks_move_task` | write | auto | — | — |

---

## Auto-accept rules

Routine, low-risk requests can be approved automatically without a prompt. Rules are configured per operation in `config/settings.yaml` under `auto_accept_rules`. When a rule matches, the gate is bypassed and the request is logged as `auto_accepted`.

### Available rules

**Gmail**

| Rule | Matches when… |
|------|--------------|
| `i_am_sender` | The authenticated account is the sender |
| `i_am_sole_recipient` | The only recipient is the authenticated account |
| `trusted_sender_domain` | Sender's domain is in the allowlist |
| `label_match` | Message carries one of the specified labels |
| `age_threshold_days` | Message is older than N days |
| `no_attachments` | Message has no attachments |

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

**Slack**

| Rule | Matches when… |
|------|--------------|
| `dm_with_myself` / `send_to_myself` | Target channel is a self-DM |
| `approved_channel` / `approved_recipient` | Channel ID is in the allowlist |
| `public_channels_only` | All messages are from public channels |
| `no_file_attachments` | Messages have no file attachments |
| `reply_in_existing_thread` | Message is a reply (has `thread_ts`) |

**Google Calendar**

| Rule | Matches when… |
|------|--------------|
| `i_am_organizer` | Authenticated account is the event organizer |
| `no_external_attendees` | All attendees share the same email domain |
| `personal_calendar` | Event is from a specified calendar ID |
| `past_event` | Event end time is in the past |
| `time_window_days` | Event starts within the next N days |
| `no_conferencing_link` | Event has no video conferencing link |

**Salesforce**

| Rule | Matches when… |
|------|--------------|
| `approved_object_types` | Object type (Account, Contact, …) is in the allowlist |
| `approved_report_ids` | Report ID is in the approved list |

**Jira**

| Rule | Matches when… |
|------|--------------|
| `approved_projects` | Issue's project key is in the allowlist |

**Confluence**

| Rule | Matches when… |
|------|--------------|
| `approved_spaces` | Page's space key is in the allowlist |

> **Google Contacts and Google Tasks** have no configurable auto-accept rules — all their tools are unconditionally auto-accepted and logged as `auto_accepted`. **Telegram** read tools (`telegram_list_chats`, `telegram_get_messages`, `telegram_search_messages`) are also unconditionally auto-accepted; `telegram_send_message` requires popup approval.

---

## Audit log

Every decision — accepted, denied, or auto-accepted — is appended to a JSON-lines file in `logs/audit/YYYY-WNN.jsonl`. At startup, any week that has a `.jsonl` file but no `.xlsx` is automatically exported to a formatted Excel workbook with a colour-coded **Decisions** sheet and a **Summary** tab.

---

## Installation

PrivacyFence splits configuration into two steps done by two different people:

1. **IT admin, once per organization:** register a cloud app for each service you want (Google,
   Slack, Salesforce, Atlassian, Telegram) and package the result into one organization config
   bundle with `scripts/build_org_bundle.py`. See the "For IT admins" section of each doc below.
2. **Every user, from the PrivacyFence menu bar:** install the bundle IT sent you, then click
   **Authenticate…** on each connector you want. Almost everywhere this opens your browser to sign
   in — Telegram is the only connector that instead asks for your phone number and a verification
   code, since MTProto has no browser-OAuth equivalent.

> See [docs/google-cloud-setup.md](docs/google-cloud-setup.md), [docs/slack-setup.md](docs/slack-setup.md), [docs/salesforce-setup.md](docs/salesforce-setup.md), [docs/atlassian-setup.md](docs/atlassian-setup.md), and [docs/telegram-setup.md](docs/telegram-setup.md) for the full walkthroughs.

### From the DMG (recommended)

The DMG carries both halves of PrivacyFence — the daemon and the Claude extension — so this is
the only download you need:

1. Download the latest `PrivacyFence-<version>.dmg` from the [Releases](../../releases) page.
2. Open the DMG, drag **PrivacyFenceApp.app** to `/Applications`, and launch it. The menu bar icon
   appears immediately — there's no setup wizard to walk through.
3. To start PrivacyFence automatically at login, install the LaunchAgent once:
   ```bash
   cp com.privacyfence.app.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.privacyfence.app.plist
   ```
4. From the menu bar: **Organization Config → Install/Update Organization Config…**, and select
   the bundle your IT team sent you.
5. **Connectors → \<service\> → Authenticate…** for each connector you want, then quit and reopen
   PrivacyFence to activate them.
6. Still in the mounted DMG, double-click **PrivacyFence.mcpb** — Claude Desktop installs the
   MCP server for you (Settings → Extensions → Install Extension… happens automatically).

### From source

**Requirements:** Python 3.11+, macOS

```bash
git clone https://github.com/andras-tkcs/privacyfence
cd privacyfence
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Copy the config (privacy policy / auto-accept rules — no secrets live here):

```bash
cp src/privacyfence/resources/settings.yaml.example config/settings.yaml
```

Build (or obtain from IT) an organization config bundle, then authorize each connector you want —
either from the menu bar once `privacyfence-app` is running, or headlessly from the CLI:

```bash
python3 scripts/build_org_bundle.py --google-client-secret /path/to/client_secret.json -o org_config.json
mkdir -p ~/.privacyfence/org && cp org_config.json ~/.privacyfence/org/

privacyfence-app --gmail-oauth
privacyfence-app --drive-oauth
privacyfence-app --calendar-oauth
privacyfence-app --contacts-oauth
privacyfence-app --tasks-oauth
privacyfence-app --slack-oauth        # if the bundle has a Slack app
privacyfence-app --salesforce-oauth   # if the bundle has a Salesforce Connected App
privacyfence-app --atlassian-oauth    # if the bundle has an Atlassian OAuth app
privacyfence-app --telegram-setup     # if the bundle has a Telegram app
```

Start the daemon:

```bash
privacyfence-app
```

---

## Connecting Claude

The daemon and the bridge are built and shipped separately:

- **PrivacyFenceApp.app** (built by `scripts/build_dmg.sh`) — the daemon: owns credentials,
  connectors, the review gate, the audit log, and the LaunchAgent. Install this first via the DMG.
- **PrivacyFence.mcpb** (built by `scripts/build_mcpb.sh`, from `PrivacyFenceBridge.spec`) — just
  the bridge: a small MCP server that talks to the daemon over a Unix socket. Install this into Claude.

### Option A: one-click extension (Claude Desktop)

`PrivacyFence.mcpb` ships inside the DMG alongside `PrivacyFenceApp.app` (see above) — just
double-click it and Claude Desktop installs the MCP server for you, no
`claude_desktop_config.json` editing.

The daemon (PrivacyFenceApp.app) must already be installed and configured first — the extension
only contains `privacyfence-bridge`, built from its own minimal dependency set (no google-auth,
slack_sdk, telethon, atlassian-python-api, rumps, or tkinter — that's why it's ~30MB instead of
the daemon's ~185MB).

To build both artifacts yourself:

```bash
pip install pyinstaller
brew install create-dmg
bash scripts/build_dmg.sh
```

This runs `scripts/build_mcpb.sh` as part of assembling the DMG. To build just the extension
on its own (e.g. for a quick local test without a full DMG), run `bash scripts/build_mcpb.sh`
directly — it produces `dist/PrivacyFence-<version>.mcpb`.

### Option B: manual MCP config (Claude Desktop, Claude Code, or other MCP clients)

Add the bridge to Claude's MCP config (`~/.claude/claude_desktop_config.json` or equivalent):

```json
{
  "mcpServers": {
    "privacyfence": {
      "command": "privacyfence-bridge"
    }
  }
}
```

If running from source, replace `privacyfence-bridge` with the full path to `.venv/bin/privacyfence-bridge`.

For Claude Code, you can skip editing JSON by running:

```bash
claude mcp add privacyfence privacyfence-bridge
```

---

## Building a DMG

```bash
pip install pyinstaller
bash scripts/build_dmg.sh
```

The script produces `dist/PrivacyFence-<version>.dmg` (containing `PrivacyFenceApp.app`).

---

## Configuration reference

See [`config/settings.yaml.example`](src/privacyfence/resources/settings.yaml.example) for a fully annotated configuration file covering all connectors, auto-accept rules, and logging options.

---

## Architecture notes

- The bridge is stateless and disposable — Claude can kill and restart it at any time without losing any state. All state (credentials, tokens, filters, queue) lives in the daemon.
- IPC between the bridge and the daemon uses a newline-delimited JSON protocol over a Unix domain socket (`~/.privacyfence/privacyfence.sock`).
- The daemon uses two threads: the main thread runs the rumps menu bar app (a hard macOS requirement for AppKit) and an IPC thread runs the asyncio event loop serving the bridge socket. Approval popups are shown via `osascript` subprocesses and can be called from any thread.
- Read tools carry `readOnlyHint = true` in their MCP annotations; write tools that modify external state carry `destructiveHint = true`.

---

## License

Proprietary. All rights reserved.
