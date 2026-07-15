# QA Environment Setup (for `connector-qa-testing.md`)

This is a complete, standalone installation guide for the environment
[`connector-qa-testing.md`](connector-qa-testing.md) and the local fixture recorder (see
[`external-api-contract-testing.md`](external-api-contract-testing.md)) run against —
**your own real accounts**, authenticated the normal way from the PrivacyFence menu bar.

**Scope**: this doc only covers QA-specific fixtures — a dedicated Jira project, a Drive sandbox
folder, sample Salesforce records, and so on, all inside your existing real accounts. Base
connector *authentication* (OAuth apps, API enablement, tokens) is a separate, one-time
prerequisite covered by each connector's own setup guide; see [Prerequisites](#prerequisites) below.

Every section below is a checklist. Work top to bottom once per environment; re-run nothing unless
a fixture was renamed or deleted.

## The one rule everything here follows

**QA/testing never reads, quotes, or acts on content you didn't create specifically for QA.**
Real accounts mean real inbox content, real contacts, real calendar history, real chat history —
none of that should ever be the *subject* of a test. Every checklist below has you create a
dedicated, obviously-synthetic seed artifact up front (a seed email, a seed contact, a seed page,
...), and every later instruction — in this doc, in `connector-qa-testing.md`, and in the fixture
recorder — targets *that specific artifact* by name/ID, never "pick any existing message" or "any
recent event."

