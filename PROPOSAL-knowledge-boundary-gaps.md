# Proposal: closing the knowledge-boundary gaps

**Status: draft, for review — nothing in this document has been implemented.** This is a working
document, not part of PrivacyFence's permanent documentation set; delete it once its items have
been triaged (accepted/rejected/deferred) and, where accepted, turned into real issues or PRs.

Each item below started as a factual observation in
[`docs/claude-knowledge-boundary.md`](docs/claude-knowledge-boundary.md) (a category-policy
bypass, a scope gap, or a leak). This document turns each one into a concrete change proposal:
current behavior, why it's worth addressing, a proposed change, an implementation sketch, and the
specific decision(s) that need a call before any of this is built.

---

## 1. `gmail_list_messages` bypasses the `metadata` category

**Current behavior:** setting `privacy.categories.metadata: block` stops `subject`/`sender`/
`recipients`/`date` from reaching Claude via `gmail_get_message`/`gmail_get_thread` — but
`gmail_list_messages` (the auto search tool) returns raw `subject`/`sender`/`date` for every
matching message regardless, since it never calls `apply_text`.

**Why it matters:** a user who sets `metadata: block` almost certainly intends "Claude shouldn't
see who emails are from/to or what they're about" — not "Claude shouldn't see that a second time."
The category currently has no effect on the tool Claude will use most (search).

**Proposed change:** apply `apply_text("privacy", "metadata", ...)` to `subject`/`sender`/`date` in
`GmailConnector._list_messages` (`src/privacyfence/connectors/gmail.py`), the same call already
used in `_get_message`/`_get_thread`.

**Implementation sketch:** small, localized change — wrap each summary dict's three fields before
returning from `_list_messages`. No new category, no schema change.

**Decision needed:** this makes search itself much less useful when `metadata` isn't `allow` —
redacted/blocked subjects and senders mean Claude can't meaningfully pick which message to open
next. Confirm that trade-off is intended before implementing (vs., e.g., leaving search
metadata-only and reserving `metadata: block` for the full-fetch tools only, which is today's
behavior).

---

## 2. `gmail_list_message_attachments` bypasses the `attachments` category

**Current behavior:** `settings.yaml.example` defaults `attachments: block`, and that does empty
the attachments list embedded in `gmail_get_message`/`gmail_get_thread` — but the dedicated auto
attachment-listing tool always returns full `name`/`mime_type`/`size`, unaffected by that same
setting.

**Why it matters:** this is the more clear-cut of the two Gmail gaps — the shipped default
(`block`) is silently not enforced by one of the tools it's supposed to cover.

**Proposed change:** apply `apply_list("privacy", "attachments", attachments)` in
`GmailConnector._list_message_attachments` before returning, mirroring `_get_message`.

**Implementation sketch:** one-line change; `attachments` is already list-shaped, so `redact` and
`block` both already resolve to "empty the list" everywhere else this category is used — no new
behavior to design, just apply the existing function at this call site too.

**Decision needed:** none functionally — this looks like a straightforward bug fix, matching
existing behavior at the other two Gmail attachment call sites. Worth confirming there's no
intentional reason `gmail_list_message_attachments` was left auto-unfiltered (e.g. "listing what's
attached, without content, was judged always-safe" — if so, this item should be rejected rather
than fixed).

---

## 3. Drive's `file_list` and `file_metadata` are independent knobs over overlapping fields

**Current behavior:** a file's `name`/`owners` appear in both `drive_list_files`'s results
(governed by `file_list`) and `drive_get_file_metadata`'s result (governed by `file_metadata`).
Blocking one doesn't block the other — `file_metadata: block` with `file_list: allow` still lets
that same name/owner pair reach Claude through `drive_list_files`.

**Why it matters:** someone configuring `file_metadata: block` expecting "Claude can't learn file
names/owners" would reasonably assume that covers both tools; it silently doesn't.

