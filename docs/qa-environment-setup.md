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
3. You can edit `settings.yaml` directly (it's a local file, not managed
   through the menu bar UI) — at `~/.privacyfence/config/settings.yaml` for a
   bundled install, or `config/settings.yaml` in the repo root if running
   from source (see [dev-vs-live-setup.md](dev-vs-live-setup.md)) — and
   either restart the daemon afterward, or trigger a hot-reload by using the
   approval popup's "Accept All" button once, which calls `reload_rules()`
   for you. The rest of this doc just says "`settings.yaml`" — use whichever
   of the two paths applies to your setup.
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
2. In `settings.yaml`:
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
   `drive.download_file` have something to match against — a `drive.folders`
   grant (see [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants))
   with `read: true` covers both, plus `sheets.read_values`:
   ```yaml
   auto_accept_grants:
     drive:
       folders:
         - id: "<QA Sandbox folder id>"
           name: "PrivacyFence QA Sandbox"
           read: true
   ```
   Equivalently, from the menu bar: **Auto-accept Rules → Drive → Trusted
   Folders → + Add folder…**, then check **Read auto-accept**. The older,
   still-supported form is a raw `approved_folder` rule under
   `auto_accept_rules` — see [Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules).
3. Every Drive/Sheets artifact the test prompt creates goes **inside this
   folder** (as `parent_folder_id`), including uploads, Docs, and moves — so
   nothing it does ever touches a real file or a real folder elsewhere in
   your Drive.
4. Drive has no bulk-empty-trash tool, so periodically empty Drive trash by
   hand if you want a clean slate; trashed items auto-purge after 30 days
   regardless.
5. **Optional** — only add this if you specifically want
   `connector-qa-testing.md`'s Phase 2 to also demonstrate that
   `sheets.rename_sheet` / `sheets.format_range` can auto-accept by folder
   (fixed after a real report that these two had no folder-scoped rule at
   all, unlike `sheets.write_range`/`sheets.add_sheet`). Skip this if you
   want Phase 2's rename/format steps to keep exercising the plain popup and
   "Accept for 5 min" flow instead — the two are mutually exclusive for the
   same spreadsheet, since a matching rule auto-accepts before any popup
   would appear.

   **Note:** a `drive.sandbox_folders` grant's `write` capability (§2 step 2's
   sibling — see the consolidated block below) auto-accepts **every** Drive/
   Sheets/Docs write operation for that folder at once — `drive.write_file`,
   `drive.write_doc`, all six `sheets.*` writes (including
   `rename_sheet`/`format_range` and, per step 6, `insert_dimensions`/
   `delete_dimensions`), and `docs.edit_content`/`docs.format_content` from
   step 7 — that's the point of a grant (one toggle, every operation it
   covers). If you specifically want *only* `sheets.rename_sheet`/
   `format_range` auto-accepted while the rest keep prompting (the narrower
   thing this step used to demonstrate), that per-operation split isn't
   expressible as a grant — use the raw rules directly instead:
   ```yaml
   auto_accept_rules:
     sheets.rename_sheet:
       - rule: approved_sandbox_folder
         value: ["<QA Sandbox folder id>"]
     sheets.format_range:
       - rule: approved_sandbox_folder
         value: ["<QA Sandbox folder id>"]
   ```
6. **Optional**, same reasoning as step 5 — `sheets.insert_dimensions` and
   `sheets.delete_dimensions` (row/column insert and delete) also accept
   `approved_sandbox_folder`. Note the asymmetry this is meant to
   demonstrate: `sheets.insert_dimensions` additionally offers "Accept for 5
   min" on its plain popup (non-destructive, like `format_range`), while
   `sheets.delete_dimensions` never does (removes cell content, no undo path
   through PrivacyFence) — configuring both the same way here still leaves
   that difference visible whenever this rule *doesn't* match (e.g. against
   a spreadsheet outside the Sandbox folder):
   ```yaml
   auto_accept_rules:
     sheets.insert_dimensions:
       - rule: approved_sandbox_folder
         value: ["<QA Sandbox folder id>"]
     sheets.delete_dimensions:
       - rule: approved_sandbox_folder
         value: ["<QA Sandbox folder id>"]
   ```
