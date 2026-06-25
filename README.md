# Loopline

**Loopline** is a macOS privacy proxy that sits between Claude (via MCP) and your personal data sources. Every time Claude tries to read an email, open a file, or fetch a Slack message, Loopline intercepts the request, applies configurable privacy filters, and shows you a floating approval window before any data reaches the AI.

---

## How it works

```
Claude ──MCP stdio──▶ loopline-bridge ──Unix socket──▶ loopline-app (daemon)
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Privacy Filter      │
                                                   │  (per-category)      │
                                                   └──────────┬──────────┘
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Auto-accept rules   │
                                                   │  (skip review gate)  │
                                                   └──────────┬──────────┘
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Floating window     │
                                                   │  Approve / Reject    │
                                                   └──────────┬──────────┘
                                                              │
                                                   ┌──────────▼──────────┐
                                                   │  Audit log           │
                                                   │  (JSONL + Excel)     │
                                                   └─────────────────────┘
```

**`loopline-bridge`** — an ephemeral MCP server spawned by Claude on each session. It auto-starts the daemon if it is not already running, fetches the connector manifest, and forwards every tool call over a Unix socket. Claude only ever talks to the bridge; the bridge carries no credentials.

**`loopline-app`** — the persistent daemon that owns all credentials, connectors, privacy filters, the review UI, and the audit log. Only one instance runs at a time (enforced via a lock file). It starts automatically at login via a LaunchAgent.

---

## Connectors

| Connector | Tools | Notes |
|-----------|-------|-------|
| **Gmail** | `gmail_list_messages`, `gmail_list_threads`, `gmail_get_message`, `gmail_get_thread`, `gmail_list_message_attachments`, `gmail_create_draft`, `gmail_add_label`, `gmail_remove_label` | OAuth2. List/search and label operations are auto-approved; reading bodies and threads requires approval. |
| **Google Drive** | `drive_list_files`, `drive_get_file_metadata`, `drive_get_file_content`, `drive_write_file_content`, `drive_create_file`, `drive_move_file`, `drive_add_comment`, `drive_list_folder` | OAuth2. Listing and metadata are auto-approved; content reads and writes are gated. |
| **Slack** | `slack_list_channels`, `slack_get_channel_history`, `slack_get_thread_replies`, `slack_search_messages`, `slack_send_message` | Bot token. Channel listing is auto-approved; reading messages and sending require approval. |
| **Google Contacts** | `contacts_list`, `contacts_search`, `contacts_get`, `contacts_update` | OAuth2. Read tools are auto-approved; writes are gated. |
| **Google Calendar** | `calendar_list_calendars`, `calendar_list_events`, `calendar_get_free_busy`, `calendar_get_event_details`, `calendar_create_event`, `calendar_update_event` | OAuth2. List and free/busy are auto-approved; event details and mutations are gated. |
| **Telegram** | `telegram_list_chats`, `telegram_get_messages`, `telegram_search_messages` | Telethon (MTProto). Read-only; all tools are auto-approved. |
| **Salesforce** | `salesforce_list_reports`, `salesforce_get_record`, `salesforce_run_report` | Username+password auth. Listing is auto-approved; record reads and reports are gated. |

---

## Privacy filter matrix

Every connector has its own set of data categories. Each category is assigned one of three policies:

| Policy | Effect |
|--------|--------|
| `allow` | Data passes through unchanged |
| `redact` | Data is partially masked (e.g. `***@domain.com`, first line only) |
| `block` | Data is replaced with `[BLOCKED BY PRIVACY FILTER]` |

The privacy filter is a **floor**, not a ceiling — data that passes the filter still goes through the human review gate (unless an auto-accept rule matches).

### Gmail categories

| Category | Controls |
|----------|---------|
| `body` | Message body text and HTML |
| `metadata` | Sender, recipients, date, subject |
| `attachments` | Attachment names and sizes (never content) |
| `thread_history` | Messages earlier in a thread |

### Google Drive categories

