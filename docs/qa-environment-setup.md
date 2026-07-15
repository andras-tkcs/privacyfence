# QA Environment Setup (for `connector-qa-testing.md`)

This is a complete, standalone installation guide for the environment that
[`connector-qa-testing.md`](connector-qa-testing.md) — and the local fixture recorder, see
[`external-api-contract-testing.md`](external-api-contract-testing.md) — run against. Work through
it top to bottom on any machine/account where you want to run either one; it doesn't assume
anything was set up before, and it doesn't assume you've read any prior QA report.

**Scope**: this doc only covers QA-specific fixtures — a dedicated Jira project, a Drive sandbox
folder, sample Salesforce records, and so on. Base connector *authentication* (OAuth apps, API
enablement, tokens) is a separate, one-time prerequisite covered by each connector's own setup
guide; see [Prerequisites](#prerequisites) below.

Once set up, these fixtures are **durable and reusable** — the test prompt re-discovers them by
name/key/flag at the start of every run instead of you recreating or re-pasting anything (see
[`connector-qa-testing.md`](connector-qa-testing.md)'s Phase 0). You should only need to work
through this guide once per environment, and revisit a section if you ever rename/delete one of the
fixtures it describes. Every section below is a checklist — check items off as you go; nothing here
needs to be re-run once done, short of that.

## The one rule this doc follows wherever it creates content

**A fixture's *identity* (which project, space, channel, folder) can be real — these are your real
accounts, and that's fine — but a fixture's *content*, wherever this doc has you type or paste
something, is always synthetic and tagged, never something copied from a real message/contact/event
you didn't create for this purpose.** Concretely: `PFQA`/`PrivacyFence QA`-prefixed names identify
*which* real project or folder a fixture lives in; a `[QATEST]` tag inside a body/title/summary
marks content that's *safe to read, act on, or record* because it was written for exactly that.

A few things below are real by necessity, not by oversight, and are called out explicitly where
they occur: `trusted_sender_domain` needs a domain you actually receive mail from (there's no
synthetic substitute for "is this domain in my real inbox"), and a couple of contrast cases lean on
"whatever other real project/space/chat already exists" rather than a dedicated second one, since
that's simpler and lower-risk than provisioning a second fixture purely to be empty. In both cases,
only the *existence* of something real matters, never its content — nothing here ever reads,
quotes, or acts on what that real thing actually contains.

**Placeholder values** — use these wherever a step needs a fake email/phone/address:

- Email: anything `@example.com` / `@example.org` / `@example.net` — reserved by RFC 2606
  specifically so these addresses never resolve to a real mailbox.
- Phone: `555-01XX` (e.g. `555-0142`) — the `555-0100`–`555-0199` block is reserved by NANPA
  specifically for fictional use in exactly this kind of test content.

Everything this guide creates uses the prefix **`PFQA`** (Jira/Confluence keys) or **`PrivacyFence
QA`** (folder/channel/report/event names, titles) so it's grep-able and obviously safe to touch, and
tags any body/message content with **`[QATEST]`**. **Names must match exactly** where noted — the
test prompt looks fixtures up by exact string match, not fuzzy search, so a typo here means Phase 0
reports the fixture as missing instead of silently guessing wrong.

---

## Prerequisites

- [ ] Authenticate every connector you want to test, from the PrivacyFence menu bar
  - [ ] Gmail, Drive, Calendar, Contacts, Tasks — [`google-cloud-setup.md`](google-cloud-setup.md)
  - [ ] Slack — [`slack-setup.md`](slack-setup.md)
  - [ ] Jira & Confluence — [`atlassian-setup.md`](atlassian-setup.md)
  - [ ] Salesforce — [`salesforce-setup.md`](salesforce-setup.md)
  - [ ] Telegram — [`telegram-setup.md`](telegram-setup.md)
- [ ] Confirm `privacyfence-app` (the daemon) is running, and you have admin/owner-level access on
      each external service to create a project/space/channel/folder in it (not just read access)
- [ ] Confirm you can edit and reload `settings.yaml`
  - [ ] Bundled install: `~/.privacyfence/config/settings.yaml`
  - [ ] From source: `config/settings.yaml` in the repo root — see
        [`dev-vs-live-setup.md`](dev-vs-live-setup.md)
  - [ ] Reload it by restarting the daemon, or by clicking "Accept All" once on any popup, which
        calls `reload_rules()` for you
- [ ] Note your own email address as PrivacyFence sees it (`my_email`) — used internally by rules
      like `i_am_sender`/`i_am_organizer`; it's just whatever address you authenticated
      Gmail/Calendar/etc. with, nothing extra to configure

---

## 1. Gmail

- [ ] (Optional) Create a synthetic seed thread, if you'd rather not point the Deny test or the
      label/archive round-trip at a real message
  - [ ] Send yourself an email:
        ```
        Subject: PrivacyFence QA seed message [QATEST]
        Body:
        Synthetic PrivacyFence QA test message. No real information. Safe to read, label,
        archive, or delete by any automated test.
        ```
  - [ ] Reply to your own message, creating a real 2-message thread that's still entirely
        synthetic:
        ```
        Subject: Re: PrivacyFence QA seed message [QATEST]
        Body:
        Synthetic PrivacyFence QA reply. No real information.
        ```
  - Note: it's also the right pick for the Deny test either way — short, no large attachment, so a
    Deny response can't be confused with a size-truncation error.
- [ ] (Optional) Configure the `trusted_sender_domain` rule
  - [ ] Look through your inbox (or run `gmail_list_messages`) and pick any sender domain you get
        recurring mail from — a newsletter, a receipt sender, a notification address
  - [ ] Add it to `settings.yaml` — rules are a list, add more than one domain if you like:
        ```yaml
        auto_accept_rules:
          gmail.read_message:
            - rule: trusted_sender_domain
              value: [example.com]   # ← whatever domain you actually receive mail from
        ```

> **Note** — no fixture is needed for the archive/label round-trip (add label → archive →
> un-archive → remove label): it targets whatever message you already picked for the first
> `gmail_get_message` call (the seed thread above, or any message) and restores it to its exact
> starting state by the end, so there's nothing to provision or clean up here either way.

## 2. Drive & Sheets

- [ ] Create the Drive QA Sandbox folder
  - [ ] Name it exactly `PrivacyFence QA Sandbox` in "My Drive"
  - [ ] Note its file ID (visible in the folder's URL) — you need it once, for the grant below; the
        test prompt itself finds the folder by name, not by ID
- [ ] Add the auto-accept fixture — a `drive.folders` grant with `read: true` covers
      `drive.read_file_contents`/`drive.download_file` and `sheets.read_values` all at once (see
      [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants)):
      ```yaml
      auto_accept_grants:
        drive:
          folders:
            - id: "<QA Sandbox folder id>"
              name: "PrivacyFence QA Sandbox"
              read: true
      ```
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Drive → Trusted Folders → + Add
    folder…**, then check **Read auto-accept**. The older, still-supported form is a raw
    `approved_folder` rule under `auto_accept_rules` — see
    [Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules).
- [ ] Confirm every Drive/Sheets artifact the test prompt creates goes **inside this folder** (as
      `parent_folder_id`), including uploads, Docs, and moves — already synthetic content by
      construction, so nothing here ever touches a real file or folder elsewhere in your Drive
- [ ] (Maintenance, periodic) Empty Drive trash by hand — there's no bulk-empty-trash tool; trashed
      items auto-purge after 30 days regardless
- [ ] (Optional) Extend `approved_sandbox_folder` to `sheets.rename_sheet` / `sheets.format_range`
  - [ ] Only add this if you specifically want `connector-qa-testing.md`'s Phase 2 to also
        demonstrate that these two can auto-accept by folder. Skip it if you'd rather Phase 2 keep
        exercising the plain popup / "Accept for 5 min" flow instead — the two are mutually
        exclusive for the same spreadsheet, since a matching rule auto-accepts before any popup
        would appear:
        ```yaml
        auto_accept_rules:
          sheets.rename_sheet:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
          sheets.format_range:
            - rule: approved_sandbox_folder
              value: ["<QA Sandbox folder id>"]
        ```
  - Note: a `drive.sandbox_folders` grant's `write` capability (this step's grant-managed sibling —
    see the consolidated block below) auto-accepts **every** Drive/Sheets/Docs write operation for
    that folder at once — `drive.write_file`, `drive.write_doc`, all six `sheets.*` writes
    (including `rename_sheet`/`format_range` and, below, `insert_dimensions`/`delete_dimensions`),
    and `docs.edit_content`/`docs.format_content`. If you want *only* `rename_sheet`/`format_range`
    auto-accepted while the rest keep prompting, that per-operation split isn't expressible as a
    grant — use the raw rules above directly instead.
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

## 3. Slack

Two channels are needed: one the test prompt is allowed to read silently, and one it isn't (to
prove the review gate still fires for anything not on the allowlist).

- [ ] Designate the approved channel
  - [ ] Pick or create a channel and join it
  - [ ] Add its ID as a `slack.channels` grant with `read: true`:
        ```yaml
        auto_accept_grants:
          slack:
            channels:
              - id: "<channel id>"
                read: true
        ```
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Slack → Trusted Channels → + Add
    channel…** — this connector supports the live picker (by channel name), so you don't need to
    look up the ID by hand. The test prompt itself still reads the ID straight out of the config —
    there's no naming requirement on this channel. The older, still-supported form is a raw
    `approved_channel` rule under `auto_accept_rules`.
- [ ] Create the control channel
  - [ ] Name it exactly `privacyfence-qa-control` and join it
  - [ ] Do **not** grant it any capability — it exists specifically to *not* match, and the test
        prompt finds it by this exact name via `slack_list_channels`
  - [ ] Post a synthetic seed message:
        ```
        PrivacyFence QA seed message [QATEST]. No real information. Safe to read/reply/delete.
        ```
  - [ ] Reply to it in-thread (replying from your own account is fine — Slack doesn't require a
        second person for a thread to exist):
        ```
        PrivacyFence QA seed reply [QATEST]. No real information.
        ```
        This gives `slack_get_thread_replies` permanent, reusable, entirely synthetic content
        instead of depending on whatever a given run happens to surface.

## 4. Calendar

- [ ] Decide `calendar_list_rooms` coverage — needs **Google Workspace** (not a consumer Gmail
      account) *and* admin rights, a hard external dependency no local config can substitute for
  - [ ] Consumer Gmail, or no Workspace admin access → skip straight to the next item. This is a
        permanent, environment-level limitation — the test prompt already treats the resulting
        error as expected rather than a regression
  - [ ] Workspace admin access available:
        1. In Google Cloud Console → APIs & Services → Library, enable **Admin SDK API** (see
           [`google-cloud-setup.md`](google-cloud-setup.md))
        2. Nothing to add manually for the OAuth scope —
           `admin.directory.resource.calendar.readonly` is requested at runtime by
           `calendar_client.py`
        3. In the Google Admin console: **Directory → Buildings and resources → Calendar
           resources → Add resource**. Create at least one, e.g. `PrivacyFence QA Room A`
        4. In PrivacyFence: **Connectors → Calendar → Reconnect…** so the token picks up the new
           admin scope
- [ ] (Optional) Add the `i_am_organizer` rule — any event you create satisfies it automatically:
      ```yaml
      auto_accept_rules:
        calendar.read_event_details:
          - rule: i_am_organizer
      ```
- [ ] (Optional) Add the `non_private_event` rule, if you want Phase 2 to demonstrate
      `calendar_get_event_details` and `calendar_set_event_visibility` auto-accepting for a
      non-private event:
      ```yaml
      auto_accept_rules:
        calendar.read_event_details:
          - rule: non_private_event
        calendar.set_visibility:
          - rule: non_private_event
      ```
  - Note: no fixture is needed for the contrast case either way — any event the test prompt sets to
    `private` still prompts regardless of this rule, since `non_private_event` checks the
    visibility being *requested*, not the event's prior state. Combine with `i_am_organizer` above
    if you want both — a matching rule short-circuits the list, so order only changes which rule
    name shows up in the audit log, not what auto-accepts.

> **Note** — event attachments (`calendar_get_event_details`'s "Notes by Gemini"/transcript Docs)
> are opportunistic, not something this doc can provision: attachments can't be created via
> `calendar_create_event`, so this only works if your account's calendar history already has a past
> Google Meet meeting with "take notes for me" enabled; otherwise that step is a known limitation,
> not a regression.
>
> **Note** — no fixture is needed for `calendar_create_out_of_office` or
> `calendar_set_working_location` either: both always operate on your own primary calendar (a
> Google Calendar API restriction), so any account works out of the box. Repeated QA runs each leave
> behind their own out-of-office event and overwrite the working-location entry — there's no delete
> tool for either, so they accumulate in "needs manual deletion" the same way plain Calendar events
> do (see Phase 11 in [`connector-qa-testing.md`](connector-qa-testing.md)).

## 5. Contacts

- [ ] (Optional) Add the `no_contact_info_change` rule — auto-accepts edits that don't touch
      `emails`/`phones` (e.g. appending `(PrivacyFence QA test)` to a name/note field), safe to
      leave enabled permanently since an edit that *does* touch email/phone still prompts anyway:
      ```yaml
      auto_accept_rules:
        contacts.edit:
          - rule: no_contact_info_change
      ```

> **Note** — no fixture is needed for `source="personal"` vs. `source="directory"`: whether your
> account has Workspace directory colleagues is a fact about your account, not something to
> provision. If it doesn't, `source="directory"` coming back empty is the correct, permanent answer.

## 6. Google Tasks

- [ ] Configure the approved list
  - [ ] Get your default list's ("My Tasks") ID from `tasks_list_task_lists` (via a live Claude
        session) or headlessly with `scripts/qa_list_ids.py tasks`
  - [ ] Add it as a `tasks.task_lists` grant with `edit`/`complete` (and optionally
        `create`/`move`, see below):
        ```yaml
        auto_accept_grants:
          tasks:
            task_lists:
              - id: "<default list id>"
                name: "My Tasks"
                edit: true
                complete: true
        ```
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Tasks → Trusted Task Lists → +
    Add task list…** (live picker by list name), then check **Auto-accept edits** and **Auto-accept
    complete/uncomplete**. `complete` covers both `tasks.complete_task` and `tasks.uncomplete_task`
    at once — one difference from the older, still-supported `auto_accept_rules` form, which
    configured them separately.
- [ ] Create the contrast list
  - [ ] Name it exactly `PrivacyFence QA Contrast List` (no tool creates a task list through
        PrivacyFence itself, so this has to be done outside it)
  - [ ] Do **not** grant it any capability — it exists specifically to *not* match, and the test
        prompt finds it by this exact name via `tasks_list_task_lists`
- [ ] (Optional) Add `create: true` to the default list's grant entry, and/or `move: true` to
      **both** the default and contrast list's grant entries (a move only auto-accepts when both
      the source and destination list have `move: true` set). Skip any capability you'd rather
      leave always-prompting — the test prompt handles "not configured" gracefully for each one
      independently.

