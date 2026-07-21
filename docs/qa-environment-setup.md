# QA Environment Setup

Standalone installation guide for the fixtures used by both
[`connector-qa-testing.md`](connector-qa-testing.md) (the manual, live-Cowork QA pass) and
`scripts/qa_fixture_recorder.py` (the local fixture recorder — see
[`testing-policy.md` §2.1](testing-policy.md#21-qa_fixture_recorderpy---check----record)). Work
through it top to bottom on any machine/account where you want to run either one. It doesn't assume
anything was set up before.

**Scope**: QA-specific fixtures only — a dedicated Jira project, a Drive sandbox folder, sample
Salesforce records, and so on. Base connector *authentication* (OAuth apps, API enablement, tokens)
is a separate, one-time prerequisite — see [Prerequisites](#prerequisites).

Fixtures created here are **durable and reusable**: both consumers re-discover them by name/key/flag
at the start of every run (`connector-qa-testing.md`'s Phase 0; `scripts/qa_fixture_recorder.py`'s
manifest lookups) instead of you recreating or re-pasting anything. Work through this guide once per
environment; revisit a section only if you rename/delete one of its fixtures.

## The one rule this doc follows wherever it creates content

**A fixture's *identity* (which project, space, channel, folder) can be real — these are your real
accounts — but a fixture's *content*, wherever this doc has you type or paste something, is always
synthetic and tagged, never copied from a real message/contact/event.** `PFQA`/`PrivacyFence
QA`-prefixed names identify *which* real project or folder a fixture lives in; a `[QATEST]` tag
inside a body/title/summary marks content that's safe to read, act on, or record.

Two things are real by necessity, not oversight: `trusted_sender_domain` needs a domain you actually
receive mail from, and contrast cases ("a different project, should still prompt") reuse whatever
other real project/space/chat already exists rather than provisioning a second one purely to be
empty. In both cases only *existence* matters, never content.

**Placeholder values** — use these wherever a step needs a fake email/phone:

- Email: anything `@example.com` / `@example.org` / `@example.net` (RFC 2606 — reserved so these
  never resolve to a real mailbox).
- Phone: `555-01XX`, e.g. `555-0142` (NANPA's `555-0100`–`555-0199` block, reserved for fiction).

Everything here uses the prefix **`PFQA`** (Jira/Confluence keys) or **`PrivacyFence QA`**
(folder/channel/report/event names), and tags body/message content with **`[QATEST]`**. Names must
match exactly where noted — lookups are by exact string match, not fuzzy search.

The local fixture recorder (`scripts/qa_fixture_recorder.py`) targets exactly one tagged item per
connector, by ID/key/name, and refuses to record if the tag doesn't match — see that script's
`CONNECTOR_CHECKS` and the guardrail check in each `check_<connector>()` function for the exact
logic. Its per-connector manifest lives in `tests/fixtures/qa_environment.yaml` (git-ignored — copy
it from [`qa_environment.yaml.example`](../tests/fixtures/qa_environment.yaml.example) first); every
"For the recorder" step below says which field(s) to fill in there.

Every grant below can also be added from the menu bar — **Manage Auto-accept Rules… → \<Connector\> →
Trusted \<Resource\> → + Add…** — instead of editing YAML by hand; both are equivalent, and the older
per-operation form under `auto_accept_rules` (e.g. `approved_folder`, `approved_channel`) still
works too. Only the YAML form is shown below. See [Auto-accept
grants](TECHNICAL_REFERENCE.md#auto-accept-grants) for the full reference.

---

## Prerequisites

- [ ] Authenticate every connector you want to test, from the PrivacyFence menu bar
  - [ ] Gmail, Drive, Calendar, Contacts, Tasks — [`google-cloud-setup.md`](google-cloud-setup.md)
  - [ ] Slack — [`slack-setup.md`](slack-setup.md)
  - [ ] Jira & Confluence — [`atlassian-setup.md`](atlassian-setup.md)
  - [ ] Salesforce — [`salesforce-setup.md`](salesforce-setup.md)
  - [ ] Telegram — [`telegram-setup.md`](telegram-setup.md)
- [ ] Confirm `privacyfence-app` (the daemon) is running, and you have admin/owner-level access on
      each external service to create a project/space/channel/folder in it
- [ ] Confirm you can edit and reload `settings.yaml`
  - [ ] Bundled install: `~/.privacyfence/config/settings.yaml`
  - [ ] From source: `config/settings.yaml` in the repo root — see
        [`dev-vs-live-setup.md`](dev-vs-live-setup.md)
  - [ ] Reload by restarting the daemon, or by clicking "Always allow" once on any popup
        (`reload_rules()`)
- [ ] Note your own email address as PrivacyFence sees it (`my_email`) — used by rules like
      `i_am_sender`/`i_am_organizer`; it's whatever address you authenticated Gmail/Calendar with

---

## 1. Gmail

- [ ] (Optional) Create a synthetic seed thread
  - [ ] Send yourself an email:
        ```
        Subject: PrivacyFence QA seed message [QATEST]
        Body:
        Synthetic PrivacyFence QA test message. No real information. Safe to read, label,
        archive, or delete by any automated test.
        ```
  - [ ] Reply to your own message, creating a real 2-message thread that's still synthetic:
        ```
        Subject: Re: PrivacyFence QA seed message [QATEST]
        Body:
        Synthetic PrivacyFence QA reply. No real information.
        ```
  - [ ] For the recorder: fill in the seed message's real id under `gmail.seed_message_id` in
        `tests/fixtures/qa_environment.yaml` — required, no resolve-by-subject fallback
        (`GmailClient.list_messages()` makes one extra API call *per result*)
- [ ] (Optional) Configure `trusted_sender_domain` — pick any sender domain you get recurring real
      mail from:
      ```yaml
      auto_accept_rules:
        gmail.read_message:
          - rule: trusted_sender_domain
            value: [example.com]   # ← a domain you actually receive mail from
      ```

No fixture is needed for the archive/label round-trip — it targets whichever message you already
picked for the first `gmail_get_message` call and restores it to its exact starting state.

## 2. Drive & Sheets

- [ ] Create the Drive QA Sandbox folder
  - [ ] Name it exactly `PrivacyFence QA Sandbox` in "My Drive"
  - [ ] Note its file ID (in the folder's URL)
- [ ] Add the grant — covers `drive.read_file_contents`/`drive.download_file`/`sheets.read_values`:
      ```yaml
      auto_accept_grants:
        drive:
          folders:
            - id: "<QA Sandbox folder id>"
              name: "PrivacyFence QA Sandbox"
              read: true
      ```
- [ ] Confirm every Drive/Sheets artifact the test prompt creates goes **inside this folder** (as
      `parent_folder_id`), including uploads, Docs, and moves
- [ ] (Maintenance, periodic) Empty Drive trash by hand — no bulk-empty-trash tool; items auto-purge
      after 30 days regardless
- [ ] For the recorder: no new fixture — targets this folder directly via `drive_get_file_metadata`,
      identified by exact name (a folder has no body to carry `[QATEST]`)
  - [ ] Fill in its file ID (or leave blank to resolve by name) under `drive.folder_id` in
        `tests/fixtures/qa_environment.yaml`
- [ ] (Optional) Add `approved_sandbox_folder` to `sheets.rename_sheet` / `sheets.format_range`,
      scoped to this same folder — kept as raw per-operation rules rather than the
      `drive.sandbox_folders` grant's all-or-nothing `write` capability specifically so
      `connector-qa-testing.md`'s Phase 2 can still exercise the plain popup / "Allow for 5 min"
      flow for the *other* Sheets/Docs writes (`write_file`, `write_doc`, `add_sheet`) in the same
      folder:
      ```yaml
      auto_accept_rules:
        sheets.rename_sheet:
          - rule: approved_sandbox_folder
            value: ["<QA Sandbox folder id>"]
        sheets.format_range:
          - rule: approved_sandbox_folder
            value: ["<QA Sandbox folder id>"]
      ```
- [ ] (Optional) Add the same rule to `sheets.insert_dimensions` / `sheets.delete_dimensions`:
      ```yaml
      auto_accept_rules:
        sheets.insert_dimensions:
          - rule: approved_sandbox_folder
            value: ["<QA Sandbox folder id>"]
        sheets.delete_dimensions:
          - rule: approved_sandbox_folder
            value: ["<QA Sandbox folder id>"]
      ```
- [ ] (Optional) Add the same rule to `docs.edit_content` / `docs.format_content`:
      ```yaml
      auto_accept_rules:
        docs.edit_content:
          - rule: approved_sandbox_folder
            value: ["<QA Sandbox folder id>"]
        docs.format_content:
          - rule: approved_sandbox_folder
            value: ["<QA Sandbox folder id>"]
      ```

## 3. Slack

Two channels: one the test prompt is allowed to read silently, one it isn't (to prove the review
gate still fires for anything not on the allowlist).

- [ ] Designate the approved channel
  - [ ] Pick or create a channel and join it
  - [ ] Add the grant:
        ```yaml
        auto_accept_grants:
          slack:
            channels:
              - id: "<channel id>"
                read: true
        ```
- [ ] Create the control channel
  - [ ] Name it exactly `privacyfence-qa-control` and join it — grant it no capability, it exists
        specifically to *not* match; found by exact name via `slack_list_channels`
  - [ ] Post a synthetic seed message:
        ```
        PrivacyFence QA seed message [QATEST]. No real information. Safe to read/reply/delete.
        ```
  - [ ] Reply to it in-thread (from your own account is fine):
        ```
        PrivacyFence QA seed reply [QATEST]. No real information.
        ```
- [ ] For the recorder: no new fixture — targets this same thread via `get_thread_replies`,
      resolving the channel by exact name and the thread by scanning history for `[QATEST]` (or fill
      in `slack.channel_id`/`slack.seed_thread_ts` in `tests/fixtures/qa_environment.yaml` directly)

## 4. Calendar

**Use a dedicated calendar, not primary.** Create a secondary calendar named exactly
`PrivacyFence test [PFQA]` (Google Calendar → Settings → "Add calendar" → "Create new calendar") and
point every QA activity below at it via its calendar id (`calendar_list_calendars` will show it once
created). The **only** exception is `calendar_create_out_of_office` and `calendar_set_working_location`
— both are hardcoded by Google's API to always operate on your primary calendar regardless of what
`calendar_id` you pass, so there is no way to keep those two off primary. Everything else (the seed
event, the trust grant, `i_am_organizer`/`non_private_event`, ad-hoc reads during a live QA pass)
should target the PFQA calendar instead.

- [ ] Decide `calendar_list_rooms` coverage — needs **Google Workspace** (not consumer Gmail) *and*
      admin rights
  - [ ] No Workspace admin access → skip to the next item; this is a permanent, environment-level
        limitation, not a regression
  - [ ] Workspace admin access available:
        1. Google Cloud Console → APIs & Services → Library → enable **Admin SDK API**
        2. No OAuth scope to add manually — `admin.directory.resource.calendar.readonly` is
           requested at runtime
        3. Google Admin console → **Directory → Buildings and resources → Calendar resources → Add
           resource** — create at least one, e.g. `PrivacyFence QA Room A`
        4. PrivacyFence menu bar → **Connectors → Calendar → Reconnect…** so the token picks up the
           new scope
- [ ] (Optional) Trust the PFQA calendar as a resource-scoped grant — covers
      `calendar.read_event_details` (`read`) and `calendar.create_modify_event`/
      `calendar.set_visibility` (`write`) for events on it, regardless of who organized them (an
      alternative to the per-rule options below):
      ```yaml
      auto_accept_grants:
        calendar:
          calendars:
            - id: "<PFQA calendar id>"
              name: "PrivacyFence test [PFQA]"
              read: true
              write: true
      ```
- [ ] (Optional) Add `i_am_organizer` instead/as well — any event you create satisfies it
      automatically, on any calendar, so this needs no calendar-specific id:
      ```yaml
      auto_accept_rules:
        calendar.read_event_details:
          - rule: i_am_organizer
      ```
- [ ] (Optional) Add `non_private_event`, to also exercise `calendar_get_event_details`
      auto-accepting for a non-private event:
      ```yaml
      auto_accept_rules:
        calendar.read_event_details:
          - rule: non_private_event
      ```
      This only applies to reads — `calendar_set_event_visibility` (`calendar.set_visibility`) is a
      write, gated by the same rules as `calendar.create_modify_event` instead (`i_am_organizer`,
      `no_external_attendees`, `personal_calendar`).
- [ ] For the recorder: create one dedicated seed event on the PFQA calendar, far enough in the
      future that it won't need recreating, no real attendees:
      ```
      Title:       PrivacyFence QA seed event [QATEST]
      Description: Synthetic PrivacyFence QA test event. No real information.
      ```
  - [ ] Fill in the PFQA calendar's id under `calendar.calendar_id`, and the event's id (or leave
        the event id blank to resolve by title search) under `calendar.seed_event_id`, both in
        `tests/fixtures/qa_environment.yaml`

Event attachments (`calendar_get_event_details`'s "Notes by Gemini"/transcript Docs) can't be
provisioned — `calendar_create_event` has no way to attach a file, so this only works if your
account's calendar history already has a past Meet meeting with "take notes for me" enabled;
otherwise it's a known limitation, not a regression. In practice such a meeting will only ever exist
on your real primary calendar, so this one check unavoidably reads primary — a one-off exception
alongside out-of-office/working-location, not a reason to move anything else off PFQA.

No fixture is needed for `calendar_create_out_of_office` or `calendar_set_working_location` — both
always operate on your own primary calendar, unconditionally, regardless of `calendar_id`. Repeated
QA runs each leave behind their own out-of-office event and overwrite the working-location entry (no
delete tool for either) directly on primary — expected, not a bug, and the one place this doc can't
avoid touching your real calendar.

## 5. Contacts

- [ ] (Optional) Add `no_contact_info_change` — auto-accepts edits that don't touch
      `emails`/`phones` (e.g. appending a note), safe to leave enabled permanently:
      ```yaml
      auto_accept_rules:
        contacts.edit:
          - rule: no_contact_info_change
      ```
- [ ] For the recorder: create one dedicated seed contact. Unlike every other connector, a contact's
      name/email/phone fields *are* the content under test, so the recorder does **not** apply its
      usual identity redaction to this fixture — see `check_contacts()` in
      `scripts/qa_fixture_recorder.py`:
      ```
      Display name: PrivacyFence QA Test Contact [QATEST]
      Email:        qatest.contact@example.com
      Phone:        555-0142
      ```
  - [ ] Fill in its resource name (e.g. `people/c12345`, or leave blank to resolve by name search)
        under `contacts.seed_contact_resource_name` in `tests/fixtures/qa_environment.yaml`

No fixture is needed for `source="personal"` vs. `source="directory"` — whether your account has
Workspace directory colleagues is a fact about the account; an empty `directory` result is correct
if it doesn't.

## 6. Google Tasks

**Don't use "My Tasks" for QA at all** — it's your real, default list. Use two dedicated lists
instead, neither of which is the default:

- [ ] Configure the approved list
  - [ ] Create a list named exactly `PrivacyFence QA List` — this is the approved/granted list.
  - [ ] Get its ID from `tasks_list_task_lists` or headlessly with `scripts/qa_list_ids.py tasks`
  - [ ] Add the grant:
        ```yaml
        auto_accept_grants:
          tasks:
            task_lists:
              - id: "<PrivacyFence QA List id>"
                name: "PrivacyFence QA List"
                edit: true
                complete: true
        ```
        `complete` covers both `tasks.complete_task` and `tasks.uncomplete_task`.
- [ ] Create the contrast list
  - [ ] Create a second list named exactly `PrivacyFence QA Contrast List`, grant it no capability —
        it exists specifically to *not* match, for `connector-qa-testing.md`'s Phase 6 steps 7–8
        (the "should still prompt even though a rule exists elsewhere" check). The recorder itself
        doesn't use this list at all.
- [ ] (Optional) Add `create: true` to the approved list's grant, and/or `move: true` to **both**
      list entries (a move only auto-accepts when both ends have `move: true`)
- [ ] For the recorder: create one dedicated seed task in `PrivacyFence QA List`:
      ```
      Title: PrivacyFence QA seed task [QATEST]
      Notes: Synthetic PrivacyFence QA test task. No real information.
      ```
  - [ ] Fill in the approved list's id and the task's id under `tasks.task_list_id` /
        `tasks.seed_task_id` in `tests/fixtures/qa_environment.yaml` — both required, no
        by-title resolve fallback (`tasks_client.py` has no search-by-title method)

## 7. Telegram

- [ ] Seed Saved Messages
  - [ ] Open Telegram and send yourself **one** message, once:
        ```
        PrivacyFence QA seed message [QATEST]. No real information.
        ```
        (`telegram_list_chats` only returns chats you've opened at least once; once sent, it stays
        forever. Found every run via the `is_self` flag, not by name.)
  - [ ] For the recorder: no new fixture — same message, same resolve-by-`is_self` logic; scans
        Saved Messages' recent history for the `[QATEST]` tag
- [ ] Decide `approved_chats`
  - [ ] Point it at Saved Messages itself (get its numeric `chat_id` from `telegram_list_chats` or
        headlessly with `scripts/qa_list_ids.py telegram` — look for `is_self=True`), or a second
        low-stakes chat
  - [ ] Add the grant:
        ```yaml
        auto_accept_grants:
          telegram:
            chats:
              - id: "<chat_id>"
                read: true
        ```
- [ ] Confirm at least one *other* chat beyond your approved one has some message history — real by
      necessity, used only for its existence: the contrast case for "not approved, should still
      prompt" and for `telegram_search_messages` to have something to find

Whether a native approval popup actually appeared for `telegram_get_messages` /
`telegram_search_messages` can be ambiguous from the tool result alone — cross-reference the audit
log's `decision` field for these calls.

## 8. Salesforce

A fresh Salesforce org typically has zero data rows anywhere reachable — every call either 404s,
comes back empty, or hits `FORBIDDEN` until you seed data:

- [ ] Seed sample records
  - [ ] **Setup → Object Manager → Account** (or any object) → create 2–3 sample records, tagged:
        ```
        Name: PrivacyFence QA — Acme Test Co [QATEST]
        Name: PrivacyFence QA — Globex Test Co [QATEST]
        ```
- [ ] Create the QA report
  - [ ] **Reports → New Report**, based on that object, named exactly `PrivacyFence QA Report`
- [ ] Add the grants — the report ID is grant-managed; the object type is a small fixed vocabulary,
      so it stays a plain `auto_accept_rules` entry:
      ```yaml
      auto_accept_grants:
        salesforce:
          reports:
            - id: "<PrivacyFence QA Report id>"
              name: "PrivacyFence QA Report"
              run: true
      auto_accept_rules:
        salesforce.read_record:
          - rule: approved_object_types
            value: [Account]
      ```
- [ ] Confirm a contrast case exists — any report/object type not added above already serves as
      the "should still prompt" contrast
- [ ] Confirm `salesforce_search` needs no separate fixture — the tagged sample records above are
      what a search for `PrivacyFence QA` should find. Salesforce's SOSL search index can take a
      minute or two to pick up freshly created records — an empty result immediately after seeding
      is a known quirk, not a bug; wait and retry.
- [ ] (Optional) Add `approved_object_types` to `salesforce.search` too:
      ```yaml
      auto_accept_rules:
        salesforce.search:
          - rule: approved_object_types
            value: [Account]
      ```
      A search scoped to `object_types="Account"` auto-accepts; one that also touches another type,
      or is left unscoped, still prompts.
- [ ] For the recorder: no new fixture — targets one of the sample Account records above directly
      (`PrivacyFence QA — Acme Test Co [QATEST]`)
  - [ ] Fill in its record ID (or leave blank to resolve via `search()` on its name) under
        `salesforce.seed_record_id` in `tests/fixtures/qa_environment.yaml`

## 9. Jira

- [ ] Create the QA project
  - [ ] Key exactly `PFQA`, any template (Kanban is fine)
- [ ] Add the grant:
      ```yaml
      auto_accept_grants:
        jira:
          projects:
            - key: PFQA
              name: "PrivacyFence QA"
              read: true
      ```
- [ ] Confirm at least one other real Jira project exists in your site as the "different project,
      should still prompt" contrast — create a throwaway second one only if `PFQA` would otherwise
      be the only project in your site
- [ ] Confirm `i_am_reporter`/`i_am_assignee` need no setup — any issue you create in `PFQA`
      satisfies both automatically. If a second Jira user exists, optionally reassign one test issue
      to them for an `i_am_assignee` contrast
- [ ] Confirm `jira_get_transitions`/`jira_transition_issue` need no setup — every project ships
      with a default workflow with at least one transition reachable from a new issue
- [ ] (Opportunistic) If `PFQA`'s issue screen has a custom field, note it for the `custom_fields`
      test; otherwise add one (e.g. a number field "Story Points"), or skip — a fixture-availability
      limitation, not a regression
- [ ] For the recorder: create one dedicated seed issue in `PFQA`:
      ```
      Summary:     PrivacyFence QA seed issue [QATEST]
      Description: Synthetic PrivacyFence QA test issue. No real information. Safe to comment on,
                   update, or transition by any automated test.
      ```
  - [ ] Fill in its issue key (or leave blank to resolve by a JQL summary search) under
        `jira.seed_issue_key` in `tests/fixtures/qa_environment.yaml`

## 10. Confluence

- [ ] Create the QA space
  - [ ] Key exactly `PFQA`
- [ ] Add the grant:
      ```yaml
      auto_accept_grants:
        confluence:
          spaces:
            - key: PFQA
              name: "PrivacyFence QA"
              read: true
      ```
- [ ] Confirm at least one other real Confluence space exists as the contrast case — create a
      throwaway second one only if `PFQA` would otherwise be the only space in your site
- [ ] Confirm `i_am_author` needs no setup — any page you create in `PFQA` satisfies it
      automatically
- [ ] Confirm the daemon build under test has the Confluence v1→v2 API migration (commit `34e7108`
      in this repo) — `confluence_get_page`/`create_page`/`update_page` return 410 without it
- [ ] For the recorder: create one dedicated seed page in `PFQA`:
      ```
      Title: PrivacyFence QA seed page [QATEST]
      Body:  Synthetic PrivacyFence QA test page. No real information. Safe to read, comment on,
             or edit by any automated test.
      ```
  - [ ] Fill in its page ID (or leave blank to resolve by title) under `confluence.seed_page_id` in
        `tests/fixtures/qa_environment.yaml`

Every connector above has recorder support (`scripts/qa_fixture_recorder.py`'s `CONNECTOR_CHECKS`).

## 11. PII Detection Gate

- [ ] Confirm **PII Detection Gate** is enabled in the menu bar (default on — equivalently,
      `pii_detection.enabled` is `true` or absent in `settings.yaml`)

No fixture to create here — `connector-qa-testing.md`'s Phase 2 (steps 17–20) creates and tears down
its own throwaway Drive subfolder and Doc seeded with synthetic PII. The gate only ever runs on the
`review` (read) direction, never `popup` (write) — see
[TECHNICAL_REFERENCE.md](TECHNICAL_REFERENCE.md#pii-detection-gate) — so a write always stays plain
while the matching read gets flagged, when the gate is enabled. If you've turned it off, the read
also stays plain (no tint, `pii_detected: false`), which is the expected disabled-state result, not
a failure.

## 12. Scheduled / unattended Cowork tasks

- [ ] No new fixture — `connector-qa-testing.md`'s Phase 11 reuses the Slack channels from §3
- [ ] Know how to restart your daemon (`privacyfence-app`, or `scripts/dev_start.sh` from source —
      see [`dev-vs-live-setup.md`](dev-vs-live-setup.md)): `unattended_sessions.enabled` in
      `org_config.json` is off by default, has no menu-bar toggle, and isn't hot-reloaded — Phase 11
      toggles it (via `scripts/build_org_bundle.py --merge --enable/disable-unattended-sessions`)
      and restarts the daemon twice as part of the phase itself. See
      [TECHNICAL_REFERENCE.md](TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks).

---

## Consolidated `auto_accept_grants` / `auto_accept_rules` blocks

- [ ] Merge both into `settings.yaml` (separate top-level keys, both shown here), replacing every
      `<placeholder>` with your actual value:
      ```yaml
      auto_accept_grants:
        drive:
          folders:
            - id: "<QA Sandbox folder id>"
              name: "PrivacyFence QA Sandbox"
              read: true
        slack:
          channels:
            - id: "<approved channel id>"
              read: true
        telegram:
          chats:
            - id: "<chat_id>"
              read: true
        calendar:
          calendars:
            - id: "<PFQA calendar id>"
              name: "PrivacyFence test [PFQA]"
              read: true
              write: true
        salesforce:
          reports:
            - id: "<PrivacyFence QA Report id>"
              name: "PrivacyFence QA Report"
              run: true
        jira:
          projects:
            - key: PFQA
              name: "PrivacyFence QA"
              read: true
        confluence:
          spaces:
            - key: PFQA
              name: "PrivacyFence QA"
              read: true
        tasks:
          task_lists:
            - id: "<PrivacyFence QA List id>"
              name: "PrivacyFence QA List"
              edit: true
              complete: true

      auto_accept_rules:
        gmail.read_message:
          - rule: trusted_sender_domain
            value: [example.com]              # a domain you actually receive mail from
        calendar.read_event_details:
          - rule: i_am_organizer
        contacts.edit:
          - rule: no_contact_info_change
        salesforce.read_record:
          - rule: approved_object_types
            value: [Account]
      ```
- [ ] Restart the daemon after editing by hand (or use "Always allow" once, which hot-reloads rules)

`sheets.read_values` → `approved_spreadsheet` (grant-managed under `drive.spreadsheets`) isn't
included above — it's created automatically the first time you click "Always allow" on a
`drive_sheets_get_values` call. `sheets.rename_sheet`/`format_range`/`insert_dimensions`/
`delete_dimensions` and `docs.edit_content`/`format_content` → `approved_sandbox_folder` (§2),
`calendar.calendars`'s `write` capability (§4), and `salesforce.search` → `approved_object_types`
(§8) are also left out of this block by default, since enabling them would make Phase 2/4/8 silently
auto-accept operations meant to exercise the popup flow — add whichever you specifically want
auto-accepted, individually.

---

## Fixture reference

What `connector-qa-testing.md`'s Phase 0 looks up, and how it finds each one. For the Telegram and
Tasks rows, `scripts/qa_list_ids.py` prints the same IDs headlessly, without a live Claude session.

| Fixture | How it's found | Source |
|---|---|---|
| Gmail seed thread | Subject tag `[QATEST]` (optional — see §1) | `gmail_list_messages` |
| Gmail recorder seed message | `gmail.seed_message_id`, required (no resolve fallback) | `tests/fixtures/qa_environment.yaml` |
| Drive QA folder | Exact name `PrivacyFence QA Sandbox` | `drive_list_files` |
| Drive recorder target | Same QA Sandbox folder, by exact name or `drive.folder_id` | `tests/fixtures/qa_environment.yaml` |
| Slack approved channel | `auto_accept_grants.slack.channels` (`read: true`), or legacy `approved_channel` | `settings.yaml` |
| Slack control channel | Exact name `privacyfence-qa-control` | `slack_list_channels` |
| Slack recorder seed thread | Same control-channel thread, tag `[QATEST]` (or `slack.channel_id`/`slack.seed_thread_ts`) | `tests/fixtures/qa_environment.yaml` |
| Telegram Saved Messages | `is_self: true` flag | `telegram_list_chats` |
| Telegram approved chat | `auto_accept_grants.telegram.chats` (`read: true`), or legacy `approved_chats` (falls back to Saved Messages) | `settings.yaml` |
| Telegram control chat | Any chat that isn't the above two | `telegram_list_chats` |
| Telegram recorder seed message | Same Saved Messages `[QATEST]` message (or `telegram.chat_id`) | `tests/fixtures/qa_environment.yaml` |
| Calendar trusted calendar | `auto_accept_grants.calendar.calendars` (`read`/`write`) — optional, see §4 | `settings.yaml` |
| Calendar recorder seed event | Title tag `[QATEST]` (or `calendar.seed_event_id`) | `tests/fixtures/qa_environment.yaml` |
| Salesforce QA report | `auto_accept_grants.salesforce.reports` (`run: true`), or legacy `approved_report_ids` (falls back to exact name `PrivacyFence QA Report`) | `settings.yaml` / `salesforce_list_reports` |
| Salesforce QA object type | `salesforce.read_record` → `approved_object_types` (falls back to `Account`) | `settings.yaml` |
| Salesforce recorder seed record | Name tag `[QATEST]`, same record as the seeded Account | `tests/fixtures/qa_environment.yaml` |
| Jira QA project | Literal key `PFQA` | — |
| Jira contrast project | Any project key that isn't `PFQA` | `jira_list_projects` |
| Jira recorder seed issue | Summary tag `[QATEST]` in `PFQA` | `tests/fixtures/qa_environment.yaml` |
| Confluence QA space | Literal key `PFQA` | — |
| Confluence contrast space | Any space key that isn't `PFQA` | `confluence_list_spaces` |
| Confluence recorder seed page | Title tag `[QATEST]` in `PFQA` | `tests/fixtures/qa_environment.yaml` |
| Tasks approved list | Exact name `PrivacyFence QA List`, or `auto_accept_grants.tasks.task_lists` (`edit: true`) | `settings.yaml` / `tasks_list_task_lists` |
| Tasks contrast list | Exact name `PrivacyFence QA Contrast List` | `tasks_list_task_lists` |
| Tasks recorder seed task | `tasks.task_list_id` + `tasks.seed_task_id`, both required | `tests/fixtures/qa_environment.yaml` |
| Contacts recorder seed contact | Display name tag `[QATEST]` (or `contacts.seed_contact_resource_name`) — fixture is **not** redacted, see §5 | `tests/fixtures/qa_environment.yaml` |

---

## Idempotency: environment fixtures vs. per-run artifacts

- **Environment fixtures** (this doc): the `PFQA` Jira project/Confluence space, the Drive Sandbox
  folder, the Slack channels, the Telegram chats, the Salesforce sample records/report. Created
  **once**; re-discovered by every subsequent run, never recreated or pasted in by hand.
- **Per-run artifacts** (`connector-qa-testing.md`'s test prompt): drafts, events, one-off
  issues/pages/files. Carry a run-scoped identifier so repeated runs don't produce indistinguishable
  duplicates, and get cleaned up by that doc's teardown phase.

If you deliberately skip a fixture (e.g. no Workspace admin access for Calendar rooms), that's a
permanent, known gap — leave the corresponding config/rule unset rather than half-configuring it.
