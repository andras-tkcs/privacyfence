# Full-Connector QA Testing via Claude Cowork

PrivacyFence's real attack surface is the interaction between ten connectors,
three gate types (`auto` / `review` / `popup`), and a growing set of
auto-accept rules — none of which unit tests exercise end to end, since they
mock the gate itself. The fastest way to catch drift between what the code
does and what a user actually experiences is to drive every tool through a
live Claude Cowork/Desktop session connected to the real `privacyfence`
daemon, against real accounts, and watch what actually prompts.

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
  (README's connector tables) still matches its *actual* gate in source.

## Prerequisites

- `privacyfence-app` (the daemon) running, with every connector you want to
  test already authenticated from the menu bar.
- The `privacyfence` MCP server attached to a Claude Cowork/Desktop
  conversation (`claude mcp add privacyfence privacyfence-bridge`, or the
  `.mcpb` extension).
- Claude needs read access to `~/.privacyfence/logs/audit/<current-ISO-week>.jsonl`
  in the Cowork/Desktop session (filesystem access, or a Bash/Read-equivalent
  tool) — the prompt below now has Claude read this file itself and reconcile
  it against every call in the same report, instead of leaving that to you
  afterward. This is a test environment against your own accounts, so there's
  no confidentiality reason to keep the log human-only. Claude still can't
  observe the popup UI directly (it only sees whether the tool call ultimately
  succeeded or errored) — the audit log's `decision` field is what closes that
  gap, since it records `accepted` / `denied` / `auto_accepted` regardless of
  whether Claude witnessed the click.
- **The environment fixtures from [`qa-environment-setup.md`](qa-environment-setup.md)
  already exist**: a `PFQA` Jira project, a `PFQA` Confluence space, a Drive
  "PrivacyFence QA Sandbox" folder, a second (non-approved) Slack channel with
  a thread in it, a Telegram "Saved Messages" chat plus one approved chat, and
  Salesforce sample records/report. Without these, several phases below fall
  back to being untestable — work through that doc once per environment; it's
  a standalone installation guide, not something you redo per run.

## The prompt

Paste this as a single message into the Cowork conversation. It walks every
connector in dependency order (list/search before get, get before write),
deliberately hits every auto-accept rule you have configured (see the
environment doc's consolidated rules block) back-to-back with a contrasting
call that should still prompt, and ends with a self-report that already has
the audit log's actual decision for every call baked in — Claude reads
`~/.privacyfence/logs/audit/<this-week>.jsonl` itself during the run rather
than leaving reconciliation to you afterward.

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
  with `{RUN_ID}` — Phase 11 (teardown) depends on that manifest to find and remove
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
  Details," or "Accept All" — send that instruction as its own message and stop.
  Don't make the tool call in the same turn.** Wait for me to reply (e.g. "go" or
  "ready") before calling the tool. The native approval popup can appear on top
  of this chat window the instant the tool call fires, so if the instruction and
  the call land in the same turn I may only see the popup, not what I was
  supposed to do with it, and default to clicking Accept out of habit. A step
  marked "pause here" below means: stop, wait for my go-ahead, then call it.
- Keep a running table as you go: `tool name | gate observed (silent / Cowork
  review / native popup) | my decision | audit-log decision | notes`. This is a
  test environment against my own accounts, so read
  `~/.privacyfence/logs/audit/<this-week>.jsonl` yourself as you go (or in a
  batch at the end of each phase) and fill in the `audit-log decision` column
  with the actual logged `accepted` / `denied` / `auto_accepted` value for each
  call — match entries by timestamp and tool/operation name. Don't leave that
  column blank or defer it to me. Print the full table at the very end.
- Keep a second running table, the manifest: `connector | artifact | id | {RUN_ID}
  tag | deletable via tool? (yes/no)`. Print it at the very end too, split into
  "cleaned up in Phase 11" vs. "needs manual deletion."

I (the human) will be watching for the approval prompts as they appear. Most
steps expect a plain Accept and you can just make the call. The ones marked
**"pause here"** expect something else (Deny / Show Details / Accept All) —
for those, stop and wait for my go-ahead as the ground rules above describe,
*then* call the tool, then report back what actually happened in the tool
result, and confirm it against the audit log entry for that call before
writing it into the table as settled rather than assumed.

---

## Phase 0 — Setup
Resolve every fixture yourself before Phase 1 — don't ask me for IDs, look
them up. Build a `{FIXTURES}` table as you go and print it before moving on,
so I can catch a wrong lookup immediately instead of at the end of the run.

1. Generate `{RUN_ID}` yourself right now as `YYYY-MM-DD-HHmm` in my local time.
   Use it verbatim in every title for the rest of this run.
2. Read `~/.privacyfence/config/settings.yaml` and keep the full
   `auto_accept_rules` block in mind for the rest of the run — several fixtures
   below come directly from it rather than a separate lookup.
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
9. For any fixture you couldn't resolve (missing folder/channel/report, or a
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
   fallback answer. Then read the audit log entry for this call and state
   definitively whether it was `denied` or a truncation with a different
   underlying cause — don't leave this as a guess.
4. Pick a message that has a thread with 2+ messages. **Pause here**: tell me
   you're about to call `gmail_get_thread` on it and that **I will click "Show
   Details"** instead of Accept/Deny directly, then approve from the native
   popup, then wait for me to say go. Once I do, make the call and report what
   came back.
5. `gmail_list_message_attachments` on any message with attachments (expect:
   silent). Then `gmail_download_attachment` on one — this is `review` gated,
   I'll Accept.
6. Auto-accept rule check: using whatever `gmail.read_message` /
   `trusted_sender_domain` value you read from `settings.yaml` in Phase 0, find
   a message from that domain and call `gmail_get_message` on it. This should
   NOT prompt me at all. Tell me whether a prompt appeared or not. If no such
   rule is configured, skip and say so.
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
    so there's nothing to add to the manifest or clean up for it in Phase 11.
13. `gmail_list_labels` (expect: silent, no prompt). Confirm the response
    includes both system labels (e.g. `INBOX`) and any user labels, each with
    an `id`/`name`/`type`.
14. `gmail_create_label` with a **nested** name:
    `PrivacyFence QA {RUN_ID}/Nested`. Popup, I'll Accept. Since Gmail has no
    parent-id field, `create_label` in `gmail_client.py` creates the missing
    `PrivacyFence QA {RUN_ID}` parent segment first, then the child — call
    `gmail_list_labels` again (silent) and confirm **both** segments now
    exist as separate labels. Add the parent label name to the manifest
    (no delete tool — see Phase 11).
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
    Phase 11).
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
5. `drive_add_comment` on it, any test comment — popup, Accept.
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
    formulas evaluate. Popup, Accept.
