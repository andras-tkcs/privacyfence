# Full-Connector QA Testing via Claude Cowork

**This guide assumes PrivacyFence is running from source via `scripts/dev_start.sh`**
(Account 1 in [dev-vs-live-setup.md](dev-vs-live-setup.md)), started from a
checkout of this repo. In that mode `config/settings.yaml` and
`logs/audit/<week>.jsonl` live **inside the repo root**, not `~/.privacyfence`
(see [`paths.py`](../src/privacyfence/paths.py)) — every path below is written
against the repo root on that basis, not hedged as "check which one exists."
If you're instead testing a bundled/DMG install, the same prompt works, but
substitute `~/.privacyfence/config/settings.yaml` and
`~/.privacyfence/logs/audit/<week>.jsonl` everywhere a path is mentioned.

PrivacyFence's real attack surface is the interaction between ten connectors,
three gate types (`auto` / `review` / `popup`), a growing set of auto-accept
rules, and the PII detection gate layered on top of the `review` (read)
dialog specifically — none of which unit tests exercise end to end, since
they mock the gate itself.
The fastest way to catch drift between what the code does and what a user
actually experiences is to drive every tool through a live Claude
Cowork/Desktop session connected to the real `privacyfence` daemon, against
real accounts, and watch what actually prompts.

This method found four real bugs and two stale-documentation sections in a
single pass (see [Example findings](#example-findings-from-the-2026-07-run)
below) that no unit test had caught, because unit tests mock `GmailClient`,
`ConfluenceClient`, etc. — they can't detect that a connector is calling a
REST endpoint the provider removed, or that a specific header-encoding
combination only breaks for non-ASCII input.

## When to use this

- Before a release, or after touching gate/auto-accept logic broadly.
- After any change to a connector client (`*_client.py`) that talks to a real
  external API — unit tests mock the client, so they won't notice if the
  provider's API itself has moved out from under it.
- Whenever you want to sanity-check that the *documented* gate for a tool
  (the Technical Reference's connector tables) still matches its *actual* gate in source.
- After any change to `privacyfence_check_policy` or unattended-session mode (Phase 11) — these
  bypass connector clients entirely, so a connector-client mock can't catch a regression there
  either; only a live daemon actually proves a call denies without ever opening a popup.

## Prerequisites

- `privacyfence-app` (the daemon) running, with every connector you want to
  test already authenticated from the menu bar.
- The `privacyfence` MCP server attached to a Claude Cowork/Desktop
  conversation — `scripts/dev_start.sh` registers `privacyfence-bridge` from
  this checkout's venv for you.
- **Claude Cowork's project/working folder (set in Cowork's UI — the folder
  picker for the conversation) must be this repo's root**, the same folder
  `dev_start.sh` was run from. This is what gives Claude filesystem access
  (Read/Bash-equivalent) to `config/settings.yaml` for Phase 0's fixture
  lookups — without it, Claude has no way to read that file, and Phase 0 will
  silently fail or come back empty rather than erroring loudly. Confirm
  before you start by asking Claude to read `config/settings.yaml` — if it
  can't find the file, fix the project folder before pasting the prompt
  below.
- No filesystem access to the audit log is needed during the run itself — a
  live mid-run read from a Cowork/Desktop session isn't reliable (recently
  written entries can appear to lag or go missing depending on how the
  session reaches the file), so the prompt below no longer has Claude read
  `logs/audit/<current-ISO-week>.jsonl` as it goes, even with the project
  folder set correctly. Instead, the very last phase asks you to **attach
  that file to the conversation** — `logs/audit/` in the repo root per the
  assumption at the top of this doc, or `~/.privacyfence/audit/` if you're
  testing a bundled/DMG install instead — and Claude reconciles every call
  against that attached copy in one pass at the end, instead of piecemeal
  during the run. This is a test environment against your own accounts, so
  there's no confidentiality reason to keep the log human-only. Claude still
  can't observe the popup UI directly (it only sees whether the tool call
  ultimately succeeded or errored) — the audit log's `decision` field is what
  closes that gap, since it records `accepted` / `denied` / `auto_accepted`
  regardless of whether Claude witnessed the click.
- **The environment fixtures from [`qa-environment-setup.md`](qa-environment-setup.md)
  already exist**: a `PFQA` Jira project, a `PFQA` Confluence space, a Drive
  "PrivacyFence QA Sandbox" folder, a second (non-approved) Slack channel with
  a thread in it, a Telegram "Saved Messages" chat plus one approved chat, and
  Salesforce sample records/report. Without these, several phases below fall
  back to being untestable — work through that doc once per environment; it's
  a standalone installation guide, not something you redo per run.
