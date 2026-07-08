# QA Environment Setup (for `connector-qa-testing.md`)

This is a complete, standalone installation guide for the environment that
[`connector-qa-testing.md`](connector-qa-testing.md) runs against. Follow it
top to bottom on any machine/account where you want to run that test — it
doesn't assume anything was set up before, and it doesn't assume you've read
any prior QA report.

**Scope**: this doc only covers QA-specific fixtures — a dedicated Jira
project, a Drive sandbox folder, sample Salesforce records, and so on. Base
connector *authentication* (OAuth apps, API enablement, tokens) is a
separate, one-time prerequisite covered by each connector's own setup guide;
see Prerequisites below.

Once set up, these fixtures are **durable and reusable** — the test prompt
re-discovers them by name/key/flag at the start of every run instead of you
recreating or re-pasting anything (see [`connector-qa-testing.md`](connector-qa-testing.md)'s
Phase 0). You should only need to work through this guide once per
environment, and revisit a section if you ever rename/delete one of the
fixtures it describes.

---

## Prerequisites

Before starting, confirm:

1. **Every connector you want to test is already authenticated** from the
   PrivacyFence menu bar. If not, work through the relevant setup guide
   first: [`google-cloud-setup.md`](google-cloud-setup.md) (Gmail, Drive,
   Calendar, Contacts, Tasks), [`slack-setup.md`](slack-setup.md),
   [`atlassian-setup.md`](atlassian-setup.md) (Jira & Confluence),
   [`salesforce-setup.md`](salesforce-setup.md),
   [`telegram-setup.md`](telegram-setup.md). This doc assumes that's already
   done for every connector you plan to exercise; it only adds QA-specific
   fixtures on top.
2. You have `privacyfence-app` (the daemon) running, and admin/owner-level
   access on each external service to create a project/space/channel/folder
   in it (not just read access).
3. You can edit `~/.privacyfence/config/settings.yaml` directly (it's a
   local file, not managed through the menu bar UI) and either restart the
   daemon afterward, or trigger a hot-reload by using the approval popup's
   "Accept All" button once, which calls `reload_rules()` for you.
4. You know your own email address as PrivacyFence sees it (the
   `my_email` used internally for rules like `i_am_sender`/`i_am_organizer`)
   — this is just whatever address you authenticated Gmail/Calendar/etc.
   with, nothing extra to configure.

Everything this guide creates uses the prefix **`PFQA`** (Jira/Confluence
keys) or **`PrivacyFence QA`** (folder/channel/report names, titles) so it's
grep-able and obviously safe to touch. **Names must match exactly** where
noted — the test prompt looks fixtures up by exact string match, not fuzzy
search, so a typo here means Phase 0 reports the fixture as missing instead
of silently guessing wrong.

---

## 1. Gmail

No fixture to create, but the `trusted_sender_domain` auto-accept rule only
does anything useful if it points at a domain your mailbox actually receives
mail from:

1. Look through your inbox (or run `gmail_list_messages`) and pick any
   sender domain you get recurring mail from — a newsletter, a receipt
   sender, a notification address. Anything works; it just has to be real.
2. In `~/.privacyfence/config/settings.yaml`:
   ```yaml
   auto_accept_rules:
     gmail.read_message:
       - rule: trusted_sender_domain
         value: [example.com]   # ← whatever domain you actually receive mail from
   ```
   Rules are a list — add more than one domain if you like.

No fixture is needed for the Deny test either, but the test prompt asks you
to pick a **short** message with no large attachments for that specific
step: a large message makes a Deny response indistinguishable from a
size-truncation error, which defeats the point of testing Deny at all.

No fixture is needed for the archive/label round-trip (add label → archive →
un-archive → remove label) — it targets whatever message you already picked
for the very first `gmail_get_message` call and restores it to its exact
starting state by the end, so there's nothing to provision or clean up here.

## 2. Drive & Sheets

1. Create a folder named **exactly `PrivacyFence QA Sandbox`** in "My
   Drive". Note its file ID (visible in the folder's URL) — you need it once,
   for step 2 below; the test prompt itself finds the folder by name, not by
   ID.