14. `drive_sheets_add_sheet` — add a tab named `Extra`. Popup, Accept.
15. `drive_sheets_rename_sheet` — rename `Extra` to `TO BE DELETED - Extra`.
    Popup, Accept.
16. `drive_sheets_format_range` — bold `A1:B2`. Popup, Accept.

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
other connector's writes. Expect **zero prompts** for step 1, and a popup for
each of steps 2, 4, 5, and 6:
1. `tasks_list_task_lists`, `tasks_list_tasks` (expect: silent).
2. `tasks_create_task` — title `PrivacyFence QA test task [{RUN_ID}] — safe to
   delete`. Popup, Accept. Add to manifest.
3. `tasks_get_task` on it (expect: silent).
4. `tasks_update_task` (change the title slightly, keep the `{RUN_ID}` tag).
   Popup, Accept.
5. `tasks_complete_task`, then `tasks_uncomplete_task`. Popup, Accept each.
6. `tasks_move_task` (move it within the same list). Popup, Accept.

## Phase 7 — Telegram
1. `telegram_list_chats` (expect: silent — the only genuinely unconditional one).
   Confirm `{FIXTURES}.telegram_saved_messages_chat_id` is now present in the
   results.
2. `telegram_get_messages` on `{FIXTURES}.telegram_approved_chat_id` — **watch
   for a Cowork review popup and tell me explicitly whether you see one before I
   respond**, don't infer it from the tool result; I'll Accept if it appears.
   Then read the audit log entry for this call and state the actual logged
   decision (`auto_accepted` vs `accepted`) — that settles it even if my own
   observation of the popup was ambiguous.