## 7. Telegram

- [ ] Seed Saved Messages
  - [ ] Open Telegram (phone or desktop) and send yourself **one** message, once:
        ```
        PrivacyFence QA seed message [QATEST]. No real information.
        ```
        (`telegram_list_chats` only returns chats you've actually opened at least once, and "Saved
        Messages" is no exception; once sent, it stays in the list forever. No naming needed
        afterward — the test prompt finds it every run via the `is_self` flag, not by matching a
        name.)
- [ ] Decide `approved_chats`
  - [ ] Point it at Saved Messages itself (get its numeric `chat_id` from `telegram_list_chats`
        after the step above, or headlessly with `scripts/qa_list_ids.py telegram` — look for
        `is_self=True`) — safe, since it's always fine to auto-accept reads of your own messages to
        yourself. Or create/repurpose a second low-stakes chat (a private group with just you, or a
        throwaway test contact) and use its `chat_id` instead
  - [ ] Add the grant:
        ```yaml
        auto_accept_grants:
          telegram:
            chats:
              - id: "<chat_id>"
                read: true
        ```
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Telegram → Trusted Chats → + Add
    chat…** (live picker by chat name). Skip this grant entirely if you'd rather leave Telegram
    reads always review-gated — the test prompt handles "not configured" gracefully. The older,
    still-supported form is a raw `approved_chats` rule under `auto_accept_rules`.