7. **Optional** — `docs.edit_content` and `docs.format_content`
   (`drive_docs_edit_content`/`drive_docs_format_content`) accept the same
   rules `drive.write_doc` does. Both are also "Accept for 5 min"-eligible on
   their plain popup, so configuring this rule mainly matters if you want
   Phase 2 to demonstrate the standing-rule path instead of the temp-accept
   one for a Doc inside the Sandbox folder:
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

Two channels are needed: one the test prompt is allowed to read silently,
and one it isn't (to prove the review gate still fires for anything not on
the allowlist).

1. Pick (or create) a channel to be the **approved** one and join it. Add its
   channel ID to `settings.yaml` as a `slack.channels` grant with `read: true`
   (see [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants)):
   ```yaml
   auto_accept_grants:
     slack:
       channels:
         - id: "<channel id>"
           read: true
   ```
   Equivalently, from the menu bar: **Auto-accept Rules → Slack → Trusted
   Channels → + Add channel…** — this connector supports the live picker (by
   channel name), so you don't need to look up the ID by hand at all. The
   test prompt itself still reads the ID straight out of the config — there's
   no naming requirement on this channel. The older, still-supported form is
   a raw `approved_channel` rule under `auto_accept_rules`.
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

**Optional, for `non_private_event`:** add the rule below if you want Phase 2
to demonstrate `calendar_get_event_details` and `calendar_set_event_visibility`
auto-accepting for a non-private event. No fixture is needed for the contrast
case either way — any event the test prompt sets to `private` (via
`calendar_set_event_visibility`) still prompts regardless of this rule, since
`non_private_event` checks the visibility being requested for that specific
tool, not the event's prior state:
```yaml
auto_accept_rules:
  calendar.read_event_details:
    - rule: non_private_event
  calendar.set_visibility:
    - rule: non_private_event
```
(Combine with `i_am_organizer` above under `calendar.read_event_details` if
you want both — a matching rule short-circuits the list, so order doesn't
change what auto-accepts, just which rule name shows up in the audit log.)

No fixture is needed for `calendar_create_out_of_office` or
`calendar_set_working_location` either: both always operate on the
authenticated user's own primary calendar (a Google Calendar API restriction,
not something local config can change), so any account works out of the box.
Be aware that repeated QA runs each leave behind their own out-of-office event
and overwrite the working-location entry for whatever date was used — there's
no delete tool for either, so they accumulate in "needs manual deletion" across
runs the same way plain Calendar events do (see Phase 11 in
[`connector-qa-testing.md`](connector-qa-testing.md)).

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
   **approved** one — get its ID from `tasks_list_task_lists` (via a live
   Claude session) or headlessly with `scripts/qa_list_ids.py tasks`, and add
   it to `settings.yaml` as a `tasks.task_lists` grant (see
   [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants)) with
   `edit`, `complete`, and (optionally, see step 3) `create`/`move`:
   ```yaml
   auto_accept_grants:
     tasks:
       task_lists:
         - id: "<default list id>"
           name: "My Tasks"
           edit: true
           complete: true
   ```
   Equivalently, from the menu bar: **Auto-accept Rules → Tasks → Trusted
   Task Lists → + Add task list…** (live picker by list name), then check
   **Auto-accept edits** and **Auto-accept complete/uncomplete**. The
   `complete` capability covers both `tasks.complete_task` and
   `tasks.uncomplete_task` at once — that's one difference from the older,
   still-supported `auto_accept_rules` form, which configured them
   separately (`create`/`move` are left off here deliberately — see step 3).