3. `telegram_get_messages` on `{FIXTURES}.telegram_control_chat_id` (not in
   `approved_chats`) — same explicit "did a popup appear?" question, then the
   same audit-log confirmation. I'll Accept.
4. `telegram_search_messages` with a query that matches the seed message from
   setup — same explicit popup check plus audit-log confirmation.
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

## Phase 11 — Teardown
Go through the manifest table and, for every artifact tagged `{RUN_ID}` that has
a delete/remove/archive/close tool available, call it now:
1. Delete/trash the Drive file, Doc, sheet, and the throwaway moved file from
   Phase 2 (`drive` has no bulk-delete — one call per artifact is fine).
2. Delete the Gmail draft(s) from Phase 1 if a delete-draft tool exists;
   otherwise leave the label/note in the manifest for manual cleanup.
3. Close or transition the Jira issue from Phase 9 to a terminal status (don't
   delete it — closed test issues are useful QA history); note its final status.
4. Delete the Confluence page from Phase 10 if a delete tool exists; otherwise
   note it for manual cleanup.
5. Uncomplete/delete the Google Task from Phase 6 if a delete tool exists.
6. The Gmail label-test message from Phase 1 steps 2/9–12 needs nothing here —
   the archive/unarchive/remove-label sequence already restores it to exactly
   its starting state.
7. For anything with **no delete tool at all** (Calendar event, Contact, Slack
   message, Telegram message, the Gmail filter from Phase 1 steps 17/19, the
   Gmail label(s) from Phase 1 step 14), do NOT attempt a workaround — list it
   plainly in the final manifest under "needs manual deletion," grouped by
   connector, so I can batch-clean these across multiple runs instead of doing
   it one run at a time. For the Gmail filter/label specifically, note that
   manual cleanup happens in Gmail's web UI (Settings → Filters and Blocked
   Addresses / Settings → Labels), not via any PrivacyFence tool.

---

## Final report
Print the full running table (tool | gate observed | my decision | audit-log
decision | notes), then the full manifest table split into "cleaned up in
Phase 11" vs. "needs manual deletion." Then:
- Call out any tool whose observed gate, or whose audit-log decision, didn't
  match what I told you to expect — these are two independent checks now, so
  flag it even if only one of them disagrees.
- Give the Phase 7 (Telegram) popup-visibility answers from steps 2–4
  explicitly, each one backed by the matching audit-log entry — don't let this
  collapse back into "I can't tell," since both your own observation and the
  log are available this time.
- List any pre-existing `PrivacyFence QA test [...]` artifacts you noticed that
  carry a **different** `{RUN_ID}` (or no `{RUN_ID}` at all, from before this
  version of the prompt existed) — flag them as leftovers from a previous run,
  don't touch them, and don't count them as part of this run's manifest.
- Note any call you couldn't find a matching audit-log entry for at all (clock
  skew, wrong week's file, a log write that never happened) — that's itself
  worth reporting, not something to silently paper over with the tool-result
  guess.
````

## Reading the results

The prompt now has Claude read the audit log itself and fill in an
`audit-log decision` column, so you shouldn't need to reconcile
`accepted` / `denied` / `auto_accepted` by hand afterward — that column *is*
ground truth, not a hypothesis. What Claude still can't do is independently
confirm the popup UI rendered correctly on your screen; it can only confirm
what the daemon actually decided. If your own observation of a popup
disagrees with the logged decision (e.g. you saw no prompt but the log says
`accepted` rather than `auto_accepted`), that's the interesting case worth
investigating — it means the popup and the logged decision disagreed, which
the tool result and the log alone can't explain by themselves.

Spot-check a handful of entries yourself if you want an independent check on
Claude's reconciliation, but treat a fully-populated `audit-log decision`
column as the run having done its job, not as something to redo from
scratch.

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
