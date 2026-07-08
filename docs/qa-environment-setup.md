# QA Environment Setup (for `connector-qa-testing.md`)

The 2026-07-08 manual run ([`connector-qa-testing.md`](connector-qa-testing.md))
left several branches **untestable** — not broken, just missing a fixture:

| Gap | Why it was untestable |
|---|---|
| Gmail `trusted_sender_domain` auto-accept | Rule was set to `slack.com`; mailbox has no mail from that domain |
| Gmail Deny vs. size-truncation | Indistinguishable from the tool result alone when the Deny target is large |
| Slack "different channel" review prompt | Workspace only had 1 channel |
| Slack `slack_get_thread_replies` | No threaded message existed anywhere |
| Calendar `calendar_list_rooms` | No Workspace admin / Admin SDK scope |
| Contacts `source="directory"` | No Workspace directory colleagues to return |
| Telegram "Saved Messages" | Chat had never been opened, so it doesn't appear in `list_chats` |
| Salesforce non-empty report / real record | Dev org has zero rows, so success paths were never actually exercised |
| Most `auto_accept.py` rules (calendar, Jira, Confluence, Telegram, Salesforce, Contacts) | README documents them; the test prompt never exercised them |

This doc is a **one-time (or occasional) setup checklist** to close those gaps.
Run through it once; the fixtures it creates are durable and get *reused*, not
recreated, by every future run of the test prompt. Per-run test artifacts
(drafts, events, issues, files…) are still created fresh each time — see
"Idempotency" at the bottom.

Everything named here uses the prefix **`PFQA`** (Jira/Confluence keys) or
**`PrivacyFence QA —`** (titles) so it's grep-able and obviously safe to
touch. Don't reuse your real `KAN` Jira project or `OOP` Confluence space as
the *target* of QA writes — keep them only as the "different
project/space, should still prompt" contrast case, which they already are.

---

## 1. Gmail

Nothing to provision — but re-point the auto-accept rule at a domain your
inbox actually receives mail from, instead of `slack.com`:

1. Run `gmail_list_messages` (or just look at your inbox) and pick any
   sender domain you get recurring mail from — a newsletter, a receipt
   sender, a notification address. The original run already had `netflix.com`
   and a Revolut domain on hand; either works.
2. In `~/.privacyfence/config/settings.yaml`, change:
   ```yaml
   auto_accept_rules:
     gmail.read_message:
       - rule: trusted_sender_domain
         value: [netflix.com]   # ← whatever domain you actually receive mail from
   ```
3. Keep `slack.com` too if you want — rules are a list, more than one can
   coexist — but don't rely on it alone.

For the **Deny test** (Phase 1, step 3): pick a small message with no large
attachments specifically for that step. A big message makes a Deny
indistinguishable from a size-truncation error in Claude's tool result; a
small one removes that ambiguity so the audit log and the chat transcript
agree.

## 2. Drive & Sheets

1. Create one durable folder: **`PrivacyFence QA Sandbox`** in "My Drive".
   Note its file ID.
2. Add an auto-accept fixture so `drive.read_file_contents` /
   `drive.download_file` have something to match against:
   ```yaml
   auto_accept_rules:
     drive.read_file_contents:
       - rule: approved_folder
         value: ["<QA Sandbox folder id>"]
   ```
3. Everything the test prompt creates in Drive/Sheets from now on should be
   created **inside this folder** (`parent_folder_id`), including
   `drive_upload_file` / `drive_write_doc_content` / `drive_move_file` /
   `drive_download_file` — those four were explicitly skipped in the
   original run "unless doable safely against your test file" — this folder
   is that safe place, so they no longer need to be skipped.
4. Drive has no bulk-empty-trash-by-search tool, so periodically empty Drive
   trash by hand; items moved to trash there auto-purge after 30 days
   regardless.

## 3. Slack

You currently have exactly one channel (`C0BF29YL6EQ`, already in
`slack.read_messages: approved_channel`). Create a second one so the
contrast case in Phase 3 step 3 has something to hit:

1. Create a channel, e.g. `#privacyfence-qa-control`. Join it. **Do not**
   add it to `approved_channel` — it exists specifically to *not* match.