2. Add an auto-accept fixture so `drive.read_file_contents` /
   `drive.download_file` have something to match against:
   ```yaml
   auto_accept_rules:
     drive.read_file_contents:
       - rule: approved_folder
         value: ["<QA Sandbox folder id>"]
   ```
3. Every Drive/Sheets artifact the test prompt creates goes **inside this
   folder** (as `parent_folder_id`), including uploads, Docs, and moves — so
   nothing it does ever touches a real file or a real folder elsewhere in
   your Drive.
4. Drive has no bulk-empty-trash tool, so periodically empty Drive trash by
   hand if you want a clean slate; trashed items auto-purge after 30 days
   regardless.

## 3. Slack

Two channels are needed: one the test prompt is allowed to read silently,
and one it isn't (to prove the review gate still fires for anything not on
the allowlist).

1. Pick (or create) a channel to be the **approved** one and join it. Add its
   channel ID to `settings.yaml`:
   ```yaml
   auto_accept_rules:
     slack.read_messages:
       - rule: approved_channel
         value: ["<channel id>"]
   ```
   The test prompt reads this ID straight out of the config — there's no
   naming requirement on this channel.
2. Create a second channel named **exactly `privacyfence-qa-control`** and
   join it. **Do not** add it to `approved_channel` — it exists specifically
   to *not* match, and the test prompt finds it by this exact name via
   `slack_list_channels`.
3. In `privacyfence-qa-control`, post one message and then reply to it
   in-thread (replying from your own account is fine — Slack doesn't require
   a second person for a thread to exist). This gives
   `slack_get_thread_replies` permanent, reusable content instead of
   depending on whatever a given run happens to surface.

## 4. Calendar

`calendar_list_rooms` needs **Google Workspace** (not a consumer Gmail
account) *and* admin rights — this is a hard external dependency, not
something any local configuration can substitute for. Determine which
situation you're in:

- **Consumer Gmail, or no Workspace admin access:** skip straight to "No
  other fixture needed" below. This is a permanent, environment-level
  limitation — the test prompt already treats the resulting error as
  expected rather than a regression, so there's nothing to configure to make
  that graceful.
- **Workspace admin access available:**
  1. In Google Cloud Console → APIs & Services → Library, enable **Admin SDK
     API** (see [`google-cloud-setup.md`](google-cloud-setup.md)).
  2. Nothing to add manually for the OAuth scope —
     `admin.directory.resource.calendar.readonly` is requested at runtime by
     `calendar_client.py`.
  3. In the Google Admin console: **Directory → Buildings and resources →
     Calendar resources → Add resource**. Create at least one, e.g.
     `PrivacyFence QA Room A`.
  4. In PrivacyFence: **Connectors → Calendar → Reconnect…** so the token
     picks up the new admin scope.

No other Calendar fixture is needed: any event the test prompt creates
satisfies the `i_am_organizer` auto-accept rule automatically (you organize
anything you create), so add that rule if you want it exercised:
```yaml
auto_accept_rules:
  calendar.read_event_details:
    - rule: i_am_organizer
```

**Optional, for exercising event attachments:** `calendar_get_event_details`
also returns file attachments — most commonly the "Notes by Gemini" and
transcript Docs that Google Meet attaches to an event once a meeting with
"take notes for me" enabled ends. There's no fixture for this (attachments
can't be created via `calendar_create_event`), so it's opportunistic: if this
account's calendar history already has a past meeting like that, the test
prompt will find and use it; otherwise that step is skipped as a known
limitation, not a regression.

## 5. Contacts

No fixture needed for `source="personal"` vs. `source="directory"` — whether
your account has Workspace directory colleagues is a fact about your
account, not something to provision. If it doesn't, `source="directory"`
coming back empty is the correct, permanent answer.

