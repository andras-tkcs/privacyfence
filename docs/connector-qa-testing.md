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
- A terminal open on `~/.privacyfence/logs/audit/<current-ISO-week>.jsonl` (or
  the auto-generated `.xlsx` after a daemon restart) to cross-reference
  decisions afterward — **Claude cannot see whether a popup appeared or what
  you clicked**, only whether the tool call ultimately succeeded or errored.
  The audit log is the only ground truth for actual gate/UI behavior.

## The prompt

Paste this as a single message into the Cowork conversation. It walks every
connector in dependency order (list/search before get, get before write),
deliberately hits any auto-accept rules you have configured back-to-back with
a contrasting call that should still prompt, and ends with a self-report you
diff against the audit log.

Before running it, swap in specifics for your own environment: your
configured auto-accept rules (e.g. a trusted sender domain, an approved Slack
channel/chat) won't match the placeholders below, so replace those steps with
whatever rules `~/.privacyfence/config/settings.yaml` actually has, or skip
them if none are configured yet.

````markdown
You are connected to my personal accounts (Gmail, Drive/Sheets, Slack, Calendar,
Contacts, Tasks, Telegram, Salesforce, Jira, Confluence) through the `privacyfence`
MCP server. I'm QA-testing PrivacyFence itself, not asking you to do real work — so
go connector by connector, in the order below, and actually call the tools rather
than describing what you'd do.

Ground rules:
- Any content you write, send, or create anywhere must be obviously a test artifact:
  prefix titles/subjects/messages with `PrivacyFence QA test —` and add "safe to
  ignore/delete" somewhere in the body. Never edit or send anything real.
- For destination-picking (which channel to message, which Jira project/Confluence
  space to write to, which contact to touch), prefer sending to myself
  (Slack self-DM, Telegram "Saved Messages", a file/sheet/event/page you just
  created) over touching someone else's data. If no safe destination is obvious
  for a given tool, stop and ask me which project/space/channel to use before
  calling it — don't guess.
