# Design: Preflight Policy Checks for Scheduled Cowork Tasks

**Status: implemented.** `privacyfence_check_policy`, `privacyfence_begin_unattended_session`,
`privacyfence_end_unattended_session`, and the `unattended_sessions.enabled` config gate all exist
in `gate.py` / `auto_accept.py` / `ipc.py` / `ipc_server.py` / `bridge_main.py` / `menu_bar.py` as
described below. This document is kept as the design record — see
[`docs/TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks) for the
user-facing reference.

## Problem

Every `review`- or `popup`-gated tool call blocks on a native macOS dialog (`gate.gated_call`,
via `approval_popup.py`'s `osascript` calls) until a human clicks a button. That's the right
model for an interactive Claude Desktop/Cowork session — a person is at the keyboard and the
popup is the point.

Claude Cowork can also run **scheduled tasks** (Routines firing on a cron/trigger) with nobody
necessarily watching. If a scheduled task calls a tool that lands on `review` or `popup` and no
configured auto-accept rule matches, `gated_call` opens a popup and waits — indefinitely, since
there is no timeout anywhere in that path. Two concrete consequences, both grounded in the
current code:

- **The task hangs.** `gated_call` (`src/privacyfence/gate.py`) only returns once a human decides.
  A scheduled run that hits one unmatched `review`/`popup` call effectively never completes; it
  just sits until the Cowork client's own tool-call timeout gives up, which is an ungraceful
  failure, not "the task did the safe part and skipped the rest."
- **It blocks everyone else, too.** All native dialogs share one lock:
  `_popup_lock = asyncio.Lock()  # only one native dialog on screen at a time` (`gate.py`). A
  3 a.m. scheduled task that opens a popup nobody answers holds that lock until someone clicks
  it — which means an unrelated *interactive* approval a human tries to answer later that morning
  queues up behind a dialog for a task they may not even remember scheduling.

The current model gives Claude no way to know, before calling a tool, whether that call would
sail through (`auto`, or a matching auto-accept rule) or need a human. That's the gap this
document addresses: **an addition Claude can use to tell, ahead of time, whether a given call
will need acceptance or will be auto-accepted**, so a scheduled task can plan around it instead
of discovering it by hanging.

## Design goals

- Claude can ask, before calling a gated tool, whether that specific call is expected to need a
  human.
- Interactive sessions are unaffected — a human at the keyboard still sees exactly the popups
  they see today.
- If a scheduled run hits something genuinely unpredictable (see [the "unknown" case](#the-honest-answer-is-sometimes-unknown) below),
  it fails that one step fast and cleanly, rather than blocking a shared lock forever.
- **The authorization boundary does not move.** This proposal adds *visibility* into the existing
  gate and a *fail-fast* behavior for the case where nothing else applies — it never gives Claude
  a new way to get something auto-accepted that the existing rule engine wouldn't already accept.
  That's the same principle already stated in the README: "The AI assistant is not the
  authorization boundary."
- No changes required to individual connector code (`connectors/*.py`).

## Part 1 — a static gate registry (prerequisite)

Right now, whether a tool is `auto`, `review`, or `popup` isn't recorded anywhere centrally — it's
implicit in which `gated_call(gate=...)` a connector method happens to call, or the absence of a
`gated_call` at all for unconditionally-auto tools. The only place this is written down today is
the hand-maintained connector tables in `docs/TECHNICAL_REFERENCE.md`, which is exactly why
`docs/connector-qa-testing.md` already calls out checking for "drift between what's documented
and what's actual" as a QA step.

Both this proposal and that existing pain point are fixed by the same addition: a static
`TOOL_TO_GATE: dict[str, str]` next to the existing `TOOL_TO_OPERATION` in `auto_accept.py`,
mapping every tool name to `"auto"`, `"review"`, or `"popup"`. It's a plain dict literal, so a
unit test can assert it's a superset of `TOOL_TO_OPERATION`'s keys and stays in sync with the
`gate=` argument each connector actually passes (e.g. by asserting against the literal in each
`connectors/*.py` call site, the same way `tests/helpers.py::assert_all_tools_leave_an_audit_trail`
already mechanically checks a different invariant). This becomes the source of truth
`docs/TECHNICAL_REFERENCE.md`'s tables are generated from or checked against, instead of being
maintained by hand twice.

## Part 2 — `privacyfence_check_policy`: a preflight tool

A new tool, exposed by the bridge alongside the dynamically-registered connector tools (added
directly in `bridge_main.py`'s `_register_tools`, not sourced from a connector manifest, since it
isn't backed by a real connector):

```
privacyfence_check_policy(connector: str, tool: str, args: dict) -> {
    "gate": "auto" | "review" | "popup",
    "verdict": "auto_accept" | "requires_review" | "unknown",
    "matched_rule": str | null,
    "reason": str,
}
```

Wire-level: a new IPC method `check_policy` (`ipc.py`, `ipc_server.py::_dispatch`), handled the
same way `manifest` is — it never reaches `connector.call()`, so it makes **no external API
call, opens no popup, and writes no audit-log decision entry** (see [Audit log](#audit-log) below
for what it does log). Claude can call it as often as it wants while planning a task without
generating noise or side effects.

### The honest answer is sometimes "unknown"

This is the part that has to be gotten right, or the tool overpromises. Auto-accept rules fall
into two groups, visible directly in `auto_accept.py`'s `AutoAcceptEvaluator._rule_*` methods:

- **Args-only rules** — evaluable from the call's arguments alone, before anything is fetched:
  `approved_folder`, `approved_sandbox_folder`, `approved_channel`, `approved_spreadsheet`,
  `approved_project_keys`, `approved_space_keys`, `approved_chats`, `approved_task_list`,
  `parent_folder_allowlist`, `label_name_allowlist`, `to_is_myself`, `approved_recipient_domain`,
  `dm_with_myself`, `no_contact_info_change`, `reply_in_existing_thread`, `personal_calendar`,
  `approved_object_types`, `approved_report_ids`, and so on.
- **Data-dependent rules** — read `ctx.raw_data`, i.e. the actual fetched object:
  `i_am_sender`, `i_am_owner`, `i_am_organizer`, `trusted_sender_domain`, `age_threshold_days`,
  `no_attachments`, `i_am_reporter`, `i_am_assignee`, `i_am_author`, `no_external_attendees`,
  `past_event`, `no_conferencing_link`, `public_channels_only`, `no_file_attachments`, etc.

For a **write** (`popup` gate), `raw_data` is usually built from the same `args` the write is
constructing, so nearly every write rule in the codebase today happens to be args-only — writes
are the case this tool serves best. For a **read** (`review` gate), several rules
(`i_am_owner`, `i_am_organizer`, `trusted_sender_domain`, …) only become knowable once the item
has actually been fetched — which is the read itself. A preflight check can't fetch the item
without doing the thing it's supposed to predict.

So `check_policy` evaluates a new function, `auto_accept.would_auto_accept_from_args(operation_key,
args, my_email) -> tuple[bool | None, str]`, that:

1. Checks the existing in-memory temp-accept state first — `evaluator._is_temp_accepted(operation_key,
   temp_accept_key(operation_key, ctx))` in `auto_accept.py` already only needs `ctx.args` (via
   `TEMP_ACCEPT_ELIGIBLE_OPERATIONS`' arg-name lookup), so a live "Accept for 5 min" window is
   just as knowable ahead of time as a standing rule. A hit → `verdict="auto_accept"`,
   `matched_rule="session_temp_accept"`.
2. Looks up the configured standing rules for that operation key.
3. Evaluates every rule tagged as args-only (a companion `ARGS_ONLY_RULES: set[str]` next to
   `TEMP_ACCEPT_ELIGIBLE_OPERATIONS`) against a `ReviewContext` built from `args` alone
   (`raw_data=None`). A match → `verdict="auto_accept"`, `matched_rule` set.
4. If nothing matched and every configured rule for that operation was args-only → the answer is
   final: `verdict="requires_review"`. There is nothing left that fetching the real data could
   change.
5. If nothing matched but at least one configured rule needs `raw_data` → `verdict="unknown"`,
   with `reason` naming which rule(s) are undetermined (e.g. `"i_am_owner not yet
   determinable — depends on the file's owner"`), so Claude knows the real call might still
   auto-accept, or might not.
6. For `review`-gated tools specifically, add one more caveat regardless of the above: the
   [PII detection gate](../docs/TECHNICAL_REFERENCE.md#pii-detection-gate) scans actual content
   and can force a popup even when a rule matches. `check_policy` cannot predict this — content
   doesn't exist yet at preflight time — so its response for any `review`-gated tool always
   includes a `pii_gate_may_apply: true` field alongside the verdict, however confident the rule
   match is.

A tool with `gate="auto"` in `TOOL_TO_GATE` (Part 1) always returns `verdict="auto_accept"`,
`matched_rule: null`, with no rule evaluation needed.

## Part 3 — unattended-session mode: fail fast instead of hang

`check_policy` narrows the "will I need a human" question but can't eliminate the `unknown`
bucket, and PII detection is unpredictable by construction. A scheduled task will still
occasionally hit a real gate it can't preflight around. The fix isn't to make that gate weaker —
it's to make what happens when nobody's there to answer it *fail loudly and immediately* instead
of hanging on the shared popup lock.

Two new bridge-exposed meta-tools, alongside `privacyfence_check_policy`:

- `privacyfence_begin_unattended_session()`
- `privacyfence_end_unattended_session()`

Claude calls `begin` itself, at the start of a run it knows was triggered by a schedule/Routine
rather than an interactive conversation — this is context only Claude has; nothing in the MCP
stdio transport tells the daemon *why* the bridge process was spawned. This sets a flag on the
daemon side, scoped to that one IPC connection (the bridge is a fresh process per Cowork task
already, per `bridge_main.py`'s docstring — "safe for Claude to kill and restart it at any
time" — so connection-scoped state maps cleanly onto "one scheduled task run").

**Opt-in, off by default.** Unlike `check_policy` (pure read of existing policy, safe to expose
unconditionally), `begin_unattended_session` changes *behavior* — it lets a Claude session
unilaterally switch its own connection from "block on a popup" to "deny without asking." That's
a decision an org should make deliberately, not something every install gets for free the moment
it upgrades. A new settings.yaml key, alongside `pii_detection.enabled`:

```yaml
unattended_sessions:
  enabled: false   # default: off
```

When `false`, the daemon still registers the meta-tool (so Claude gets a clear, actionable error
rather than "unknown tool") but `begin_unattended_session` returns an error explaining that an
admin needs to enable `unattended_sessions.enabled` in `settings.yaml` first. Turning it on is a
config-file edit, so it shows up the same way any other policy change does — in version control
for an org-distributed config, or as a deliberate local edit otherwise.

**Visible while it's running, not just after the fact.** The menu bar already lists live,
at-a-glance state (PII Detection Gate on/off, connector status). This gets the same treatment: a
menu bar item showing the count of connections currently in unattended mode (e.g. "1 unattended
session active"), sourced from the same per-connection flag `gated_call` checks — not a new
tracking structure, just surfacing what the daemon already knows. Clicking it can jump to the
audit log filtered to `denied_unattended` entries. This matters because unattended mode is the
one addition here that changes behavior invisibly from the human's point of view until they
happen to open the audit log — everything else in this proposal is either read-only
(`check_policy`) or scoped to a documented settings.yaml rule a human already wrote
(Part 4). A live indicator keeps that one exception from being a silent one.

With the flag set, `gated_call` (`gate.py`) changes exactly one thing: whenever it would
otherwise call `show_read_popup` / `show_popup` / `show_pii_confirmation_popup` and block, it
instead **denies immediately** — raises the same `RuntimeError("Request denied by user")` a real
click-Deny would raise, no dialog ever opens, and the audit entry records a new decision value,
`denied_unattended` (see [Audit log](#audit-log)), instead of `rejected` — a "no human was even
asked" outcome must stay visually distinct from "a human looked at this and said no." This
applies identically to a `review` call that matched an auto-accept rule but was still routed to
the popup because the PII gate fired — that path is exactly as "nobody's here to click it" as
any other.

Nothing about which calls *pass* changes. Every call that would auto-accept today still
auto-accepts identically in unattended mode; the flag only changes the failure mode for calls
that would otherwise open a dialog no one will answer, from "block forever, taking the shared
lock with it" to "fail this one step, right now, with a clear reason." That keeps `_popup_lock`
free for interactive approvals the whole time, and gives Claude an error it can act on mid-task
(skip the step, note it in the run's summary, keep going) instead of a silent stall.

## Part 4 — scoping a task's blast radius (the sandbox-folder idea)

`check_policy` and unattended mode handle *finding out* whether something needs a human; they
don't reduce *how often* something does. The other half — proposed by the stakeholder as "give
me a drive folder I can write to" — is to lean on the auto-accept rule system that already
exists, deliberately, for exactly this: give a scheduled task a narrow scope where everything it
plausibly needs to do is genuinely `auto`.

This needs no new mechanism, only a documented convention: when setting up a Routine, create (or
reuse) the tightest auto-accept scope the task's job requires — a single `approved_sandbox_folder`
Drive folder for a task that only writes generated reports, a single `approved_channel` for one
that only posts to one Slack channel, a single `approved_project_keys` for one Jira project — and
confirm the scope is actually sufficient by having Claude run `privacyfence_check_policy` against
its planned calls before scheduling the Routine for real, rather than discovering a gap in
production at 3 a.m.

## Putting it together

A nightly "triage new Jira tickets in project OPS" Routine would:

1. Call `privacyfence_begin_unattended_session()`.
2. For each planned step (e.g. `jira_update_issue` to set a priority), call
   `privacyfence_check_policy("jira", "jira_update_issue", {...})` first.
   - `verdict: "auto_accept"`, `matched_rule: "approved_project_keys"` → proceed, call the real
     tool; it auto-accepts exactly as predicted.
   - `verdict: "requires_review"` → skip that step, note it for the human's review in the task's
     summary rather than attempting a call known to fail.
   - `verdict: "unknown"` → attempt it if the task's risk tolerance allows; if it turns out to
     need a human, unattended mode denies it immediately (not a hang) and Claude reports that
     specific step as skipped.
3. Call `privacyfence_end_unattended_session()` when done, so a later interactive session on the
   same bridge connection (if any) isn't accidentally left in fail-fast mode.

## Audit log

Two additions to `audit_log.py`'s `AuditEntry` usage, both additive (no schema break):

- A `policy_check` decision value, recorded for `check_policy` calls — lightweight (no
  `pii_detected`/`auto_accept_rule` semantics beyond "what did we tell Claude"), useful for
  spotting a scheduled task that keeps probing something it never ends up being allowed to do.
- A `denied_unattended` decision value (Part 3), kept distinct from `rejected` so the weekly
  Excel export's Decisions sheet can show, at a glance, how many gate hits during scheduled runs
  had no human involved at all versus how many a human actively declined.

## Security note

This proposal does not add a new way to get a write or a sensitive read approved. Every path
through `gated_call` that currently ends in `auto_accepted` still requires the same matching
auto-accept rule it does today; `check_policy` only *reports* that fact earlier, and unattended
mode only changes what happens on the *deny* path when nobody is present to answer a popup. If
anything, unattended mode is stricter than today's behavior on the margin — a call that would
otherwise sit open until a human eventually notices and clicks Deny now fails immediately with
the same outcome, and does so without holding `_popup_lock` hostage from every other approval in
the meantime.

## Alternatives considered

- **Auto-detect "scheduled" from the MCP transport instead of self-declaration.** Rejected: stdio
  spawn gives the daemon no signal about *why* Claude started the bridge — that context (cron
  trigger vs. a person typing) exists only on Claude's side. Self-declaration via
  `begin_unattended_session` is the only place this information can come from; the trade-off is
  that it's advisory; Claude could fail to call it, or call it during an interactive session by
  mistake. That's acceptable because the flag never controls *authorization* — it only controls
  whether an unresolved `review`/`popup` hit hangs or fails fast — so misuse degrades UX (an
  interactive session gets a fast denial instead of a popup, or a scheduled task hangs instead of
  failing fast) rather than security.
- **Let scheduled tasks queue approvals for async, later resolution** (e.g. a push notification
  the human answers hours after the task ran) instead of failing fast. Rejected for this proposal:
  it needs a pending-approval handshake `gated_call`'s docstring explicitly says doesn't exist
  today ("There is no pending-approval handshake... Claude never holds a tool that can release
  gated data on its own"), and it means a scheduled task's *result* depends on a decision made
  after the task already finished, which doesn't compose with "the task's Cowork run reports a
  result now." Worth reconsidering later as a separate design if there's real demand for it.

## Decisions made

- `begin_unattended_session` is opt-in via `unattended_sessions.enabled` in `settings.yaml`,
  default `false`.
- The menu bar shows a live indicator while a connection is in unattended mode, not just
  after-the-fact audit-log entries.

## Implementation notes (resolved during build)

- `ARGS_ONLY_RULES`/`DATA_DEPENDENT_RULES` maintenance: `test_every_rule_is_classified` in
  `tests/unit/test_auto_accept.py` asserts every `_rule_*` method on `AutoAcceptEvaluator` is
  classified into exactly one of the two sets, so a newly added rule can't silently fall through
  `preflight_from_args` untagged. Classifying them by hand (rather than reusing
  `should_auto_accept` with `raw_data=None`) turned out to matter: several rules
  (`no_attachments`, `shared_drive_exclusion`, `no_conferencing_link`, `no_file_attachments`,
  `no_external_attendees`, `no_media_attachments`) are "absence" checks that would silently
  evaluate to a false "matched" the moment `raw_data` is `None` — an empty/missing attribute reads
  the same as a genuinely absent one. Those are classified data-dependent specifically so
  `preflight_from_args` never speculates on them.
- `unattended_scope`'s flag is connection-scoped via a `contextvars.ContextVar`, set by
  `ipc_server.py` around each dispatched `call` request based on whether that connection is in
  `IPCServer._unattended_connections`. No connector code changes at all — `gate.gated_call()` is
  the only reader, via `is_unattended()`.
- `IPCServer._handle_connection`'s `finally` block clears a connection's unattended flag on
  disconnect, so `end_unattended_session` is a courtesy, not a requirement, for the normal case
  (bridge is one process per Cowork task).
- Live menu bar indicator: `IPCServer.set_unattended_changed_listener()` fires (on the IPC
  server's own asyncio thread) whenever `unattended_session_count()` actually changes;
  `PrivacyFenceMenuBar` marshals that through `AppHelper.callAfter` into a menu rebuild, the same
  pattern `auto_accept.set_rules_changed_listener` already used for rule changes.

## Open questions

- Whether unattended mode should also auto-expire after some wall-clock ceiling (e.g. a few hours)
  as a backstop against a `begin` call that's never matched by an `end` and outlives its intended
  scope on a long-lived connection — bridge-exit cleanup covers the normal case, but a TTL would be
  cheap insurance against a code path that doesn't.
- Whether the settings.yaml opt-in should be a plain on/off, or should also support scoping it to
  specific operations/connectors (e.g. "unattended mode is fine for Jira but never for Gmail
  sends") rather than being all-or-nothing per install.