**Proposed change — two options, need a decision between them:**
- **(a) Documentation only.** Leave the two categories independent (they arguably cover distinct
  use cases — bulk listing vs. single-file lookup) and just document the overlap loudly (this
  proposal + a `settings.yaml.example` comment) so operators configure both consistently.
- **(b) Config-time consistency warning.** At daemon startup (or in the menu bar's settings
  validation), warn if `file_metadata` is more restrictive than `file_list` (or vice versa), e.g.
  "`file_metadata` is set to block, but `file_list` still allows the same file names/owners through
  `drive_list_files`." No runtime filtering behavior changes — this only surfaces the
  misconfiguration.

**Decision needed:** pick (a) or (b). (b) is more helpful but is new surface area (a config
linter); (a) is nearly free. Merging the two categories into one is **not** recommended — they
genuinely gate different tools and a operator might legitimately want list results filtered but
single-file lookups open, or vice versa.

---

## 4. Contacts and Tasks have no read-side gate, including free-text `notes`

**Current behavior:** `contacts_list`/`contacts_search`/`contacts_get` and
`tasks_list_tasks`/`tasks_get_task` are all auto-approved, with no category schema at all (Contacts
and Tasks aren't among the three connectors `privacy_filter.py` knows about) — every field,
including the free-text `notes`/biography field, reaches Claude unconditionally.

**Why it matters:** `notes` is the one field on both connectors that can hold arbitrary
user-authored personal content (a contact's biography, a task's private notes) — closer in kind to
Gmail's `body` or Drive's `file_content` than to structural metadata like a display name. It's
currently in the same no-gate bucket as `display_name`/`title`.

**Proposed change:** extend `privacy_filter.py`'s (currently hardcoded, three-group) scope with two
new groups — `contacts_privacy` and `tasks_privacy` — each with a single `notes` category, and wire
`apply_text("contacts_privacy", "notes", ...)` / `apply_text("tasks_privacy", "notes", ...)` into
the relevant fields in `contacts.py`/`tasks.py`'s existing auto methods. This does **not** add a new
approval gate (both connectors stay auto, matching `tasks.py`'s documented "low-sensitivity
metadata" design intent) — it only gives operators a per-field redact/block knob for the one field
that can carry unstructured personal data.

**Implementation sketch:**
- `privacy_filter.py`: add `"contacts_privacy"` and `"tasks_privacy"` to the `_GROUPS` dict parsed
  in `init_privacy_filter()`.
- `settings.yaml.example`: document the two new groups alongside the existing three, each defaulting
  `notes: block` to match the conservative posture the other three groups ship with.
- `contacts.py`: apply the filter to `Contact.to_dict()["notes"]` in `_contacts_list`/
  `_contacts_search`/`_contacts_get`.
- `tasks.py`: apply the filter to `Task.notes` in `_run` (used by `tasks_list_tasks`/
  `tasks_get_task`) and in `_serialize`.

**Decision needed:** this is the largest-scope item here — it widens `privacy_filter.py`'s
explicitly-documented "deliberately narrow" scope from three connectors to five. Confirm that's the
right lever before building it, versus the lighter-weight alternative of adding a single boolean
`tasks.hide_notes` / `contacts.hide_notes` setting instead of a full category schema (less
consistent with the rest of the codebase, but touches less shared machinery).

---

## 5. Gmail's `snippet` and Confluence's `excerpt` leak content through auto search

**Current behavior:** `gmail_list_threads`'s `snippet` field is a genuine (if short) excerpt of the
last message's body, straight from the Gmail API — despite the tool's own description claiming "no
body content is returned." `confluence_search`/`confluence_cql_search`'s `excerpt` field is more
substantial: built specifically to show *why* a page matched, it can span a full sentence or two of
page content, and Confluence has no category schema at all today (it's outside
`privacy_filter.py`'s three-connector scope).

**Why it matters:** both tools are otherwise the "search returns metadata only, drill in via a
gated tool for content" pattern that Gmail/Drive/Jira follow — these two are the exceptions, and
neither is currently documented as configurable.

**Proposed change — two sub-items, independent decisions:**
- **Gmail `snippet`:** reuse the existing `privacy.body` category — apply
  `apply_text("privacy", "body", snippet)` in `_list_threads` (`gmail.py`). Same category already
  governs the full body in `gmail_get_message`; this makes the snippet respect the same setting
  rather than being a separate, currently-unguarded leak of the same underlying content.
- **Confluence `excerpt`:** no existing category to reuse (Confluence has none). Two options:
  - **(a)** extend `privacy_filter.py` with a fourth-turned-fifth-or-sixth group,
    `confluence_privacy`, with a `search_excerpt` category — consistent with the rest of the
    codebase's pattern, but another scope extension (stacks with item 4 above).
  - **(b)** simpler: unconditionally drop the `excerpt` field from `confluence_search`/
    `confluence_cql_search`'s returned results (keep title/space/id/type only), forcing a
    `confluence_get_page` approval to see any actual content. No config knob, but no new scope
    either.

**Decision needed:** for Confluence, choose (a) — configurable, more consistent, more code — or (b)
— a flat behavior change, no configurability, minimal code. Also confirm whether Gmail's fix should
ship independently of Confluence's (they're unrelated connectors and don't need to land together).

---

## 6. `calendar_get_free_busy` can return full event titles, not just busy/free blocks

**Current behavior:** the tool is auto-approved and, whenever the authenticated account already has
calendar-read access to a queried colleague, returns that colleague's full event `title`/time/
`status` rather than a plain busy/free block. It only falls back to free/busy-only when access
isn't available.

**Why it matters:** scheduling queries across a team can surface meeting titles (which can
themselves be sensitive — "1:1: performance concerns", interview panel names, etc.) without ever
hitting a review gate, for every colleague the user happens to already have calendar visibility
into.

**Proposed change:** add a single boolean setting, e.g. `calendar.free_busy_full_event_details`
(under a `calendar` section of `settings.yaml`, no new category schema needed — this is a binary
switch, not a multi-category group), that when `false` forces `calendar_get_free_busy` to always
return free/busy-only blocks, even when full event access is available.

**Implementation sketch:** `calendar.py`'s `_get_free_busy` already branches per-colleague on
`source == "events"` vs `"free_busy"` (from `CalendarClient.get_colleagues_schedule`); gate that
branch on the new setting, or have the setting suppress `title`/other event fields from the
`"events"`-sourced results before returning.

**Decision needed:** what should the default be? `true` (today's behavior, full details when access
allows — no behavior change out of the box) vs. `false` (more conservative default, matches the
"private by default" posture `default_policy: block` uses elsewhere, but changes default behavior
for existing installs).

---

## Items reviewed and not recommended for action

For completeness, three other observations from the original list were assessed and are **not**
proposed as changes:

- **Search tools split into two families** (Gmail/Drive/Jira/Confluence's list/search tools stay
  metadata-only and auto; Slack/Telegram/Salesforce gate their content-bearing search). This
  follows a consistent principle — auto only when structural metadata, gate when actual content is
  returned — and needs no change (Confluence's `excerpt` is the one real exception, covered in
  item 5).
- **Downloads never return bytes to Claude** (`gmail_download_attachment`, `drive_download_file`).
  This is intentional and consistent across both connectors. One adjacent, smaller idea worth a
  quick gut-check: the approval copy on these two tools currently reads like a content-disclosure
  decision, when it's really "may PrivacyFence write this file to local disk." A one-line wording
  tweak to the popup's `details_text` (not a behavior change) could make that clearer, if worth the
  churn — flagging it here rather than recommending it outright, since it's copy, not a gap.
- **Metadata a gated tool "reveals" is often already known** (e.g. `gmail_download_attachment`'s
  from/subject/size were already available via the auto attachment-listing tool). This is a
  description of how the two tools compose, not a bug — no change proposed.