2. In Google Tasks (web or mobile), create a second list named **exactly
   `PrivacyFence QA Contrast List`**. There's no tool to create a task list
   through PrivacyFence itself, so this has to be done outside it. **Do not**
   grant it any capability — it exists specifically to *not* match, and the
   test prompt finds it by this exact name via `tasks_list_task_lists`.
3. Optional: add `create: true` to the same grant entry from step 1, and/or
   `move: true` to **both** the default list's grant entry and a new grant
   entry for the contrast list (a move only auto-accepts when both the
   source and destination list have `move: true` set). Skip any capability
   you'd rather leave always-prompting — the test prompt handles "not
   configured" gracefully for each one independently.

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
     `telegram_list_chats` after step 1, or headlessly with
     `scripts/qa_list_ids.py telegram` — look for `is_self=True`) — safe,
     since it's always fine to auto-accept reads of your own messages to
     yourself, or
   - Create/repurpose a second low-stakes chat (a private group with just
     you, or a throwaway test contact) and use its `chat_id` instead.
   ```yaml
   auto_accept_grants:
     telegram:
       chats:
         - id: "<chat_id>"
           read: true
   ```
   Equivalently, from the menu bar: **Auto-accept Rules → Telegram → Trusted
   Chats → + Add chat…** (live picker by chat name). The test prompt reads
   this ID from `settings.yaml` directly either way — no naming requirement
   on the chat. Skip this grant entirely if you'd rather leave Telegram reads
   always review-gated; the test prompt handles "not configured" gracefully.
   The older, still-supported form is a raw `approved_chats` rule under
   `auto_accept_rules`.
3. Make sure at least one *other* chat beyond your approved one has some
   message history — the test prompt picks any such chat dynamically for
   the "not approved, should still prompt" contrast case and for
   `telegram_search_messages` to have something to find.

Whether a native approval popup actually appears for `telegram_get_messages` /
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
3. Add auto-accept fixtures (recommended — this is also how you get rule
   coverage for both the report and the object type). The report ID is
   grant-managed (see
   [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants) →
   `salesforce.reports`); the object type is a small fixed vocabulary, not a
   resource identity, so it stays a plain `auto_accept_rules` entry:
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
   Equivalently, from the menu bar: **Auto-accept Rules → Salesforce →
   Trusted Reports → + Add report…** (live picker by report name), then
   check **Read auto-accept**. If you skip the report grant, the test prompt
   falls back to finding the report by its exact name via
   `salesforce_list_reports` — either path works, the name is just the
   fallback when there's no grant/rule to read the ID from.
4. Keep at least one report/object type you *don't* add here (or that you
   genuinely can't access) as the "should still prompt" contrast case — any
   report other than the QA one satisfies this, nothing extra to create.
5. `salesforce_search` needs no separate fixture — the sample Account records
   from step 1 (prefixed `PrivacyFence QA — `) are exactly what a search for
   e.g. `PrivacyFence QA` should find. **Known quirk, not a bug:** Salesforce's
   SOSL search index can take a minute or two to pick up freshly created
   records — if a search run immediately after step 1 comes back empty, wait
   briefly and retry before treating it as a regression.