2. Post one message in it, then reply to that message in-thread (replying
   from your own account is fine — Slack doesn't require a second person for
   a thread to exist). This gives `slack_get_thread_replies` permanent,
   reusable content instead of depending on whatever Phase 3 step 3/4
   happens to surface that run.
3. Optional: rename your existing approved channel to something obviously
   QA-owned (e.g. `#privacyfence-qa`) if it isn't already, so both QA
   channels are easy to tell apart from real workspace channels in the
   final report.

## 4. Calendar

`calendar_list_rooms` needs **Google Workspace** (not a consumer Gmail
account) **and** admin rights. Check which situation you're in first:

- **Consumer Gmail / no Workspace admin access:** this is a permanent,
  environment-level limitation, not something any amount of local setup
  fixes. Tell the test prompt to treat the resulting error as expected and
  stop re-flagging it as a defect every run (see the updated prompt's Phase
  4 step 1).
- **Workspace admin access available:**
  1. In Google Cloud Console → APIs & Services → Library, enable **Admin SDK
     API** (see [`google-cloud-setup.md`](google-cloud-setup.md)).
  2. The OAuth consent screen needs
     `admin.directory.resource.calendar.readonly` grantable — this is
     requested at runtime by `calendar_client.py`, nothing to add manually
     in the Cloud Console scopes list.
  3. In the Google Admin console: **Directory → Buildings and resources →
     Calendar resources → Add resource**. Create at least one, e.g.
     `PrivacyFence QA Room A`.
  4. In PrivacyFence: **Connectors → Calendar → Reconnect…** so the token
     picks up the new admin scope.

No other Calendar fixture is needed: the event Phase 4 step 3 creates each
run already satisfies the `i_am_organizer` auto-accept rule (you organize
anything you create), so that rule gets exercised for free.

## 5. Contacts

`source="directory"` coming back empty is only a gap if you *do* have
Workspace colleagues and expected to see them. If this is a personal/solo
account, empty is the correct, permanent answer — stop treating it as
untested and start treating it as a confirmed invariant.

If you do have Workspace colleagues: no setup needed, `contacts_list
source="directory"` will simply return them next run.

Add a fixture for the never-tested `no_contact_info_change` rule:
```yaml
auto_accept_rules:
  contacts.edit:
    - rule: no_contact_info_change
```
This is safe to enable permanently — it only auto-accepts edits that don't
touch `emails`/`phones`, e.g. appending `(PrivacyFence QA test)` to a name or
note field. An edit that *does* touch email/phone should still prompt,
giving you a same-tool contrast pair in one phase.

## 6. Google Tasks

No gaps — already fully covered by the original prompt.

## 7. Telegram

`telegram_list_chats` only returns chats you've actually opened at least
once, and "Saved Messages" is no exception:

1. Open Telegram (phone or desktop) and send yourself **one** message in
   Saved Messages, manually, right now. This is a one-time action — once it
   exists it stays in `list_chats` forever.
2. Create (or repurpose) one more low-stakes chat — a private group with
   just yourself, or a chat with a throwaway/test contact — to use as the
   `approved_chats` fixture:
   ```yaml
   auto_accept_rules:
     telegram.read_chat_messages:
       - rule: approved_chats
         value: ["<chat_id of that group/chat>"]
   ```
   Get the numeric `chat_id` from `telegram_list_chats` after step 1. Saved
   Messages itself is a fine choice here too, since it's always safe to
   auto-accept reads of your own messages to yourself.
3. Send at least one message into whichever chat you didn't just approve, so
   `telegram_search_messages` has something to actually find rather than
   coming back empty.