- The PII detection gate check (Phase 2, steps 17–20) needs **no environment
  fixture** — it's self-contained, creating and tearing down its own
  throwaway Drive subfolder and Doc. Confirm **PII Detection Gate** is
  enabled in the menu bar (it is by default; see
  [`qa-environment-setup.md`](qa-environment-setup.md#11-pii-detection-gate))
  so the check exercises the tinted-popup/confirmation path rather than the
  (equally valid, but different) disabled path.

## The prompt

Paste this as a single message into the Cowork conversation. It walks every
connector in dependency order (list/search before get, get before write),
deliberately hits every auto-accept rule you have configured (see the
environment doc's consolidated rules block) back-to-back with a contrasting
call that should still prompt, and ends with a dedicated reconciliation phase:
Claude asks you to attach the current week's audit log to the conversation
(repo root, per the assumption at the top of this doc, or `~/.privacyfence/`
for a bundled/DMG install), then goes back through every call it made and
fills in the actual logged decision for each one, instead of reading the file
live during the run.

**This version is safe to run repeatedly against the same accounts, and needs
no editing before you paste it.** Every artifact it creates is stamped with a
run ID, so re-running it never collides with (or gets confused by) what a
previous run left behind, and it ends with a teardown phase that cleans up
everything it can. Phase 0 has Claude look up every fixture itself — by exact
name (the Drive Sandbox folder, the Slack control channel, the Salesforce
report), by literal key (the `PFQA` Jira project/Confluence space), by a flag
the connector already exposes (Telegram's `is_self` for Saved Messages), or
straight out of `settings.yaml`'s own `auto_accept_rules` — instead of you
pasting IDs into a `<QA_FIXTURES>` block by hand. That only works if your
environment setup used the exact names [`qa-environment-setup.md`](qa-environment-setup.md)
specifies; if a fixture doesn't resolve, Phase 0 reports exactly which one is
missing and skips only the steps that need it.

````markdown
You are connected to my personal accounts (Gmail, Drive/Sheets, Slack, Calendar,
Contacts, Tasks, Telegram, Salesforce, Jira, Confluence) through the `privacyfence`
MCP server. I'm QA-testing PrivacyFence itself, not asking you to do real work — so
go connector by connector, in the order below, and actually call the tools rather
than describing what you'd do.

Ground rules:
- Any content you write, send, or create anywhere must be obviously a test artifact:
  prefix titles/subjects/messages with `PrivacyFence QA test [{RUN_ID}] —` and add
  "safe to ignore/delete" somewhere in the body. Never edit or send anything real.
- Everything durable this run creates (drafts, files, events, issues, pages,
  contacts, tasks…) must go in the running manifest table described below, tagged
  with `{RUN_ID}` — Phase 12 (teardown) depends on that manifest to find and remove
  them, and future runs depend on it to tell "this run's" artifacts apart from a
  previous run's leftovers.
- Prefer QA-owned destinations over guessing at real ones: the Drive "PrivacyFence
  QA Sandbox" folder, the `PFQA` Jira project, the `PFQA` Confluence space, the
  non-approved Slack channel, Telegram "Saved Messages" / the approved chat. If a
  step needs a destination Phase 0 couldn't resolve, stop and ask me — don't
  guess, and don't fall back to a real project/space/channel.
- When a step says "pick any existing X," prefer the most recently created
  `PrivacyFence QA test [...]`-tagged item over real data, and ignore items tagged
  with a *different* `{RUN_ID}` (previous runs' leftovers) — call them out in the
  final report instead of touching or recounting them.
- After each numbered phase, pause and give me a one-line status ("Phase 3 done,
  4 tool calls, 2 required approval") before moving to the next phase, so I'm not
  flooded with 40 popups back to back.
- **Whenever a step expects something other than a plain Accept — Deny, "Show
  Details," "Accept All," or "Accept for 5 min" — send that instruction as its own message and stop.
  Don't make the tool call in the same turn.** Wait for me to reply (e.g. "go" or
  "ready") before calling the tool. The native approval popup can appear on top
  of this chat window the instant the tool call fires, so if the instruction and
  the call land in the same turn I may only see the popup, not what I was
  supposed to do with it, and default to clicking Accept out of habit. A step
  marked "pause here" below means: stop, wait for my go-ahead, then call it.
- **The PII detection gate can fire on any `review` (read) dialog, in any
  phase, not just the dedicated steps in Phase 2** — it scans whatever
  content a read popup is about to show (message body, file content, page
  body...) for likely personal data in Hungarian/English/German, and real
  accounts routinely contain real emails, phone numbers, etc. It never fires
  on a native write popup (drafting an email, posting to Slack, writing a
  file, etc.) — that content is something Claude itself just generated, not
  external data newly reaching Claude, so there's nothing for this gate to
  check on the write side. If a *read* popup renders tinted red with a
  category banner: Accept it as the step normally instructs, then a second
  **"Are you sure?"** dialog appears — click **Proceed** to continue as
  planned (or **Cancel** only if the step's *own* instruction was to Deny).
  Note "PII gate fired" plus the categories shown in that step's row of the
  running table; this is expected behavior on real data, not a bug, unless a
  step explicitly says otherwise.
  **This includes read steps marked "should NOT prompt" for an auto-accept
  rule** (e.g. Phase 1 step 6, Phase 4 step 5): the PII scan runs before the
  auto-accept check and overrides a matching rule, so a message/event/page
  whose *content* contains something PII-shaped will still show the tinted
  popup even though the rule matched — that's the gate doing its job, not a
  rule-evaluation bug. Only flag it as a bug if the read popup appears with no
  PII category banner at all (a plain popup for a step that should've been
  silent is still worth reporting) — and treat any *write* popup rendering
  tinted/PII-flagged as a bug in itself, since that gate should never fire
  there anymore.
- Keep a running table as you go: `tool name | gate observed (silent / native
  popup) | my decision | audit-log decision | notes`. Leave
  the `audit-log decision` column blank for now — don't guess it, don't ask me
  for it mid-run, and don't try to read the log file yourself during the run.
  Phase 13, the very last phase, is where that column gets filled in: I'll
  attach the current week's audit log to the conversation at that point, and
  you'll go back through this table and fill in every row's actual logged
  `accepted` / `denied` / `auto_accepted` value from the attached file, matching
  entries by timestamp and tool/operation name. Print the full table (still
  with the column blank) at the end of each phase as normal; the populated
  version only appears once, in the Phase 13 / final report.
- Keep a second running table, the manifest: `connector | artifact | id | {RUN_ID}
  tag | deletable via tool? (yes/no)`. Print it at the very end too, split into
  "cleaned up in Phase 12" vs. "needs manual deletion."

I (the human) will be watching for the approval prompts as they appear. Most
steps expect a plain Accept and you can just make the call. The ones marked
**"pause here"** expect something else (Deny / Accept All) —
for those, stop and wait for my go-ahead as the ground rules above describe,
*then* call the tool, then report back what actually happened in the tool
result and write it into the table as provisional — Phase 13's audit-log
reconciliation, not this step, is what turns it from "reported" into
"settled."

---

## Phase 0 — Setup
Resolve every fixture yourself before Phase 1 — don't ask me for IDs, look
them up. Build a `{FIXTURES}` table as you go and print it before moving on,
so I can catch a wrong lookup immediately instead of at the end of the run.

1. Generate `{RUN_ID}` yourself right now as `YYYY-MM-DD-HHmm` in my local time.
   Use it verbatim in every title for the rest of this run.
2. Read `settings.yaml` yourself — `config/settings.yaml` in the repo root
   (this run assumes PrivacyFence was started from source via
   `scripts/dev_start.sh`, in the same repo checkout you have filesystem
   access to; see [dev-vs-live-setup.md](dev-vs-live-setup.md)) — and keep the
   full `auto_accept_rules` block in mind for the rest of the run — several
   fixtures below come directly from it rather than a separate lookup.
3. `drive_list_files` (or equivalent search) for a folder named exactly
   `PrivacyFence QA Sandbox` → `drive_qa_folder_id`. If more than one file
   matches, prefer the one whose `mime_type` is a folder.
4. `slack_list_channels` →
   - `slack_approved_channel`: the channel ID(s) already listed under
     `slack.read_messages` → `approved_channel` in the config you just read.
     If that rule isn't configured, tell me and skip Phase 3 step 2.
   - `slack_control_channel`: the channel named exactly
     `privacyfence-qa-control`.
5. `telegram_list_chats` →
   - `telegram_saved_messages_chat_id`: the chat with `is_self: true`.
   - `telegram_approved_chat_id`: the chat ID from `telegram.read_chat_messages`
     → `approved_chats` in settings.yaml, if configured; otherwise the same
     value as `telegram_saved_messages_chat_id`.
   - `telegram_control_chat_id`: any other chat in the list that isn't either
     of the two above, for the "not approved" contrast case.
6. `salesforce_list_reports` and `salesforce.run_report` → `approved_report_ids`
   in settings.yaml →
   - `salesforce_qa_report_id`: the ID from the config rule if set; otherwise
     the report named exactly `PrivacyFence QA Report`.
   - `salesforce_qa_object_type`: the value from `salesforce.read_record` →
     `approved_object_types` in settings.yaml if set; otherwise `Account`.
7. `jira_list_projects` →
   - `jira_qa_project_key`: confirm `PFQA` exists in the list.
   - `jira_contrast_project_key`: any other project key in the list.
8. `confluence_list_spaces` →
   - `confluence_qa_space_key`: confirm `PFQA` exists in the list.
   - `confluence_contrast_space_key`: any other space key in the list.
9. `tasks_list_task_lists` →
   - `tasks_qa_list_id`: the list ID from `tasks.update_task` (or
     `tasks.complete_task`/`tasks.uncomplete_task`) → `approved_task_list` in
     settings.yaml, if configured; otherwise the default list (usually named
     "My Tasks").
   - `tasks_contrast_list_id`: the list named exactly
     `PrivacyFence QA Contrast List`. If it doesn't exist, tell me and skip
     the auto-accept contrast step in Phase 6 — the rest of Phase 6 doesn't
     depend on it.
10. For any fixture you couldn't resolve (missing folder/channel/report, or a
    list that only contains `PFQA` with no contrast candidate), don't invent a
    substitute — tell me which one, point at the relevant section of
    `qa-environment-setup.md`, skip only the steps that depend on it, and keep
    going with the rest.

## Phase 1 — Gmail
1. `gmail_list_messages` and `gmail_list_threads` (expect: silent, no prompt).
2. Pick any recent message — call it the **label-test message**, you'll reuse
   it in steps 9–12 — and call `gmail_get_message` on it. **I will click
   Accept.** Confirm you received the body.
3. Pick a **short** message with no large attachments (this matters — a big one
   makes a Deny indistinguishable from a size-truncation error). **Pause here**:
   tell me you're about to call `gmail_get_message` on it and that **I will
   click Deny this time**, then wait for me to say go. Once I do, make the
   call and confirm you get an error, not data — and don't fabricate a
   fallback answer. Record the raw error in the table and flag this row for
   Phase 13: whether it was actually `denied` versus a size-truncation error
   with a different underlying cause isn't decidable from the tool result
   alone, so don't guess it now.
4. Pick a message that has a thread with 2+ messages. Call `gmail_get_thread`
   on it. **I will click Accept.** Confirm the response includes all messages
   in the thread, not just one.
5. `gmail_list_message_attachments` on any message with attachments (expect:
   silent). Then `gmail_download_attachment` on one — this is `review` gated,
   I'll Accept.
6. Auto-accept rule check: using whatever `gmail.read_message` /
   `trusted_sender_domain` value you read from `settings.yaml` in Phase 0, find
   a message from that domain and call `gmail_get_message` on it. This should
   NOT prompt me at all. Tell me whether a prompt appeared or not. If no such
   rule is configured, skip and say so. `trusted_sender_domain` matches
   subdomains too (e.g. a configured `trusted.com` also matches
   `mail.trusted.com`) — if a message from a subdomain of the configured
   value is available, prefer it for this check so the subdomain behavior
   gets exercised, not just exact-domain matches.
7. `gmail_create_draft` — draft to myself, subject `PrivacyFence QA test [{RUN_ID}]
   — safe to delete`. This is popup-gated, I'll Accept. Add it to the manifest.
8. `gmail_reply_draft` on the thread from step 4, again clearly marked as a test
   with `{RUN_ID}`. Popup, I'll Accept. Add it to the manifest.
9. `gmail_add_label` on the **label-test message from step 2** — use a fresh
   label named exactly `PrivacyFence QA {RUN_ID}` (the tool creates it if it
   doesn't already exist, per `_get_or_create_label` in `gmail_client.py`).
   Popup, Accept.
10. `gmail_archive_message` on the same message. Popup, Accept. Then
    `gmail_list_messages` (silent, no prompt) with query
    `in:inbox label:"PrivacyFence QA {RUN_ID}"` and confirm it comes back
    empty — that's archiving's entire visible effect: `archive_message` in
    `gmail_client.py` does nothing but remove the `INBOX` system label via
    `modify(removeLabelIds=["INBOX"])`.
11. Because archiving is *only* removing the `INBOX` label, un-archiving is
    just adding it back: `gmail_add_label` on the same message again, this
    time with label name `INBOX` (the literal system label, not a new one —
    `_get_or_create_label` resolves it to Gmail's existing `INBOX` label by
    name instead of creating a duplicate). Popup, Accept. Then repeat the same
    `gmail_list_messages` query from step 10 and confirm the message is back.
12. `gmail_remove_label` on the same message, removing the
    `PrivacyFence QA {RUN_ID}` label added in step 9. Popup, Accept. Then
    `gmail_list_messages` (silent) with query `label:"PrivacyFence QA {RUN_ID}"`
    and confirm zero results. After this step the label-test message is back
    exactly where it started — still in the inbox, with no leftover label —
    so there's nothing to add to the manifest or clean up for it in Phase 12.
13. `gmail_list_labels` (expect: silent, no prompt). Confirm the response
    includes both system labels (e.g. `INBOX`) and any user labels, each with
    an `id`/`name`/`type`.
14. `gmail_create_label` with a **nested** name:
    `PrivacyFence QA {RUN_ID}/Nested`. Popup, I'll Accept. Since Gmail has no
    parent-id field, `create_label` in `gmail_client.py` creates the missing
    `PrivacyFence QA {RUN_ID}` parent segment first, then the child — call
    `gmail_list_labels` again (silent) and confirm **both** segments now
    exist as separate labels. Add the parent label name to the manifest
    (no delete tool — see Phase 12).
15. `gmail_create_label` again with the **exact same** nested name from step
    14. This should fail with a "label already exists" error rather than
    prompting a popup — `create_label` raises before ever reaching the gate,
    unlike `gmail_add_label`'s silent get-or-create. Confirm you got an
    error, and that no new popup or audit entry appeared for it.
16. `gmail_list_filters` (expect: silent, no prompt). Note the current count.
17. `gmail_create_filter` — criteria `subject="PrivacyFence QA {RUN_ID}"`,
    action `add_label_names="PrivacyFence QA {RUN_ID}/Nested"` (reusing the
    label from step 14) plus `archive=true`. Popup, I'll Accept. Record the
    returned filter `id` and add it to the manifest (no delete tool — see
    Phase 12).
18. `gmail_list_filters` (silent) and confirm the new filter appears with the
    `id` from step 17 and the criteria/action you set.
19. `gmail_update_filter` on that `id`, changing `archive` to `false` and
    `mark_as_read` to `true` (same `subject`/`add_label_names` as step 17).
    Popup, I'll Accept — the details popup should explicitly say the old
    filter is being deleted and replaced. Record the **new** `id` returned
    (Gmail has no update endpoint, so `update_filter` deletes the old filter
    and creates a fresh one — the id changes). Update the manifest entry from
    step 17 with the new id.
20. `gmail_list_filters` (silent) and confirm the `id` from step 17 is now
    **gone** and the `id` from step 19 is present with the updated action
    (`mark_as_read` instead of `archive`).

## Phase 2 — Drive & Sheets
All of this run's Drive/Sheets artifacts go inside `{FIXTURES}.drive_qa_folder_id`
(pass it as `parent_folder_id` everywhere that accepts one) — that's also what
should trigger the `drive.read_file_contents` / `approved_folder` auto-accept rule
in step 3, if you configured it.
1. `drive_list_files`, `drive_get_file_metadata`, `drive_list_folder`,
   `drive_list_shared_drives` (expect: all silent).
2. `drive_create_blank_file` named `PrivacyFence QA test file [{RUN_ID}] — safe to
   delete`, inside the QA Sandbox folder (expect: silent, auto). Add to manifest.
3. `drive_get_file_content` on that new file. If `drive.read_file_contents` has an
   `approved_folder` rule matching the QA Sandbox folder, this should NOT prompt —
   tell me either way. If no such rule exists, expect the normal review gate,
   Accept.
4. `drive_write_file_content` on it, write a short test sentence — popup, Accept.
5. `drive_add_comment` on it, any test comment. This is one of six write ops
   with a temp-accept shortcut on its popup (the others are steps 13, 16, 24,
   27, and 28 below — see the Technical Reference's
   [Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules)).
   **Pause here**: tell me you're about to call it and that **I will click
   "Accept for 5 min"** this time, then wait for me to say go. Once I do, make
   the call, then immediately call `drive_add_comment` on the same file again
   with a different test comment — this second call should NOT prompt (silent,
   logged `auto_accepted` with rule `session_temp_accept`). Tell me whether the
   second call prompted or not.
6. `drive_upload_file` — upload any small local text file into the QA Sandbox
   folder. Popup, Accept. Add to manifest.
7. `drive_write_doc_content` — create/write a short Google Doc in the QA Sandbox
   folder, title `PrivacyFence QA test doc [{RUN_ID}] — safe to delete`. Popup,
   Accept. Add to manifest.
8. `drive_download_file` on the file from step 2 — popup, Accept. Save it
   somewhere obviously temporary and tell me the path.
9. `drive_move_file` — create one more throwaway blank file, then move it into a
   subfolder of the QA Sandbox folder (create the subfolder first if needed).
   Popup, Accept.
10. `drive_sheets_create` named `PrivacyFence QA test sheet [{RUN_ID}] — safe to
    delete`, inside the QA Sandbox folder (expect: silent, auto). Add to manifest.
11. `drive_sheets_get_metadata` on it (expect: silent).
12. `drive_sheets_get_values` on a small range like `Sheet1!A1:B2` — review gate.
    **Pause here**: tell me you're about to call it and that **I will click
    "Accept All"** this time, then wait for me to say go. Once I do, make the
    call and tell me exactly what rule text/scope it proposes (expect: scoped
    to this spreadsheet + tab, not a broad rule).
13. `drive_sheets_write_range` — write `A1: "hello"`, `A2: "=1+1"` to prove
    formulas evaluate. **Pause here**: tell me you're about to call it and
    that **I will click "Accept for 5 min"** this time, then wait for me to
    say go. Once I do, make the call, then immediately call
    `drive_sheets_write_range` again on the same spreadsheet, a different
    range like `A3: "world"` — this second call should NOT prompt (silent,
    logged `auto_accepted` with rule `session_temp_accept`). Tell me whether
    the second call prompted or not.
14. `drive_sheets_add_sheet` — add a tab named `Extra`. Popup, Accept. Unlike
    step 13, this one has no temp-accept shortcut (it's a one-shot action, not
    something called repeatedly against the same file) — plain Accept only.
15. `drive_sheets_rename_sheet` — rename `Extra` to `TO BE DELETED - Extra`.
    Same as step 14: no temp-accept shortcut here either. If you configured
    the **optional** `sheets.rename_sheet` → `approved_sandbox_folder`
    fixture (`qa-environment-setup.md` §2 step 5) matching the QA Sandbox
    folder, this should NOT prompt — tell me either way. If you didn't
    configure it (the default), expect the normal popup, Accept.
16. `drive_sheets_format_range` — bold `A1:B2`. If you configured the
    **optional** `sheets.format_range` → `approved_sandbox_folder` fixture
    (same place as step 15's), this should NOT prompt — tell me either way,
    and skip the rest of this step. Otherwise (the default), **pause here**:
    tell me you're about to call it and that **I will click "Accept for 5
    min"** this time, then wait for me to say go. Once I do, make the call,
    then immediately call `drive_sheets_format_range` again on the same
    spreadsheet, a different range like `A3:B3` (italic instead of bold) —
    this second call should NOT prompt (silent, logged `auto_accepted` with
    rule `session_temp_accept`). Tell me whether the second call prompted or
    not.

### PII detection gate check (steps 17–20)

Self-contained: this doesn't depend on any fixture from Phase 0 or
`qa-environment-setup.md` — it creates its own throwaway subfolder and Google
Doc, and Phase 12 tears both down. It deliberately does **not** reuse
`{FIXTURES}.drive_qa_folder_id` as the doc's direct parent: that folder is
what `drive.read_file_contents` → `approved_folder` (if you configured it)
matches against, and an auto-accepted read never shows a popup at all — which
would make it impossible to actually see the tint/banner this check exists to
demonstrate. A subfolder one level down has a different (unmatched) parent
ID, so the normal `review` gate — and the PII gate layered on top of it —
is guaranteed to fire regardless of what auto-accept rules this environment
has configured.

This check also demonstrates the write/read split directly: the PII gate
only ever runs on the `review` (read) direction, never on `popup` (write) —
see the Technical Reference's "PII detection gate" section. Step 18 (a write) and step 19 (a
read of that same write's content) exist specifically to contrast the two.

17. Create a subfolder named `PrivacyFence QA PII test [{RUN_ID}]` inside the
    QA Sandbox folder (same pattern as step 9). Add it to the manifest.
18. `drive_write_doc_content` — create a new Google Doc **inside that
    subfolder**, titled `PrivacyFence QA test PII doc [{RUN_ID}] — safe to
    delete`, with this exact body (obviously-fake, clearly-labeled test data —
    do not substitute anything real):

    ```
    FAKE TEST DATA for PrivacyFence QA — no real person. Safe to delete.

    Email: test.user+pii-qa@example.com
    Phone: +36 20 123 4567
    Hungarian TAJ szám: 123 456 789
    Adóazonosító jel: 8123456789
    Születési dátum: 1990.01.01
    US SSN: 123-45-6789
    Date of birth: 1990-01-01
    German Steuer-IdNr. 65 929 970 489
    Geburtsdatum: 01.01.1990
    UK NI number: AB123456C
    ```

    This write is popup-gated regardless of any rule, and — because it's a
    write, not a read — the PII gate never scans it. **Pause here**: tell me
    you're about to call it and that, even though the body above contains
    synthetic PII spanning all three supported languages, you expect a
    **plain, untinted** popup with no category banner and no second "Are you
    sure?" dialog, since writes are never scanned. I'll click **Accept**.
    Wait for me to say go. Once I do, make the call and report back: confirm
    the popup was plain (no tint, no banner, no second dialog). If it renders
    tinted or shows a category banner, that's a bug — flag it. Add the doc to
    the manifest.
19. `drive_get_file_content` on the doc you just created — `review` gate, not
    covered by any `approved_folder` rule per the note above, so this must
    prompt every time regardless of environment config, and — unlike step
    18 — this is a read, so the PII gate does apply. **Pause here**: tell me
    you're about to call it and that, because the content contains synthetic
    PII spanning all three supported languages, you expect the popup to
    render tinted red with a category-listing banner covering the eight
    non-email/phone lines, and that after I click **Accept** a second **"Are
    you sure?"** dialog should appear — I'll click **Proceed** on that one.
    The Email/Phone lines are a deliberate **negative** check, not a typo —
    `pii_detector.py` intentionally never flags email addresses or phone
    numbers (see the Technical Reference's "PII detection gate" section), so those two
    lines must *not* contribute to the category banner. Wait for me to say
    go. Once I do, make the call and report back: did the tint/banner appear,
    which categories did it list (and did it correctly omit email/phone), did
    the second confirmation dialog appear, and confirm the returned content
    matches what was written.
20. Report the categories PrivacyFence actually detected (from the popup
    banner in step 19) against what `src/privacyfence/pii_detector.py`
    documents as supported, and flag anything that should have matched but
    didn't (or vice versa) as a finding, not something to silently reconcile.
    Flag both rows for Phase 13: confirming `"pii_detected": false` for
    step 18's write and `"pii_detected": true` for step 19's read in the
    audit log is the one field-level proof of the write/read split,
    independent of the popup banner — do it there, not now.

### Auto-accept override check (steps 21–23)

On the `review` (read) direction, the PII scan runs *before* the auto-accept
check and overrides a matching rule — a read that would otherwise pass
through silently must still stop for the popup + second confirmation if its
content contains PII. Steps 17–20 above prove the gate fires (on the read
side) with no rule in play at all; this section proves the more specific
claim: it also fires when a rule *would* have matched. It reuses
`{FIXTURES}.drive_qa_folder_id` itself as the parent this time — deliberately
the opposite choice from step 18 — since that's exactly the folder
`drive.read_file_contents` → `approved_folder` matches against, if you
configured it per `qa-environment-setup.md`.

21. `drive_write_doc_content` — create a new Google Doc **directly inside
    `{FIXTURES}.drive_qa_folder_id`** (not the PII-test subfolder from step
    17), titled `PrivacyFence QA test PII-vs-rule doc [{RUN_ID}] — safe to
    delete`, with the same fake-PII body as step 18. This write is
    popup-gated regardless of any rule (writes never auto-accept via
    `approved_folder`, and — same as step 18 — writes are never PII-scanned
    either), so nothing to prove here — expect a plain, untinted popup with
    no second confirmation, just Accept. Add the doc to the manifest.
22. `drive_get_file_content` on that doc. This file's parent *is*
    `{FIXTURES}.drive_qa_folder_id` — if `drive.read_file_contents` →
    `approved_folder` is configured, this read would normally auto-accept
    with **no popup at all** (confirm that's what you saw for the plain,
    no-PII file back in step 3). **Pause here**: tell me you're about to
    call it and that, even though this file lives in the auto-accepted
    folder, you expect a popup anyway — tinted, with a category banner,
    then the second "Are you sure?" confirmation — because the PII scan
    overrides the rule. Wait for me to say go. Once I do, make the call and
    report explicitly whether a popup appeared. If `approved_folder` isn't
    configured in this environment, say so plainly — this step can't
    distinguish "the override worked" from "there was no rule to override"
    in that case, so don't claim the override was proven either way.
23. Flag step 22 for Phase 13: the field-level proof that the override fired,
    independent of whether the popup was visually confirmed, is its audit
    entry showing `"decision": "approved"` (not `"auto_accepted"`),
    `"auto_accept_rule": ""`, and `"pii_detected": true` — contrasted against
    step 3's entry for the plain file (`"decision": "auto_accepted"` if the
    rule is configured) and against step 21's write entry, which should show
    `"pii_detected": false` regardless of the same fake-PII body, since
    writes are never scanned. Confirm all three once the log is attached, not
    now.

### Sheets rows/columns and Docs partial edits (steps 24–31)

Reuses the spreadsheet from step 10 and the Doc from step 7 — no new
manifest entries needed, both are already tracked.

24. `drive_sheets_insert_dimensions` — insert 1 row before index 0 in the
    sheet from step 10. This is one of the temp-accept-eligible write ops
    (see the Technical Reference's [Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules)).
    **Pause here**: tell me you're about to call it and that **I will click
    "Accept for 5 min"** this time, then wait for me to say go. Once I do,
    make the call, then immediately call `drive_sheets_insert_dimensions`
    again on the same spreadsheet, this time inserting 1 column — this
    second call should NOT prompt (silent, logged `auto_accepted` with rule
    `session_temp_accept`). Tell me whether the second call prompted or not.
25. `drive_sheets_delete_dimensions` — delete the row inserted in step 24.
    Unlike step 24, this one has **no** temp-accept shortcut at all —
    deleting rows/columns removes cell content with no undo path through
    PrivacyFence, so it only ever gets a plain popup or a standing rule, not
    "Accept for 5 min". Plain popup, Accept.
26. Invalid-dimension check: call `drive_sheets_insert_dimensions` with
    `dimension="sideways"` — expect a clear error naming `ROWS`/`COLUMNS` as
    the valid values, raised **before** any popup appears. Confirm no popup
    showed.
27. `drive_docs_edit_content` on the Doc from step 7 — find a short, unique
    phrase already in its body and replace it with new text tagged
    `{RUN_ID}`. Also temp-accept-eligible. **Pause here**: tell me you're
    about to call it and that **I will click "Accept for 5 min"**, then wait
    for go. Once I do, make the call, then immediately call
    `drive_docs_edit_content` again on the **same Doc**, replacing a
    different short phrase — this second call should NOT prompt (silent,
    `auto_accepted`, `session_temp_accept`). Tell me whether it prompted.
28. `drive_docs_format_content` on the same Doc — apply `highlight_color`
    (e.g. `#fff59d`) to a short existing phrase. Same temp-accept pattern,
    and its own separate 5-minute window (temp-accept is scoped per
    operation, not per file — accepting it for `docs_edit_content` in step
    27 does **not** cover this call): **pause here**, tell me you're about
    to call it and that **I will click "Accept for 5 min"**, wait for go,
    call it, then immediately call it again on the same Doc with `bold`
    instead — should NOT prompt the second time. Open the Doc in Google
    Docs and confirm the highlighted span actually renders highlighted.
29. Ambiguous-match check: call `drive_docs_edit_content` (or
    `drive_docs_format_content`) on the same Doc with `find_text` set to
    something that now matches more than one location (e.g. a short common
    word already repeated in the body) and `replace_all` left at its
    default `false` — expect a clear error stating how many locations
    matched and instructing to add more context or set `replace_all=true`,
    not a silent guess at which occurrence was meant. Then retry the same
    call with `replace_all=true` and confirm every occurrence changed.
30. `drive_write_doc_content` on the same Doc, writing entirely fresh
    content (plain text is fine). Confirm everything from steps 27–29 is
    now gone — this proves the full-rewrite tool is unchanged, and that
    steps 27–28's partial edits really were additive, not a new default
    behavior for `drive_write_doc_content` itself.
31. Highlight-syntax check: `drive_write_doc_content` again on the same
    Doc, with a body containing `==highlighted text==` somewhere. Popup,
    Accept. Open the Doc in Google Docs and confirm that span renders with
    a highlight background, the same as step 28's did.

## Phase 3 — Slack
1. `slack_list_channels` (expect: silent).
2. Auto-accept rule check: `slack_get_channel_history` on
   `{FIXTURES}.slack_approved_channel`. This should NOT prompt me. Confirm.
3. `slack_get_channel_history` on `{FIXTURES}.slack_control_channel` (the
   non-approved one) — should prompt for review. I'll Accept.
4. `slack_search_messages` with any query — review gate, Accept.
5. `slack_get_thread_replies` on the thread that already exists in
   `slack_control_channel` — review gate, Accept. (This fixture exists precisely
   so this step always has something to find, instead of depending on step 3/4
   having surfaced a threaded message.)
6. `slack_send_message` to my own self-DM only, test text tagged `{RUN_ID}`.
   Popup, Accept.

## Phase 4 — Calendar
1. `calendar_list_calendars`, `calendar_list_events`, `calendar_get_free_busy`
   (expect: all silent). Then `calendar_list_rooms`: if the Calendar room fixture
   from `qa-environment-setup.md` is set up, expect it to succeed and list at
   least one room, silently. If it isn't (no Workspace admin access), expect the
   same permissions error as before — that's a standing, known environment
   limitation, not a new finding, so don't report it as a regression each run.
2. Pick any existing event, `calendar_get_event_details` — review gate, Accept.
3. `calendar_create_event` — title `PrivacyFence QA test event [{RUN_ID}] — safe
   to delete`, date far in the future, no attendees, no Meet link. Popup, Accept.
   Add to manifest (not deletable via tool).
4. `calendar_update_event` on the event you just created. Popup, Accept.
5. `calendar_get_event_details` again on that same event — you're its organizer,
   so if `calendar.read_event_details` has an `i_am_organizer` rule this should
   NOT prompt. Tell me either way.
6. Attachments: use `calendar_list_events` to look through recent past events on
   the primary calendar for one that has a Google Meet "Notes by Gemini" /
   transcript doc attached (any real past meeting where "take notes for me" was
   used works — this isn't something the test prompt can fabricate, since
   `calendar_create_event` has no way to attach a file). If you find one:
   - `calendar_get_event_details` on it — review gate, Accept. Confirm the
     result's `attachments` list is non-empty and each entry has `file_id`,
     `title`, `mime_type`, `file_url`.
   - `drive_get_file_content` using one of those `file_id` values — confirm it
     resolves to the actual notes/transcript Doc content (Drive's own
     `review` gate applies here as normal).
   If no such event exists in this account's calendar history, note that and
   skip this step rather than reporting a gap — it's a fixture-availability
   limitation, not a regression.
7. `calendar_create_out_of_office` — title `PrivacyFence QA OOO [{RUN_ID}] — safe
   to delete`, a two-hour window far in the future, no decline message. Popup,
   Accept. Add to manifest (not deletable via tool). Confirm the created event's
   details in Google Calendar show it as an Out of Office entry that auto-declines
   only new conflicting invitations (not existing ones) — this is fixed behavior,
   not something the tool call can override.
8. `calendar_create_out_of_office` again, this time with a `decline_message`.
   Popup, Accept. Confirm the decline message appears on the created event.
9. `calendar_set_working_location` for today's date, `location="office"`. Popup,
   Accept. Confirm your presence shows as "In the office" in Google Calendar's
   web UI for that day (not deletable via tool — calling it again for the same
   day overwrites the prior value, so no separate cleanup is needed beyond noting
   it in the manifest).
10. `calendar_set_working_location` again for the same date with
    `location="home"` — confirm it overwrites the office entry from step 9
    rather than adding a second one.
11. `calendar_get_event_visibility` on the event from step 3 (expect: silent,
    auto — no popup at all, cheaper than the full `calendar_get_event_details`
    fetch). Confirm the returned `visibility` is `"default"`.
12. `calendar_set_event_visibility` on the same event, set to `"private"`.
    Popup-gated, and — unlike Phase 2's Sheets/Docs write tools — this one
    never gets a temp-accept shortcut either. If you configured
    `calendar.set_visibility` → `non_private_event` per
    `qa-environment-setup.md` §4, this call must **still** prompt regardless:
    the rule checks the visibility being *requested*, and `"private"` never
    matches it. Confirm that's what happened. Popup, Accept.
13. `calendar_get_event_visibility` again on the same event (silent). Confirm
    it now returns `"private"`.
14. `calendar_set_event_visibility` again, this time to `"public"`. If
    `calendar.set_visibility` → `non_private_event` is configured, this
    should NOT prompt (the requested value isn't private) — tell me either
    way.
15. `calendar_get_event_details` on the same event, now that it's back to
    `"public"`. You're still its organizer (from step 3), so `i_am_organizer`
    may already short-circuit this if configured; if not, and
    `calendar.read_event_details` → `non_private_event` is configured, it
    should still auto-accept via that rule instead. Tell me which rule (if
    any) actually matched — check the audit log in Phase 13 if it's not
    obvious from the popup (or lack of one) alone.
16. Invalid-visibility check: call `calendar_set_event_visibility` with
    `visibility="hidden"` — expect a clear error naming the valid values
    (`default`/`public`/`private`/`confidential`), raised **before** any
    popup appears. Confirm no popup showed.

## Phase 5 — Contacts
1. `contacts_list`, `contacts_search`, `contacts_get` (expect: all silent).
2. `contacts_list` with `source="personal"`, then again with `source="directory"`.
   Confirm the two result sets don't overlap and every returned contact's
   `source` field matches what you asked for (or `"both"` for a contact that's
   both a saved contact and a colleague). If I've told you this account has no
   Workspace directory colleagues, `source="directory"` coming back empty is the
   expected, correct result — confirm it stays empty, don't re-flag it as a gap.
3. `contacts_get` on a contact from the `source="personal"` list above, passing
   `source="directory"` — expect this to fail with a clear "source mismatch"
   error rather than silently returning the contact.
4. `contacts_create` — display name `PrivacyFence QA test contact [{RUN_ID}] —
   safe to delete`. Popup, Accept. Add to manifest (not deletable via tool).
5. `contacts_update` on the contact you just created — append
   ` (edited [{RUN_ID}])` to its name/note only, no email/phone change. If
   `contacts.edit` has a `no_contact_info_change` rule, this should NOT prompt —
   tell me either way.
6. `contacts_update` on the same contact again, this time changing its phone or
   email — even with that rule configured, this must still prompt (the rule only
   covers non-contact-info fields). Popup, Accept. Tell me the exact before/after
   for both updates.
7. `contacts_add_label` on the contact, label `PrivacyFence QA test`. Popup,
   Accept. Confirm the label appears on the contact in Google Contacts.
8. `contacts_remove_label` on the same contact/label. Popup, Accept. Confirm
   the label is gone.

## Phase 6 — Google Tasks
Reads are unconditionally auto-accepted; writes are `popup`-gated like every
other connector's writes, each independently configurable via the
`approved_task_list` auto-accept rule:
1. `tasks_list_task_lists`, `tasks_list_tasks` (expect: silent).
2. `tasks_create_task` in `{FIXTURES}.tasks_qa_list_id` — title
   `PrivacyFence QA test task [{RUN_ID}] — safe to delete`. Popup, Accept.
   Add to manifest.
3. `tasks_get_task` on it (expect: silent).
4. `tasks_update_task` (change the title slightly, keep the `{RUN_ID}` tag).
   If `tasks.update_task` has an `approved_task_list` rule covering
   `tasks_qa_list_id`, this should NOT prompt — tell me either way.
5. `tasks_complete_task`, then `tasks_uncomplete_task` on the same task — same
   "should NOT prompt if configured" check, against `tasks.complete_task` /
   `tasks.uncomplete_task` respectively.
6. `tasks_move_task` (move it within the same list — a no-op move, just to
   exercise the tool). Popup, Accept.
7. Auto-accept contrast check (skip if `tasks_contrast_list_id` wasn't
   resolved in Phase 0): `tasks_create_task` in `tasks_contrast_list_id`,
   title `PrivacyFence QA contrast task [{RUN_ID}] — safe to delete`. This
   MUST prompt regardless of any `approved_task_list` rule, since that list
   is deliberately never added to one. Popup, Accept. Add to manifest.
8. `tasks_update_task` on the contrast task, appending
   ` (edited [{RUN_ID}])` to its title — same "must still prompt" expectation
   as step 7, even if `tasks.update_task` has a rule configured for the QA
   list. Popup, Accept.

## Phase 7 — Telegram
1. `telegram_list_chats` (expect: silent — the only genuinely unconditional one).
   Confirm `{FIXTURES}.telegram_saved_messages_chat_id` is now present in the
   results.
2. `telegram_get_messages` on `{FIXTURES}.telegram_approved_chat_id` — **watch
   for a native approval popup and tell me explicitly whether you see one before I
   respond**, don't infer it from the tool result; I'll Accept if it appears.
   Flag this row for Phase 13: the audit log's decision (`auto_accepted` vs
   `accepted`) is what settles it if my own observation of the popup was
   ambiguous — check that once the log is attached, not now.
3. `telegram_get_messages` on `{FIXTURES}.telegram_control_chat_id` (not in
   `approved_chats`) — same explicit "did a popup appear?" question, same
   Phase 13 flag. I'll Accept.
4. `telegram_search_messages` with a query that matches the seed message from
   setup — same explicit popup check, same Phase 13 flag.
5. `telegram_send_message` to "Saved Messages"
   (`telegram_saved_messages_chat_id`), test text tagged `{RUN_ID}`. Popup,
   Accept.

## Phase 8 — Salesforce
1. `salesforce_list_reports` (expect: silent).
2. `salesforce_run_report` on `{FIXTURES}.salesforce_qa_report_id` — if
   `salesforce.run_report` has an `approved_report_ids` rule matching it, this
   should NOT prompt. Tell me either way. Confirm you get actual data rows back,
   not an empty result.
3. `salesforce_run_report` on any *other* report you can access — should still
   prompt for review regardless of the rule above. Accept.
4. From the QA report's rows, pick a record and call `salesforce_get_record`
   with `object_type` set to `{FIXTURES}.salesforce_qa_object_type`. If
   `salesforce.read_record` has an `approved_object_types` rule matching it, this
   should NOT prompt — tell me either way. Confirm you get real field data back,
   not `NOT_FOUND`.
5. `salesforce_get_record` on a record of a *different* object type — should
   still prompt. Accept. (No write tools exist for Salesforce in this build.)
6. `salesforce_search` — search for `PrivacyFence QA` scoped to
   `object_types="Account"` (or `{FIXTURES}.salesforce_qa_object_type` if
   that's `Account`). If you configured `salesforce.search` →
   `approved_object_types` per `qa-environment-setup.md` §8 step 6, this
   should NOT prompt — tell me either way. Confirm the sample records from
   setup show up in the results. **Known quirk, not a bug:** if this comes
   back empty and the sample records were created very recently, Salesforce's
   SOSL search index may not have caught up yet — wait a minute and retry
   before treating it as a finding.
7. `salesforce_search` again, same query, but leave `object_types` empty
   (unscoped — searches Salesforce's default globally-searchable objects).
   This must still prompt regardless of the rule above: `approved_object_types`
   only matches when every requested object type is on the allowlist, and an
   unscoped search requests none in particular, so it never matches. Accept.
8. Validation check: call `salesforce_search` with `account_id` set to any
   valid-looking Salesforce ID but `object_types` left empty — expect a
   clear error (`account_id requires object_types to be specified`), raised
   **before** any popup appears. Confirm no popup showed.
9. `salesforce_search` with `object_types="Opportunity"` (or another object
   type that has an `AccountId` field) and `account_id` set to an Account ID
   from step 6's results — confirm the results are limited to records
   related to that account specifically (an empty list is a valid, expected
   result if that account has no such related records — not a bug).

## Phase 9 — Jira
1. `jira_list_projects`, `jira_search_issues` (expect: both silent).
2. `jira_get_issue` on any existing issue in `{FIXTURES}.jira_qa_project_key`.
   If `jira.read_issue` has an `approved_project_keys` rule matching it, this
   should NOT prompt — tell me either way.
3. `jira_get_issue` on any issue in `jira_contrast_project_key` — should still
   prompt regardless of that rule. Accept.
4. `jira_create_issue` in `jira_qa_project_key` — summary `PrivacyFence QA test
   issue [{RUN_ID}] — safe to delete/close`. Popup, Accept. Add to manifest.
5. `jira_get_issue` on the issue you just created — you're both its reporter and
   assignee, so if `i_am_reporter`/`i_am_assignee` rules are configured for
   `jira.read_issue` this should NOT prompt either, independent of the project
   rule. Tell me which rule (if any) actually matched.
6. `jira_add_comment` on it, test comment. Popup, Accept.
7. `jira_update_issue` on it (e.g. change description). Popup, Accept.
8. `jira_update_issue` with `custom_fields` targeting one custom field already
   configured on `jira_qa_project_key`'s issue screen (any type works — check
   the issue's "..." menu in Jira's web UI for available custom fields), by its
   exact display name (e.g. `{"Story Points": 5}`). Popup, Accept. Confirm the
   value is set correctly in Jira. If the QA project has no custom field
   configured, note that and skip this step — it's a fixture-availability
   limitation, not a regression.
9. `jira_get_transitions` on it (expect: silent). Confirm the result lists at
   least one transition name and target status reachable from the issue's
   current status.
10. `jira_transition_issue` to one of the transition names from step 9. Popup,
    Accept. Confirm the issue's status actually changed in Jira, and that the
    tool's result reflects the new status.
11. `jira_transition_issue` again with a transition name that is *not* in the
    list from a fresh `jira_get_transitions` call on the now-transitioned issue
    — expect a clear error naming the invalid transition and listing what's
    actually available, not a raw Jira API error.

## Phase 10 — Confluence
1. `confluence_list_spaces`, `confluence_search`, `confluence_cql_search`,
   `confluence_list_pages` (expect: all silent).
2. Pick any existing page in `{FIXTURES}.confluence_qa_space_key`,
   `confluence_get_page`(`_by_title`) — if `confluence.read_page` has an
   `approved_space_keys` rule matching it, this should NOT prompt. Tell me
   either way.
3. Same call against a page in `confluence_contrast_space_key` — should still
   prompt regardless. Accept.
4. `confluence_create_page` in `confluence_qa_space_key`, titled `PrivacyFence
   QA test page [{RUN_ID}] — safe to delete`. Popup, Accept. Add to manifest.
5. `confluence_get_page` on the page you just created — you're its author, so if
   an `i_am_author` rule is configured for `confluence.read_page` this should NOT
   prompt, independent of the space rule. Tell me which rule (if any) matched.
6. `confluence_update_page` on it, minor edit. Popup, Accept.

## Phase 11 — Scheduled / unattended Cowork tasks

Not a connector — this exercises `privacyfence_check_policy` and the unattended-session mode
(`privacyfence_begin_unattended_session` / `privacyfence_end_unattended_session`), added for
scheduled Cowork Routines that may run with nobody watching. See
[`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks). Unlike
every phase above, this one needs a **daemon restart partway through**: `unattended_sessions.enabled` is read once at
`run_app()` startup, not hot-reloaded like `auto_accept_rules`/`pii_detection.enabled` — there is
no menu-bar toggle for it (deliberately: enabling it is meant to be a config-file edit an
administrator makes, not a live daemon toggle).

1. Confirm `unattended_sessions.enabled` is **absent or `false`** in `settings.yaml` right now (the
   default). `privacyfence_check_policy` needs no such flag and works regardless — start there:
2. `privacyfence_check_policy` on a tool you know is unconditionally `auto` (e.g.
   `connector="gmail", tool="gmail_list_messages", args={}`). Expect `{"gate": "auto", "verdict":
   "auto_accept", "matched_rule": null, ...}` with **no popup and no tool actually called**.
3. `privacyfence_check_policy` on `connector="slack", tool="slack_get_channel_history"` with
   `args={"channel_id": "<{FIXTURES}.slack_approved_channel>"}` (reuse Phase 3's fixture). If
   `slack.read_messages` → `approved_channel` is configured, expect `{"gate": "review", "verdict":
   "auto_accept", "matched_rule": "approved_channel", ...}`.
4. Same call, but with `channel_id` set to `{FIXTURES}.slack_control_channel` instead (not on the
   allowlist). Expect `{"verdict": "requires_review", "matched_rule": null, ...}` — the args-only
   rule is fully evaluable and it doesn't match, so preflight can say so with certainty, not just
   "unknown".
5. `privacyfence_check_policy` on `connector="gmail", tool="gmail_get_message"` with any
   `message_id`. This tool's rules (`i_am_sender`, `trusted_sender_domain`, etc.) all need the
   actual fetched message to evaluate — expect `{"verdict": "unknown", ...}` with a `reason`
   naming the undetermined rule(s), regardless of what's configured in `settings.yaml`.
6. Confirm none of steps 2–5 produced a popup, called a connector, or changed anything — they're
   pure preflight. If you want to double-check, note the current audit log line count before step
   2 and after step 5: it should have grown by exactly 4 (one `policy_check` entry per call).
7. `privacyfence_begin_unattended_session` — since `unattended_sessions.enabled` is still `false`/
   absent, expect a clear error mentioning `unattended_sessions.enabled` in `settings.yaml`, **not**
   a popup and **not** a partial success. Confirm the menu bar's top item still reads plain
   "PrivacyFence is running" (no session count).
8. Set `unattended_sessions.enabled: true` in `settings.yaml` and **restart the daemon** (not just
   an "Accept All" hot-reload — see the note above). Reconnect the Cowork/Desktop session so its
   bridge process talks to the freshly restarted daemon.
9. `privacyfence_begin_unattended_session` again — expect `{"unattended": true}`, no popup. Check
   the PrivacyFence menu bar now: the top item should read "PrivacyFence is running — 1 unattended
   session active". **Pause here**: tell me you're about to call the next step's tool and that,
   even though it's normally review/popup-gated, **you expect no popup to appear at all this
   time** — instead the tool call itself should come back as an error. Wait for me to say go.
10. Once I say go, call `slack_get_channel_history` for real on `{FIXTURES}.slack_control_channel`
    (not on the allowlist — same fixture as step 4). Confirm: no native popup appeared, and the
    call returned an error mentioning "unattended session" rather than data. This is the core
    behavior this phase exists to prove — flag it as a bug if a popup appeared instead, or if the
    call silently succeeded.
11. Now call `slack_get_channel_history` on `{FIXTURES}.slack_approved_channel` (on the allowlist)
    — this should succeed silently with **no popup and no error**, exactly as it would outside an
    unattended session. Confirms the flag only changes the *deny* path, never what auto-accepts.
12. `privacyfence_end_unattended_session` — expect `{"unattended": false}`, no popup. Confirm the
    menu bar's top item goes back to plain "PrivacyFence is running" (no count).
13. Call `slack_get_channel_history` on `{FIXTURES}.slack_control_channel` once more (same as step
    10). **Pause here**: tell me you now expect a normal native popup, same as any other
    non-matching review call — the session is no longer unattended. Wait for me to say go, then
    make the call and confirm the popup actually appeared this time. I'll Deny it (it's not data
    this run needs to keep).
14. Set `unattended_sessions.enabled` back to `false` (or leave it — your call, but note in the
    final report which state you left it in) and restart the daemon once more so later runs of
    this whole prompt start from the documented default.

No manifest entries from this phase — nothing persistent gets created (Slack reads, preflight
checks, and session toggles leave no artifact behind), so Phase 12 (Teardown) has nothing to do for
it. Flag these rows for Phase 13 (Audit Log Reconciliation) instead of guessing them now:
steps 2–5's `policy_check` entries (`decision: "policy_check"`, `auto_accept_rule` matching each
step's `matched_rule`), step 10's entry (`decision: "denied_unattended"`), and steps 9/12's session
entries (`decision: "unattended_session_started"` / `"unattended_session_ended"`, both with empty
`connector`/`tool` fields since they're not tied to a specific tool call).

## Phase 12 — Teardown
Go through the manifest table and, for every artifact tagged `{RUN_ID}` that has
a delete/remove/archive/close tool available, call it now:
1. Delete/trash the Drive file, Doc, sheet, and the throwaway moved file from
   Phase 2 (`drive` has no bulk-delete — one call per artifact is fine). This
   includes the PII test doc and its subfolder from steps 17–20 (trash the
   doc, then the now-empty subfolder) and the PII-vs-rule doc from step 21.
2. Delete the Gmail draft(s) from Phase 1 if a delete-draft tool exists;
   otherwise leave the label/note in the manifest for manual cleanup.
3. The Jira issue from Phase 9 was already moved via `jira_transition_issue` in
   steps 10–11 of that phase (don't delete it — closed test issues are useful
   QA history); if its status after step 11 isn't already terminal (e.g. Done,
   Closed), use `jira_get_transitions` + `jira_transition_issue` once more to
   land it in a terminal status now. Note its final status either way.
4. Delete the Confluence page from Phase 10 if a delete tool exists; otherwise
   note it for manual cleanup.
5. Uncomplete/delete both Google Tasks from Phase 6 (the QA-list one and the
   contrast-list one) if a delete tool exists.
6. The Gmail label-test message from Phase 1 steps 2/9–12 needs nothing here —
   the archive/unarchive/remove-label sequence already restores it to exactly
   its starting state.
7. For anything with **no delete tool at all** (Calendar event, the
   out-of-office and working-location entries from Phase 4 steps 7–10, Contact,
   Slack message, Telegram message, the Gmail filter from Phase 1 steps 17/19,
   the Gmail label(s) from Phase 1 step 14), do NOT attempt a workaround — list
   it plainly in the final manifest under "needs manual deletion," grouped by
   connector, so I can batch-clean these across multiple runs instead of doing
   it one run at a time. For the Gmail filter/label specifically, note that
   manual cleanup happens in Gmail's web UI (Settings → Filters and Blocked
   Addresses / Settings → Labels), not via any PrivacyFence tool.

## Phase 13 — Audit Log Reconciliation
Everything above ran with the `audit-log decision` column blank and every
audit-dependent question (Phase 1 step 3, Phase 2 steps 20/23, Phase 7 steps
2–4) flagged rather than answered, on purpose — a live mid-run read from a
Cowork/Desktop session isn't reliable. This phase closes all of that out
against a copy of the log I hand you directly.

1. **Ask me to attach the audit log now, and wait for me to do it before
   continuing.** Tell me exactly which file: `logs/audit/<this-week>.jsonl` —
   under `~/.privacyfence/audit/` if this is a bundled/DMG install, or
   `logs/audit/` in the project root if the daemon is running from source
   (see [dev-vs-live-setup.md](dev-vs-live-setup.md)). If you're not sure
   which applies, ask me rather than guessing.
2. Once I've attached it, read it and go through the running table row by
   row, filling in `audit-log decision` for every call that has one — match
   by timestamp and tool/operation name, not by row order (a retried or
   re-run call can shift the order). If a row has no matching entry, write
   "no matching entry" explicitly rather than leaving it blank.
3. Resolve every item flagged earlier, specifically:
   - Phase 1 step 3: state definitively whether it was `denied` or a
     size-truncation error with a different underlying cause.
   - Phase 2 steps 18–19: confirm step 18's (write) entry shows
     `"pii_detected": false` and step 19's (read) entry shows
     `"pii_detected": true` — the write/read split, not identical results.
   - Phase 2 step 22: confirm `"decision": "approved"` (not
     `"auto_accepted"`), `"auto_accept_rule": ""`, and `"pii_detected": true`,
     contrasted against step 3's entry for the plain file and against step
     21's write entry (`"pii_detected": false`).
   - Phase 7 steps 2–4: state the actual logged decision (`auto_accepted` vs
     `accepted`) for each — this is what settles it even where your own
     popup observation was already unambiguous.
   - Phase 11 steps 2–5: confirm one `"decision": "policy_check"` entry per
     `privacyfence_check_policy` call, each `"auto_accept_rule"` matching that
     step's `matched_rule` (empty string for steps where it was `null`).
   - Phase 11 step 10: confirm `"decision": "denied_unattended"` — distinct
     from `"rejected"`, since no human was ever asked. Contrast this against
     step 13's entry for the identical call made outside an unattended
     session, which should show a normal `"rejected"` or `"approved"`
     depending on what you clicked.
   - Phase 11 steps 9/12: confirm one `"decision": "unattended_session_started"`
     entry and one `"unattended_session_ended"` entry, both with empty
     `"connector"`/`"tool"` fields (these aren't tied to a specific tool call).
4. Note any call in the table you couldn't find a matching audit-log entry
   for at all (clock skew, wrong week's file, a log write that never
   happened) — that's itself worth reporting, not something to silently paper
   over with the tool-result guess.

---

## Final report
Print the full running table (tool | gate observed | my decision | audit-log
decision | notes), now fully reconciled from Phase 13, then the full manifest
table split into "cleaned up in Phase 12" vs. "needs manual deletion." Then:
- Call out any tool whose observed gate, or whose audit-log decision, didn't
  match what I told you to expect — these are two independent checks now, so
  flag it even if only one of them disagrees.
- Give the Phase 7 (Telegram) popup-visibility answers from steps 2–4
  explicitly, each one backed by the matching audit-log entry from Phase 13 —
  don't let this collapse back into "I can't tell," since both your own
  observation and the log are available by this point.
- Give the Phase 2 PII detection gate check (steps 17–20) its own explicit
  answer: confirm the write in step 18 stayed plain (no tint, no banner, no
  second dialog, `pii_detected: false`) and the read in step 19 got flagged
  (tint, category banner, second "Are you sure?" dialog, `pii_detected: true`)
  — per the audit-log entries from Phase 13. If PrivacyFence's menu bar has
  **PII Detection Gate** turned off, that changes step 19's expected result
  to "no tint, no second dialog, `pii_detected: false`" too (step 18 is
  unaffected by the toggle either way, since writes are never scanned
  regardless) — state which case you're actually in rather than assuming
  it's enabled.
- Give the auto-accept override check (steps 21–23) its own explicit answer
  too, separate from the above: was `approved_folder` actually configured
  for `drive.read_file_contents` in this environment (check what you read
  from `settings.yaml` in Phase 0)? If yes, did step 22 still prompt despite
  the rule matching, and did step 22's audit entry (per Phase 13) show
  `"decision": "approved"` rather than `"auto_accepted"`? If the rule wasn't
  configured, say explicitly that this check only re-confirmed the plain
  PII-gate behavior and didn't exercise the override itself — don't report it
  as a pass for a claim it couldn't actually test.
- List every other step across the whole run where the PII gate fired
  organically (per the ground rule above) — real accounts routinely surface
  this outside the dedicated check, and it's useful signal for how noisy the
  detector is against this account's actual data, not just the synthetic
  case in Phase 2.
- List any pre-existing `PrivacyFence QA test [...]` artifacts you noticed that
  carry a **different** `{RUN_ID}` (or no `{RUN_ID}` at all, from before this
  version of the prompt existed) — flag them as leftovers from a previous run,
  don't touch them, and don't count them as part of this run's manifest.
- Give Phase 11 (scheduled/unattended tasks) its own explicit answer: did every
  `privacyfence_check_policy` verdict in steps 2–5 match what was expected
  (`auto_accept`/`requires_review`/`unknown`)? Did step 10 actually deny
  without a popup, and step 13 actually prompt normally once the session
  ended? State plainly which `unattended_sessions.enabled` value you left
  `settings.yaml` in at step 14, so a later run (or a human reading this
  report) isn't surprised by its current state.
````

## Reading the results

Phase 13 is what fills in the `audit-log decision` column, once you've
attached the log file — the prompt no longer has Claude read it live during
the run, since a Cowork/Desktop session reading the file mid-run isn't
reliable (recently written entries can appear to lag or go missing depending
on how the session reaches the file; attaching a fixed copy at the end avoids
that entirely). Once Phase 13 has run, that column *is* ground truth, not a
hypothesis, and you shouldn't need to reconcile `accepted` / `denied` /
`auto_accepted` by hand afterward. What Claude still can't do is independently
confirm the popup UI rendered correctly on your screen; it can only confirm
what the daemon actually decided. If your own observation of a popup
disagrees with the logged decision (e.g. you saw no prompt but the log says
`accepted` rather than `auto_accepted`), that's the interesting case worth
investigating — it means the popup and the logged decision disagreed, which
the tool result and the log alone can't explain by themselves.

Spot-check a handful of entries yourself against the attached file if you
want an independent check on Claude's Phase 13 reconciliation, but treat a
fully-populated `audit-log decision` column as the run having done its job,
not as something to redo from scratch.

Watch for these three error shapes, which are easy to mis-file as gate bugs
when they're really something else:

- **Provider/API errors** (`SERVICE_DISABLED`, `FORBIDDEN`, `NOT_FOUND`,
  strict-JQL rejections) — these mean the gate let the call through correctly;
  the failure is downstream, in the third-party API or org permissions.
- **Truncation vs. denial** — a response that hits a size cap can produce a
  generic error that's indistinguishable, from the tool result alone, from a
  Deny. Only the audit log disambiguates.
- **A connector bug masquerading as a gate bug** — if a call errors
  consistently regardless of which record/message/page you pick, suspect the
  client code, not the gate. That pattern is what led to the Confluence and
  Gmail fixes below.

## Example findings from the 2026-07 run

Running this method surfaced, in one pass:

- **Confluence's entire page-content path was broken.** `list_pages_in_space`,
  `get_page`, `get_page_by_title`, `create_page`, and `update_page` all called
  a Confluence v1 REST endpoint Atlassian has since removed (410 Gone) — only
  the space/search list tools, already migrated to v2, worked. Fixed in
  [`confluence_client.py`](../src/privacyfence/confluence_client.py) by
  porting the remaining five methods to the v2 API.
- **`gmail_reply_draft` failed with "Invalid To header"** for senders with a
  non-ASCII display name (e.g. an accented Hungarian name). Root cause:
  assigning `"Name <addr>"` straight to a `Message` header RFC-2047-encodes
  the *whole* string once it contains non-ASCII text, including the
  address — Gmail's parser then rejects the encoded blob as an invalid
  addr-spec. Fixed by routing To/Cc through `parseaddr`/`formataddr` so only
  the display name gets encoded.
- **`contacts_update` leaked a raw `'NoneType' object is not iterable`**
  instead of a clean error — and the daemon's own dispatch-error log only
  recorded the message, not a traceback, which is why the exact trigger
  couldn't be pinned down from the log alone. Hardened the update path to
  fail cleanly, and fixed `ipc_server.py` to log `exc_info=True` so the next
  occurrence is actually diagnosable.
- **The README's auto-accept-rules section had drifted from the code**: a
  stale footnote claimed Telegram's read tools were unconditionally
  auto-accepted (only `telegram_list_chats` is) and that Tasks writes needed
  popup approval like every other connector (a later doc-only edit briefly
  reverted this footnote to claim all 8 Tasks tools were auto — they aren't;
  the 5 write tools are `popup`-gated in `tasks.py`, matching the table
  elsewhere in the README). The same cross-check also turned up real,
  working auto-accept rule evaluators for Jira, Confluence, Telegram, and
  Contacts in `auto_accept.py` that were entirely missing from the README's
  rule tables.

None of these were caught by the unit test suite, because the unit tests mock
each connector's client — they verify the code does what it's told, not that
what it's told still matches the live API or the live docs.