6. **Optional** — `salesforce.search` also accepts `approved_object_types`,
   the same rule `salesforce.read_record` uses, generalized to check *every*
   object type in the search's comma-separated `object_types` (not just one):
   ```yaml
   auto_accept_rules:
     salesforce.search:
       - rule: approved_object_types
         value: [Account]
   ```
   With this configured, a search scoped to `object_types="Account"` (or left
   matching the allowlist exactly) auto-accepts; a search that also touches
   any other object type, or one left unscoped entirely (empty
   `object_types`, Salesforce's default globally-searchable set), still
   prompts — that asymmetry is itself worth confirming in Phase 2, not just
   the auto-accept path.

## 9. Jira

1. Create a Jira project with key **exactly `PFQA`**, any template (Kanban is
   fine). The key has to match exactly — the test prompt uses it as a
   literal string, not a fuzzy lookup.
2. Add the fixture as a `jira.projects` grant (see
   [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants)) with
   `read: true`:
   ```yaml
   auto_accept_grants:
     jira:
       projects:
         - key: PFQA
           name: "PrivacyFence QA"
           read: true
   ```
   Equivalently, from the menu bar: **Auto-accept Rules → Jira → Trusted
   Projects → + Add project…** (live picker by project name), then check
   **Read auto-accept**. The older, still-supported form is a raw
   `approved_project_keys` rule under `auto_accept_rules`.
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
5. `jira_get_transitions` / `jira_transition_issue` need no setup — every Jira
   project ships with a default workflow that has at least one transition
   reachable from a new issue's initial status (e.g. "To Do" → "In Progress"),
   so the QA project's default workflow is enough.
6. `jira_update_issue`'s `custom_fields` test is opportunistic, not a required
   fixture: it needs at least one custom field on `PFQA`'s issue screen (check
   an issue's "..." menu or **Project settings → Fields** in Jira's web UI). If
   `PFQA` doesn't have one and you want this exercised, add any custom field
   (e.g. a number field called "Story Points") to the project's issue screen;
   otherwise the test prompt skips that step as a fixture-availability
   limitation, not a regression.

## 10. Confluence

1. Create a Confluence space with key **exactly `PFQA`**.
2. Add the fixture as a `confluence.spaces` grant (see
   [Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants)) with
   `read: true`:
   ```yaml
   auto_accept_grants:
     confluence:
       spaces:
         - key: PFQA
           name: "PrivacyFence QA"
           read: true
   ```
   Equivalently, from the menu bar: **Auto-accept Rules → Confluence →
   Trusted Spaces → + Add space…** (live picker by space name), then check
   **Read auto-accept**. The older, still-supported form is a raw
   `approved_space_keys` rule under `auto_accept_rules`.
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
synthetic, obviously-fake PII, writes it, reads it back, and tears both down
in Phase 11 — nothing here to provision ahead of time, and nothing that
persists between runs.

The gate only ever runs on the `review` (read) direction, never on `popup`
(write) — see the Technical Reference's "PII detection gate" section — so the write (step 18)
and the read (step 19) are expected to produce *different* results even
though they carry the same synthetic-PII body: the write always stays plain,
the read gets flagged (when the gate is enabled; see point 1 below).

The only thing worth confirming beforehand:

1. **PII Detection Gate** is enabled in the PrivacyFence menu bar (it is by
   default — equivalently, `pii_detection.enabled` is `true` or absent in
   `settings.yaml`). If you've turned it off, the dedicated check still runs
   but the read step (19) now also produces the *disabled* result (no tint,
   no second confirmation, `pii_detected: false` in the audit log) — the same
   result the write step (18) always produces regardless of the toggle,
   since writes are never scanned in either state. That's expected behavior
   for the disabled case, not a failure, but the test prompt needs to know
   which state it's in rather than assume enabled.
2. The check deliberately writes to a **subfolder** of the Drive QA Sandbox
   folder, not the Sandbox folder's own top level. The `drive.folders` grant
   (or the legacy `approved_folder` rule) from §2 above matches a file's
   *immediate* parent folder ID only, not folders nested inside it — so even
   if you configured it, it does not cover the subfolder, and the read step
   is guaranteed to hit the normal `review` gate (and the PII gate layered on
   top of it) instead of being silently auto-accepted. No action needed here
   beyond knowing why the check is structured that way, in case you ever
   restructure the Sandbox folder yourself.
3. A second, related check (`connector-qa-testing.md` steps 21–23) proves
   the stronger claim that PII detection *overrides* a matching auto-accept
   rule, rather than just running independently of one — it deliberately
   writes the same synthetic PII directly into `drive_qa_folder_id` itself,
   the folder the §2 grant/rule *does* cover. This one only exercises the
   override if you actually configured `drive.folders`
   (`auto_accept_grants`) or the legacy `drive.read_file_contents` →
   `approved_folder` rule from §2; without either, there's no rule in play
   to override, and the test prompt is told to say so plainly rather than
   claim the override was proven. The write step in that check (step 21)
   again stays plain regardless — only the read (step 22) can exercise the
   override, since only reads are ever scanned.