Add this rule to get `no_contact_info_change` exercised:
```yaml
auto_accept_rules:
  contacts.edit:
    - rule: no_contact_info_change
```
It only auto-accepts edits that don't touch `emails`/`phones` — e.g.
appending `(PrivacyFence QA test)` to a name or note field — so it's safe to
leave enabled permanently. An edit that *does* touch email/phone still
prompts even with this rule present.

## 6. Google Tasks

Reads and the create/update/complete/uncomplete lifecycle work against
whatever task list you already have — no fixture needed for those; the test
prompt creates and cleans up its own task. Exercising `approved_task_list`
needs a second list, though:

1. Your default list (usually named **My Tasks**) works fine as the
   **approved** one — get its ID from `tasks_list_task_lists` and add it to
   `settings.yaml`:
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
   (`tasks.create_task` and `tasks.move_task` are left out here deliberately —
   see step 3.)
2. In Google Tasks (web or mobile), create a second list named **exactly
   `PrivacyFence QA Contrast List`**. There's no tool to create a task list
   through PrivacyFence itself, so this has to be done outside it. **Do not**
   add it to `approved_task_list` — it exists specifically to *not* match, and
   the test prompt finds it by this exact name via `tasks_list_task_lists`.
3. Optional: add `tasks.create_task` the same way as step 1, and/or add
   `tasks.move_task` pointing at *both* list IDs (a move only auto-accepts
   when both the source and destination list are approved):
   ```yaml
   auto_accept_rules:
     tasks.move_task:
       - rule: approved_task_list
         value: ["<default list id>", "<contrast list id>"]
   ```
   Skip any of these you'd rather leave always-prompting — the test prompt
   handles "not configured" gracefully for each one independently.

## 7. Telegram

1. Open Telegram (phone or desktop) and send yourself **one** message in
   Saved Messages, manually, right now — this is a one-time action.
   `telegram_list_chats` only returns chats you've actually opened at least
   once, and "Saved Messages" is no exception; once you've sent that first
   message it stays in the list forever. No naming needed afterward: the
   test prompt finds it every run via the `is_self` flag
   `telegram_list_chats` already returns, not by matching a name.
2. Decide what `approved_chats` should point at. Either:
   - Point it at Saved Messages itself (get its numeric `chat_id` from
     `telegram_list_chats` after step 1) — safe, since it's always fine to
     auto-accept reads of your own messages to yourself, or
   - Create/repurpose a second low-stakes chat (a private group with just
     you, or a throwaway test contact) and use its `chat_id` instead.
   ```yaml
   auto_accept_rules:
     telegram.read_chat_messages:
       - rule: approved_chats
         value: ["<chat_id>"]
   ```
   The test prompt reads this ID from `settings.yaml` directly — no naming
   requirement on the chat either way. Skip this rule entirely if you'd
   rather leave Telegram reads always review-gated; the test prompt handles
   "not configured" gracefully.
3. Make sure at least one *other* chat beyond your approved one has some
   message history — the test prompt picks any such chat dynamically for
   the "not approved, should still prompt" contrast case and for
   `telegram_search_messages` to have something to find.

Whether a Cowork review popup actually appears for `telegram_get_messages` /
`telegram_search_messages` can be ambiguous from the tool result alone — the
test prompt has Claude watch for the popup *and* cross-reference the audit
log's `decision` field for these calls, so this doesn't require any special
environment setup, just both checks being run.

## 8. Salesforce

A fresh Salesforce org (dev/sandbox) typically has zero data rows anywhere
reachable, which means every call either 404s, comes back empty, or hits
`FORBIDDEN` — none of that is a gate bug, but it also means the *success*
path (real rows, a real record) never gets exercised unless you seed some
data:

1. **Setup → Object Manager → Account** (or any object you're comfortable
   using) → create 2–3 sample records prefixed `PrivacyFence QA — `.
2. **Reports → New Report**, base it on that object, name it **exactly**
   `PrivacyFence QA Report`.