| Category | Controls |
|----------|---------|
| `file_content` | Document text / bytes |
| `file_metadata` | Name, owners, timestamps, sharing status |
| `file_list` | Results from list / search operations |
| `folder_structure` | Results from folder listing operations |

### Slack categories

| Category | Controls |
|----------|---------|
| `message_content` | Text of messages |
| `user_identity` | User names, emails, real names |
| `channel_list` | Channel names and metadata |
| `thread_content` | Thread reply content |

Policies are set in `config/settings.yaml` and can be toggled at runtime from the menu bar **Privacy Settings** submenu.

---

## Auto-accept rules

Routine, low-risk requests can be approved automatically without a UI prompt. Rules are configured per operation in `config/settings.yaml` under `auto_accept_rules`. When a rule matches, the request is logged as `auto_accepted` and returned to Claude immediately.

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
| `approved_folder` | File is in an approved folder (by Drive folder id) |
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

---

## Audit log

Every decision — approved, rejected, or auto-accepted — is appended to a JSON-lines file in `logs/audit/YYYY-WNN.jsonl`. At startup, any week that has a `.jsonl` file but no `.xlsx` is automatically exported to a formatted Excel workbook with a colour-coded **Decisions** sheet and a **Summary** tab.

---

## Installation

> **Google Cloud setup required for Google connectors (Gmail, Drive, Calendar, Contacts).**  
> See [docs/google-cloud-setup.md](docs/google-cloud-setup.md) for step-by-step instructions on creating a project, enabling APIs, and generating the `client_secret.json` file needed below.

### From the DMG (recommended)

1. Download the latest `Loopline.dmg` from the [Releases](../../releases) page.
2. Open the DMG, drag **Loopline.app** to `/Applications`.
3. Launch **Loopline.app** — the setup wizard opens automatically on first run and walks you through:
   - Importing your Google OAuth `client_secret.json`
   - Authorizing Gmail, Drive, Calendar, and Contacts
   - Entering your Slack bot token (optional)
   - Installing the LaunchAgent so Loopline starts at login
   - Copying the MCP config snippet for Claude

### From source

**Requirements:** Python 3.11+, macOS

```bash
git clone https://github.com/andras-tkcs/loopline
cd loopline
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Copy and edit the config:

```bash
cp src/loopline/resources/settings.yaml.example config/settings.yaml
# Edit config/settings.yaml with your credentials
```

Authorize each Google service (first-time setup):

```bash
loopline-app --gmail-oauth
loopline-app --drive-oauth
loopline-app --contacts-oauth
loopline-app --telegram-setup   # optional
```

Start the daemon:

```bash
loopline-app
```

---

## Connecting Claude

Add the bridge to Claude's MCP config (`~/.claude/claude_desktop_config.json` or equivalent):

```json
{
  "mcpServers": {
    "loopline": {
      "command": "loopline-bridge"
    }
  }
}
```

If running from source, replace `loopline-bridge` with the full path to `.venv/bin/loopline-bridge`.

---

## Building a DMG

```bash
pip install pyinstaller
bash scripts/build_dmg.sh
```

The script produces `dist/Loopline.dmg`.

---

## Configuration reference

See [`config/settings.yaml.example`](src/loopline/resources/settings.yaml.example) for a fully annotated configuration file covering all connectors, privacy categories, auto-accept rules, and logging options.

---

## Architecture notes

- The bridge is stateless and disposable — Claude can kill and restart it at any time without losing any state. All state (credentials, tokens, filters, queue) lives in the daemon.
- IPC between the bridge and the daemon uses a newline-delimited JSON protocol over a Unix domain socket (`~/.loopline/loopline.sock`).
- The daemon uses two threads: the main thread runs the tkinter floating window (a hard macOS requirement) and an IPC thread runs the asyncio event loop serving the bridge socket.
- Privacy filtering is applied *before* data reaches the review UI, so blocked content is never visible to the user in the approval window.

---

## License

Proprietary. All rights reserved.