Because whether a Cowork review popup appeared for `telegram_get_messages` /
`telegram_search_messages` is genuinely ambiguous from the tool result alone
(see the original report's Phase 7 verdict), this is a process fix, not an
environment fix: watch for the popup yourself and say out loud whether it
appeared, rather than letting Claude infer it from success/failure.

## 8. Salesforce

The dev org currently has zero data rows anywhere reachable, so every
Salesforce call in the original run either 404'd, came back empty, or hit
`FORBIDDEN`. None of that is a gate bug, but it also means the *success*
path (real rows, real record) has never actually been exercised. Fix:

1. **Setup → Object Manager → Account** (or any object you're comfortable
   using) → create 2–3 sample records prefixed `PrivacyFence QA — `.
2. **Reports → New Report**, base it on that object, name it
   `PrivacyFence QA Report`. Note its report ID (from its URL).
3. Add auto-accept fixtures:
   ```yaml
   auto_accept_rules:
     salesforce.run_report:
       - rule: approved_report_ids
         value: ["<PrivacyFence QA Report id>"]
     salesforce.read_record:
       - rule: approved_object_types
         value: [Account]
   ```
4. Keep at least one report/object type you *don't* add here (or that your
   user genuinely can't access) as the contrast case — the original run's
   `FORBIDDEN` reports already serve that purpose, no new setup needed.

## 9. Jira

`KAN` (your existing real project) stays as-is — use it only as the
"different project, should still prompt" contrast. Create a dedicated QA
project instead of writing more test issues into `KAN`:

1. Create a Jira project, key **`PFQA`**, any template (Kanban is fine —
   matches what `KAN` already uses).
2. Add the fixture:
   ```yaml
   auto_accept_rules:
     jira.read_issue:
       - rule: approved_project_keys
         value: [PFQA]
   ```
3. `i_am_reporter` / `i_am_assignee` need no setup — any issue you create in
   `PFQA` satisfies both automatically. If a second Jira user exists in your
   site, optionally reassign one test issue to them to get a contrast case
   for `i_am_assignee`; skip this if you're the only user.

## 10. Confluence

Same pattern as Jira. `OOP` (existing space) becomes the contrast case;
create a dedicated space for actual QA writes:

1. Create a Confluence space, key **`PFQA`**.
2. Add the fixture:
   ```yaml
   auto_accept_rules:
     confluence.read_page:
       - rule: approved_space_keys
         value: [PFQA]
   ```
3. `i_am_author` needs no setup — any page you create in `PFQA` satisfies it
   automatically.
4. Confirm the content-path fix (commit `34e7108`, v1→v2 migration) is
   actually deployed to the daemon you're testing against before running —
   `confluence_get_page`/`create_page`/`update_page` were completely broken
   before that fix, and re-running this test against a stale daemon build
   will reproduce the old "Gone" bug, not a new one.

---

## Consolidated `auto_accept_rules` block

Merge this into `~/.privacyfence/config/settings.yaml` (adjust the IDs —
they're placeholders until you've done the steps above):

```yaml
auto_accept_rules:
  gmail.read_message:
    - rule: trusted_sender_domain
      value: [netflix.com]          # replace with a domain you actually receive mail from
  drive.read_file_contents:
    - rule: approved_folder
      value: ["<QA Sandbox folder id>"]
  sheets.read_values:
    - rule: approved_spreadsheet
      value:
        - spreadsheet_id: "<kept from a prior run, harmless>"
          tab: Sheet1
  slack.read_messages:
    - rule: approved_channel
      value: ["C0BF29YL6EQ"]
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
```

Restart the daemon (or trigger a hot reload, if you've already used the
"Accept All" popup once — that path calls `reload_rules()` for you) after
editing this by hand.

---

## Idempotency: environment fixtures vs. per-run artifacts

Two different lifetimes are at play, and conflating them is what caused the
"leftovers" problem in the two-stage 2026-07-08 run:

- **Environment fixtures** (this doc): the `PFQA` Jira project/Confluence
  space, the Drive Sandbox folder, the two Slack channels, the Telegram
  chats, the Salesforce sample records/report. Created **once**, looked up
  by fixed name/key on every subsequent run, never recreated.
- **Per-run artifacts** (the test prompt itself): drafts, events, one-off
  issues/pages/files. These should carry a run-scoped identifier so
  repeated runs don't produce indistinguishable duplicates — see the
  updated [`connector-qa-testing.md`](connector-qa-testing.md), which now
  stamps every title with a timestamp and ends with a teardown phase.

If you skip a fixture step above (e.g. you have no Workspace admin access
for Calendar rooms), that's a permanent, known gap — record it once instead
of having the test re-discover and re-report it every single run.