3. Add auto-accept fixtures (recommended — this is also how you get
   `approved_report_ids`/`approved_object_types` rule coverage):
   ```yaml
   auto_accept_rules:
     salesforce.run_report:
       - rule: approved_report_ids
         value: ["<PrivacyFence QA Report id>"]
     salesforce.read_record:
       - rule: approved_object_types
         value: [Account]
   ```
   If you skip this rule, the test prompt falls back to finding the report
   by its exact name via `salesforce_list_reports` — either path works, the
   name is just the fallback when there's no rule to read the ID from.
4. Keep at least one report/object type you *don't* add here (or that you
   genuinely can't access) as the "should still prompt" contrast case — any
   report other than the QA one satisfies this, nothing extra to create.

## 9. Jira

1. Create a Jira project with key **exactly `PFQA`**, any template (Kanban is
   fine). The key has to match exactly — the test prompt uses it as a
   literal string, not a fuzzy lookup.
2. Add the fixture:
   ```yaml
   auto_accept_rules:
     jira.read_issue:
       - rule: approved_project_keys
         value: [PFQA]
   ```
3. That's the only project you need to create. Whatever other Jira
   project(s) already exist in your site serve as the "different project,
   should still prompt" contrast automatically — the test prompt picks
   whichever project isn't `PFQA` from `jira_list_projects` at runtime. If
   `PFQA` is the *only* project in your site, create one throwaway second
   project (any key) purely so a contrast case exists.
4. `i_am_reporter` / `i_am_assignee` need no setup — any issue you create in
   `PFQA` satisfies both automatically. If a second Jira user exists in your
   site, optionally reassign one test issue to them to get a contrast case
   for `i_am_assignee`; skip this if you're the only user.

## 10. Confluence

1. Create a Confluence space with key **exactly `PFQA`**.
2. Add the fixture:
   ```yaml
   auto_accept_rules:
     confluence.read_page:
       - rule: approved_space_keys
         value: [PFQA]
   ```
3. Same as Jira: whatever other space(s) already exist serve as the contrast
   case automatically (the test prompt picks whichever space isn't `PFQA`
   from `confluence_list_spaces`). Create one throwaway second space only if
   `PFQA` would otherwise be the only one in your site.
4. `i_am_author` needs no setup — any page you create in `PFQA` satisfies it
   automatically.
5. Confirm the daemon build you're testing against actually has the
   Confluence v1→v2 API migration (commit `34e7108` in this repo) —
   `confluence_get_page`/`create_page`/`update_page` are completely broken
   without it (a 410 "Gone" error from Atlassian's removed v1 endpoint), and
   that failure has nothing to do with this environment setup.

## 11. PII Detection Gate

No fixture to create — this is the one part of the QA test that's genuinely
self-contained. The dedicated check in
[`connector-qa-testing.md`](connector-qa-testing.md) (Phase 2, steps 17–20)
creates its own throwaway Drive subfolder and Google Doc seeded with
synthetic, obviously-fake PII, reads it back, and tears both down in
Phase 11 — nothing here to provision ahead of time, and nothing that
persists between runs.

The only thing worth confirming beforehand:

1. **PII Detection Gate** is enabled in the PrivacyFence menu bar (it is by
   default — equivalently, `pii_detection.enabled` is `true` or absent in
   `~/.privacyfence/config/settings.yaml`). If you've turned it off, the
   dedicated check still runs but correctly produces the *disabled* result
   (no tint, no second confirmation, `pii_detected: false` in the audit log)
   — that's expected behavior for that state, not a failure, but the test
   prompt needs to know which state it's in rather than assume enabled.
2. The check deliberately writes to a **subfolder** of the Drive QA Sandbox
   folder, not the Sandbox folder's own top level. `approved_folder` (§2
   above) matches a file's *immediate* parent folder ID only, not folders
   nested inside it — so even if you configured that rule, it does not
   cover the subfolder, and the read step is guaranteed to hit the normal
   `review` gate (and the PII gate layered on top of it) instead of being
   silently auto-accepted. No action needed here beyond knowing why the
   check is structured that way, in case you ever restructure the Sandbox
   folder yourself.
