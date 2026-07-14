# QA Environment Setup — Isolated Accounts

This is a complete, standalone installation guide for the environment
[`connector-qa-testing.md`](connector-qa-testing.md) and the local fixture recorder (see
[`external-api-contract-testing.md`](external-api-contract-testing.md)) run against.

**This supersedes the previous version of this doc**, which scoped test fixtures to a
`PFQA`-prefixed project/space/channel/folder *inside your real accounts*. That model is retired:
every fixture below now lives in a **dedicated throwaway account, org, workspace, or site created
solely for this purpose** — a bug in a test, or a leaked/misused credential, can now only ever
touch synthetic data, never your real mailbox, real Jira/Confluence site, real Slack workspace, or
real Salesforce org. If you already built fixtures under the old model, they still work for a
human-supervised `connector-qa-testing.md` run, but don't reuse those credentials for the local
recorder — that tool is designed around the assumption that whatever credentials it holds are
already fully disposable.

---

## Prerequisites

1. **A dedicated QA identity** — set this up first; every section below depends on it. See
   [Step 0](#step-0--the-dedicated-qa-identity).
2. You have `privacyfence-app` running **from source, on your developer/test macOS account**
   ("Account 1" in [`dev-vs-live-setup.md`](dev-vs-live-setup.md)) — never authenticate any QA
   fixture from your real, live macOS account. This is what keeps the QA credentials and your real
   credentials in physically separate `~/.privacyfence`/repo-local config, with no shared state.
3. You can edit `config/settings.yaml` directly on Account 1 (repo root, since this is a
   from-source run — see [`dev-vs-live-setup.md`](dev-vs-live-setup.md)), and either restart the
   daemon afterward or trigger a hot-reload via the approval popup's "Accept All" button once.
4. You'll need `scripts/build_org_bundle.py` (already in this repo) to package the **QA-only**
   OAuth app credentials you create below into a bundle — kept entirely separate from whatever
   `org_config.json` your organization's real users install.

Everything this guide creates uses the prefix **`PFQA1`**/**`PFQA2`** (Jira/Confluence keys, two of
them now — see why below) or **`PrivacyFence QA`** (folder/channel/workspace/org names). Because
every one of these now lives in an account that has *nothing else in it*, the naming convention is
a courtesy for readability, not the isolation mechanism — the isolation comes from the account
being dedicated, not from a naming pattern a bug could still ignore.

---

## Step 0 — The dedicated QA identity

One mailbox, used for nothing except registering the accounts below, for the rest of this
project's life:

1. Create one new mailbox at a provider **not already linked to your real identity** — a fresh
   address at an independent provider (e.g. Proton Mail, Tutanota) is preferable to a Gmail alias
   of your real address, since an alias still resolves back to the same underlying account. If you
   do use Gmail for this mailbox itself, that's the one exception where you'll also need a fresh
   Google Account (see [§1](#1-google--dedicated-account--dedicated-oauth-app)) — don't reuse your
   real Google Account's alias/`+tag` for it.
2. Because this mailbox is used for **nothing else, ever**, isolation is automatic in both
   directions: nothing QA-related can leak into your real inbox, and nothing real can leak into
   this one. That's the actual guarantee — not a filter rule or a naming convention, which a
   misconfiguration could always defeat.
3. Use this mailbox as the recovery/contact email for every account created below, and set its own
   recovery to something *you* control but that is itself not your primary personal recovery chain
   (a password manager entry is enough — you don't need SMS/phone recovery on a mailbox that holds
   nothing sensitive).

**Copy-paste identity kit** — use these values verbatim wherever a signup form asks, so every
account is instantly recognizable and nothing has to be improvised per-service:

| Field | Value |
|---|---|
| Display name | `PrivacyFence QA` |
| Organization/company field | `PrivacyFence QA (isolated test environment)` |
| Google Cloud project name | `privacyfence-qa` |
| Atlassian site name | `privacyfence-qa` (→ `privacyfence-qa.atlassian.net`) |
| Jira project 1 (primary) | name `PrivacyFence QA Primary`, key `PFQA1` |
| Jira project 2 (contrast) | name `PrivacyFence QA Contrast`, key `PFQA2` |
| Confluence space 1 (primary) | name `PrivacyFence QA Primary`, key `PFQA1` |
| Confluence space 2 (contrast) | name `PrivacyFence QA Contrast`, key `PFQA2` |
| Slack workspace name | `PrivacyFence QA` |
| Slack channels | `#privacyfence-qa-approved`, `#privacyfence-qa-control` |
| Salesforce Developer Edition company name | `PrivacyFence QA` |
| Telegram display name | `PrivacyFence QA` |

---

## 1. Google — dedicated account + dedicated OAuth app

A personal Google Account's OAuth grant is account-wide (no project-level scoping the way Jira has
project keys), so this is the connector where *not* reusing your real account matters most.

1. Create a new Google Account using the Step 0 mailbox as its recovery address (or as the address
   itself, if you went the Gmail route). Google will ask for a phone number for verification — if
   you have a number you're comfortable dedicating to this identity, use it consistently across
   Google and Telegram (§7) rather than your real personal number; otherwise accept that this one
   piece of linkage exists and treat it as a known, minor limitation.
2. Create a **second, separate Google Cloud project** — `privacyfence-qa` — following
   [`google-cloud-setup.md`](google-cloud-setup.md) §§1–4 exactly as written, but signed in as the
   new QA Google Account, not your organization's real Cloud project. This is deliberate: it means
   the QA OAuth app's consent-screen configuration (Internal vs. External, test-user list) never
   touches your organization's real app, and deleting this Cloud project later fully tears down the
   QA Google integration with zero effect on production.
3. On step 3 of that guide (OAuth consent screen), choose **External**, and add the QA Google
   Account itself as the sole **Test user**. Skip the Admin SDK API (step 2's table) — no Workspace
   admin will ever exist on this account, so `calendar_list_rooms` isn't testable here regardless
   (see the caveat below).
4. Build a **QA-only** bundle, kept separate from your organization's real one:
   ```bash
   python3 scripts/build_org_bundle.py \
     --org-name "PrivacyFence QA" \
     --google-client-secret /path/to/qa_client_secret.json \
     -o org_config_qa.json
   ```
5. On your **dev/test macOS account only** (Account 1 — never Account 2, see
   [`dev-vs-live-setup.md`](dev-vs-live-setup.md)): install `org_config_qa.json` via the menu bar's
   **Organization Config → Install/Update…**, then authenticate headlessly:
   ```bash
   privacyfence-app --gmail-oauth
   privacyfence-app --drive-oauth
   privacyfence-app --calendar-oauth
   privacyfence-app --contacts-oauth
   privacyfence-app --tasks-oauth
   ```
   signing in as the new QA Google Account in the browser window each flow opens.

**Known, permanent gaps on a personal (non-Workspace) account** — not bugs, not worth working
around:
- `calendar_list_rooms` needs a Google Workspace admin directory; always empty/errors here.
- `contacts_list`/`contacts_search`/`contacts_get` with `source="directory"` or `"both"` needs
  Workspace directory colleagues; always empty here — only `source="personal"` is exercisable.
- `drive_list_shared_drives` needs a Shared Drive, which only a Workspace org can create; always
  empty here.

If you later want positive-path coverage for these three, the only way is a real Google Workspace
account (paid, trial-only otherwise) — treat that as a deliberate, separate decision, not something
to bolt onto this free setup.

## 2. Drive & Sheets

1. In the QA Google Account's Drive, create a folder named exactly `PrivacyFence QA Sandbox`. Note
   its file ID.
2. On the dev macOS account, add to `config/settings.yaml`:
   ```yaml
   auto_accept_rules:
     drive.read_file_contents:
       - rule: approved_folder
         value: ["<QA Sandbox folder id>"]
   ```
3. Everything created/uploaded/moved by a QA run goes inside this folder — trivially true now,
   since there's nothing else in this Drive to accidentally touch.
4. Optional `sheets.rename_sheet`/`sheets.format_range` → `approved_sandbox_folder` fixture: same as
   before, add if you want Phase 2 of `connector-qa-testing.md` to exercise the auto-accept path for
   those two tools instead of the popup path.

## 3. Slack — dedicated workspace

Not a channel in a real workspace anymore — a whole separate, free-tier Slack workspace:

1. Create a new Slack workspace named `PrivacyFence QA`, signed up with the Step 0 mailbox.
2. Create `#privacyfence-qa-approved` and join it. Add its channel ID to `settings.yaml`:
   ```yaml
   auto_accept_rules:
     slack.read_messages:
       - rule: approved_channel
         value: ["<channel id>"]
   ```
3. Create `#privacyfence-qa-control`, join it, post one message and reply to it in-thread (so
   `slack_get_thread_replies` always has something to find). **Do not** add it to `approved_channel`
   — it's the contrast case.
4. Build a QA-only Slack app/bundle the same way as Google (§1 step 4), using
   `--slack-client-id`/`--slack-client-secret`, and authenticate via `privacyfence-app
   --slack-oauth` on the dev account, signed in as this workspace.

## 4. Calendar

No separate fixture beyond the QA Google Account already created in §1. Add, if desired:
```yaml
auto_accept_rules:
  calendar.read_event_details:
    - rule: i_am_organizer
```
`calendar_list_rooms` stays a known gap (§1). `calendar_create_out_of_office` and
`calendar_set_working_location` always operate on the authenticated user's own primary calendar, so
they work here exactly as they would on a real account.

## 5. Contacts

No fixture beyond §1. Add, if desired:
```yaml
auto_accept_rules:
  contacts.edit:
    - rule: no_contact_info_change
```
`source="directory"`/`"both"` stays a known gap (§1) — only `source="personal"` is exercisable on
this account, and its "always empty" result for the directory case is itself the correct,
expected behavior to assert.

## 6. Google Tasks

1. The QA Google Account's default list ("My Tasks") works as the approved list — get its ID via
   `scripts/qa_list_ids.py tasks` (run against the QA account's auth) and add:
   ```yaml
   auto_accept_rules:
     tasks.update_task:
       - rule: approved_task_list
         value: ["<default list id>"]
     tasks.complete_task:
       - rule: approved_task_list
         value: ["<default list id>"]
     tasks.uncomplete_task:
       - rule: approved_task_list
         value: ["<default list id>"]
   ```
2. In Google Tasks (web), create a second list named exactly `PrivacyFence QA Contrast List` — the
   deliberately-unapproved contrast case.

## 7. Telegram

Telegram is the one connector with no clean "separate org" concept — `telethon` authenticates a
real user session bound to a phone number, and there's no bot-token equivalent for reading your own
"Saved Messages" or chat history.

1. **Use a phone number you actually own long-term** — a spare SIM/eSIM you control, not a
   disposable SMS-receiving service. This account will be signed in persistently and reused
   repeatedly, unlike a one-time OTP; a disposable number can be recycled by the carrier to a
   stranger later, who would then be able to take over the account via SMS-based recovery. If you
   don't have a spare number you're willing to dedicate to this long-term, treat Telegram as
   **out of scope for the local recorder** and keep it on the manual, human-supervised
   `connector-qa-testing.md` path only — that's a reasonable, explicit trade-off, not a gap to force
   a fix for.
2. Register a new Telegram account with that number, display name `PrivacyFence QA`.
3. Send yourself one message in Saved Messages (one-time, manual — `telegram_list_chats` only
   returns chats you've opened at least once).
4. Decide `approved_chats` the same way as before (Saved Messages itself, or a second low-stakes
   chat within this same throwaway account):
   ```yaml
   auto_accept_rules:
     telegram.read_chat_messages:
       - rule: approved_chats
         value: ["<chat_id>"]
   ```
5. Authenticate on the dev macOS account via `privacyfence-app --telegram-setup`, using this
   account's number.

## 8. Salesforce

Salesforce Developer Edition is already a real, isolated sandbox by construction — the change here
is only *whose* identity signs up for it:

1. Sign up for a Developer Edition org at [developer.salesforce.com](https://developer.salesforce.com)
   using the Step 0 mailbox and the company name `PrivacyFence QA` — not your real name/email/company.
2. **Setup → Object Manager → Account** → create 2–3 sample records prefixed `PrivacyFence QA — `.
3. **Reports → New Report** on that object, named exactly `PrivacyFence QA Report`.
4. Add fixtures:
   ```yaml
   auto_accept_rules:
     salesforce.run_report:
       - rule: approved_report_ids
         value: ["<PrivacyFence QA Report id>"]
     salesforce.read_record:
       - rule: approved_object_types
         value: [Account]
   ```
5. Build a QA-only Connected App (`--salesforce-consumer-key`/`--salesforce-consumer-secret`) and
   authenticate via `privacyfence-app --salesforce-oauth` on the dev account.
6. Because this org is freshly created and otherwise empty, there's no "other report/object type"
   to borrow as the contrast case the way the old doc did — create one more throwaway object record
   (any second standard object, e.g. Contact) purely to serve as "should still prompt."

## 9. Jira — dedicated Atlassian site

Previously this reused your real company/personal Atlassian site with a scoped project key. Now:
create an entirely new site, since the old design's "any other existing project in your site" was
implicitly relying on your site having *real* projects in it — a dedicated site starts empty, so
the contrast project has to be created deliberately.

1. Sign up for a new Atlassian account with the Step 0 mailbox, and create a **new site** named
   `privacyfence-qa` (not adding yourself to an existing company site).
2. Create Jira project **`PFQA1`** ("PrivacyFence QA Primary", Kanban template).
3. Create a **second** Jira project **`PFQA2`** ("PrivacyFence QA Contrast") — this is the
   deliberate replacement for "whatever other project already exists," since none will.
4. Add:
   ```yaml
   auto_accept_rules:
     jira.read_issue:
       - rule: approved_project_keys
         value: [PFQA1]
   ```
5. Build a QA-only Atlassian OAuth app (`--atlassian-client-id`/`--atlassian-client-secret`, one
   grant covers both Jira and Confluence) and authenticate via `privacyfence-app --atlassian-oauth`
   on the dev account.
6. `i_am_reporter`/`i_am_assignee`, `jira_get_transitions`/`jira_transition_issue`, and the optional
   `custom_fields` check need no separate setup beyond what's already documented in the previous
   version of this guide — create the custom field on `PFQA1`'s issue screen if you want that
   opportunistic check exercised.

## 10. Confluence

Same site as §9 (Atlassian sites bundle both products):

1. Create Confluence space **`PFQA1`** ("PrivacyFence QA Primary").
2. Create a **second** space **`PFQA2`** ("PrivacyFence QA Contrast") — same reasoning as the second
   Jira project: nothing else exists on this site to borrow as a contrast case.
3. Add:
   ```yaml
   auto_accept_rules:
     confluence.read_page:
       - rule: approved_space_keys
         value: [PFQA1]
   ```
4. `i_am_author` needs no setup — any page created in `PFQA1` satisfies it automatically.
5. Confirm the daemon build under test has the Confluence v1→v2 migration (`confluence_client.py`)
   — `get_page`/`create_page`/`update_page` 410 without it, unrelated to this environment setup.

## 11. PII Detection Gate

No fixture to create — unchanged from before. The dedicated check in
[`connector-qa-testing.md`](connector-qa-testing.md) (Phase 2, steps 17–20) creates its own
throwaway Drive subfolder and Google Doc seeded with synthetic PII inside the QA Google Account from
§1, and tears both down itself. Nothing here was ever account-scoping-dependent — it was already
self-contained.

Confirm **PII Detection Gate** is enabled in the menu bar on the dev account (default on) before
running it.

---

## Consolidated `auto_accept_rules` block

Merge into `config/settings.yaml` **on the dev/test macOS account only**:

```yaml
auto_accept_rules:
  drive.read_file_contents:
    - rule: approved_folder
      value: ["<QA Sandbox folder id>"]
  slack.read_messages:
    - rule: approved_channel
      value: ["<approved channel id>"]
  calendar.read_event_details:
    - rule: i_am_organizer
  contacts.edit:
    - rule: no_contact_info_change
  telegram.read_chat_messages:
    - rule: approved_chats
      value: ["<chat_id>"]
  salesforce.run_report:
    - rule: approved_report_ids
      value: ["<PrivacyFence QA Report id>"]
  salesforce.read_record:
    - rule: approved_object_types
      value: [Account]
  jira.read_issue:
    - rule: approved_project_keys
      value: [PFQA1]
  confluence.read_page:
    - rule: approved_space_keys
      value: [PFQA1]
  tasks.update_task:
    - rule: approved_task_list
      value: ["<default task list id>"]
  tasks.complete_task:
    - rule: approved_task_list
      value: ["<default task list id>"]
  tasks.uncomplete_task:
    - rule: approved_task_list
      value: ["<default task list id>"]
```

`gmail.read_message` → `trusted_sender_domain` is intentionally left out here: it needs a domain
you *actually* receive recurring mail from, which a freshly-created, single-purpose QA mailbox
won't have. Leave `gmail.read_message` always review-gated in this environment, or skip that one
Phase 1 auto-accept check in `connector-qa-testing.md` and note it as an environment limitation.

Restart the daemon after editing (or use "Accept All" once, which hot-reloads rules).

---

## Fixture reference

| Fixture | How it's found | Source |
|---|---|---|
| Drive QA folder | Exact name `PrivacyFence QA Sandbox` | `drive_list_files` |
| Slack approved channel | `slack.read_messages` → `approved_channel` | `settings.yaml` |
| Slack control channel | Exact name `privacyfence-qa-control` | `slack_list_channels` |
| Telegram Saved Messages | `is_self: true` flag | `telegram_list_chats` |
| Telegram approved chat | `telegram.read_chat_messages` → `approved_chats` (falls back to Saved Messages) | `settings.yaml` |
| Salesforce QA report | `salesforce.run_report` → `approved_report_ids` (falls back to exact name `PrivacyFence QA Report`) | `settings.yaml` / `salesforce_list_reports` |
| Jira QA project | Literal key `PFQA1` | — |
| Jira contrast project | Literal key `PFQA2` | — |
| Confluence QA space | Literal key `PFQA1` | — |
| Confluence contrast space | Literal key `PFQA2` | — |
| Tasks approved list | `tasks.update_task` → `approved_task_list` (falls back to the default list) | `settings.yaml` / `tasks_list_task_lists` |
| Tasks contrast list | Exact name `PrivacyFence QA Contrast List` | `tasks_list_task_lists` |

---

## Idempotency: environment fixtures vs. per-run artifacts

Unchanged from before: the accounts/orgs/sites/workspaces and the fixtures within them (this doc)
are created **once**; per-run artifacts (drafts, events, one-off issues/pages/files created by a
`connector-qa-testing.md` run or the local recorder) carry a run-scoped identifier and get torn down
by that process. The only thing that changed is *where* those fixtures live — inside dedicated
accounts instead of scoped corners of your real ones.