A few read operations unavoidably enumerate real account content just by existing
(`gmail_list_messages`, `contacts_list`, `calendar_list_events`, `slack_list_channels`, ...). Those
are flagged inline as notes, not checklist items, with the same rule every time: confirm the
response's *shape* (results come back, expected fields present, no error) — never its *content*
(don't quote, copy, or persist what a real message/contact/event actually says).

This is the property that makes it safe to keep using your real accounts: nothing here depends on
the account being disposable, because nothing here ever touches anything in it that isn't
synthetic and self-labeled.

**Placeholder values** — use these everywhere a field needs a fake email/phone/address:

- Email: anything `@example.com` / `@example.org` / `@example.net` — reserved by RFC 2606
  specifically so these addresses never resolve to a real mailbox.
- Phone: `555-01XX` (e.g. `555-0142`) — the `555-0100`–`555-0199` block is reserved by NANPA
  specifically for fictional use in exactly this kind of test content.

**Naming convention** — everything this guide creates is prefixed `PFQA` (Jira/Confluence keys) or
`PrivacyFence QA` (folder/channel/event/contact names) and tagged `[QATEST]` in body text, so it's
grep-able and obviously safe, for you and for anything reading it later (a human, Claude, the
fixture recorder).

---

## Prerequisites

- [ ] Authenticate every connector you want to test, from the PrivacyFence menu bar
  - [ ] Gmail, Drive, Calendar, Contacts, Tasks — [`google-cloud-setup.md`](google-cloud-setup.md)
  - [ ] Slack — [`slack-setup.md`](slack-setup.md)
  - [ ] Jira & Confluence — [`atlassian-setup.md`](atlassian-setup.md)
  - [ ] Salesforce — [`salesforce-setup.md`](salesforce-setup.md)
  - [ ] Telegram — [`telegram-setup.md`](telegram-setup.md)
- [ ] Confirm `privacyfence-app` (the daemon) is running
- [ ] Confirm you can edit and reload `settings.yaml`
  - [ ] Bundled install: `~/.privacyfence/config/settings.yaml`
  - [ ] From source: `config/settings.yaml` in the repo root — see
        [`dev-vs-live-setup.md`](dev-vs-live-setup.md)
  - [ ] Reload it by restarting the daemon, or by clicking "Accept All" once on any popup
        (hot-reloads rules)
- [ ] Note your own email address as PrivacyFence sees it (`my_email`) — used internally by rules
      like `i_am_sender`/`i_am_organizer`

---

## 1. Gmail

- [ ] Create the Gmail seed thread
  - [ ] Send yourself an email:
        ```
        To:      (your own address)
        Subject: PrivacyFence QA seed message [QATEST]
        Body:
        This is a synthetic PrivacyFence QA test message. No real information.
        Safe to read, label, archive, or delete by any automated test.

        Lorem ipsum dolor sit amet, consectetur adipiscing elit.
        ```
  - [ ] Attach a small text file named `qatest-attachment.txt` containing:
        ```
        PrivacyFence QA test attachment. No real information.
        ```
  - [ ] Reply to your own message, creating a real 2-message thread that's still entirely
        synthetic:
        ```
        Subject: Re: PrivacyFence QA seed message [QATEST]
        Body:
        This is a synthetic PrivacyFence QA reply. No real information.
        Safe to read, label, archive, or delete by any automated test.
        ```
  - [ ] Confirm this thread — not "any recent message" — is what every later Gmail test step
        targets: `gmail_get_message`, `gmail_get_thread`, `gmail_list_message_attachments` +
        `gmail_download_attachment`, the add-label/archive/unarchive/remove-label round-trip, and
        the Deny test (it's short with no large attachment, so a Deny can't be confused with a
        size-truncation error)
- [ ] (Optional) Configure the `trusted_sender_domain` rule
  - [ ] Pick a real domain you actually receive recurring mail from — a newsletter or receipt
        sender works
  - [ ] Add it to `settings.yaml`:
        ```yaml
        auto_accept_rules:
          gmail.read_message:
            - rule: trusted_sender_domain
              value: [example.com]   # ← a domain you actually receive mail from
        ```

> **Note** — this rule is inherently about a real sender domain; there's no synthetic substitute
> for "does mail from a real trusted domain skip the popup." The test only needs to confirm gate
> behavior (prompted vs. not), never read/quote/log that message's actual content.
>
> **Note** — `gmail_list_messages`/`gmail_list_threads` (silent, auto-approved) return real subject
> lines as part of proving the tool works at all. Confirm the response shape and move on; don't
> transcribe or persist what any of those subjects actually say.

## 2. Drive & Sheets

- [ ] Create the Drive QA Sandbox folder
  - [ ] Name it exactly `PrivacyFence QA Sandbox` in "My Drive"
  - [ ] Note its file ID
- [ ] Add the auto-accept fixture:
      ```yaml
      auto_accept_rules:
        drive.read_file_contents:
          - rule: approved_folder
            value: ["<QA Sandbox folder id>"]
      ```
- [ ] Confirm every Drive/Sheets test artifact is created fresh, **inside this folder**, with
      synthetic content from scratch — `connector-qa-testing.md` already does this correctly (e.g.
      its PII-gate check writes an entirely fabricated body)
- [ ] (Optional) Extend `approved_sandbox_folder` to `sheets.rename_sheet` / `sheets.format_range`
  - [ ] Only add this if you want Phase 2 to demonstrate these two auto-accepting by folder,
        instead of exercising the popup / "Accept for 5 min" flow — the two are mutually exclusive
        for the same spreadsheet
        ```yaml
        auto_accept_rules:
          sheets.rename_sheet:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
          sheets.format_range:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
        ```
- [ ] (Optional) Extend the same rule to `sheets.insert_dimensions` / `sheets.delete_dimensions`
  - [ ] Add both:
        ```yaml
        auto_accept_rules:
          sheets.insert_dimensions:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
          sheets.delete_dimensions:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
        ```
  - Note: `sheets.insert_dimensions` still offers "Accept for 5 min" on its plain popup
    (non-destructive, like `format_range`); `sheets.delete_dimensions` never does (removes cell
    content, no undo path) — that asymmetry stays visible whenever this rule doesn't match.
- [ ] (Optional) Extend the same rule to `docs.edit_content` / `docs.format_content`
  - [ ] Add both:
        ```yaml
        auto_accept_rules:
          docs.edit_content:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
          docs.format_content:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
        ```
- [ ] (Maintenance, periodic) Empty Drive trash by hand — there's no bulk-empty-trash tool; trashed
      items auto-purge after 30 days regardless

## 3. Slack

- [ ] Designate the approved channel
  - [ ] Pick or create a channel and join it
  - [ ] Add its ID:
        ```yaml
        auto_accept_rules:
          slack.read_messages:
            - rule: approved_channel
              value: ["<channel id>"]
        ```
- [ ] Create the control channel
  - [ ] Name it exactly `privacyfence-qa-control` and join it
  - [ ] Do **not** add it to `approved_channel` — it's the "should still prompt" contrast case
  - [ ] Post a synthetic seed message:
        ```
        PrivacyFence QA seed message [QATEST]. No real information. Safe to read/reply/delete.
        ```
  - [ ] Reply to it in-thread:
        ```
        PrivacyFence QA seed reply [QATEST]. No real information.
        ```
        This gives `slack_get_thread_replies` permanent, entirely synthetic content instead of
        depending on whatever real conversation happens to exist.

> **Note** — `slack_list_channels` (silent) lists your real channel names as part of proving the
> tool works. Confirm shape, don't transcribe content — same rule as Gmail's list calls.

## 4. Calendar

- [ ] (Optional) Add the `i_am_organizer` rule — any event you create satisfies it automatically:
      ```yaml
      auto_accept_rules:
        calendar.read_event_details:
          - rule: i_am_organizer
      ```
- [ ] Confirm no calendar fixture needs pre-provisioning here — `connector-qa-testing.md`'s Phase 4
      already creates its own synthetic event (`PrivacyFence QA test event [{RUN_ID}] — safe to
      delete`, no real attendees) per run rather than reading an existing one
- [ ] Decide `calendar_list_rooms` coverage
  - [ ] Consumer Google account, or no Workspace admin access → accept as a permanent, expected
        gap (`connector-qa-testing.md` already treats the resulting error as expected, not a
        regression)
  - [ ] Workspace admin access available → enable the **Admin SDK API** in the same Google Cloud
        project ([`google-cloud-setup.md`](google-cloud-setup.md)), then reconnect Calendar from
        the menu bar to pick up the new scope
- [ ] (Optional) Add the `non_private_event` rule
  - [ ] Only add this if you want Phase 2 to demonstrate `calendar_get_event_details` and
        `calendar_set_event_visibility` auto-accepting for a non-private event:
        ```yaml
        auto_accept_rules:
          calendar.read_event_details:
            - rule: non_private_event
          calendar.set_visibility:
            - rule: non_private_event
        ```
  - Note: no fixture needed for the contrast case either way — any event set to `private` via
    `calendar_set_event_visibility` still prompts regardless of this rule, since it checks the
    visibility being requested, not the event's prior state. Combine with `i_am_organizer` above
    under `calendar.read_event_details` if you want both — a matching rule short-circuits the
    list, so order doesn't change what auto-accepts, just which rule name shows up in the audit
    log.

> **Note** — `calendar_list_events`/`calendar_list_calendars` (silent) show real events as part of
> proving the tool works. Confirm shape only.

## 5. Contacts

- [ ] Create the seed contact:
      ```
      Display name: PrivacyFence QA Test Contact
      Email:        qatest.contact@example.com
      Phone:        555-0142
      Note:         Synthetic PrivacyFence QA test contact [QATEST]. No real information.
      ```
- [ ] (Optional) Add the `no_contact_info_change` rule:
      ```yaml
      auto_accept_rules:
        contacts.edit:
          - rule: no_contact_info_change
      ```

> **Note** — `contacts_list`/`contacts_search` with `source="personal"` enumerates your real saved
> contacts as part of proving the tool works. Shape only, same rule as above.
>
> **Note** — `source="directory"` needs Google Workspace directory colleagues; on a consumer
> account this is always (correctly) empty. That's the expected result, not a gap to fix.

## 6. Google Tasks

- [ ] Configure the approved list
  - [ ] Get your default list's ("My Tasks") ID via `scripts/qa_list_ids.py tasks`
  - [ ] Add it to all three rules:
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
- [ ] Create the contrast list
  - [ ] Name it exactly `PrivacyFence QA Contrast List`
  - [ ] Do **not** add it to any `approved_task_list` rule
- [ ] Confirm no task fixture needs pre-provisioning beyond the two lists above —
      `connector-qa-testing.md` already creates its own synthetic task
      (`PrivacyFence QA test task [{RUN_ID}] — safe to delete`) per run

## 7. Telegram

- [ ] Seed Saved Messages
  - [ ] Send yourself **one** synthetic message, once:
        ```
        PrivacyFence QA seed message [QATEST]. No real information.
        ```
        (`telegram_list_chats` only returns chats you've opened at least once; this stays in the
        list forever after.)
- [ ] Decide `approved_chats`
  - [ ] Point it at Saved Messages (safest choice — it's always fine to auto-accept reads of your
        own synthetic messages to yourself)
  - [ ] Add the rule:
        ```yaml
        auto_accept_rules:
          telegram.read_chat_messages:
            - rule: approved_chats
              value: ["<chat_id>"]
        ```
- [ ] Confirm the search and contrast targets
  - [ ] `telegram_search_messages`'s "should find something" case searches for the `[QATEST]` tag
        from the seed message, not an arbitrary real query
  - [ ] Any *other* chat with history serves as the "not approved, should still prompt" contrast
        case — its existence is used, never its content; the test only confirms a popup appears

## 8. Salesforce

- [ ] Seed sample records
  - [ ] **Setup → Object Manager → Account** → create 2–3 sample records, all clearly fake:
        ```
        Name: PrivacyFence QA — Acme Test Co
        Name: PrivacyFence QA — Globex Test Co
        ```
- [ ] Create the QA report
  - [ ] **Reports → New Report**, based on that object, named exactly `PrivacyFence QA Report`
- [ ] Add the fixtures:
      ```yaml
      auto_accept_rules:
        salesforce.run_report:
          - rule: approved_report_ids
            value: ["<PrivacyFence QA Report id>"]
        salesforce.read_record:
          - rule: approved_object_types
            value: [Account]
      ```
- [ ] Confirm a contrast case exists — any report/object type you don't add above already serves
      as the "should still prompt" contrast; no extra setup needed
- [ ] Confirm `salesforce_search` needs no separate fixture — the sample Account records from
      above (prefixed `PrivacyFence QA — `) are exactly what a search for `PrivacyFence QA` should
      find
  - Note: **known quirk, not a bug** — Salesforce's SOSL search index can take a minute or two to
    pick up freshly created records. If a search run immediately after seeding comes back empty,
    wait briefly and retry before treating it as a regression.
- [ ] (Optional) Add `approved_object_types` to `salesforce.search`
  - [ ] Same rule `salesforce.read_record` uses, generalized to check *every* object type in the
        search's comma-separated `object_types`:
        ```yaml
        auto_accept_rules:
          salesforce.search:
            - rule: approved_object_types
              value: [Account]
        ```
  - Note: with this configured, a search scoped to `object_types="Account"` auto-accepts; a
    search that also touches any other object type, or one left unscoped entirely (Salesforce's
    default globally-searchable set), still prompts — that asymmetry is worth confirming too, not
    just the auto-accept path.

## 9. Jira

- [ ] Create the QA project
  - [ ] Key exactly `PFQA`
  - [ ] Confirm at least one other real project exists in your site to serve as the "different
        project, should still prompt" contrast — no need to create a second dummy project
- [ ] Add the rule:
      ```yaml
      auto_accept_rules:
        jira.read_issue:
          - rule: approved_project_keys
            value: [PFQA]
      ```
- [ ] Create the seed issue:
      ```
      Summary:     PrivacyFence QA seed issue [QATEST]
      Description: Synthetic PrivacyFence QA test issue. No real information. Safe to comment on,
                   update, or transition by any automated test.
      ```
  - [ ] Confirm `i_am_reporter`/`i_am_assignee` are satisfied automatically (you created it)
- [ ] (Opportunistic, no setup required) Confirm `jira_get_transitions`/`jira_transition_issue`
      work against the project's default workflow
- [ ] (Opportunistic) If `PFQA`'s issue screen already has a custom field, note it for the
      `custom_fields` check — use a placeholder value, never real data

## 10. Confluence

- [ ] Create the QA space
  - [ ] Key exactly `PFQA`
  - [ ] Confirm at least one other real space exists in your site to serve as the contrast case
- [ ] Add the rule:
      ```yaml
      auto_accept_rules:
        confluence.read_page:
          - rule: approved_space_keys
            value: [PFQA]
      ```
- [ ] Create the seed page:
      ```
      Title: PrivacyFence QA seed page [QATEST]
      Body:  Synthetic PrivacyFence QA test page. No real information. Safe to read, comment on,
             or edit by any automated test.
      ```
  - [ ] Confirm `i_am_author` is satisfied automatically (you created it)
- [ ] Confirm the daemon build under test has the Confluence v1→v2 migration
      (`confluence_client.py`) — `get_page`/`create_page`/`update_page` return 410 without it; this
      is unrelated to this environment setup, but worth ruling out first

## 11. PII Detection Gate

- [ ] Confirm **PII Detection Gate** is enabled in the menu bar (default on)

> **Note** — no fixture to create here. `connector-qa-testing.md`'s Phase 2 is already
> self-contained: it creates its own throwaway Drive subfolder and Google Doc, seeds it with
> obviously-fake synthetic PII, writes it, reads it back, and tears both down.

## 12. Scheduled / unattended Cowork tasks

- [ ] Confirm no new fixture is needed — Phase 11 of `connector-qa-testing.md` reuses the Slack
      channels from §3 (an approved one, a control one)
- [ ] Know how to restart your daemon (`privacyfence-app`, or `scripts/dev_start.sh` if running
      from source — see [`dev-vs-live-setup.md`](dev-vs-live-setup.md))
  - Note: `unattended_sessions.enabled` in `settings.yaml` is off by default and toggled (with a
    daemon restart) as part of the phase itself, not something to pre-configure here. Unlike
    `pii_detection.enabled`, it has no menu-bar toggle and isn't hot-reloaded — the phase requires
    an actual restart partway through, twice. See
    [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks) for what
    this mode does and why.

---

## Consolidated `auto_accept_rules` block

- [ ] Merge this into `settings.yaml`, replacing every `<placeholder>` with your actual value:
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
- [ ] Restart the daemon (or click "Accept All" once, to hot-reload)

> **Note** — deliberately left out of this block: `sheets.rename_sheet` / `sheets.format_range`,
> `sheets.insert_dimensions` / `sheets.delete_dimensions`, and `docs.edit_content` /
> `docs.format_content` → `approved_sandbox_folder` (§2); `calendar.read_event_details` /
> `calendar.set_visibility` → `non_private_event` (§4); `salesforce.search` →
> `approved_object_types` (§8). Each is optional and, unlike everything above, actively changes
> what Phase 2 exercises for those tools (silent auto-accept instead of the popup/review-gate
> flow), so they're opt-in rather than assumed.

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