3. A second, related check (`connector-qa-testing.md` steps 21–23) proves
   the stronger claim that PII detection *overrides* a matching auto-accept
   rule, rather than just running independently of one — it deliberately
   writes the same synthetic PII directly into `drive_qa_folder_id` itself,
   the folder `approved_folder` *does* cover. This one only exercises the
   override if you actually configured the `drive.read_file_contents` →
   `approved_folder` rule from §2; without it there's no rule in play to
   override, and the test prompt is told to say so plainly rather than
   claim the override was proven.

---

## Consolidated `auto_accept_rules` block

Everything from the sections above, in one place. Merge this into
`~/.privacyfence/config/settings.yaml` under the `auto_accept_rules:` key,
replacing every `<placeholder>` with your actual value:

```yaml
auto_accept_rules:
  gmail.read_message:
    - rule: trusted_sender_domain
      value: [example.com]              # a domain you actually receive mail from
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
      value: [PFQA]
  confluence.read_page:
    - rule: approved_space_keys
      value: [PFQA]
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

`sheets.read_values` → `approved_spreadsheet` isn't included here because it
gets created automatically the first time you click **"Accept All"** on a
`drive_sheets_get_values` call during a test run — nothing to pre-configure.

Restart the daemon after editing this file by hand (or use the "Accept All"
popup once, which hot-reloads rules for you via `reload_rules()`).

---

## Fixture reference

What [`connector-qa-testing.md`](connector-qa-testing.md)'s Phase 0 looks up,
and how it finds each one. Use this table to sanity-check your setup before
a run, or to debug a fixture Phase 0 reports as missing.

| Fixture | How it's found | Source |
|---|---|---|
| Drive QA folder | Exact name `PrivacyFence QA Sandbox` | `drive_list_files` |
| Slack approved channel | `slack.read_messages` → `approved_channel` | `settings.yaml` |
| Slack control channel | Exact name `privacyfence-qa-control` | `slack_list_channels` |
| Telegram Saved Messages | `is_self: true` flag | `telegram_list_chats` |
| Telegram approved chat | `telegram.read_chat_messages` → `approved_chats` (falls back to Saved Messages) | `settings.yaml` |
| Telegram control chat | Any chat that isn't the above two | `telegram_list_chats` |
| Salesforce QA report | `salesforce.run_report` → `approved_report_ids` (falls back to exact name `PrivacyFence QA Report`) | `settings.yaml` / `salesforce_list_reports` |
| Salesforce QA object type | `salesforce.read_record` → `approved_object_types` (falls back to `Account`) | `settings.yaml` |
| Jira QA project | Literal key `PFQA` | — |
| Jira contrast project | Any project key that isn't `PFQA` | `jira_list_projects` |
| Confluence QA space | Literal key `PFQA` | — |
| Confluence contrast space | Any space key that isn't `PFQA` | `confluence_list_spaces` |
| Tasks approved list | `tasks.update_task` → `approved_task_list` (falls back to the default list) | `settings.yaml` / `tasks_list_task_lists` |
| Tasks contrast list | Exact name `PrivacyFence QA Contrast List` | `tasks_list_task_lists` |

---

## Idempotency: environment fixtures vs. per-run artifacts

Two different lifetimes are at play:

- **Environment fixtures** (this doc): the `PFQA` Jira project/Confluence
  space, the Drive Sandbox folder, the Slack channels, the Telegram chats,
  the Salesforce sample records/report. Created **once** by working through
  this guide; re-discovered by every subsequent test run, never recreated
  and never pasted in by hand.
- **Per-run artifacts** (the test prompt itself): drafts, events, one-off
  issues/pages/files. These carry a run-scoped identifier so repeated runs
  don't produce indistinguishable duplicates — see
  [`connector-qa-testing.md`](connector-qa-testing.md), which stamps every
  title with a timestamp and ends with a teardown phase.

If you deliberately skip a fixture (e.g. no Workspace admin access for
Calendar rooms), that's a permanent, known gap — the test prompt already
treats it as expected rather than re-discovering and re-reporting it every
run, as long as you leave the corresponding config/rule unset rather than
half-configuring it.