## 12. Scheduled / unattended Cowork tasks

No fixture to create — Phase 11 of `connector-qa-testing.md` reuses the Slack channels from §3
above (an approved one, a control one) rather than needing anything new. The only environment
state this check touches is `unattended_sessions.enabled` in `settings.yaml`, which is off by
default and toggled (with a daemon restart) as part of the phase itself, not something to
pre-configure here. See
[`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks) for what this
mode does and why.

The one thing worth confirming beforehand: know how to restart your daemon (`privacyfence-app`, or
`scripts/dev_start.sh` if running from source — see [dev-vs-live-setup.md](dev-vs-live-setup.md)).
Unlike `pii_detection.enabled`, `unattended_sessions.enabled` has no menu-bar toggle and isn't
hot-reloaded — the phase requires an actual restart partway through, twice.

---

## Consolidated `auto_accept_grants` / `auto_accept_rules` blocks

Everything from the sections above, in one place. Merge both into
`settings.yaml` (they're separate top-level keys, both shown here),
replacing every `<placeholder>` with your actual value. Most of the
per-connector fixtures are grant-managed as of
[Auto-accept grants](TECHNICAL_REFERENCE.md#auto-accept-grants) — the four
that aren't (sender-domain trust, calendar organizer, contact-edit scope,
Salesforce object type) stay under `auto_accept_rules`, same as before:

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

The menu bar builds/edits exactly this `auto_accept_grants` shape from
**Auto-accept Rules → \<Connector\> → Trusted \<Resource\>** — editing it by
hand here and editing it from the menu are equivalent; use whichever's more
convenient per fixture.

`sheets.read_values` → `approved_spreadsheet` (also grant-managed, under
`drive.spreadsheets`) isn't included here because it gets created
automatically the first time you click **"Accept All"** on a
`drive_sheets_get_values` call during a test run — nothing to pre-configure.

`sheets.rename_sheet` / `sheets.format_range` / `sheets.insert_dimensions` /
`sheets.delete_dimensions` / `docs.edit_content` / `docs.format_content` → `approved_sandbox_folder`
(§2, steps 5–7) — also grant-managed as part of `drive.sandbox_folders`' `write` capability, same as
`drive.write_file`/`drive.write_doc`/`sheets.write_range`/`sheets.add_sheet` — is deliberately left
out of this block's `drive.sandbox_folders` entry, since enabling `write` there would make Phase 2
silently auto-accept all ten of these operations at once instead of exercising their popup /
"Accept for 5 min" flow. `calendar.read_event_details` / `calendar.set_visibility` →
`non_private_event` (§4) and `salesforce.search` → `approved_object_types` (§8, step 6) are left out
for the same reason (each has no grant form — see the note on those sections). Add whichever of
these you specifically want Phase 2 to exercise as silent auto-accept instead, individually.

Restart the daemon after editing this file by hand (or use the "Accept All"
popup once, which hot-reloads rules for you via `reload_rules()`).

---

## Fixture reference

What [`connector-qa-testing.md`](connector-qa-testing.md)'s Phase 0 looks up,
and how it finds each one. Use this table to sanity-check your setup before
a run, or to debug a fixture Phase 0 reports as missing. For the Telegram and
Tasks rows, `scripts/qa_list_ids.py` prints the same IDs Phase 0 would find,
headlessly, without needing a live Claude session first.

| Fixture | How it's found | Source |
|---|---|---|
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
| Tasks approved list | `auto_accept_grants.tasks.task_lists` (`edit: true`), or the legacy `tasks.update_task` → `approved_task_list` rule (falls back to the default list) | `settings.yaml` / `tasks_list_task_lists` |
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
