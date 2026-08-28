# P3 implementation plan: deferred approvals, concurrency, and the approval list

**Status: plan, no code written.** It sequences two documents into one buildable order:

- [`https-connector-refactor-plan.md`](https-connector-refactor-plan.md) **P3** — the deferred
  approval protocol (§5), retiring `_popup_lock` (§6), and the `/approvals` endpoints (§7.1).
- [`approval-list-ui-ux.md`](approval-list-ui-ux.md) — the human surface on top of them, and the
  notification tiers.

Everything below was checked against the code on `main` at `a6766d0` (P2 merged), not derived from
the design documents alone. §2 is the part worth reading first: seven things the code says that the
plan doesn't, three of which break the protocol as written if they are found during the phase
instead of before it.

---

## Contents

1. [Scope and exit criteria](#1-scope-and-exit-criteria)
2. [What the code says that the plan doesn't](#2-what-the-code-says-that-the-plan-doesnt)
3. [Target shape](#3-target-shape)
4. [The protocol, in code terms](#4-the-protocol-in-code-terms)
5. [The PR sequence](#5-the-pr-sequence)
6. [Configuration and rollback](#6-configuration-and-rollback)
7. [Test plan](#7-test-plan)
8. [What the beta has to measure](#8-what-the-beta-has-to-measure)
9. [Risks and open questions](#9-risks-and-open-questions)

---

## 1. Scope and exit criteria

P3's exit criterion in §12 is: *"Three approvals pending at once, each decidable in any order;
`_popup_lock` gone; the stop-and-ask path P0 found (§5.4) has designed copy and a measured re-call
rate from the beta."* Adding the UI design, the full bar for calling this phase done:

- [ ] Three gated calls from one MCP session are pending simultaneously and can be decided in any
      order, each releasing only its own data.
- [ ] `_popup_lock` no longer exists in `gate.py`; the native popup keeps its one-dialog-at-a-time
      invariant through its own lock, in its own module.
- [ ] A gated call not decided inside the hold window returns a pending result; the identical call,
      re-issued after a human decision, releases the data with no second prompt; a write's release
      is single-consumption.
- [ ] Every invocation leaves exactly one audit entry, the pending/release pair shares one
      `request_id`, and no decision a human actually made is missing from the log — including the
      case where Claude never re-calls.
- [ ] `/approvals` renders the real list: rows, groups, both clocks, the seven row states, the
      recently-decided section, live updates over SSE.
- [ ] Deciding returns to the list rather than a dead-end page, and a new approval raises a
      Notification Center notification while the tab is open.
- [ ] The blocking-only configuration (hold window = pending TTL) is a tested, supported
      configuration, not just a rollback switch — D3, as amended by P0.
- [ ] `pytest -v` green on macOS **and** the platform-independent subset green on Linux.

Out of scope, explicitly: principals and per-user storage (P6 — the registry takes a principal
argument from its first commit, but there is exactly one, `"local"`), settings on the web (P4,
parallel), web push and the manifest (org mode, P7+), retiring the bridge (P5).

---

## 2. What the code says that the plan doesn't

Seven findings from reading the code. The first three change the protocol's implementation shape.

### 2.1 The pending result must be **raised**, not returned

§5.2 says `gated_call()` "returns a structured pending result". It cannot.

`gated_call()` is not a value-producing function at most of its call sites. Of the 70 gated calls
in `connectors/`, only 18 are `return await gated_call(...)`; the other 52 are bare
`await gated_call(...)` **followed by the action itself**:

```python
# connectors/gmail.py, gmail_download_attachment
await gated_call(..., tool="gmail_download_attachment", ...)
# Gate before touching disk: gated_call raises on denial, and only a
# decision made here should ever cause the attachment to be written.
return await self._fetch(self._gmail.save_attachment_bytes, ...)
```

A sentinel return value at these sites is discarded and **the write happens anyway**. The barrier
semantics are the security property, and they are expressed as "raises, or falls through".

So: `gate.ApprovalPending(Exception)` carrying `approval_id`, `url`, `expires_at`, and the message,
raised where a denial raises today. Verified safe to raise: an AST scan of every `gated_call()` call
site in `src/privacyfence/` finds **none** inside a `try` with a bare `except`, `except Exception`,
or `except BaseException` — nothing swallows it on the way out. That scan belongs in the PR as a
one-off check, and the invariant belongs in `gate.py`'s module docstring.

### 2.2 The dedupe cache eats the release call

`web/mcp_dispatch.py`'s `call_tool()` keys `self._inflight` on
`f"{connector}:{tool}:{json.dumps(args, sort_keys=True)}"` — **the same key the re-call uses**, by
design — and reuses a *completed* result for `_DEDUPE_TTL_SECONDS = 30`:

```python
still_fresh = (now - recorded_at) < self._DEDUPE_TTL_SECONDS
reusable = not fut.done() or (still_fresh and tool not in self._DEDUPE_EXEMPT_TOOLS and not read_is_stale)
if reusable:
    return await fut
```

The hold window is also 30 s. A human who approves at 35 s and a Claude that re-calls at 36 s lands
inside neither, but a human who approves at 8 s (the common case, and the one the hold window exists
to make cheap) and a re-call at 12 s gets **the cached pending result served again**, with the gate
never re-entered. The failure is silent and looks exactly like "Claude re-called and nothing
happened".

Fix, in `mcp_dispatch` and `ipc_server` alike: a pending outcome is never cached. Pop the key in the
`except ApprovalPending` path, exactly as `except asyncio.CancelledError` already pops it. In-flight
*sharing* of a not-yet-resolved identical call stays — that is §6's coalescing, and it is the
behavior we want: two identical concurrent calls await one approval.

### 2.3 An `ApprovalPending` reaching the MCP layer must not become an error result

`web/routes_mcp.py`'s `handle_call_tool` wraps everything:

```python
except Exception as exc:  # surfaced to the client as a tool error
    return mcp_tools.error_result(str(exc))
```

A pending result delivered as `isError: true` is the worst possible framing given P0's finding that
Claude already reads the re-call contract as a probable injection. It needs its own `except
ApprovalPending` branch *before* the generic one, returning a normal `CallToolResult` whose
`structuredContent` is the pending dict — `to_call_tool_result()` already attaches
`structuredContent` for any plain dict, so this is a two-line branch, not new machinery.

### 2.4 `_popup_lock` has a second tenant, and the native UI still needs it

`_popup_lock` guards two things in `gate.py`: the two gated branches, and `propose_rule_change()`
(line 857), which shows the same rule-confirmation dialog from a completely different entry point.
Deleting the lock without rehoming that one leaves two dialogs racing on screen.

And the native dialog genuinely cannot go concurrent — it is one modal window. So the lock does not
disappear; it **moves into `NativeApprovalUI`**, where the one-dialog-at-a-time constraint actually
lives, and `gate.py` loses all knowledge of it. `_popup_executor` (the dedicated single-thread
executor) moves with it, for the same reason.

This makes the split honest: the *policy* engine becomes concurrent, and the *native surface*
declares that it is not, through a new `ApprovalUI.supports_pending()` (default `False`).
`WebApprovalUI` returns `True`. A deployment on the native UI runs with an effectively infinite hold
window, which is the D3-amended blocking configuration anyway.

### 2.5 The releasing call re-fetches, so what is released is not what was previewed

In §5.2's protocol the registry does not hold the payload: Claude re-issues the identical tool call,
the connector fetches again, and `gated_call()` finds the ledger entry and falls through. That is a
real simplification — the registry holds presentation state (the card HTML, preview bytes) and never
the gated data, which also bounds §7.1's memory concern to what is on screen.

It also opens a genuine TOCTOU window that neither document names: **a human approves a preview of
what the mailbox said at 14:02; the release at 14:07 returns what it says now.** For writes this is
harmless — the payload is Claude's own arguments, and the arguments are the ledger key. For reads it
is not: the thread gained a message, the doc was edited, the record changed.

Proposal, cheap and fail-closed: the ledger entry stores a **content fingerprint** — a SHA-256 of
the `details_text`/`pii_scan_text` actually shown — and the releasing call recomputes it. Mismatch
re-gates (a fresh approval with the new content), rather than releasing something no human saw.
Reads only; writes skip it. Config key to disable, defaulting to on. Without this, "approved" means
"approved something like this recently", which is not the guarantee the card's whole design implies.

### 2.6 The audit log can lose a decision a human actually made

§5.4 defines two entries — `approval_pending` on the deferring invocation, the real decision on the
releasing one — plus `expired` when the TTL lapses. There is a third outcome: **the human decided,
and Claude never re-called.** The ledger entry TTLs out at 5 minutes and the human's Allow or Deny is
recorded nowhere. Given P0 measured that Claude mostly *doesn't* re-call on its own, this is not a
corner case; it may be the modal one during the beta.

So the vocabulary needs a fourth value — `decided_not_released` — written when a ledger entry expires
unconsumed, carrying `decided_at` and what the human actually chose. Otherwise the beta's own
headline metric ("how often does the loop complete?") is unmeasurable from the log, and an auditor
sees a pending that dissolved.

Good news on the neighbouring worry: `AuditLogger.recent_matches()` counts only
`APPROVED_LIKE_DECISIONS`, so adding `approval_pending`/`expired`/`decided_not_released` entries does
**not** inflate the card's "Seen N times this week" fingerprint. No change needed there — but a test
should pin it, because the next new decision value could get it wrong.

### 2.7 The bridge stays blocking, deliberately

`ipc_server.py` is alive until P5 and Claude Desktop still reaches the daemon through it. The
deferred protocol should not be ported to the IPC wire only to be deleted a phase later — §12's own
ordering argument, in reverse.

Concrete rule: a request arriving over IPC runs with the hold window pinned to the pending TTL, i.e.
it blocks, exactly as today. Mechanism: the same contextvar shape `unattended_scope`/`reason_scope`
already use — `blocking_transport_scope(True)`, set once in `ipc_server._call_connector`. One
contextvar, one call site, no protocol on the wire, and it deletes itself with `ipc_server.py` at P5.

---

## 3. Target shape

### New modules

| Module | What it owns |
|---|---|
| `approvals.py` | `PendingApprovalRegistry`: pending approvals, their futures, hold window, TTL, coalescing, the decision ledger, the rules-changed re-evaluation broadcast, per-principal caps. The one new module with real logic. |
| `approval_list_html.py` | Pure `build_list_html(state)` + the client-side `__pfRenderApprovals(state)` re-render, mirroring `settings_window_html.py`'s established shape. |
| `web/routes_approvals.py` (extended) | `/api/approvals`, `/api/approvals/stream` (SSE), `/api/approvals/{id}/preview/{n}`, the decide endpoint's confirmation step, `/sw.js`. |
| `resources/approval_list/styles.css` | List-page styling. Pulls the same token block as the card — see PR 6 on extracting `resources/tokens.css`. |
| `scripts/approval_stats.py` | Local-only beta report over the audit JSONL (§8). |

### Changed modules

| Module | Change |
|---|---|
| `gate.py` | `_popup_lock`/`_popup_executor` out; registry in; `ApprovalPending`; ledger consultation; fingerprint binding; `propose_rule_change` rehomed onto the native UI's lock. |
| `approval_ui.py` | `supports_pending()`; `NativeApprovalUI` gains the lock and the executor. |
| `web_approval_ui.py` | Shrinks: its single-slot `current()` becomes a view over the registry; it stops being the thing that owns pending state. |
| `web/mcp_dispatch.py` | Pending results excluded from the dedupe cache (§2.2). |
| `web/routes_mcp.py` | `except ApprovalPending` before the generic handler (§2.3). |
| `web/mcp_tools.py` | `privacyfence_await_approval` meta tool. |
| `ipc_server.py` | `blocking_transport_scope(True)` around dispatch (§2.7). |
| `audit_log.py` | `decided_at` field; four new decision values; docstring. |
| `auto_accept.py` | Nothing structural — the registry subscribes to the existing `set_rules_changed_listener`. Note it is a *single* listener slot today, already taken by `SettingsController`, so it becomes a list rather than a slot. |
| `tests/conftest.py` | Reset for `approvals._INSTANCE` (standing §2.7 DoD item). |
| `resources/settings.yaml.example` | The `web.approvals` and `web.notifications` blocks (§6). |
| `docs/TECHNICAL_REFERENCE.md`, `docs/security-and-compliance.md`, `docs/testing-policy.md` | The deferred protocol, the new trust argument, the browser smoke test. |

Nothing in `connectors/` changes. If a PR finds itself editing a connector or a `*_client.py`, that
is the signal it has left P3's scope — the same tripwire §12 sets for the fixture recorder.

---

## 4. The protocol, in code terms

### Data

```python
@dataclass
class PendingApproval:
    id: str                       # uuid4 hex; unguessable but not a bearer credential (§10.4)
    principal: str                # "local" until P6
    request_id: str               # shared with both audit entries
    connector: str
    tool: str
    operation_key: str
    dedupe_key: str               # (principal, connector, tool, canonical args) -- coalescing
    kind: str                     # "card" | "confirm"
    html: str                     # today's card stack, unchanged
    summary: ApprovalSummary      # what the list row renders (see approval-list-ui-ux.md §2.1)
    fingerprint: str              # sha256 of the reviewed content; reads only (§2.5)
    created_at: float
    hold_expires_at: float
    expires_at: float
    waiters: int                  # coalesced identical calls awaiting this one
    _future: asyncio.Future       # (decision, choice)
```

```python
@dataclass
class Decision:
    decision: str                 # "accept" | "accept_all" | "deny"
    choice: int | None
    decided_at: float
    fingerprint: str
    single_use: bool              # True for gate="popup" (§5.4)
    expires_at: float             # ledger TTL, default 5 min
```

`ApprovalSummary` is the row's data and nothing more — id, connector, tool, title, one-line summary,
`is_read`, `pii_categories`, `seen_count`, `dupe_count`, `session_label`, the two deadlines, state.
It carries no gated content, which is what makes it safe to hand to the SSE stream and (at
`standard` detail) to a notification.

### The flow

```
gated_call()
  fetch → privacy filter → PII scan → seen_count      (unchanged)
  auto-accept check                                    (unchanged)
  ledger.take(principal, connector, tool, args, fingerprint)
      hit  → audit(real decision, decided_at) → return filtered_data / raise denial
      miss ↓
  registry.register(PendingApproval)                   ← coalesces onto an identical pending
      native UI  → shows the modal under its own lock, resolves the future
      web UI     → the row appears; SSE pushes it; notification fires
  await future, timeout = hold_window
      decided  → identical to today. audit(real decision) → return / raise
      timeout  → audit(approval_pending) → raise ApprovalPending(id, url, expires_at)
```

and, on the decision side:

```
POST /api/approvals/{id}/decide
  → registry.resolve(id, result, choice)
      resolves the future (releasing anyone inside their hold window, all coalesced waiters)
      writes a ledger entry for anyone who already timed out
      broadcasts removal over SSE
  → "accept_all" or a PII match returns {"status": "confirm_required", "html": <confirmation doc>}
    and the shim swaps the body; the confirmation POST does the rule write and the
    re-evaluation broadcast in one registry-side critical section (§6's job 2b)
```

### The invariant, restated for the code review that will ask

There is no tool, endpoint, or ledger key that turns an `approval_id` into content. The only path to
data is the original gated tool call, with identical arguments, under the same principal, against
content whose fingerprint still matches. `privacyfence_await_approval` returns an enum. That is the
§5.3 argument, and each clause of it is a test in §7.

---

## 5. The PR sequence

Nine PRs. Each is independently mergeable, leaves the suite green, and — through PR 5 — leaves
observable behavior unchanged unless a config key is flipped. Sizes are the plan's own S/M/L.

### PR 1 — `approvals.py`, standalone · **M**

The registry, with no caller. Register, coalesce, resolve, expire, ledger write/take/expire,
rules-changed re-evaluation, per-principal cap, byte accounting. Pure asyncio, no Starlette, no
`gate.py`, no AppKit — Linux-testable from the first line.

*Done when:* the §7 registry tests pass and nothing else in the repo imports it.

### PR 2 — Rehome the lock; route `gate.py` through the registry · **M**

The refactor that unlocks everything and changes nothing:

- `_popup_lock` and `_popup_executor` move into `NativeApprovalUI`; `propose_rule_change()` moves onto
  the same lock; `ApprovalUI.supports_pending()` lands.
- `gate.py`'s two branches call `registry.register(...)` + `await handle.wait(hold_window)` instead of
  `async with _popup_lock: await _run_in_popup_executor(show_*_popup, ...)`.
- Hold window is pinned to the pending TTL, so **every call still blocks**. No pending results, no
  protocol change, no user-visible difference.

*Why it is its own PR:* the existing `gate.py` suite is the parity oracle. If it stays green with the
lock gone and the registry in the path, the restructuring is proven before any behavior rides on it.

*Done when:* `pytest` green with no edits to `tests/unit/test_gate*.py` beyond the monkeypatch seam,
and `scripts/qa_popup_smoke.py` still passes on macOS.

### PR 3 — Deferred returns · **L**

The protocol itself: `ApprovalPending`; the configurable hold window; the ledger consulted at the top
of `gated_call()`; fingerprint binding for reads (§2.5); `blocking_transport_scope` for the bridge
(§2.7); dedupe-cache exclusion (§2.2); the non-error MCP result (§2.3); `AuditEntry.decided_at` and
the four new decision values, including `decided_not_released` (§2.6).

*Done when:* a scripted MCP client gets a pending result, a decision is POSTed, the identical call
releases the data, the audit log holds exactly two entries sharing one `request_id`, and a second
identical write call re-gates.

### PR 4 — `privacyfence_await_approval` · **S**

The long-poll meta tool: `{ids: [...], timeout_seconds} -> {id: "pending"|"approved"|"denied"|
"expired"}`. Status only, never content. Registered in `mcp_tools.META_TOOLS`, dispatched in
`mcp_dispatch`, audited like the other meta tools.

Carries the **stop-and-ask copy** P0's finding makes load-bearing (§5.4): the tool description and
the pending message are written for a model that will stop and tell a human, not for one that will
silently re-call. Copy is reviewed as copy in this PR, and it is the thing the beta iterates.

### PR 5 — Concurrency for real · **M**

Retire the "queued behind a popup" re-check in favour of §6's broadcast: the registry subscribes to
`auto_accept.set_rules_changed_listener`, re-evaluates every pending approval for the principal
through `should_auto_accept`, resolves the covered ones as `auto_accepted`, and pushes their removal.
Coalescing moves out of `_inflight` into the registry's `dedupe_key`. Per-principal cap enforced
(reject with "too many pending approvals" — fail-closed, not queued). Payload bytes over the cap
spill to a `0600` file, unlinked on decision or expiry.

*Done when:* three approvals are pending at once and decidable in any order; a rule created on one
clears the other two when it covers them; an unreviewed PII match still overrides.

### PR 6 — The list · **M**

`approval_list_html.build_list_html(state)`, `/api/approvals`, the SSE stream, and
`window.__pfRenderApprovals`. Rows, badges, both clocks, the seven states, grouping by session and
arrival window, recently-decided, the live indicator, the empty state — [`approval-list-ui-ux.md`
§2](approval-list-ui-ux.md#2-the-list) is the spec.

Includes the small shared-token extraction: `:root` moves to `resources/tokens.css`, embedded by both
stylesheets, so the list cannot drift from the card's palette.

`claude_reason` escaping is tested here, not assumed (§2.1 of the UI doc).

### PR 7 — Return to the list, and the two-step confirmations · **M**

Replaces the shim's `document.body.innerHTML = "…close this tab"` with the five-step flow in
[§3](approval-list-ui-ux.md#3-after-a-decision-back-to-the-list): `replaceState`, restored scroll,
the toast, focus moved but nothing auto-opened, the 409/decided-elsewhere path.

Same PR because it shares the endpoint: the PII and "Always allow" confirmations become
`{"status": "confirm_required", "html": ...}` responses rendering
`dialog_window_html.build_confirmation_html()` — the shipped document, unmodified — in place, with the
rule write and the re-evaluation broadcast in one registry critical section.

### PR 8 — Notifications, tiers 0 and 1 · **S**

Title/favicon badge, `aria-live` announcement, the service worker, `registration.showNotification()`
driven by the SSE stream, the burst coalescing and rate limit, tag-per-approval and close-on-resolve,
`notificationclick` focusing the existing tab. Permission asked after the first decision, behind a
pre-prompt, never on load. The three detail levels and the two content invariants (§4.3 of the UI
doc) — the second of which is a test, not a convention.

No push, no VAPID, no manifest: [§4.1](approval-list-ui-ux.md#41-the-four-tiers) puts those in org
mode, and nothing here should pretend otherwise.

### PR 9 — Beta instrumentation and docs · **S**

`scripts/approval_stats.py` (§8); `TECHNICAL_REFERENCE.md`'s deferred-protocol section;
`security-and-compliance.md`'s revised trust argument (§5.3, plus the fingerprint binding and the
notification content policy); `testing-policy.md` for the headless-Chromium smoke test; release notes
naming the beta and the blocking-only configuration.

### Order and parallelism

```
PR1 ──▶ PR2 ──▶ PR3 ──▶ PR4
                 │  └──▶ PR5 ──┐
                 └──▶ PR6 ──▶ PR7 ──▶ PR8 ──▶ PR9
```

PR 6 needs PR 3's summaries but not PR 5's concurrency — it renders a list of one perfectly well, so
the UI work can run alongside the registry work with one person on each. PR 4 is independent of
everything after PR 3. Total: roughly L + 3×M + … — call it the "several weeks" §12 already assigns
P3, with the UI work as the part that parallelizes.

---

## 6. Configuration and rollback

```yaml
web:
  approvals:
    # Seconds gated_call() blocks before returning a pending result. Set it
    # to the pending TTL (or 0 for "always block") for today's behavior --
    # the supported blocking-only configuration, not just a rollback lever
    # (D3, as amended by P0's finding that Claude mostly does not re-call).
    hold_window_seconds: 30
    pending_ttl_seconds: 900
    ledger_ttl_seconds: 300
    # Re-gate a read whose content changed between approval and release
    # (§2.5). Off means "approved" covers whatever the re-call fetches.
    bind_content_fingerprint: true
    max_pending_per_principal: 20
  notifications:
    enabled: true          # tiers 0-1
    detail: standard       # minimal | standard | detailed
    sound: false
```

Rollback ladder, cheapest first: `hold_window_seconds` to the TTL restores blocking behavior with the
list and notifications intact; `web.approval_ui: native` (P1's own lever) turns the whole web surface
off; `web.mcp.enabled: false` returns Claude to the bridge, which never had the deferred protocol at
all (§2.7). No downgrade needed at any rung.

No version bump on any PR here — `CLAUDE.md`'s rule; the beta gets tagged `vX.Y.Z-beta.N` at release
time, in its own commit.

---

## 7. Test plan

Beyond the standing §2.7 checklist, per area:

**`approvals.py`** — hold-window expiry; TTL expiry; idempotent decisions (first wins, second 409);
coalescing of identical pendings, including that one decision releases every waiter; ledger
single-consumption for writes and TTL-bounded reuse for reads; fingerprint mismatch re-gates; the
rules-changed broadcast resolving covered pendings and *not* resolving a PII-flagged one; the
per-principal cap failing closed; cross-principal isolation (asserted now, meaningful at P6).

**`gate.py`** — every existing test stays valid for the decided-in-hold-window path, which is why
PR 2 keeps it. New: pending → decide → re-call → release; the two audit entries and their shared
`request_id`; `decided_not_released` on an unconsumed ledger entry; unattended sessions never receive
a pending result (they are denied before any prompt, as today); `ApprovalPending` propagating
uncaught through a representative connector.

**Dispatch** — a pending result is never served from the dedupe cache (the §2.2 regression, as a
named test); a pending result reaches the client as a normal result, not `isError`; two identical
concurrent calls share one approval.

**HTML** — `test_approval_list_html.py` per [§7 of the UI doc](approval-list-ui-ux.md#7-tests):
fixture states for empty/one/group-of-three and each row state; `claude_reason` containing markup
comes out inert; the "no `http://` or `https://` anywhere in the document" assertion the card already
carries, applied to the list.

**Notifications** — the payload builder against a card stuffed with marker strings: nothing gated
appears at any detail level, and a *push* payload carries no approval data at all.

**Browser** — headless Chromium, the successor to `qa_popup_smoke.py` §13 already names: decide a
card and land back on `/approvals` with the row gone; two tabs, decide in one, the row disappears in
the other.

**Platform** — everything above except the `qa_popup_smoke` leg runs on Linux. P1 added the Linux CI
job; P3 is where it starts carrying most of the new surface.

---

## 8. What the beta has to measure

P3's exit criterion names "a measured re-call rate from the beta". All of it is derivable from the
audit log once `decided_at` and the four decision values land — no telemetry, no phone-home, not even
opt-in (§5.3 of the UI doc):

| Number | Derived from |
|---|---|
| % decided inside the hold window | `approved`-like entries with no preceding `approval_pending` for the same `request_id` |
| time-to-decision distribution | `decided_at − timestamp` on the pending entry |
| **re-call rate** | `request_id`s with a pending entry that also have a release entry |
| decided-but-never-released | `decided_not_released` count — the P0 failure mode, made visible |
| expiry rate | `expired` count |
| concurrency reached | max simultaneous live pendings per session |

`scripts/approval_stats.py` prints these as a report a beta tester can choose to paste into an issue.
The headline decision they inform is whether to raise the hold window toward whatever tool-call
timeout P2 measured — §5.4's live option, and the one change that would most reduce the human cost of
an approval.

---

## 9. Risks and open questions

| # | Risk | Handling |
|---|---|---|
| 1 | **Claude doesn't re-call** — P0's 0-of-5 finding. | Not fixable here; designed around. PR 4's copy and PR 8's notifications make the human turn cheap, and PR 9's stats make the rate visible. Escalation path if the beta confirms it on all four surfaces: keep the blocking configuration the default for `local` mode indefinitely. |
| 2 | Fingerprint binding (§2.5) re-gates too often on live data (a thread with a new message). | Ship it on, measure the re-gate rate in the same beta report, and make it per-connector if reads turn out to churn more than expected. Failing closed while we learn is the right direction. |
| 3 | The single `_rules_changed_listener` slot has two tenants after PR 5. | `SettingsController` already holds it (`settings_controller.py:604`), so registering the registry naively unregisters the settings window's own live refresh — silently. Make it a list in PR 5, with a test that two listeners both fire. |
| 4 | Preview bytes now live for up to 15 minutes, many at once. | The cap and the `0600` spill in PR 5; `Cache-Control: no-store` and unlink-on-decision already specified in §7.1. Worth a real memory check with three WIDE cards holding PDFs. |
| 5 | Does a service worker registered from a link opened *inside* Claude's app survive? | Unknown, same shape as §10.6's WebAuthn question. Tier 0 works regardless; PR 8 must degrade silently, and the beta should check it on each surface. |
| 6 | Two approval surfaces exist through P10. | `supports_pending()` keeps the split explicit rather than implicit, and `qa_popup_smoke.py` stays in the DoD until the native UI goes. |

Open questions that want an answer before PR 3 lands, not after:

1. **Does `decided_not_released` need to be a decision value, or a field on the pending entry?** A new
   value is cheaper to query and harder to miss; a field keeps the vocabulary shorter. Decide with
   whoever parses the JSONL downstream.
2. **Is fingerprint binding on by default?** §2.5 argues yes. It is a real behavior change on reads,
   so it deserves a deliberate call rather than a default chosen by whoever writes the PR.
3. **Where does the pending message's copy live** — `mcp_tools.py` beside the tool description, or a
   copy module the UI shares? The UI doc's §5.9 table and the tool description are saying the same
   thing to two audiences, and they will drift if they live apart.