- After each numbered phase, pause and give me a one-line status ("Phase 3 done,
  4 tool calls, 2 required approval") before moving to the next phase, so I'm not
  flooded with 40 popups back to back.
- Keep a running table as you go: `tool name | gate observed (silent / Cowork
  review / native popup) | decision | notes`. Print the full table at the very end.

I (the human) will be watching for the approval prompts as they appear. Some
instructions below tell you to expect a prompt and note what I'll do with it
(Accept / Deny / Show Details) — do that, then report back what actually happened
in the tool result.

---

## Phase 1 — Gmail
1. `gmail_list_messages` and `gmail_list_threads` (expect: silent, no prompt).
2. Pick any recent message and call `gmail_get_message` on it. **I will click
   Accept.** Confirm you received the body.
3. Pick a different recent message and call `gmail_get_message` again. **I will
   click Deny this time.** Confirm you get an error, not data — and don't
   fabricate a fallback answer.
4. Pick a message that has a thread with 2+ messages, call `gmail_get_thread`.
   **I will click "Show Details"** instead of Accept/Deny directly, then approve
   from the native popup. Report what came back.
5. `gmail_list_message_attachments` on any message with attachments (expect:
   silent). Then `gmail_download_attachment` on one — this is `review` gated,
   I'll Accept.
6. Auto-accept rule check: [replace with your own configured rule, e.g. find a
   message from a sender domain in your `trusted_sender_domain` list] and call
   `gmail_get_message` on it. This should NOT prompt me at all. Tell me whether
   a prompt appeared or not.
7. `gmail_create_draft` — draft to myself, subject `PrivacyFence QA test — safe
   to delete`. This is popup-gated, I'll Accept.
8. `gmail_reply_draft` on the thread from step 4, again clearly marked as a test.
   Popup, I'll Accept.
9. `gmail_add_label` on the test message from step 7/8 (any existing label).
   Popup, I'll Accept.
10. Skip `gmail_archive_message` and `gmail_remove_label` unless I tell you
    otherwise in chat.

## Phase 2 — Drive & Sheets
1. `drive_list_files`, `drive_get_file_metadata`, `drive_list_folder`,
   `drive_list_shared_drives` (expect: all silent).
2. `drive_create_blank_file` named `PrivacyFence QA test file — safe to delete`
   (expect: silent, auto).
3. `drive_get_file_content` on that new file — review gate, I'll Accept.
4. `drive_write_file_content` on it, write a short test sentence — popup, Accept.
5. `drive_add_comment` on it, any test comment — popup, Accept.
6. `drive_sheets_create` named `PrivacyFence QA test sheet — safe to delete`
   (expect: silent, auto).
7. `drive_sheets_get_metadata` on it (expect: silent).
8. `drive_sheets_get_values` on a small range like `Sheet1!A1:B2` — review gate,
   I'll click **"Accept All"** this time. Tell me exactly what rule text/scope
   it proposes (expect: scoped to this spreadsheet + tab, not a broad rule).
9. `drive_sheets_write_range` — write `A1: "hello"`, `A2: "=1+1"` to prove
   formulas evaluate. Popup, Accept.
10. `drive_sheets_add_sheet` — add a tab named `Extra`. Popup, Accept.
11. `drive_sheets_rename_sheet` — rename `Extra` to `TO BE DELETED - Extra`.
    Popup, Accept.
12. `drive_sheets_format_range` — bold `A1:B2`. Popup, Accept.
13. Skip `drive_download_file`, `drive_upload_file`, `drive_write_doc_content`,
    `drive_move_file` unless doable safely against your test file/sheet —
    otherwise tell me it's skipped and why.

## Phase 3 — Slack
1. `slack_list_channels` (expect: silent).
2. Auto-accept rule check: `slack_get_channel_history` on [replace with your own
   `approved_channel`-listed channel ID]. This should NOT prompt me. Confirm.
3. `slack_get_channel_history` on a **different** channel — should prompt for
   review. I'll Accept.
4. `slack_search_messages` with any query — review gate, Accept.
5. If step 3/4 surfaced a message with replies, `slack_get_thread_replies` on
   it — review gate, Accept.
6. `slack_send_message` to my own self-DM only, test text. Popup, Accept.

## Phase 4 — Calendar
1. `calendar_list_calendars`, `calendar_list_events`, `calendar_get_free_busy`,
   `calendar_list_rooms` (expect: all silent).
2. Pick any existing event, `calendar_get_event_details` — review gate, Accept.
3. `calendar_create_event` — title `PrivacyFence QA test event — safe to
   delete`, date far in the future, no attendees, no Meet link. Popup, Accept.
4. `calendar_update_event` on the event you just created. Popup, Accept.
5. Note for me: there's no delete-event tool, so I'll remove it manually after.

## Phase 5 — Contacts
1. `contacts_list`, `contacts_search`, `contacts_get` (expect: all silent).
2. Ask me which contact is safe to touch before calling `contacts_update` —
   don't pick one yourself. Append ` (PrivacyFence QA test)` to a low-stakes
   field and tell me the exact before/after. Popup, Accept.

## Phase 6 — Google Tasks
Reads are unconditionally auto-accepted; writes are `popup`-gated like every
other connector's writes. Expect **zero prompts** for step 1, and a popup for
each of steps 2, 4, 5, and 6:
1. `tasks_list_task_lists`, `tasks_list_tasks` (expect: silent).
2. `tasks_create_task` — title `PrivacyFence QA test task — safe to delete`. Popup, Accept.
3. `tasks_get_task` on it (expect: silent).
4. `tasks_update_task` (change the title slightly). Popup, Accept.
5. `tasks_complete_task`, then `tasks_uncomplete_task`. Popup, Accept each.
6. `tasks_move_task` (move it within the same list). Popup, Accept.

## Phase 7 — Telegram
1. `telegram_list_chats` (expect: silent — the only genuinely unconditional one).
2. `telegram_get_messages` on any chat — tell me whether a Cowork review
   prompt appeared. I'll Accept if it does.
3. `telegram_search_messages` with any query — same check.
4. `telegram_send_message` to "Saved Messages" only, test text. Popup, Accept.

## Phase 8 — Salesforce
1. `salesforce_list_reports` (expect: silent).
2. `salesforce_run_report` on any report — review gate, Accept.
3. From its rows, pick a record and call `salesforce_get_record` — review
   gate, Accept. (No write tools exist for Salesforce in this build.)

## Phase 9 — Jira
1. `jira_list_projects`, `jira_search_issues` (expect: both silent).
2. Pick any existing issue, `jira_get_issue` — review gate, Accept.
3. Ask me which project key is safe for a test issue before creating anything.
   Then `jira_create_issue` — summary `PrivacyFence QA test issue — safe to
   delete/close`. Popup, Accept.
4. `jira_add_comment` on it, test comment. Popup, Accept.
5. `jira_update_issue` on it (e.g. change description). Popup, Accept.

## Phase 10 — Confluence
1. `confluence_list_spaces`, `confluence_search`, `confluence_cql_search`,
   `confluence_list_pages` (expect: all silent).
2. Pick any existing page, `confluence_get_page`(`_by_title`) — review gate,
   Accept.
3. Ask me which space is safe for a test page before creating anything. Then
   `confluence_create_page` titled `PrivacyFence QA test page — safe to
   delete`. Popup, Accept.
4. `confluence_update_page` on it, minor edit. Popup, Accept.

---

## Final report
Print the full running table (tool | gate observed | decision | notes). Then:
- Call out any tool whose observed gate didn't match what I told you to expect.
- Give the Phase 7 (Telegram) discrepancy verdict, if applicable.
- List every test artifact you created so I know what to clean up.

I'll separately check `~/.privacyfence/logs/audit/<this-week>.jsonl` myself to
cross-reference every call's logged decision — you don't need to access that file.
````

## Reading the results

Claude's self-report tells you whether each call **succeeded or errored** —
not whether a popup rendered, or what you clicked. Treat its "gate observed"
column as a hypothesis, not ground truth: cross-reference the audit log for
each call's actual `decision` field (`accepted` / `denied` / `auto_accepted`)
before concluding a gate misbehaved.

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
