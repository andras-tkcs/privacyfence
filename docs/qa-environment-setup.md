# QA Environment Setup (for `connector-qa-testing.md`)

This is a complete, standalone installation guide for the environment
[`connector-qa-testing.md`](connector-qa-testing.md) and the local fixture recorder (see
[`external-api-contract-testing.md`](external-api-contract-testing.md)) run against —
**your own real accounts**, authenticated the normal way from the PrivacyFence menu bar.

**Scope**: this doc only covers QA-specific fixtures — a dedicated Jira project, a Drive sandbox
folder, sample Salesforce records, and so on, all inside your existing real accounts. Base
connector *authentication* (OAuth apps, API enablement, tokens) is a separate, one-time
prerequisite covered by each connector's own setup guide; see Prerequisites below.

## The one rule everything else here follows

**QA/testing never reads, quotes, or acts on content you didn't create specifically for QA.**
Real accounts mean real inbox content, real contacts, real calendar history, real chat history —
none of that should ever be the *subject* of a test. Every section below gives you exact,
copy-paste-ready content to create a dedicated, obviously-synthetic artifact (a seed email, a
seed contact, a seed page, ...) up front, and every later instruction — in this doc, in
`connector-qa-testing.md`, and in the fixture recorder — targets *that specific artifact* by name/ID,
never "pick any existing message" or "any recent event." A few read operations unavoidably
enumerate real account content just by existing (`gmail_list_messages`, `contacts_list`,
`calendar_list_events`, `slack_list_channels`, ...) — those are called out explicitly below, with
the rule for them: confirm the response's *shape* (right fields present, no error), never its
*content* (don't quote, copy, or persist what a real message/contact/event actually says).

This is the property that makes it safe to keep using your real accounts: nothing here depends on
the account being disposable, because nothing here ever touches anything in it that isn't
synthetic and self-labeled.

**Placeholder values** — use these everywhere a field needs a fake email/phone/address, so nothing
resembles a real one by coincidence:
- Email: anything `@example.com` / `@example.org` / `@example.net` — reserved by RFC 2606
  specifically so these addresses never resolve to a real mailbox.
- Phone: `555-01XX` (e.g. `555-0142`) — the `555-0100`–`555-0199` block is reserved by NANPA
  specifically for fictional use in exactly this kind of test content.

Everything this guide creates is prefixed **`PFQA`** (Jira/Confluence keys) or **`PrivacyFence QA`**
(folder/channel/event/contact names) and tagged `[QATEST]` in body text, so it's grep-able and
obviously safe — both for you to recognize later, and for anything reading it (a human, Claude, the
fixture recorder) to recognize it was deliberately created for this purpose, not real data that
happens to be short/simple.

---

## Prerequisites

1. **Every connector you want to test is already authenticated** from the PrivacyFence menu bar. If
   not, work through the relevant setup guide first: [`google-cloud-setup.md`](google-cloud-setup.md)
   (Gmail, Drive, Calendar, Contacts, Tasks), [`slack-setup.md`](slack-setup.md),
   [`atlassian-setup.md`](atlassian-setup.md) (Jira & Confluence),
   [`salesforce-setup.md`](salesforce-setup.md), [`telegram-setup.md`](telegram-setup.md).
2. You have `privacyfence-app` (the daemon) running.
3. You can edit `settings.yaml` directly — at `~/.privacyfence/config/settings.yaml` for a bundled
   install, or `config/settings.yaml` in the repo root if running from source (see
   [dev-vs-live-setup.md](dev-vs-live-setup.md)) — and either restart the daemon afterward, or use
   the approval popup's "Accept All" button once, which hot-reloads rules for you.
4. You know your own email address as PrivacyFence sees it (`my_email`, used internally for rules
   like `i_am_sender`/`i_am_organizer`).

---

## 1. Gmail

Create one synthetic seed thread — this single thread covers every Gmail test in
`connector-qa-testing.md` (read, thread-read, label/archive round-trip, the Deny case), so nothing
ever touches a real message:

1. Send yourself an email:
   ```
   To:      (your own address)
   Subject: PrivacyFence QA seed message [QATEST]
   Body:
   This is a synthetic PrivacyFence QA test message. No real information.
   Safe to read, label, archive, or delete by any automated test.

   Lorem ipsum dolor sit amet, consectetur adipiscing elit.
   ```
   Attach one small text file named `qatest-attachment.txt` containing:
   ```
   PrivacyFence QA test attachment. No real information.
   ```
2. Reply to your own message (creates a real 2-message thread, still entirely synthetic):
   ```
   Subject: Re: PrivacyFence QA seed message [QATEST]
   Body:
   This is a synthetic PrivacyFence QA reply. No real information.
   Safe to read, label, archive, or delete by any automated test.
   ```
3. Use **this thread** — not "pick any recent message" — for every Gmail test step:
   `gmail_get_message`, `gmail_get_thread`, `gmail_list_message_attachments` +
   `gmail_download_attachment`, and the add-label/archive/unarchive/remove-label round-trip. It's
   also the right pick for the Deny test (it's short, no large attachment, so a Deny response can't
   be confused with size-truncation).

**The one exception — `trusted_sender_domain`**: this auto-accept rule is inherently about a real
sender domain you actually receive mail from (a newsletter, a receipt sender). There's no synthetic
substitute for "does mail from a real trusted domain skip the popup." Pick one from your real inbox
for the rule config below, but the *test* only needs to confirm the gate behavior (prompted vs.
not) — never read, quote, or log that message's actual content:
```yaml
auto_accept_rules:
  gmail.read_message:
    - rule: trusted_sender_domain
      value: [example.com]   # ← a domain you actually receive mail from
```

**`gmail_list_messages`/`gmail_list_threads`** (silent, auto-approved) will return real subject
lines as part of proving the tool works at all — that's unavoidable for a list call against a real
inbox. Confirm the response shape (results come back, each has the expected fields) and move on;
don't transcribe or persist what any of those subjects actually say.

## 2. Drive & Sheets

1. Create a folder named **exactly `PrivacyFence QA Sandbox`** in "My Drive". Note its file ID.
2. Add an auto-accept fixture:
   ```yaml
   auto_accept_rules:
     drive.read_file_contents:
       - rule: approved_folder
         value: ["<QA Sandbox folder id>"]
   ```
3. Every Drive/Sheets artifact any test creates goes **inside this folder**, with synthetic
   content from scratch (this repo's own `connector-qa-testing.md` already does this correctly —
   e.g. its PII-gate check writes an entirely fabricated body, never real content) — so nothing
   ever reads or writes anything outside this folder, and nothing inside it was ever real to begin
   with.
4. Drive has no bulk-empty-trash tool; empty Drive trash by hand periodically if you want a clean
   slate (trashed items auto-purge after 30 days regardless).

## 3. Slack

1. Pick (or create) a channel to be the **approved** one and join it. Add its ID:
   ```yaml
   auto_accept_rules:
     slack.read_messages:
       - rule: approved_channel
         value: ["<channel id>"]
   ```
2. Create a second channel named **exactly `privacyfence-qa-control`** and join it. **Do not** add
   it to `approved_channel` — it's the "should still prompt" contrast case.
3. In `privacyfence-qa-control`, post a synthetic seed message and reply to it in-thread:
   ```
   PrivacyFence QA seed message [QATEST]. No real information. Safe to read/reply/delete.
   ```
   (thread reply) —
   ```
   PrivacyFence QA seed reply [QATEST]. No real information.
   ```
   This gives `slack_get_thread_replies` permanent, entirely synthetic content instead of depending
   on whatever real conversation happens to exist.

`slack_list_channels` (silent) will list your real channel names as part of proving the tool works
— same rule as Gmail's list calls: confirm shape, don't transcribe content.

## 4. Calendar

No fixture needed beyond a rule, if you want `i_am_organizer` exercised (any event you create
satisfies it automatically):
```yaml
auto_accept_rules:
  calendar.read_event_details:
    - rule: i_am_organizer
```
`connector-qa-testing.md`'s Phase 4 already creates its own synthetic event
(`PrivacyFence QA test event [{RUN_ID}] — safe to delete`, no real attendees) rather than reading an
existing one — keep following that pattern; there's no need to pre-provision a calendar fixture
here. `calendar_list_events`/`calendar_list_calendars` (silent) are the one unavoidable "sees real
data" case here — same shape-only rule as above.

**`calendar_list_rooms`** needs Google Workspace *and* admin rights — a hard external dependency,
not something local config can substitute for. If you're on a consumer Google account, or lack
Workspace admin access, this is a permanent, expected gap — `connector-qa-testing.md` already treats
the resulting error as expected, not a regression. If you do have Workspace admin access: enable the
**Admin SDK API** in the same Google Cloud project ([`google-cloud-setup.md`](google-cloud-setup.md)),
and reconnect Calendar from the menu bar to pick up the new scope.

## 5. Contacts

Create one synthetic contact, used as the create/update/label fixture instead of touching a real
one:
```
Display name: PrivacyFence QA Test Contact
Email:        qatest.contact@example.com
Phone:        555-0142
Note:         Synthetic PrivacyFence QA test contact [QATEST]. No real information.
```
Add, if you want `no_contact_info_change` exercised:
```yaml
auto_accept_rules:
  contacts.edit:
    - rule: no_contact_info_change
```
`contacts_list`/`contacts_search` with `source="personal"` will enumerate your real saved contacts
as part of proving the tool works — shape-only rule applies. `source="directory"` needs Google
Workspace directory colleagues; on a consumer account this is always (correctly) empty — that's the
expected result, not a gap to fix.

## 6. Google Tasks

1. Your default list ("My Tasks") works as the approved list — get its ID via
   `scripts/qa_list_ids.py tasks`, and add:
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
2. Create a second list named exactly `PrivacyFence QA Contrast List` — the deliberately-unapproved
   contrast case. `connector-qa-testing.md` already creates its own synthetic task
   (`PrivacyFence QA test task [{RUN_ID}] — safe to delete`) inside these lists rather than touching
   an existing one.

## 7. Telegram

1. Send yourself **one** synthetic message in Saved Messages, once:
   ```
   PrivacyFence QA seed message [QATEST]. No real information.
   ```
   (`telegram_list_chats` only returns chats you've opened at least once; this stays in the list
   forever after.)
2. Decide `approved_chats` — pointing at Saved Messages is the safest choice (it's always fine to
   auto-accept reads of your own synthetic messages to yourself):
   ```yaml
   auto_accept_rules:
     telegram.read_chat_messages:
       - rule: approved_chats
         value: ["<chat_id>"]
   ```
3. `telegram_search_messages`'s "should find something" case should search for the `[QATEST]` tag
   from step 1, not an arbitrary real query. The "not approved, should still prompt" contrast case
   (any *other* chat with history) is the one place a real chat's existence — not its content — gets
   used; the test only needs to confirm a popup appears, never read what that chat actually says.

## 8. Salesforce

A fresh org typically has no data anywhere reachable, so seed a few obviously-fake records:

1. **Setup → Object Manager → Account** → create 2–3 sample records, all clearly fake:
   ```
   Name: PrivacyFence QA — Acme Test Co
   Name: PrivacyFence QA — Globex Test Co
   ```
2. **Reports → New Report**, base it on that object, name it exactly `PrivacyFence QA Report`.
3. Add fixtures:
   ```yaml
   auto_accept_rules:
     salesforce.run_report:
       - rule: approved_report_ids
         value: ["<PrivacyFence QA Report id>"]
     salesforce.read_record:
       - rule: approved_object_types
         value: [Account]
   ```
4. Keep at least one report/object type you don't add here as the "should still prompt" contrast —
   any report other than the QA one satisfies this.

## 9. Jira

1. Create a project with key **exactly `PFQA`**. Whatever other real project(s) already exist in
   your site serve as the "different project, should still prompt" contrast automatically — no need
   to create a second dummy project just for this.
2. Add:
   ```yaml
   auto_accept_rules:
     jira.read_issue:
       - rule: approved_project_keys
         value: [PFQA]
   ```
3. Create one seed issue with synthetic content, reused instead of "pick any existing issue":
   ```
   Summary:     PrivacyFence QA seed issue [QATEST]
   Description: Synthetic PrivacyFence QA test issue. No real information. Safe to comment on,
                update, or transition by any automated test.
   ```
   `i_am_reporter`/`i_am_assignee` are satisfied automatically since you created it.
4. `jira_get_transitions`/`jira_transition_issue` need no setup beyond the project's default
   workflow. The `custom_fields` check is opportunistic — only exercise it if `PFQA`'s issue screen
   already has a custom field, using a placeholder value, never real data.

## 10. Confluence

1. Create a space with key **exactly `PFQA`**. Same as Jira: whatever other real space(s) exist
   serve as the contrast case automatically.
2. Add:
   ```yaml
   auto_accept_rules:
     confluence.read_page:
       - rule: approved_space_keys
         value: [PFQA]
   ```
3. Create one seed page with synthetic content:
   ```
   Title: PrivacyFence QA seed page [QATEST]
   Body:  Synthetic PrivacyFence QA test page. No real information. Safe to read, comment on,
          or edit by any automated test.
   ```
   `i_am_author` is satisfied automatically since you created it.
4. Confirm the daemon build under test has the Confluence v1→v2 migration (`confluence_client.py`)
   — `get_page`/`create_page`/`update_page` 410 without it, unrelated to this environment setup.

## 11. PII Detection Gate

No fixture to create — this part of `connector-qa-testing.md` is already, deliberately,
self-contained: it creates its own throwaway Drive subfolder and Google Doc, seeds it with
obviously-fake synthetic PII (already following the "no real information" rule this doc states
explicitly for everything else), writes it, reads it back, and tears both down. Confirm **PII
Detection Gate** is enabled in the menu bar (default on) before running it.

---

## Consolidated `auto_accept_rules` block

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

Restart the daemon after editing by hand (or use "Accept All" once to hot-reload).

---

## Fixture reference

| Fixture | How it's found | Source |
|---|---|---|
| Gmail seed thread | Subject tag `[QATEST]` | `gmail_list_messages` / created in §1 |
| Drive QA folder | Exact name `PrivacyFence QA Sandbox` | `drive_list_files` |
| Slack approved channel | `slack.read_messages` → `approved_channel` | `settings.yaml` |
| Slack control channel | Exact name `privacyfence-qa-control` | `slack_list_channels` |
| Contacts seed contact | Display name `PrivacyFence QA Test Contact` | created in §5 |
| Telegram Saved Messages | `is_self: true` flag | `telegram_list_chats` |
| Telegram approved chat | `telegram.read_chat_messages` → `approved_chats` (falls back to Saved Messages) | `settings.yaml` |
| Salesforce QA report | `salesforce.run_report` → `approved_report_ids` (falls back to exact name `PrivacyFence QA Report`) | `settings.yaml` / `salesforce_list_reports` |
| Jira QA project | Literal key `PFQA` | — |
| Jira seed issue | Subject tag `[QATEST]` in `PFQA` | created in §9 |
| Confluence QA space | Literal key `PFQA` | — |
| Confluence seed page | Title tag `[QATEST]` in `PFQA` | created in §10 |
| Tasks approved list | `tasks.update_task` → `approved_task_list` (falls back to the default list) | `settings.yaml` / `tasks_list_task_lists` |
| Tasks contrast list | Exact name `PrivacyFence QA Contrast List` | `tasks_list_task_lists` |

---

## Idempotency: environment fixtures vs. per-run artifacts

Two different lifetimes are at play:

- **Environment fixtures** (this doc): the `PFQA` Jira project/Confluence space, the Drive Sandbox
  folder, the Slack channels, the seed messages/contact/issue/page. Created **once**, with the exact
  synthetic content given above; re-discovered by every subsequent test run.
- **Per-run artifacts** (the `connector-qa-testing.md` prompt, or the fixture recorder): drafts,
  events, one-off issues/pages/files, each carrying a run-scoped `{RUN_ID}` tag and its own
  entirely-fabricated content, cleaned up at the end of the run.

If you deliberately skip a fixture (e.g. no Workspace admin access for Calendar rooms), that's a
permanent, known gap — treated as expected rather than re-discovered and re-reported every run, as
long as the corresponding config/rule stays unset.