- [ ] Confirm at least one *other* chat beyond your approved one has some message history — real by
      necessity (no bot-token equivalent exists for reading a real chat's own history), used only
      for its existence, never its content: the test prompt picks any such chat dynamically for the
      "not approved, should still prompt" contrast case and for `telegram_search_messages` to have
      something to find

> **Note** — whether a native approval popup actually appears for `telegram_get_messages` /
> `telegram_search_messages` can be ambiguous from the tool result alone. The test prompt has Claude
> watch for the popup *and* cross-reference the audit log's `decision` field for these calls, so
> this doesn't require any special environment setup, just both checks being run.

## 8. Salesforce

A fresh Salesforce org (dev/sandbox) typically has zero data rows anywhere reachable, which means
every call either 404s, comes back empty, or hits `FORBIDDEN` — none of that is a gate bug, but it
also means the *success* path never gets exercised unless you seed some data:

- [ ] Seed sample records
  - [ ] **Setup → Object Manager → Account** (or any object you're comfortable using) → create 2–3
        sample records, all clearly fake and tagged:
        ```
        Name: PrivacyFence QA — Acme Test Co [QATEST]
        Name: PrivacyFence QA — Globex Test Co [QATEST]
        ```
- [ ] Create the QA report
  - [ ] **Reports → New Report**, based on that object, named exactly `PrivacyFence QA Report`
- [ ] Add the auto-accept fixtures — recommended, this is also how you get rule coverage for both
      the report and the object type. The report ID is grant-managed; the object type is a small
      fixed vocabulary, not a resource identity, so it stays a plain `auto_accept_rules` entry:
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
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Salesforce → Trusted Reports → +
    Add report…** (live picker by report name), then check **Read auto-accept**. If you skip the
    report grant, the test prompt falls back to finding the report by its exact name via
    `salesforce_list_reports` — either path works.
- [ ] Confirm a contrast case exists — any report/object type you don't add above already serves as
      the "should still prompt" contrast; no extra setup needed
- [ ] Confirm `salesforce_search` needs no separate fixture — the tagged sample records above are
      exactly what a search for `PrivacyFence QA` should find
  - Note: **known quirk, not a bug** — Salesforce's SOSL search index can take a minute or two to
    pick up freshly created records. If a search run immediately after seeding comes back empty,
    wait briefly and retry before treating it as a regression.
- [ ] (Optional) Add `approved_object_types` to `salesforce.search`, generalized to check *every*
      object type in the search's comma-separated `object_types` (not just one):
      ```yaml
      auto_accept_rules:
        salesforce.search:
          - rule: approved_object_types
            value: [Account]
      ```
  - Note: with this configured, a search scoped to `object_types="Account"` auto-accepts; a search
    that also touches any other object type, or one left unscoped entirely (Salesforce's default
    globally-searchable set), still prompts — that asymmetry is worth confirming too, not just the
    auto-accept path.

## 9. Jira

- [ ] Create the QA project
  - [ ] Key exactly `PFQA`, any template (Kanban is fine) — the key has to match exactly, the test
        prompt uses it as a literal string, not a fuzzy lookup
- [ ] Add the fixture as a `jira.projects` grant with `read: true`:
      ```yaml
      auto_accept_grants:
        jira:
          projects:
            - key: PFQA
              name: "PrivacyFence QA"
              read: true
      ```
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Jira → Trusted Projects → + Add
    project…** (live picker by project name), then check **Read auto-accept**. The older,
    still-supported form is a raw `approved_project_keys` rule under `auto_accept_rules`.
- [ ] Confirm at least one other real Jira project exists in your site to serve as the "different
      project, should still prompt" contrast — the test prompt picks whichever project isn't
      `PFQA` at runtime; create one throwaway second project (any key) only if `PFQA` would
      otherwise be the only one in your site
- [ ] Confirm `i_am_reporter`/`i_am_assignee` need no setup — any issue you create in `PFQA`
      satisfies both automatically. If a second Jira user exists in your site, optionally reassign
      one test issue to them to get a contrast case for `i_am_assignee`; skip this if you're the
      only user
- [ ] Confirm `jira_get_transitions`/`jira_transition_issue` need no setup — every Jira project
      ships with a default workflow that has at least one transition reachable from a new issue's
      initial status (e.g. "To Do" → "In Progress")
- [ ] (Opportunistic) If `PFQA`'s issue screen has a custom field, note it for the `custom_fields`
      test — check an issue's "..." menu or **Project settings → Fields**. If it doesn't have one
      and you want this exercised, add any custom field (e.g. a number field called "Story
      Points"); otherwise the test prompt skips that step as a fixture-availability limitation, not
      a regression

## 10. Confluence

- [ ] Create the QA space
  - [ ] Key exactly `PFQA`
- [ ] Add the fixture as a `confluence.spaces` grant with `read: true`:
      ```yaml
      auto_accept_grants:
        confluence:
          spaces:
            - key: PFQA
              name: "PrivacyFence QA"
              read: true
      ```
  - Note: equivalently, from the menu bar — **Auto-accept Rules → Confluence → Trusted Spaces → +
    Add space…** (live picker by space name), then check **Read auto-accept**. The older,
    still-supported form is a raw `approved_space_keys` rule under `auto_accept_rules`.
- [ ] Confirm at least one other real Confluence space exists in your site to serve as the contrast
      case — the test prompt picks whichever space isn't `PFQA` at runtime; create one throwaway
      second space only if `PFQA` would otherwise be the only one in your site
- [ ] Confirm `i_am_author` needs no setup — any page you create in `PFQA` satisfies it
      automatically
- [ ] Confirm the daemon build under test has the Confluence v1→v2 API migration (commit `34e7108`
      in this repo) — `confluence_get_page`/`create_page`/`update_page` return 410 without it; this
      is unrelated to this environment setup, but worth ruling out first
- [ ] **For `scripts/qa_fixture_recorder.py`** (the local fixture recorder — see
      [`external-api-contract-testing.md`](external-api-contract-testing.md)), as opposed to the
      manual `connector-qa-testing.md` process above: create one dedicated seed page in `PFQA`,
      with synthetic content, tagged so the recorder can find it and confirm it's the right one
      before recording anything from it:
      ```
      Title: PrivacyFence QA seed page [QATEST]
      Body:  Synthetic PrivacyFence QA test page. No real information. Safe to read, comment on,
             or edit by any automated test.
      ```
  - [ ] Fill in its page ID (or leave blank to resolve by title — one extra API call per run)
        under `confluence.seed_page_id` in `tests/fixtures/qa_environment.yaml`
  - Note: this is a narrower requirement than the steps above — the recorder only ever reads this
    one page, by ID/title and the `[QATEST]` tag, never "any page in `PFQA`" — see
    `external-api-contract-testing.md`'s "Guardrail against recording the wrong thing" for why.
    Only Confluence has recorder support today (`scripts/qa_fixture_recorder.py`'s
    `CONNECTOR_CHECKS`); other connectors will need the same kind of tagged seed artifact once
    they're wired in — this section is the pattern to follow when that happens, not a one-off.

## 11. PII Detection Gate

- [ ] Confirm **PII Detection Gate** is enabled in the PrivacyFence menu bar (default on —
      equivalently, `pii_detection.enabled` is `true` or absent in `settings.yaml`)

> **Note** — no fixture to create here. This is the one part of the QA test that's genuinely
> self-contained: the dedicated check in `connector-qa-testing.md` (Phase 2, steps 17–20) creates
> its own throwaway Drive subfolder and Google Doc seeded with synthetic, obviously-fake PII, writes
> it, reads it back, and tears both down in Phase 11 — nothing here to provision ahead of time.
>
> The gate only ever runs on the `review` (read) direction, never on `popup` (write) — see the
> Technical Reference's "PII detection gate" section — so the write (step 18) and read (step 19)
> are expected to produce *different* results even with the same synthetic-PII body: the write
> always stays plain, the read gets flagged (when the gate is enabled).
>
> If you've turned the gate off, step 19 now also produces the *disabled* result (no tint, no
> second confirmation, `pii_detected: false`) — the same result step 18 always produces regardless
> of the toggle, since writes are never scanned in either state. Expected for the disabled case, not
> a failure — but the test prompt needs to know which state it's in rather than assume enabled.
>
> The check deliberately writes to a **subfolder** of the Drive QA Sandbox folder, not the Sandbox
> folder's own top level: the `drive.folders` grant (or the legacy `approved_folder` rule) from §2
> matches a file's *immediate* parent folder ID only, not folders nested inside it, so the read step
> is guaranteed to hit the normal `review` gate (and the PII gate on top of it) instead of being
> silently auto-accepted.
>
> A second, related check (`connector-qa-testing.md` steps 21–23) proves the stronger claim that PII
> detection *overrides* a matching auto-accept rule, rather than just running independently of one —
> it writes the same synthetic PII directly into `drive_qa_folder_id` itself, the folder §2's
> grant/rule *does* cover. This one only exercises the override if you actually configured
> `drive.folders` (or the legacy rule); without either, there's no rule in play to override, and the
> test prompt says so plainly rather than claim the override was proven. The write step there (step
> 21) again stays plain regardless — only the read (step 22) can exercise the override, since only
> reads are ever scanned.

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

## Consolidated `auto_accept_grants` / `auto_accept_rules` blocks

- [ ] Merge both into `settings.yaml` (they're separate top-level keys, both shown here), replacing
      every `<placeholder>` with your actual value. Most per-connector fixtures are grant-managed
      as of [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants) — the four that aren't
      (sender-domain trust, calendar organizer, contact-edit scope, Salesforce object type) stay
      under `auto_accept_rules`, same as before:
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
            - id: "<default task list id>"
              name: "My Tasks"
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
- [ ] Restart the daemon after editing this file by hand (or use the "Accept All" popup once, which
      hot-reloads rules for you via `reload_rules()`)

> **Note** — the menu bar builds/edits exactly this `auto_accept_grants` shape from **Auto-accept
> Rules → \<Connector\> → Trusted \<Resource\>** — editing it by hand here and editing it from the
> menu are equivalent; use whichever's more convenient per fixture.
>
> `sheets.read_values` → `approved_spreadsheet` (also grant-managed, under `drive.spreadsheets`)
> isn't included here because it gets created automatically the first time you click **"Accept
> All"** on a `drive_sheets_get_values` call during a test run — nothing to pre-configure.
>
> `sheets.rename_sheet` / `sheets.format_range` / `sheets.insert_dimensions` /
> `sheets.delete_dimensions` / `docs.edit_content` / `docs.format_content` → `approved_sandbox_folder`
> (§2) — also grant-managed as part of `drive.sandbox_folders`'s `write` capability, same as
> `drive.write_file`/`drive.write_doc`/`sheets.write_range`/`sheets.add_sheet` — is deliberately
> left out of this block's `drive.sandbox_folders` entry, since enabling `write` there would make
> Phase 2 silently auto-accept all ten of these operations at once instead of exercising their
> popup / "Accept for 5 min" flow. `calendar.read_event_details`/`calendar.set_visibility` →
> `non_private_event` (§4) and `salesforce.search` → `approved_object_types` (§8) are left out for
> the same reason (each has no grant form). Add whichever of these you specifically want Phase 2 to
> exercise as silent auto-accept instead, individually.

---

## Fixture reference

What [`connector-qa-testing.md`](connector-qa-testing.md)'s Phase 0 looks up, and how it finds each
one. Use this table to sanity-check your setup before a run, or to debug a fixture Phase 0 reports
as missing. For the Telegram and Tasks rows, `scripts/qa_list_ids.py` prints the same IDs Phase 0
would find, headlessly, without needing a live Claude session first.

| Fixture | How it's found | Source |
|---|---|---|
| Gmail seed thread | Subject tag `[QATEST]` (optional — see §1) | `gmail_list_messages` |
| Drive QA folder | Exact name `PrivacyFence QA Sandbox` | `drive_list_files` |
| Slack approved channel | `auto_accept_grants.slack.channels` (`read: true`), or the legacy `slack.read_messages` → `approved_channel` rule | `settings.yaml` |
| Slack control channel | Exact name `privacyfence-qa-control` | `slack_list_channels` |
| Telegram Saved Messages | `is_self: true` flag | `telegram_list_chats` |
| Telegram approved chat | `auto_accept_grants.telegram.chats` (`read: true`), or the legacy `telegram.read_chat_messages` → `approved_chats` rule (falls back to Saved Messages) | `settings.yaml` |
| Telegram control chat | Any chat that isn't the above two | `telegram_list_chats` |
| Salesforce QA report | `auto_accept_grants.salesforce.reports` (`run: true`), or the legacy `salesforce.run_report` → `approved_report_ids` rule (falls back to exact name `PrivacyFence QA Report`) | `settings.yaml` / `salesforce_list_reports` |
| Salesforce QA object type | `salesforce.read_record` → `approved_object_types` (falls back to `Account`) | `settings.yaml` |
| Jira QA project | Literal key `PFQA` | — |
| Jira contrast project | Any project key that isn't `PFQA` | `jira_list_projects` |
| Confluence QA space | Literal key `PFQA` | — |
| Confluence contrast space | Any space key that isn't `PFQA` | `confluence_list_spaces` |
| Confluence recorder seed page | Title tag `[QATEST]` in `PFQA` (only used by `scripts/qa_fixture_recorder.py`) | `tests/fixtures/qa_environment.yaml` |
| Tasks approved list | `auto_accept_grants.tasks.task_lists` (`edit: true`), or the legacy `tasks.update_task` → `approved_task_list` rule (falls back to the default list) | `settings.yaml` / `tasks_list_task_lists` |
| Tasks contrast list | Exact name `PrivacyFence QA Contrast List` | `tasks_list_task_lists` |

---

## Idempotency: environment fixtures vs. per-run artifacts

Two different lifetimes are at play:

- **Environment fixtures** (this doc): the `PFQA` Jira project/Confluence space, the Drive Sandbox
  folder, the Slack channels, the Telegram chats, the Salesforce sample records/report. Created
  **once** by working through this guide; re-discovered by every subsequent test run, never
  recreated and never pasted in by hand.
- **Per-run artifacts** (the test prompt itself): drafts, events, one-off issues/pages/files. These
  carry a run-scoped identifier so repeated runs don't produce indistinguishable duplicates — see
  [`connector-qa-testing.md`](connector-qa-testing.md), which stamps every title with a timestamp
  and ends with a teardown phase.

If you deliberately skip a fixture (e.g. no Workspace admin access for Calendar rooms), that's a
permanent, known gap — the test prompt already treats it as expected rather than re-discovering and
re-reporting it every run, as long as you leave the corresponding config/rule unset rather than
half-configuring it.
