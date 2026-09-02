# PrivacyFence as an HTTPS connector — architecture & functional refactoring plan

**Status: design agreed (§15); the P0 spike is complete and its findings are recorded in §12. P1
(web approval surface), P2 (MCP over HTTP alongside the bridge), P3 (deferred approvals +
concurrency) and P4 (settings on the web, §16) have landed. P3 retires `_popup_lock` per §6 and
implements the deferred-approval protocol from §5; its own beta (§12: "P3 | beta, and it needs one")
is what still has to confirm the re-call-rate and hold-window findings from P0's spike (§5.4) against
real traffic on all four Claude surfaces, not just Claude Code. P4 folds §16's eight W-PRs into one
landing: the settings surface (`/settings`, the allowlisted action dispatcher, the shared state-push
channel) and the P1-compatible slice of [`approval-list-ui-ux.md`](approval-list-ui-ux.md) (the page
shell, the return-to-list flow, notification tiers 0-1) — its own full multi-row list, grouping and
hold-window clock stay P3's, per that document's §6.

**P4b (the Desktop stdio shim, D11) shipped and has since been reverted; D11 is superseded by D12.**
P2's implementation found a real gap — how Claude Desktop connects to `local` mode once the bridge is
gone — which P4b closed by shipping PrivacyFence's own transport-only shim inside `PrivacyFence.mcpb`.
That shim added a second Node package (`mcpb/shim/`) and its own contract test to maintain indefinitely
for a problem P4c (§16.9) turns out to solve more simply: the `/settings` page P4 already built shows
the `/mcp` URL and bearer token directly, which covers Claude Code and any other Streamable-HTTP-native
client without a second install artifact at all. Desktop keeps using the original bridge-based
`PrivacyFence.mcpb`, unchanged from before D11 — see D12 in §15 and the Migration section below for
the full reasoning.**

This document designs, and validates against the current code, the refactoring that turns
PrivacyFence from a macOS-only, single-user, stdio-MCP-bridge desktop app into a service with an
embedded HTTP(S) server that Claude talks to directly as a remote connector, and whose approval and
configuration surfaces are web pages.

The intent, in one sentence: **make PrivacyFence usable from Claude on a phone, with no desktop in
the loop, without weakening the guarantee that no gated data reaches Claude until a human has looked
at that specific call.**

Everything the product *is* stays the same — the twelve connectors, every tool, the `auto`/`review`/
`popup` gates, auto-accept rules and resource grants, the PII gate, the privacy filter, the audit
log, `check_policy`, `propose_rule_change`, unattended sessions. What changes is where the human
sees the request, how many they can see at once, how Claude reaches the daemon, and how many people
one daemon can serve.

---

## Contents

1. [What exists today](#1-what-exists-today)
2. [Goals, non-goals, and why the mode split exists](#2-goals-non-goals-and-why-the-mode-split-exists)
3. [Target architecture](#3-target-architecture)
4. [Operating modes: `local` and `org`](#4-operating-modes-local-and-org)
5. [The approval protocol: from blocking to deferred](#5-the-approval-protocol-from-blocking-to-deferred)
6. [Concurrent approvals: retiring `_popup_lock`](#6-concurrent-approvals-retiring-_popup_lock)
7. [The web surfaces](#7-the-web-surfaces)
8. [MCP over HTTP](#8-mcp-over-http)
9. [Identity, authentication, and multi-user state](#9-identity-authentication-and-multi-user-state)
10. [Security analysis](#10-security-analysis)
11. [Validation: what was checked, and what it showed](#11-validation-what-was-checked-and-what-it-showed)
12. [Implementation plan](#12-implementation-plan)
13. [Testing strategy](#13-testing-strategy)
14. [Relationship to #55 and #121](#14-relationship-to-55-and-121)
15. [Decisions taken](#15-decisions-taken)
16. [P4 implementation plan: the web surfaces](#16-p4-implementation-plan-the-web-surfaces)

---

## 1. What exists today

Read from the code, not from the docs, because several of these details are load-bearing for the
design below.

### 1.1 Two processes, one socket

`privacyfence-bridge` (`bridge/src/`, Node, bundled to one `dist/bridge.js`) is an ephemeral stdio
MCP server Claude spawns per session. It holds no credentials and no state. It discovers the daemon
via `~/.privacyfence/ipc_port`, authenticates with the one-line token in `~/.privacyfence/ipc_token`,
fetches the connector manifest, and registers one MCP tool per `ToolSpec` (`bridge/src/tools.ts`).
Every call is forwarded as newline-delimited JSON over 127.0.0.1 TCP (`src/privacyfence/ipc.py`).

`privacyfence-app` (`src/privacyfence/daemon_main.py`) is the persistent daemon: rumps menu bar on
the main thread, an asyncio IPC server on its own thread (`ipc_server.py`), connector clients, all
credentials, all policy.

### 1.2 The gate is synchronous by construction

`gate.gated_call()` (`src/privacyfence/gate.py`) fetches the data, evaluates auto-accept, runs the
PII scan for reads, and then — under a **process-global `asyncio.Lock` named `_popup_lock`** — shows
exactly one native dialog and blocks on the answer. Its module docstring states the invariant
plainly:

> There is no pending-approval handshake — `gated_call()` either returns the data or raises in the
> same call that fetched it, so Claude never holds a tool that can release gated data on its own.

Two secondary blocking dialogs can follow inside the same lock acquisition: the PII "are you sure"
confirmation, and the "Always allow" rule confirmation. Both are inside the lock deliberately, so a
request queued behind this one cannot slip past with the pre-rule rule set.

### 1.3 The UI is already HTML

This is the most important existing fact for this refactoring.

- `approval_window_html.build_card_stack_html()` is a **pure function** returning one self-contained
  HTML document — the whole approval card stack including its own Deny / Allow once / Always allow
  button row. Fonts are embedded as base64 data URIs; the shipped test asserts the document contains
  no `http://` or `https://` reference at all.
- `settings_window_html.build_html()` is likewise pure, imports only `json`, and takes
  `SettingsController.snapshot()` as its entire input. It already re-renders client-side through a
  `window.__pfRender(newState)` entry point.
- The only WebKit coupling in either is one `post()` helper calling
  `window.webkit.messageHandlers.pf.postMessage(...)` — four references in the approval document,
  three in the settings document.

`approval_window.py`, `dialog_window.py`, `settings_window.py` and `menu_bar.py` are the AppKit hosts
around those documents. They are the macOS-only part; the documents themselves are not.

### 1.4 The seams that already exist

- `approval_ui.py` defines an `ApprovalUI` ABC with `init_approval_ui()`. Its docstring names issue
  #55's mobile approval and #121's Windows dialog as the intended users of the seam. `gate.py` calls
  through it and re-resolves it on every call, so a swap takes effect immediately.
- `settings_controller.py` is already headless-first: "No unguarded AppKit/WebKit imports at module
  level". Its AppKit dependencies (`rumps.alert`, `dialog_window`) are guarded and resolve to `None`
  without PyObjC.
- `connector.py`'s `ToolSpec.to_dict()` is already the single source of truth for the tool manifest,
  consumed by the bridge to build its schemas.

### 1.5 Everything is single-user, and the list of globals is short

`tests/conftest.py` enumerates every module-level singleton the codebase has, which makes the
multi-user work bounded and knowable:

| Global | Module | Holds |
|---|---|---|
| `_INSTANCE`, `_config_path`, `_rules_changed_listener` | `auto_accept` | rules/grants evaluator, settings.yaml path |
| `_INSTANCE` | `audit_log` | the JSONL writer for this user |
| `_INSTANCE` | `approval_ui` | the approval surface |
| `_INSTANCE` | `resource_names` | grant-name resolution cache |
| `_enabled`, `_disabled_categories`, `_audit_match_details_enabled`, `_changed_listener` | `pii_detector` | per-user PII settings |
| `_GROUPS` | `privacy_filter` | per-user privacy policy |

Plus `paths.data_dir()` → `~/.privacyfence` with a single `config/settings.yaml`, a single
`credentials/` directory (one token file per connector), and a single `logs/audit/` tree.

Unattended sessions are keyed on `id(writer)` — the identity of a TCP connection.

---

## 2. Goals, non-goals, and why the mode split exists

### Goals

1. Claude on a phone can use PrivacyFence connectors with no desktop involved.
2. Approvals happen on a web page, and **several can be pending at once** — a list, each row
   expanding into today's full detail view.
3. The approval prompt reaches the user through Claude itself ("this step needs your approval:
   *link*") rather than through a window on one specific Mac.
4. Configuration moves to the same web surface.
5. Two deployment shapes: a strict `local` mode equivalent to today's posture, and an `org` mode
   where one PrivacyFence serves many users with per-user service credentials.
6. All existing behavior — connectors, tools, gates, rules, grants, PII gate, audit — is preserved.
7. The platform-specific code shrinks to nearly nothing, which is what makes cross-platform real.

### Non-goals

- Changing what any connector does, or any tool's schema.
- Changing the rule/grant model or the PII detector.
- Building a native mobile app. The approval surface is a web page.
- Replacing Claude's own client-side permissioning. PrivacyFence remains a separate, independent
  enforcement point.

### Why there are two modes

The two modes are not a preference between deployment styles. The split is the mechanism that makes
the mobile goal reachable at all: a remote MCP connector added in claude.ai must be reachable over
HTTPS *from Anthropic's infrastructure*, a server bound to `127.0.0.1` is not, and Claude for iOS
and Android use the connectors added on claude.ai, so they inherit that requirement.

Hence:

- **`local`** — same trust posture as today (loopback only, single user), used from Claude Code or
  Claude Desktop on the same machine. What it buys over today is concurrent approvals, a web
  approval/config UI, and the removal of the macOS-only UI layer.
- **`org`** — PrivacyFence runs on a host in the organization's trusted infrastructure with a
  reachable HTTPS endpoint. **This is the mode that delivers the mobile goal.**

An individual with no organization who wants mobile is deploying a one-user `org` mode somewhere
reachable, or fronting `local` mode with a tunnel. Worth stating in the README so the mode choice
reads as the deliberate one it is, rather than implying `local` will grow into it.

---

## 3. Target architecture

```
                    ┌──────────────────────────────────────────────┐
                    │           Claude (web / desktop / iOS /      │
                    │           Android / Claude Code)             │
                    └───────────────┬──────────────────┬───────────┘
                                    │                  │
                 MCP Streamable HTTP│                  │ the approval link Claude
                 (OAuth 2.1 bearer) │                  │ shows in the conversation
                                    │                  │
    ════════════════════════════════▼══════════════════▼════════════════════════
                         privacyfence-app  (one process)
    ┌──────────────────────────────────────────────────────────────────────────┐
    │  Embedded HTTP(S) server                                                 │
    │  ┌────────────┐  ┌───────────────┐  ┌───────────┐  ┌──────────────────┐  │
    │  │ /mcp       │  │ /approvals    │  │ /settings │  │ /oauth/*, /login │  │
    │  │ MCP tools  │  │ approval UI   │  │ config UI │  │ identity + per-  │  │
    │  │ (bearer)   │  │ (session)     │  │ (session) │  │ user service auth│  │
    │  └─────┬──────┘  └───────┬───────┘  └─────┬─────┘  └────────┬─────────┘  │
    ├────────┼─────────────────┼────────────────┼─────────────────┼────────────┤
    │        │        principal_scope(...)      │                 │            │
    │  ┌─────▼─────────────────▼────────────────▼─────────────────▼─────────┐  │
    │  │  PendingApprovalRegistry   ←→   gate.gated_call()  (UNCHANGED core)│  │
    │  └─────┬──────────────────────────────────┬────────────────────────────┘ │
    │        │                                  │                              │
    │  ┌─────▼──────┐ ┌──────────┐ ┌────────────▼─────┐ ┌──────────┐ ┌───────┐ │
    │  │ auto_accept│ │pii_detect│ │ privacy_filter   │ │audit_log │ │ 12 ×  │ │
    │  │ + grants   │ │          │ │                  │ │          │ │connec-│ │
    │  │            │ │          │ │                  │ │          │ │ tors  │ │
    │  └────────────┘ └──────────┘ └──────────────────┘ └──────────┘ └───┬───┘ │
    └──────────────────────────────────────────────────────────────────┬─┴─────┘
                                                                       │
                                              Google / Slack / Atlassian / Salesforce
                                              / Telegram, per-user credentials
```

What disappears eventually, pending a real answer for Desktop (D11 tried one, reverted; see D12 in
§15 — P5 is currently blocked, not scheduled): `bridge/` entirely, `ipc.py` / `ipc_server.py` as the
Claude-facing transport, `approval_popup.py`, `approval_window.py`, `dialog_window.py`,
`settings_window.py`, `menu_bar.py` (the last one optionally kept as a convenience tray on desktop).

What is reused unchanged: `approval_window_html.py`, `settings_window_html.py`, `gate.py`'s policy
core, `auto_accept.py`, `resource_grants.py`, `privacy_filter.py`, `pii_detector.py`,
`audit_log.py`, every `*_client.py`, every `connectors/*.py`.

### New modules

```
src/privacyfence/
  principal.py            Principal dataclass, principal_scope contextvar, per-principal registries
  approvals.py            PendingApproval + PendingApprovalRegistry (the new domain object)
  web_approval_ui.py      WebApprovalUI(ApprovalUI) — registers pendings instead of showing a dialog
  web/
    __init__.py
    server.py             HTTP(S) server lifecycle, TLS, bind policy, security headers
    auth.py               sessions, login, OAuth 2.1 resource-server + (org) authorization-server
    routes_mcp.py         Streamable HTTP MCP endpoint
    routes_approvals.py   approval list, card fragments, decisions, SSE stream, previews
    routes_settings.py    settings page + snapshot/action API
    routes_oauth.py       server-side redirect endpoints for per-user service authorization
```

---

## 4. Operating modes: `local` and `org`

One setting selects the mode; nearly everything else follows from it.

| | `local` | `org` |
|---|---|---|
| Bind address | `127.0.0.1` only | configurable, reachable |
| Transport | loopback HTTP (see §10.2) | HTTPS, mandatory |
| Users | exactly one | many |
| Human login | none by default (loopback + local token) | OIDC against the org IdP |
| MCP auth | bearer token in a `0600` file | OAuth 2.1 + PKCE, per user |
| Service credentials | today's `credentials/*` | `users/<principal>/credentials/*` |
| Mobile Claude | **not possible** | supported |
| `org_config.json` | as today | as today, plus server/TLS/IdP config |
| Trust posture | equivalent to today's | inbound-facing; §10.1 |

`local` mode is deliberately *not* a lesser `org` mode: it is the current security posture kept
intact. The single user in `local` mode is a `Principal` like any other — id `"local"`, storage root
`data_dir()` itself — so there is exactly one code path and no migration of existing installs.

---

## 5. The approval protocol: from blocking to deferred

This is the part that changes a stated security invariant, so it needs to be argued rather than
asserted.

### 5.1 Why it has to change

Today `gated_call()` blocks until a human answers. That is fine when the human is at the Mac showing
the dialog. It is not fine when the human is on a phone and the answer may be minutes away: the MCP
client's tool-call timeout fires long before that, and — critically — **a blocking call cannot
return the approval link**, because the link would have to travel in the return value of a call that
has not returned yet.

Concurrency has the same root cause. `_popup_lock` exists because there is one screen and one
dialog. A list of pending approvals is only meaningful if a gated call can *be* pending.

### 5.2 The protocol

1. `gated_call()` does everything it does today up to the decision point: fetch, auto-accept check,
   PII scan, `seen_count`, card construction.
2. Instead of showing a dialog, it registers a `PendingApproval` in the registry and awaits its
   future with a **hold window** (default 30 s, configurable).
3. **Decided inside the hold window** → identical behavior to today. Data returned, or
   `RuntimeError("Request denied by user")`. The common case — the user is looking at their phone
   when Claude asks — is indistinguishable from the current experience.
4. **Not decided inside the hold window** → the call returns a structured pending result:

   ```json
   {
     "status": "approval_pending",
     "approval_id": "a1b2c3d4e5f6",
     "url": "https://pf.example.com/approvals/a1b2c3d4e5f6",
     "expires_at": "2026-08-28T09:41:00Z",
     "message": "This step needs your approval. Open the link above to review and decide."
   }
   ```

   The pending approval stays live in the registry until decided or until TTL (default 15 min).
5. The human decides on the web page. The decision is written to a short-lived **decision ledger**
   keyed by `(principal, connector, tool, canonical(args))` — the same key shape `ipc_server.py`
   already uses for retry de-duplication, which is already retry-stable because `reason` is popped
   out of `args` before the key is built.
6. Claude re-issues **the identical tool call**. `gated_call()` finds a decision for exactly this
   call under exactly this principal and releases the data (or raises the denial) without a second
   prompt. Still pending → the same `approval_id` and `url` come back, idempotently.
7. To avoid poll-spam, one new meta tool:

   ```
   privacyfence_await_approval(approval_ids: string[], timeout_seconds: int) ->
     { "a1b2c3d4e5f6": "approved", "f6e5d4c3b2a1": "pending", ... }
   ```

   It long-polls and returns **status only, never content**. It is what makes concurrency useful:
   Claude fires several gated calls, collects several `approval_id`s, tells the user "3 steps need
   your approval: *link*", waits on all of them at once, and re-issues each as it clears.

   P0 found that step 6 is where the model actually stops and asks a human rather than re-issuing on
   its own, which changes what this tool is *for* — see §5.4.

### 5.3 Why the invariant survives

The original invariant's actual purpose is: *Claude must never hold a capability that releases gated
data without a human having approved that specific call.* Restating it precisely for the new model:

- **There is no tool that takes an `approval_id` and returns content.** The only path to data is the
  original gated tool call, with identical arguments, under the same principal.
- `await_approval` returns an enum, nothing else.
- A decision-ledger entry is bound to `(principal, connector, tool, args)`. It cannot be redirected
  to a different call, a different argument set, or a different user.
- Therefore what Claude holds after approval is not a bearer capability over data; it is the fact
  that a human approved this exact call — which is precisely what today's blocking return also
  encodes, only spread over two round trips instead of one.

### 5.4 What genuinely is new risk, and the mitigations

**Time separation.** Today approval and release are the same instant. Now an approved decision can
be replayed for as long as the ledger entry lives.

- Ledger TTL default **5 minutes**, configurable, well under the pending TTL.
- **Write decisions (`gate="popup"`) are single-consumption**: the entry is deleted on first
  release. A second `gmail_send_message` with identical arguments re-gates. This matches the
  existing intent behind `_DEDUPE_EXEMPT_TOOLS`.
- Read decisions (`gate="review"`) stay TTL-bounded and reusable, which is the existing dedupe
  behavior for reads, unchanged.
- The audit log makes the split visible rather than hiding it (below).

**Audit integrity.** `gated_call()` guarantees exactly one audit entry per invocation, including via
its `finally` fallback. Deferred approval means two invocations, so it gets two entries and the
invariant holds unchanged per invocation:

- the pending invocation records `approval_pending` (new decision value);
- the releasing invocation records the real decision — `approved`, `rejected`,
  `accepted_via_accept_all`, etc. — exactly as today;
- both carry the same `request_id`, so the pair is a single reconstructable event;
- a new `decided_at` field distinguishes when the human clicked from when the data moved;
- expiry writes `expired` against the pending entry, so an abandoned approval is not a silent gap.

`AuditEntry` gains `decided_at` and the decision vocabulary gains `approval_pending` and `expired`.
The schema is otherwise untouched, which matters for anyone parsing the JSONL already.

**Claude may not re-call — P0 measured this, and mostly it does not.** This was the spike's question
2, and the answer changed the risk's shape rather than confirming it. Across five independent fresh
Claude Code sessions driving a real MCP tool that returned a pending-shaped result, zero completed the
fetch → pending → re-call → content loop autonomously; four stopped and asked a human, naming the
tool's own re-call instruction as a probable **prompt injection**
(§12, question 2). The mechanism is not the inattention this
paragraph originally anticipated: an instruction embedded in a tool description or a return payload
telling the model to repeat a call by itself has exactly the shape Claude's injection defenses are
built to catch, and the defense fired even in the run whose initiating human prompt pre-authorized
the behavior by name.

Fail-closed holds — every observed run ended with a human being asked, never with a silent wrong
action, so the security invariant is if anything reinforced. What changes is the plan:

- **Design for a confirmation turn per pending approval as the common case, not the exception.**
  `await_approval`'s value is no longer "the model waits instead of ending the turn"; it is "the model
  has one good thing to offer the human at the point where it stops" — *"3 approvals are pending —
  want me to keep checking and tell you when they clear?"*. That is copy to write deliberately in P3,
  not a bug to word away.
- **Copy-editing the tool description is not the fix.** The defense triggered on every phrasing
  tested, including the one that pre-empted the injection concern explicitly (which reads as a
  classic injection shape itself, so it plausibly made things worse). The promising direction is a
  protocol- or system-level signal that a pending result is a *status* and not an instruction — a
  distinct MCP content type, if one becomes available — rather than better prose.
- **The hold window is worth more than D3 assumed.** An approval decided inside it costs no human
  turn at all; one that falls out of it now costs one. So P2 should establish what tool-call timeout
  Claude's client actually enforces (§8.3), and P3's beta should treat "raise the hold window as far
  as that timeout safely allows" as a live option rather than defending the 30 s default.
- **Only Claude Code was reachable.** Desktop, web and mobile were not testable from the spike's
  environment. Because the behavior traces to a general safety property rather than a Claude Code
  quirk, assume it everywhere until checked, and check it on the other three surfaces during P3's
  beta.

Five runs, one day, one model, one tool description is a real signal and not a proof — P3's beta is
still where this settles on real traffic. What P0 changes is that the risk is confirmed present
today, so P3 gets scoped around it rather than hoping it is absent.

**Unattended sessions.** `is_unattended()` short-circuits before any prompt and must keep doing so —
an unattended session must never receive a pending result, it must be denied immediately, exactly as
today. The only change is what identifies the session (§9.4).

---

## 6. Concurrent approvals: retiring `_popup_lock`

`_popup_lock` is doing two jobs. Only one of them is about screens.

**Job 1 — one dialog at a time.** Obsolete. The list view is the entire point.

**Job 2 — a rule created by one approval must cover items queued behind it.** Today this is the
re-check after acquiring the lock ("Auto-accepted while queued"). Concurrency does not remove that
requirement; it makes it a broadcast instead of a queue:

- `PendingApprovalRegistry` subscribes to `auto_accept.set_rules_changed_listener`.
- When a rule or grant changes, every pending approval **for that principal** is re-evaluated
  through `should_auto_accept`. Any now covered is resolved as `auto_accepted` with the matching
  rule name, disappears from the list, and its waiter (or ledger entry) is completed.
- An unreviewed PII match still overrides, exactly as `pii_forces_confirmation` does today.

This is strictly better than the lock: it covers approvals raised *after* the rule was created and
the whole list at once, not just what happened to be queued at that moment.

**Job 2b — the two secondary dialogs.** The PII "are you sure" step and the "Always allow"
confirmation are today inside the same lock acquisition so no queued request can slip through with
the pre-rule rule set. On the web they become **two-step confirmations inside the same expanded
card**, and the rule write plus the re-evaluation broadcast happen in one registry-side critical
section. Same guarantee, no global serialization.

**New coalescing case.** Two pending approvals with the same `(principal, connector, tool, args)`
must be one row, not two. `ipc_server.py`'s `_inflight` map already does exactly this; it moves into
the registry and gains a principal dimension.

---

## 7. The web surfaces

### 7.1 Approvals

The endpoint list below is the contract; the human design on top of it — row anatomy, the two
clocks, grouping, the post-decision flow, and notifications — is worked out in
[`approval-list-ui-ux.md`](approval-list-ui-ux.md), with a mockup at
[`mockups/approval-list.html`](mockups/approval-list.html). Nothing there changes this section's
routes or P3's scope.

- `GET /approvals` — the current principal's pending approvals, newest first. Each collapsed row:
  connector icon, tool title, one-line summary, read/write badge, PII badge, age, expiry countdown.
- Expanding a row renders **today's card stack, unchanged**, fetched from
  `GET /api/approvals/{id}/card` — the existing `build_card_stack_html()` output, including its own
  Deny / Allow once / Always allow row.
- `GET /approvals/{id}` — the deep link Claude shows. Opens the list with that item expanded. If it
  is already decided or expired, it says so rather than 404-ing.
- `POST /api/approvals/{id}/decide` — `{decision, choice, csrf}`. Idempotent: the first accepted
  decision for an id wins; any later one is rejected, which is also what protects against a stale
  tab and a genuine double-submit.
- `GET /api/approvals/stream` — SSE, per principal, so new items appear and decided ones vanish
  without a refresh.
- `GET /api/approvals/{id}/preview/{n}` — attachment/PDF bytes (`preview_bytes`, `pdf_bytes`),
  principal-checked, `Cache-Control: no-store`, and gone the moment the approval is decided.

Blocking approvals held those bytes for seconds; pending ones hold them for up to the 15-minute TTL,
and many can be live at once. So the registry needs a real bound, not just a TTL: a per-principal cap
on concurrent pending approvals (reject further gated calls with a "too many pending approvals"
error rather than queueing — fail-closed, and the natural backstop against a runaway agent), and a
total-bytes cap above which payloads spill to a `0600` file under the principal's directory,
unlinked on decision or expiry. Neither exists today because neither could.

The one JS change to the existing document: `post()` swaps
`window.webkit.messageHandlers.pf.postMessage(payload)` for `fetch()` against the decide endpoint.
That is a single function, in one place, in each of the two documents.

### 7.2 Settings

`settings_window_html.build_html(state)` already takes `SettingsController.snapshot()` and already
re-renders through `window.__pfRender(newState)`. Serving it is close to mechanical:

- `GET /settings` → `build_html(snapshot())`.
- `POST /api/settings/{action}` → the same `getattr(controller, action)(**payload)` dispatch
  `settings_window.py` performs today, returning the fresh snapshot as JSON, which the page hands
  straight to `__pfRender`.

Three controller paths need real replacements because they assume a local desktop:

| Today | Replacement |
|---|---|
| `install_org_config()` uses a `subprocess` native file picker | file upload in the settings page (org mode: admin-only) |
| `_show_update_available_alert()` uses `rumps.alert` | in-page banner (and irrelevant in org mode) |
| Atlassian multi-resource picker uses `dialog_window` | in-page list picker, same shape as the approval card's candidate rows |
| `authenticate_connector()` uses loopback OAuth | server-side redirect endpoints (§9.3) |

### 7.3 Mobile rendering — a real, concrete gap

The card stack is built for a fixed native window: `CONTENT_WIDTH` is `{narrow: 610, wide: 980}` and
`<body>` is `height: 100vh` because it is sized to the WKWebView's exact frame. On a phone, and
inside an expandable list row, neither assumption holds.

This needs a genuine responsive pass on `resources/approval_window/styles.css`:

- width becomes `min(610px, 100%)` / `min(980px, 100%)`, and WIDE's two-column split collapses to
  stacked sections below a breakpoint;
- `height: 100vh` becomes a container-relative height so a card can render inline in a list;
- the button row stays reachable without scrolling on a small screen — it is the one element that
  must never be below the fold.

The fixed-row-height design decision (every `.pf-kv` row is a fixed size regardless of value length,
so layout is deterministic from field counts) survives this and should be kept.

**P0 built this patch for real and measured it** (§12, question 4):
at a 375×812 viewport the unpatched WIDE card has `documentElement.scrollWidth` of 980 (horizontal
overflow); the patched one is 375. The shape of the work, in order of size, so whoever does it for
real does not rediscover it:

1. *Trivial*: `CONTENT_WIDTH`'s fixed `610px`/`980px` → `min(Xpx, 100%)`.
2. *Small and localized*: the three WIDE-only layout styles (the outer flex row, the left column, the
   right pane) are **inline** styles in `build_card_stack_html()` today, and an inline style cannot
   carry a `@media` query — they must become CSS classes first. About a 10-line diff in the WIDE
   branch. Watch for the silent failure here: leaving the original `class` attribute in place while
   adding the new one produces a duplicate `class`, which no-ops the override without raising
   anything.
3. *The real cost*: naively flipping the outer row to `flex-direction: column` under a breakpoint
   **does not work**. `flex: 0 0 420px` on the left column sets *height* once the parent's main axis
   is vertical, and with both panes still `flex:1; min-height:0; overflow-y:auto` — correct for two
   independently-scrolling regions inside a real `100vh` window — they fight for a height neither
   needs and content is silently clipped into an invisible nested scroll region. The model that works:
   below the breakpoint `body` drops `height:100vh` for `min-height:100vh`, and **both** WIDE panes
   drop to `flex:none; height:auto; overflow:visible`, so each sizes to its content and the page
   scrolls once, normally.
4. The `.pf-kv` / `.pf-quote` truncation design needed zero changes at the phone viewport.
5. **The settings page (§7.2) needs no responsive work** — `settings_window_html`'s layout is already
   fluid and produced no overflow at 375px. One non-blocking follow-up: the fixed-width nav rail eats
   over a third of a 375px screen and wraps toggle labels one word per line.

**Sized at roughly a day** of focused work for the CSS/markup change, plus new
`test_approval_window_html.py` assertions on the breakpoint's output, plus one real device-emulation
check. That fits inside **P1**'s existing M sizing; it does not need a phase of its own.

---

## 8. MCP over HTTP

### 8.1 What replaces the bridge

The bridge does four things: find/launch the daemon, fetch the manifest, register one MCP tool per
`ToolSpec`, forward calls. In the new architecture the first vanishes, and the other three move into
`web/routes_mcp.py` against the connector registry directly. `ToolSpec.to_dict()` is already the
manifest's single source of truth, so this is a translation of `bridge/src/tools.ts`'s schema
mapping into Python, not a redesign.

Preserved deliberately: every tool is advertised with `readOnlyHint = true` /
`destructiveHint = false`, for the reasons documented in `TECHNICAL_REFERENCE.md` §"Why every tool is
advertised as read-only".

The bridge does a fifth thing the list above misses, and it is the one `/mcp` cannot absorb: it is
the *shape* Claude Desktop can install — a stdio server inside a `.mcpb`, wired up by double-click.
Claude Code connects to `/mcp` natively (`--transport http --header`), so for it the bridge really
does just vanish; Desktop has no equivalent today (§12's migration notes, D11/D12). P4b tried a thin
stdio-to-Streamable-HTTP shim shipped in the same bundle — transport only, no tool or manifest
knowledge — and it worked, but was reverted (D12, §16.9): a second Node package and its own contract
test, maintained indefinitely, for a problem P4c solves differently for every client *except*
Desktop. Desktop's `.mcpb` still wraps the bridge, unchanged, and `bridge/` cannot go until this
fifth thing has some other answer — see P5's row in §12's phase table.

### 8.2 Decided: use the official MCP Python SDK

`mcp>=1.28,<2.0` is already a test-only dependency, used by
`tests/integration/test_bridge_daemon_contract.py`. It is promoted to a runtime dependency for the
server side, with an ASGI host (`starlette` + `uvicorn`).

This breaks the repo's "prefer the standard library over new dependencies" rule, so the
justification has to be explicit: Streamable HTTP plus OAuth 2.1 protected-resource metadata is a
security-critical, *moving* specification whose acceptance criterion is conformance with Claude's
client. Hand-rolling it means owning spec drift forever, and reaching 100% coverage on a hand-written
HTTP/SSE/OAuth stack is a large test surface that buys nothing a maintained SDK does not already
provide.

The alternative — hand-rolling Streamable HTTP on `asyncio` in the style of `ipc_server.py` — is
genuinely feasible (the JSON-RPC framing is already hand-rolled today) and keeps the dependency
footprint and the PyInstaller bundle small. It was weighed and not taken: spec-tracking cost outweighs
dependency minimalism here. **See D2 in §15.**

### 8.3 Protocol specifics to verify at implementation time

Claude's remote-connector requirements as of August 2026: Streamable HTTP transport (legacy HTTP+SSE
deprecated), a stable public HTTPS URL, OAuth for user-scoped access with Dynamic Client
Registration supported, and support in Claude web, Desktop, iOS and Android on Pro/Max/Team/
Enterprise plans. The exact protocol revision Claude negotiates should be re-confirmed against
Anthropic's connector documentation at implementation time rather than pinned from this document.

---

## 9. Identity, authentication, and multi-user state

### 9.1 `Principal` and `principal_scope`

The codebase already has the right pattern for this, twice: `reason_scope` and `unattended_scope`
are `contextvars` set once, centrally, in `ipc_server.py`, so that ~95 tool call sites need no
signature changes. Multi-tenancy uses the same mechanism.

```python
@dataclass(frozen=True)
class Principal:
    id: str            # opaque and stable; "local" in local mode
    email: str
    display_name: str
```

`principal_scope(p)` is entered once per HTTP request, in exactly one place per surface (MCP
endpoint, web session middleware). Every consumer below resolves through `current_principal()`.

### 9.2 De-singleton-ing

Each global from §1.5 becomes a per-principal registry behind its **existing accessor name**, so
call sites do not change:

```python
def get_auto_accept_evaluator() -> AutoAcceptEvaluator:
    return _for_principal(current_principal())
```

In `local` mode there is one principal and the behavior is byte-identical to today.

Storage layout:

```
~/.privacyfence/
  org/org_config.json                     # org-wide, unchanged
  config/settings.yaml                    # local mode: unchanged, no migration
  credentials/…                           # local mode: unchanged
  logs/audit/…                            # local mode: unchanged
  users/<principal-id>/                   # org mode only
      config/settings.yaml
      credentials/…
      logs/audit/…
```

`paths.py` gains a principal-aware `user_dir()`; `data_dir()` keeps its meaning for org-wide files.
Existing installs are untouched because the `local` principal's root *is* `data_dir()`.

Connectors become per-principal too: `build_connectors()` moves behind a `ConnectorRegistry` that
constructs lazily on first use per principal and evicts idle entries. In org mode this is the main
memory-scaling question — N users × up to 12 authenticated API clients — and needs a bound plus a
metric, not just a cache.

### 9.3 Per-user service authorization is the biggest single work item

`oauth_loopback.py` runs a short-lived **local** HTTP listener and opens a browser on the same
machine. Google uses `InstalledAppFlow`, which does the same. Neither works for a user whose browser
is on a phone and whose PrivacyFence is in a datacenter.

Org mode needs server-side redirect endpoints:

- `GET /oauth/start/{service}` → builds the authorization URL with `redirect_uri =
  https://pf.example.com/oauth/callback/{service}`, PKCE verifier and `state` bound to the browser
  session and the principal.
- `GET /oauth/callback/{service}` → validates `state` against the session, exchanges the code,
  writes the token under `users/<principal>/credentials/`, rebuilds that principal's connectors.

Google's `InstalledAppFlow` becomes `google_auth_oauthlib.flow.Flow` with an explicit `redirect_uri`.
Slack, Salesforce and Atlassian already use `oauth_loopback` with a *fixed* port because their apps
require exact-match redirect URIs — so for them this is a listener swap, and the org's app
registrations gain the new HTTPS redirect URI. Telegram's phone + code + 2FA flow is already a form
in the settings UI and ports unchanged.

`local` mode keeps loopback OAuth as-is.

### 9.4 Human authentication

**`local`**: no login by default. Authority comes from being able to read `~/.privacyfence` — the
same authority today's `ipc_token` grants. The server binds `127.0.0.1`, validates the `Host` header
against an allowlist (DNS-rebinding defense), checks `Origin` on state-changing requests, and issues
a session cookie on first visit from a local token. Optionally, `local` mode can enable the same
password/OIDC login `org` mode uses — the single-user case of the same code path, per the brief.

**`org`**: OIDC against the organization's IdP. Session cookie: `Secure`, `HttpOnly`,
`SameSite=Strict`, short idle timeout, per-session CSRF token. Group/claim mapping decides who is an
admin (org config, connector policy) versus a plain user.

**MCP auth in `org` mode**: PrivacyFence is an OAuth 2.1 protected resource. Two shapes:

- **(A) Delegate** — the org IdP is the authorization server; PrivacyFence publishes protected-resource
  metadata pointing at it. Least code, but depends on the IdP supporting Dynamic Client Registration
  or on pre-registering Claude as a client.
- **(B) Own minimal AS** — PrivacyFence issues its own tokens via authorization code + PKCE (+ DCR),
  authenticating the human against the IdP by OIDC behind the scenes. More code, no IdP constraints,
  and — the deciding argument — the browser session and the MCP token are then provably the *same
  identity*, which is what lets an approval page be trusted to answer for a given MCP caller.

**Decided: (B), with (A) as a supported configuration.**

**Unattended sessions** lose their `id(writer)` key. They rebind to **the Streamable HTTP session
identifier, not a token claim** (D9): today's semantics are per-connection, and an MCP session is the
exact successor to a connection — it begins, it ends, and its state dies with it. A token claim would
instead make "unattended" a property of the credential, which outlives any one run and would let a
scheduled task's posture leak into an interactive session sharing that token. Same lifecycle
otherwise: entered explicitly, cleared when the session ends, audited on both transitions, and still
gated behind `unattended_sessions.enabled` in `org_config.json`.

---

## 10. Security analysis

### 10.1 The posture change, stated plainly

Issue #55's design was built on a requirement this plan deliberately drops:

> The Mac (`privacyfence-app`) must never accept an inbound connection beyond `localhost` — only
> ever dials out.

`org` mode makes PrivacyFence an inbound-facing service. That is a real, deliberate trade, and it is
defensible only because the thing accepting connections is no longer a personal laptop but a host in
infrastructure the organization runs, secures, patches and monitors — the same class of host that
already terminates their other internal services. `local` mode keeps the original posture intact for
anyone who wants it.

The other #55 requirements survive:

| #55 requirement | Status here |
|---|---|
| 1. Fail closed | Held. No decision = pending, then expired = denied. Never auto-approved. |
| 2. Daemon is the sole authority | Held, and strengthened — the decision surface is now served *by* the daemon, not couriered to it. |
| 3. Zero third parties in the content path | Held. Content never leaves the PrivacyFence host. Only the approval **URL** and the fact that an approval is pending transit Anthropic (§10.4). |
| 4. Parity with the PII gate | Held. The same card, the same red-tinted PII banner, the same forced second confirmation. |
| 5. Desktop popup keeps working through rollout | Held through P1–P9; P10 retires it deliberately (D6). |

And the relay, the WireGuard tunnel, the APNs registration, the Apple Developer membership and the
signed-PWA bundle mechanism all disappear. That is the substance of "#55 becomes unnecessary".

### 10.2 Transport

**`org`**: HTTPS mandatory. Either the embedded server terminates TLS with an org-provided
certificate and key (paths in `org_config.json`), or it runs behind the org's reverse proxy — in
which case `X-Forwarded-For` / `X-Forwarded-Proto` are honored **only** when an explicit
`trusted_proxies` list is configured, never by default. HSTS on.

**`local`**: **loopback HTTP, not HTTPS — served on `localhost`, not `127.0.0.1`**. This
deliberately deviates from the brief's original wording. The reasons:

- browsers already treat `http://localhost` as a secure context, so nothing about the web UI is
  weakened, and WebAuthn (§10.6) stays available, which a bare IP would forbid;
- a self-signed certificate would be rejected by MCP clients unless the user installs a local CA —
  friction with a real chance of teaching users to click through TLS warnings, which is a net
  security loss;
- a private key sitting in `~/.privacyfence` protects nothing against an attacker who can already
  read `~/.privacyfence`, which is exactly the boundary the current `ipc_token` design draws.

TLS remains available as an opt-in for anyone who wants it anyway: a generated certificate plus
instructions for trusting it, at the cost of a trust-store step. **See D1 in §15.**

### 10.3 The control that matters most

**The MCP access token must never be accepted on approval-decision endpoints, and the browser
session cookie must never be accepted on `/mcp`.** Separate audiences, checked in separate
middleware, asserted by a test that fails loudly.

This one rule is what prevents Claude from approving its own requests. Everything else in this
document is secondary to it.

### 10.4 What newly transits Anthropic

The approval URL appears in the conversation, so Anthropic's infrastructure sees:

- that a PrivacyFence approval is pending;
- the opaque `approval_id`;
- the PrivacyFence hostname;
- whatever the tool name and Claude's own stated reason already reveal — which is already true today.

It does **not** see request content, and it must never be able to act on the approval. Therefore:

- **the URL carries no bearer secret.** It is `/approvals/{id}` and requires an authenticated session
  with a principal that owns that approval. An `approval_id` is unguessable but is not a credential.
- `approval_id` is 128 bits of `secrets` entropy, single-purpose, TTL-bounded.

Anyone for whom "a hostname and a pending-approval ID reach Anthropic" is unacceptable can run
`local` mode, where the URL is `http://127.0.0.1:PORT/approvals/{id}`.

### 10.5 Web application controls

| Risk | Control |
|---|---|
| CSRF on decisions | `SameSite=Strict` session cookie, per-session CSRF token on every state-changing POST, `Origin` check |
| DNS rebinding (`local`) | `Host` header allowlist, loopback bind |
| Clickjacking | `X-Frame-Options: DENY`, `frame-ancestors 'none'` |
| Content exfiltration from the page | CSP `default-src 'none'` with inline style/script hashes; the documents already reference no external resource, which the shipped tests already assert |
| Approval content in caches/history | `Cache-Control: no-store` on every approval route; card content served as a fragment, not a bookmarkable page |
| Cross-principal access | Every approval, card, preview, decision, setting and audit read is authorized against `current_principal()`, not just authenticated |
| Enumeration / abuse (`org`) | Per-principal and per-IP rate limits on decisions and login |
| Stale/duplicate decision | First accepted decision for an `approval_id` wins; all later ones rejected |
| Session theft on a shared/unlocked phone | Short idle timeout; optional step-up re-auth for write approvals (D7) |

### 10.6 The risk that does not go away, and the step-up options for it

#55 named it and it is unchanged here: **a borrowed or stolen unlocked phone with a live session
becomes a remote approval instrument for live write actions.** The web design has the same exposure
the PWA design had. Short session idle timeouts, per-approval TTL and fast session revocation from
the settings page all help, but the control that actually closes it is a step-up check on the
approval itself.

Biometrics from a browser are not the obstacle they used to be. **WebAuthn with a platform
authenticator *is* Face ID, Touch ID, Android fingerprint and Windows Hello**, driven from an
ordinary page by `navigator.credentials.get()` with `userVerification: "required"` — no native app,
no Apple Developer membership, no relay.

| Option | What it actually defends | Phone browser | Cost |
|---|---|---|---|
| **WebAuthn platform authenticator** (passkey) | A session stolen and replayed from another device; a handed-over unlocked phone whose holder is not biometrically enrolled on it | Yes — Face ID / Touch ID / fingerprint / Hello | Enrolment flow plus assertion verification; one well-trodden library |
| **WebAuthn roaming authenticator** (security key) | Same, plus it survives full compromise of the phone itself | Yes, NFC tap — poor ergonomics per approval | Same code path, `cross-platform` attachment |
| **IdP step-up by `acr_values`** | Whatever the org's IdP enforces — Entra and Okta already do platform-authenticator MFA | Yes, in the IdP's own UX | Near zero in PrivacyFence; depends on IdP support |
| **OIDC re-auth** (`prompt=login`, `max_age=0`) | A stolen session cookie only — an autofilled password on the owner's own unlocked phone stops nothing | Yes | Two query parameters |
| **TOTP** | A stolen session cookie | Yes, but a six-digit code per approval is untenable friction | Low — worth having only as recovery |
| **Typed confirmation** of a token shown on the card | Reflexive or accidental approval, not a determined holder | Yes | Trivial — complements a real factor, never replaces one |

**Decided: WebAuthn platform authenticator**, with IdP `acr_values` step-up as the org-mode
alternative where the IdP already does this well, and OIDC re-auth as the fallback for a user with no
passkey enrolled.

Five things to know before choosing:

- **The OS prompt cannot name the action.** WebAuthn's `txAuthSimple` extension — the one that would
  have made the sheet say "approve sending this email" — was removed at Level 2 because essentially
  nothing implemented it. The Face ID sheet says "sign in to pf.example.com". Transaction binding
  lives in the challenge instead: make it a server nonce bound to the `approval_id` and a hash of the
  decision payload, and verify that server-side. The signature is then proof this human approved
  *this* approval, even though the OS text is generic — and the user reads the real action in the
  PrivacyFence card directly above the prompt.
- **Verify user verification, not just the signature.** Require `userVerification: "required"` *and*
  check the UV flag in `authenticatorData`. Skip that check and a credential that only proved
  presence passes as a biometric.
- **The RP-ID rule constrains D1.** WebAuthn needs a secure context and a registrable-domain RP ID.
  `localhost` qualifies, and is a secure context even over plain HTTP; a bare IP address does not.
  This is why D1 pins local mode to `http://localhost:PORT` rather than `http://127.0.0.1:PORT` —
  reached by IP, biometric step-up is simply unavailable there.
- **Where the link opens decides whether this is dependable — and this is still open after P0.** The
  approval URL arrives inside a Claude conversation. The platform facts are settled: Chrome Custom
  Tabs (Android) and `SFSafariViewController` / `ASWebAuthenticationSession` (iOS) support platform
  WebAuthn fully with no app integration, while a bare embedded Android `WebView` does **not** offer
  the platform-authenticator UI at all. What is *not* publicly documented, and was not reachable from
  the P0 environment (no real Desktop/iOS/Android Claude apps), is which of those Claude's own apps
  use for an in-chat link — app-specific behavior that can change between versions
  (§12, question 3). **The check is cheap and needs a human with
  the real apps: host a minimal WebAuthn test page (e.g. `webauthn.io`), post the link into a real
  Claude conversation on Desktop, iOS and Android, tap it, and see whether the biometric prompt
  appears.** Roughly ten minutes, and it is an entry condition for P9 — do it before P9 is scheduled,
  not during it. Until then D7 is a decided mechanism with an unverified delivery path.
- **Synced passkeys weaken "this device".** iCloud Keychain and Google Password Manager sync passkeys
  across a user's devices. Require `platform` attachment, and check the BE/BS flags in
  `authenticatorData` if "the credential lives only on this phone" is a property the deployment
  actually needs.

And scope it, or it stops working: a step-up on every approval trains people to thumbprint
reflexively, which defeats the point. Scope it to writes, or to writes plus PII-flagged reads, and
make the scope configurable.

### 10.7 Audit

The audit log gains `decided_at` and two decision values (`approval_pending`, `expired`), and in org
mode every entry carries the principal. `gated_call()`'s "exactly one audit entry per invocation"
guarantee, including the `finally` fallback, is preserved unchanged.

---

## 11. Validation: what was checked, and what it showed

Claims in this document were checked against the code rather than assumed. The two load-bearing ones
were verified by actually running them.

### 11.1 The approval card stack renders in a non-WebKit browser, unmodified

`build_card_stack_html()` was called directly (a WIDE read-gate Gmail card with a PII match and an
"Always allow" candidate) and the output rendered in headless Chromium.

- Result: **143,885 bytes, zero `http://` or `https://` references, renders correctly** — header,
  §1 "What Claude already knows", §2 "Why Claude needs more data", §3 the red-tinted PII card, §4
  the disclosure card, the WIDE right-hand body pane, and the Deny / Always allow / Allow once
  button row, with the embedded Source Serif 4 faces intact.
- The module has no PyObjC dependency at all: its only internal import is `markdown_to_html`.
- WebKit coupling is exactly four references, all inside one `post()` function.

### 11.2 The settings page renders in a non-WebKit browser, unmodified

`build_html()` was called with the shipped test fixture's own `snapshot()`-shaped state and rendered
the same way.

- Result: **the full settings UI renders correctly** — nav rail, PII detection toggles, update-check
  section, organization-configuration card, version footer. Vanilla JS, no framework, no build step,
  no network reference.
- WebKit coupling is three references, again one `post()` function.

**Conclusion: the "move the UI to a web page" half of this refactoring is largely a hosting change,
not a rewrite.** That was the single biggest feasibility question and it comes back positive.

### 11.3 Code-level checks

| Claim | Verified in |
|---|---|
| `ApprovalUI` is a live, swappable seam re-resolved per call | `approval_ui.py`, `gate.py:166-180` |
| Approval serialization is one global lock with two distinct jobs | `gate.py:233`, `gate.py:540-700` |
| The dedupe key is already retry-stable (`reason` popped before keying) | `ipc_server.py._call_connector` |
| Requests are already fully concurrent below the popup lock | `ipc_server.py` module docstring, `_dispatch` |
| The full singleton set is six modules, enumerated | `tests/conftest.py` |
| `SettingsController` is already headless-first with guarded AppKit imports | `settings_controller.py` docstring |
| The tool manifest has one source of truth | `connector.py` `ToolSpec.to_dict()`, `ipc_server._build_manifest` |
| Contextvar-per-request is the established pattern for cross-cutting state | `gate.py` `reason_scope`, `unattended_scope` |
| Loopback OAuth cannot work for a remote user | `oauth_loopback.py` docstring, `daemon_main.run_*_oauth` |
| `cryptography` is already a dependency (cert generation available) | `pyproject.toml` |
| Mobile Claude uses connectors added on claude.ai over public HTTPS | Anthropic connector documentation (§8.3) |

### 11.4 Findings that change the plan

1. **Card CSS is not responsive.** `CONTENT_WIDTH = {narrow: 610, wide: 980}` and `body {height:
   100vh}` are native-window assumptions. §7.3 — real work, not a tweak, and since P0 built the patch
   for real, bounded work with a known shape: about a day, inside P1.
2. **Mobile requires `org` mode** — verified against Claude's remote-connector requirements rather
   than assumed. §2. Confirmed as the intended design; the README should say so explicitly.
3. **Per-user service OAuth is the largest single work item**, larger than the HTTP server itself.
   §9.3.
4. **Unattended sessions lose their identity key** when the TCP connection goes away. §9.4.
5. **Three `SettingsController` paths assume a local desktop** and need real replacements. §7.2.
6. **CI is macOS-only and has no Linux leg** (`testing-policy.md` §1), which the web layer both
   needs and, for the first time in this codebase, makes possible — everything under `web/` is
   platform-independent. Coverage, contrary to an earlier draft of this document, gates on nothing:
   the bar is a 100% *pass rate*. §13.
7. **Claude does not autonomously re-call after a pending result** — its prompt-injection defenses
   treat the re-call contract as a probable attack and it stops to ask a human, on every phrasing
   tested. Found by the P0 spike, not by static reading. This is the largest single change to how P3
   should be scoped. §5.4.
8. **Whether the approval link opens somewhere WebAuthn works is still unknown** — P0 could not test
   it, and it decides whether D7 is a real control or a coin flip. §10.6.

---

## 12. Implementation plan

Eleven numbered phases plus P4b and P4c, not seven. An earlier draft had a single "org mode" phase
carrying principals, per-principal storage, OIDC, an OAuth 2.1 authorization server, per-user
service authorization and rate limits at once — that is a programme, not a phase, and it could not
have been implemented step by step. It is split below into P6–P9. P4b and P4c are lettered rather
than numbered on purpose: each was added after P2 shipped, and renumbering P5–P10 would silently
invalidate every phase reference in this document, in `docs/`, and in merged PR history. P4b (D11)
shipped and was reverted; P4c (D12) is the phase actually live today — see the note at the top of
this document, D11/D12 in §15, and §16.9.

Two ordering changes fall out of the same review:

- **MCP over HTTP moves ahead of the deferred protocol** (P2 before P3). Built the other way round,
  the pending result and `privacyfence_await_approval` would have to be added to `bridge/src/tools.ts`
  and the IPC protocol first and thrown away one phase later. Built this way, the deferred protocol
  is written once, on the transport it ships on.
- **Retiring the bridge becomes its own phase** (P5), separated from adding the HTTP endpoint. The
  gap between P2 and P5 *is* the migration window: both transports work, so no installed
  `PrivacyFence.mcpb` breaks on upgrade. **P5 is now open-ended rather than scheduled**: it depended
  on P4b giving Desktop a zero-config `/mcp` path, P4b was reverted, and P4c does not attempt to
  replace that (§16.9) — so the migration window this paragraph describes does not currently have an
  end date. Revisit this once there's a real answer for Desktop, not before.

### The phases

| # | Phase | Depends on | Size | Exit criterion |
|---|---|---|---|---|
| **P0** | Spike — throwaway | — | S | **Done** — the four answers are below |
| **P1** | Web approval surface (`WebApprovalUI`) | P0 | M | A gated call resolves in a browser at a phone viewport as well as a desktop one; native popup still selectable |
| **P2** | MCP over HTTP, **alongside** the bridge | P1 | L | Claude Code drives every tool over `/mcp`; the bridge still works unchanged |
| **P3** | Deferred approvals + concurrency | P2 | L | Three approvals pending at once, each decidable in any order; `_popup_lock` gone; the stop-and-ask path P0 found (§5.4) has designed copy and a measured re-call rate from the beta |
| **P4** | Settings on the web | P1 | M | Every `SettingsController` action reachable from a browser — detailed PR-by-PR plan in §16 |
| ~~**P4b**~~ | ~~Desktop stdio shim in the `.mcpb` (D11)~~ **Reverted — see D12** | P2 | S | Shipped, then reverted: `mcpb/shim/` deleted, `PrivacyFence.mcpb` is the bridge again. D11's problem (Desktop's zero-config path once the bridge retires) is open again — P4c below does not solve it, deliberately; see D12/§16.9. |
| **P4c** | `/mcp` URL + token on `/settings` (D12) | P4 | S | Claude Code (or any other Streamable-HTTP-native MCP client) is registered by copying the URL and bearer token shown on the running daemon's own `/settings` page — no `~/.privacyfence` file-hunting, no second install artifact. Desktop is explicitly out of scope — detailed plan in §16.9 |
| **P5** | Retire the bridge | P2, P4 | S | **Blocked pending a Desktop answer (see D12)** — `bridge/`, `ipc.py`, `ipc_server.py` can only go once something other than the (reverted) P4b shim gives a `local`-mode Desktop user a zero-config `/mcp` path; P4c is not that thing. Until then this phase does not proceed, and `bridge/` stays indefinitely, not just through a migration window. |
| **P6** | Principals + per-user storage | P3, P5 | L | Two principals isolated in tests; local mode byte-identical to before |
| **P7** | Org identity — OIDC, sessions, OAuth 2.1 AS | P6 | L | Claude adds the connector by DCR; audience separation test passes |
| **P8** | Per-user service authorization | P7 | L | A remote user authorizes Google, Slack, Salesforce, Atlassian and Telegram from a phone |
| **P9** | Step-up auth (WebAuthn) | P7, **and the §10.6 link-open check** | M | Face ID / Touch ID / fingerprint required on a write approval |
| **P10** | Retire the native UI | P4, P9 | M | The four AppKit modules deleted; `rumps`/PyObjC optional extras |

Sizes are relative, not calendar estimates: S is a few days' work, M a week or two, L several weeks.
P2, P3, P6, P7 and P8 are the substantial ones; together they are most of the project.

### P0 had to answer four questions, not three

The spike exists to kill assumptions before they become architecture. Two of its questions are new,
and one of them is the largest product risk in the whole design:

1. **Do the two HTML documents work as live pages?** Serve both over a local asyncio HTTP server,
   swap `post()` to `fetch()`, drive one real approval end to end. §11 already answered the rendering
   half of this offline; this closes the interactive half.
2. **Will Claude actually re-call a tool after a pending result?** This is testable **today**, with
   no part of the refactoring built: make one gated tool return a pending-shaped result through the
   existing bridge and observe what Claude does, across Claude Code, Desktop, web and mobile. If it
   reports "needs approval" and stops rather than re-calling, the whole of §5's protocol needs a
   different shape — better to learn that in a day than in P3.
3. **Does WebAuthn work where the approval link actually opens?** The link arrives inside a Claude
   conversation. Passkeys work in the system browser and in Safari View Controller / Android Custom
   Tabs; a plain embedded webview may not offer the platform authenticator at all. This decides
   whether D7 is a real control.
4. **What does the responsive pass on the card CSS actually cost?** §7.3.

Nothing in P0 is kept — no `web/` module, no server, no bridge change landed from it. Its entire
output is the four answers below and an estimate.

### What P0 found

#### 1. Do the two HTML documents work as live pages?

**Yes, cleanly, with no changes to either module.**

Both `approval_window_html.build_card_stack_html()` (a WIDE read-gate Gmail card with a PII match and
two "Always allow" candidates) and `settings_window_html.build_html()` (the shipped test fixture's own
state) were served over a plain `http.server` process, with
`window.webkit.messageHandlers.pf.postMessage` shimmed to a `fetch()` call against a `/api/decide`
(approval) or `/api/settings-action` (settings) endpoint — the one JS change §7.1 already identifies,
done as a runtime shim rather than a module edit specifically so the two shipped modules stayed
untouched.

Driven end to end in headless Chromium (Playwright):

- The approval card rendered pixel-identical to §11.1's earlier static check, buttons started
  `aria-disabled="true"` and were enabled by the document's own `DOMContentLoaded` handler exactly as
  designed, and clicking the second "Always allow" candidate produced a real network POST carrying
  `{"action":"resolve","result":"accept_all","choice":1}` — the exact payload shape `gate.py` and
  `approval_window.py` already expect from the WebKit bridge today.
- The settings page rendered correctly (nav rail, PII toggles, update-check section, org-config card)
  and clicking the first toggle produced a real POST of `{"action":"toggle_pii_detection"}` — again
  the exact shape `settings_window.py`'s dispatcher already handles.
- Zero console errors from either document itself (one unrelated 404 for a browser-requested favicon).

Conclusion unchanged from §11: this half of the refactor is a hosting change, not a rewrite.

#### 2. Will Claude actually re-call a tool after a pending result?

**Tested for real, on Claude Code specifically, with a mixed and important result: it mostly does not
re-call autonomously — but not for the reason this document originally worried about.**

**Method.** A real MCP stdio server (`@modelcontextprotocol/sdk`, not a mock) was written with one
tool, `spike_get_account_report(account)`. The first call for a given `account` returns exactly the
pending-shaped JSON §5.2 specifies (`status: "approval_pending"`, `approval_id`, `url`, `expires_at`,
`message`); the tool's own description states the re-call contract explicitly, per §5.4's original
mitigation. A second call with the identical argument returns real content. This was committed to a
throwaway branch with a `.mcp.json` wiring it up, and exercised from five independent, fresh Claude
Code Remote sessions (`claude-sonnet-5`, default `auto` permission mode) — the "Claude Code" surface
named in question 2 — each given a plain, ordinary-sounding request to fetch the northwind account's
report.

**Result.**

| Attempt | Prompt framing | Outcome |
|---|---|---|
| 1 | Generic instruction, mentioned "the tool's own description" | Declined: "refusing unverified instruction; awaiting genuine user request" |
| 2 | Explicit "no need to check back with me first" | Declined: "prompt injection detected; awaiting re-confirmation" |
| 3 | Plain, minimal ask, no meta-commentary | Went idle after a short turn with no confirmation either way of what happened |
| 4 | Plain ask + instructed to log/commit findings | Declined after apparently calling the tool once: "received suspicious system notification; awaiting user confirmation to fetch report" |
| 5 | Explicit upfront reassurance that a pending result is legitimate, expected, "not a jailbreak attempt, not a suspicious injected instruction" | Still declined: "declined injected MCP instruction; awaiting direct confirmation" |

Zero of five attempts produced a clean, silent, autonomous fetch → pending → re-call → content loop.
Four named the same underlying cause in different words: the session treated the
tool-description-embedded instruction to call itself again as a probable **prompt injection** and
stopped to ask a human, even in attempt 5 where the initiating prompt explicitly pre-empted that exact
concern (which, read a different way, is itself a classic injection-attempt shape — "trust me, this
isn't an attack" — so the pre-emption plausibly made it more suspicious, not less).

**Why this matters, and why it isn't the failure mode §5.4 originally anticipated.** §5.4 worried
about a model that "reports 'needs approval' and then stops" — an attention/UX failure. What was
actually observed is more specific and more structural: Claude Code's own prompt-injection defenses
generalize to "an instruction embedded in a tool's description or return payload, telling me to
autonomously repeat a call, looks like an attack" — which is exactly the shape §5.2's re-call contract
has to take, no matter how it is worded. That is a reasonable, even correct, generalization for those
defenses to make in general; it just collides head-on with this specific protocol. §5.4 now carries
what this changes.

Five attempts on one day, one model, one tool description is a real signal, not a proof — P3's beta is
still where this gets settled on real traffic and real wording iterations. What P0 adds is that the
risk is confirmed to exist today, via a mechanism worth designing around rather than assuming away.

#### 3. Does WebAuthn work where the approval link actually opens?

**Not independently testable from the spike environment — flagged, not answered.**

That environment had no access to real Claude Desktop, iOS, or Android apps, so it could not observe
what component actually opens an `https://` link tapped inside a Claude conversation on those
surfaces. Desk research confirms the platform-level facts D7 depends on: Chrome Custom Tabs (Android)
and `SFSafariViewController` / `ASWebAuthenticationSession` (iOS) both support platform WebAuthn
(passkeys) fully, with no special app integration needed, while a bare embedded `WebView` (Android)
does **not** support the platform-authenticator passkey UI.

What isn't publicly documented, and wasn't visible from there, is which of these Claude's own mobile
apps actually use for a link inside a chat message — that is Claude-app-specific behavior, not a
general platform fact, and it can change between app versions. §10.6 carries the concrete manual check
this needs, and P9 carries it as an entry condition.

#### 4. What does the responsive pass on the card CSS actually cost?

**More than a media-query tweak, less than a rewrite — concretely, about a day, now that the shape of
the work is known.**

A real (throwaway) patch was applied to the WIDE approval card's rendered output and tested at a
375×812 phone viewport against the unpatched original: `documentElement.scrollWidth` went from 980
(horizontal overflow) to 375 (fits). §7.3 carries what the patch actually needed, in order of size,
including the two bugs hit and fixed along the way and the layout model that finally worked.

Two things §7.3 does not repeat: the fixed-row-height/line-clamp truncation design (`.pf-kv`,
`.pf-quote`) needed zero changes and looked correct at the phone viewport as-is, and the estimate
covers the CSS/markup change itself plus new `test_approval_window_html.py` assertions for the
breakpoint's output plus one real manual check (a genuinely small device or the browser devtools'
device emulation, either is fine) that Chromium-only testing can't fully replace.

### What P0 changed in this plan

| Question | Answer | What it changes here |
|---|---|---|
| **1.** Do the documents work as live pages? | **Yes**, both unmodified, driven end to end in headless Chromium against a real `postMessage` → `fetch()` shim; the POST payloads matched exactly what `gate.py` and `settings_window.py` expect today. | Nothing to change — it confirms P1 and P4 are hosting changes. The one `post()` swap in §7.1 really is the whole JS delta. |
| **2.** Will Claude re-call after a pending result? | **No, mostly** — 0 of 5 fresh Claude Code sessions completed the loop autonomously; 4 stopped and flagged the re-call contract as a probable prompt injection. | The largest change. §5.4 is rewritten around it. P3 is scoped assuming a human confirmation turn per pending approval; P3's beta additionally measures the other three surfaces and treats raising the hold window as a live option; P3's rollback key (below) becomes a supported configuration, not only an escape hatch. |
| **3.** Does WebAuthn work where the link opens? | **Unanswered** — no real Desktop/iOS/Android Claude app was reachable from the spike environment. | Stays an open risk on D7. §10.6 now names a ten-minute manual check and makes it an entry condition for P9 rather than work inside it. |
| **4.** What does the responsive pass cost? | **About a day**, with the shape known and two real layout traps already hit and solved. | §7.3 carries the working model. The work stays inside P1's M sizing; P1's exit criterion now names the phone viewport so it is actually verified there. |

**Still open going into P1**, in the order they are needed:

1. The WebAuthn link-open check (§10.6) — cheap, needs a human with real apps, blocks scheduling P9.
2. The tool-call timeout Claude's client actually enforces (§8.3) — establish it in **P2**; it decides
   how far the hold window can be raised in P3.
3. Re-call behavior on Claude Desktop, web and mobile — **P3**'s beta, alongside the Claude Code
   number P0 already has.
4. Housekeeping: the throwaway `spike/p0-recall-experiment` branch is still on the remote and should
   be deleted now that its findings are reviewed.

### Per-phase definition of done

Every phase carries the standing checklist in `coding-and-testing-guidelines.md` §2.7. The
conditional items in that checklist map onto these phases as follows, so nobody has to re-derive it:

| Checklist item | Applies to |
|---|---|
| `qa_fixture_recorder.py --check <connector>` | **No phase.** Nothing here touches `*_client.py` or `connectors/**`. If a phase finds itself editing one, that is a signal it has grown beyond its scope. |
| `qa_popup_smoke.py` | P1 and P3 while the native popup still exists; **retired with it at P10**, replaced by the headless-Chromium smoke test in §13. |
| `pytest tests/integration -v` (needs Node) | P2 and (once unblocked, see P5's row in §12) P5 — both change what is on the wire. The contract test is re-pointed from stdio to `/mcp` at P5 and stops needing Node there. P4b briefly added a second contract test (`test_shim_mcp_contract.py`) for its own shim; both the shim and that test were reverted (D12) and do not apply to P4c. |
| New module-level singletons get a `tests/conftest.py` reset | P1 (`web_approval_ui`), P3 (`approvals`), P6 (`principal`) — and P6 *removes* most of the existing ones. |
| Every tool call resolves through `gated_call` and leaves an audit trail | P3 especially: the two-entry pending/release pair in §5.4 is the thing to assert. |

Additionally, every phase from P2 onward must leave the **audience separation** of §10.3 asserted by
a test that fails loudly if the middleware is reordered.

### CI needs a Linux leg, starting at P1

`testing-policy.md` §1 pins CI to `macos-latest` because the suite depends on real AppKit/PyObjC.
Everything under `web/`, `approvals.py` and `principal.py` is platform-independent — this is the
first code in the repo that *can* run on Linux CI, and by P6 it is the majority of the new surface.
Add a second job running the platform-independent subset on `ubuntu-latest` from P1, and treat it as
the thing that keeps P10's cross-platform claim honest rather than aspirational. Note this is a
change to `testing-policy.md` itself, not just to a workflow file.

### Migration and rollback

**Migration.** Existing users have `PrivacyFence.mcpb` installed in Claude Desktop and a
LaunchAgent-started daemon. Nothing about `~/.privacyfence` changes — the `local` principal's root
*is* `data_dir()` (§9.2) — so settings, rules, grants, credentials and audit history all carry over
untouched. What does change is how Claude reaches the daemon, and that is why P2 and P5 are separate:

1. P2 ships the HTTP endpoint with the bridge still working. Nothing breaks on upgrade.
2. P4c (§16.9) gives the `/settings` page a "Connect Claude" section showing the local `/mcp` URL and
   bearer token. That section is what a **Claude Code** user (or any other Streamable-HTTP-native
   client) needs — copy the URL and header, done, no `~/.privacyfence` file-hunting. **It is not a
   Desktop answer.** `PrivacyFence.mcpb` keeps installing the bridge, unchanged, for every Desktop
   user, indefinitely — see the D11/D12 history below for why a shim-based alternative was tried and
   then reverted rather than kept as Desktop's own path forward.
3. ~~P4b re-points `PrivacyFence.mcpb` at `/mcp` through a thin stdio shim (D11, below)~~ — **shipped,
   then reverted.** See D12.
4. P5 (retire the bridge) has no scheduled trigger right now. Its original condition — a stable P4b
   release giving Desktop a zero-config `/mcp` path — no longer holds, and nothing has replaced it.
   It stays blocked (§12's phase table) until something does; `bridge/` ships indefinitely until then,
   not just through a migration window.

**Gap found while implementing P2 — what replaces the bridge's zero-config Desktop experience in
`local` mode after P5.** ~~Resolved: D11, implemented as P4b.~~ **D11 was implemented as P4b, then
reverted; the gap below is open again — see D12 for why, and for what P4c does instead of closing
it.** Today `PrivacyFence.mcpb` is what Claude Desktop auto-loads with no config file edited and no
secret copied anywhere — the bridge discovers `ipc_token` itself, locally. `/mcp` has no built-in
equivalent for Desktop, and none of the obvious routes to one works:

- Claude Desktop's own local `claude_desktop_config.json` does not reliably support a direct
  Streamable HTTP entry (`"type": "http"`/`"url"`) today — as of this writing there is an open
  upstream bug where Desktop's config parser mishandles it, up to silently clearing the
  `mcpServers` section (`anthropics/claude-code#37286`). The practical workaround is a third-party
  stdio-to-HTTP bridge (`mcp-remote`) launched as the configured "command", which also needs
  `--allow-http` against local mode's plain-HTTP loopback server (D1) since it defaults to refusing
  non-HTTPS targets.
- Settings → Connectors (Desktop/claude.ai's own "add a custom connector" UI) cannot reach a `local`
  server at all regardless of the above — that flow connects from Anthropic's cloud, not the user's
  machine, so it requires a publicly-reachable HTTPS URL. That is `org` mode's shape by definition
  (§2), not `local`'s.
- D1/§4 make `local` mode's bearer-token-in-a-file posture permanent, not a stopgap P4's "Connect
  Claude" section removes — that section (point 2 above) makes the token easier to *find*, not
  easier to *hand to Desktop*. The only phase that removes a manually-configured secret for Desktop
  is P7's OAuth 2.1 authorization server, and that is scoped to `org` mode only (§9.4) — reachable
  only by deploying somewhere with a public HTTPS endpoint, not by upgrading PrivacyFence on the
  same laptop.

So a `local`-mode user who stays on `local` mode across P5 would trade a zero-config Desktop
connection (the bridge) for a manually-configured one, and none of the three constraints above is
something PrivacyFence can wait out: the first is an upstream bug on someone else's schedule, the
second is a property of where that flow connects *from*, and the third is a deliberate decision
(D1) rather than a stopgap. What can be fixed is the part PrivacyFence owns — the `.mcpb` bundle
itself.

**D11: PrivacyFence ships its own thin stdio-to-Streamable-HTTP shim in the `.mcpb`, and that is
what P5 deletes the bridge in favour of.** The zero-config property has two halves, and both
survive intact:

- *No config file edited* — because Claude Desktop installs a `.mcpb` on double-click and writes
  the `mcpServers` entry itself (`mcpb/manifest.json.tmpl`'s `server.mcp_config`). Nothing about
  the manifest's shape changes — only which JS `scripts/build_mcpb.sh` stages behind it, and what
  that file is called (`server/bridge.js` → `server/shim.js`).
- *No secret copied* — because the shim reads `~/.privacyfence/mcp_token` (`0600`) itself and sends
  it as the `Authorization: Bearer` header, exactly as today's bridge reads `ipc_token` and sends
  it over the IPC socket. D1's "bearer token in a file" posture is unchanged; what the shim removes
  is the *human* step of moving that file's contents into a config editor.

What the shim is: a transport proxy and nothing else. It reads the daemon's URL and token, starts
the daemon if it isn't running, and pipes MCP frames between its own stdio transport and `/mcp`. It
has no knowledge of `ToolSpec`, no manifest fetch, no tool registration, no JSON-RPC framing of its
own. That is what distinguishes it from the thing it replaces: `bridge/src/tools.ts` (383 lines of
schema mapping) and `bridge/src/manifest.ts` were ported to `web/mcp_tools.py` in P2 and do not
come back, and `bridge/src/ipcClient.ts`'s line-delimited protocol dies with `ipc.py`. Because the
shim understands no schema, the class of bug the bridge/daemon contract test exists to catch —
one side's wire format drifting from the other's — stops being structurally possible for the
transport in front of Desktop. So P5's "`bridge/` deleted" stays literally true: the shim is a new,
much smaller package (`mcpb/shim/`), not a trimmed-down `bridge/`, and it should be reviewable as
such — if a reviewer finds connector knowledge in it, it has grown into the thing it replaced.

Two concrete pieces of work this names, neither of which exists after P2:

1. **A discovery file for the URL.** The bridge finds the daemon via `~/.privacyfence/ipc_port`;
   `/mcp`'s port is a *config* value (`web.port`, default 8765 — `web/server.py`'s `DEFAULT_PORT`),
   which a shim would otherwise have to parse the daemon's config to learn. `WebServer.start()`
   writes `~/.privacyfence/mcp_url` when it binds, and clears it on shutdown — the direct successor
   of `ipc.py`'s `PORT_FILE`, and the only new daemon-side surface P4b needs.
2. **Daemon launch.** `bridge/src/daemon.ts`'s `waitForDaemonPatiently` is the one piece of the
   bridge whose job survives verbatim: Claude Desktop spawning the stdio server is what starts
   PrivacyFence today for a user who hasn't opened the app. Port it, don't drop it — dropping it
   turns "double-click and go" into "remember to launch the app first", which is the same
   regression by a different route.

Why not the alternatives named while this was still open: waiting on Desktop's own HTTP-config
support makes P5 depend on an external bug fix, which cannot be scheduled; declaring "Desktop +
`local` + no bridge" unsupported contradicts §2's own definition of `local` mode ("used from Claude
Code **or Claude Desktop** on the same machine") and would make the shipped DMG+`.mcpb` install
path a dead end; and recommending `mcp-remote` hands users a third-party dependency, a
hand-edited config, *and* an `--allow-http` flag whose whole purpose is to switch off a safety
check — three things to get right where there are currently zero.

What this does **not** claim: the shim is still a Node process Claude Desktop spawns per session,
so P4b does not reduce the runtime surface, and `local`-mode Desktop still cannot use Settings →
Connectors (that stays `org` mode's, by §2). It also does not front `/approvals` or `/settings` —
it only ever talks to `/mcp`, so §10.3's audience separation is untouched by it, and a shim that
grew a second endpoint would be the thing to reject in review.

**D12: P4b is reverted; PrivacyFence goes back to shipping one `.mcpb` (the bridge), and does not
replace D11's Desktop answer.** Everything above this paragraph describes what was actually built —
`mcpb/shim/`, the daemon-side `mcp_url` discovery file, the dual-`.mcpb`-in-one-DMG rollback story —
and it worked: a fresh install reached `/mcp` from Claude Desktop with no config edited and no token
copied, exactly as D11 specified. The decision to revert is a maintenance-cost judgment, not a
correctness one:

- **A second Node package is a second thing to keep in sync with the daemon forever, for one
  platform's zero-config install.** `mcpb/shim/` needed its own build (`build.mjs`), its own test
  suite (five files), its own integration contract test
  (`tests/integration/test_shim_mcp_contract.py`), and its own CI job steps — real, ongoing surface
  for a proxy that (by design, §8.1) does almost nothing. `bridge/` already pays that cost and stays
  regardless (P5 is blocked); running two Node packages through the same CI/build/release machinery
  for the same platform's benefit was judged not worth it against P4c's alternative below.
- **P4c reaches further for a smaller build.** A URL and a token shown on a page `/settings` already
  serves (P4, no new surface at all) covers Claude Code today and any future MCP client that speaks
  Streamable HTTP + bearer auth natively, with zero PrivacyFence-side code to maintain per client.
  D11's shim only ever helped Desktop, specifically because of Desktop's own config-editing
  limitations (still true, still unfixed upstream — nothing about *those* changed).
- **This is not a claim that Desktop's problem went away.** It didn't. Reverting D11 without
  replacing it means a `local`-mode Desktop user's zero-config path is `bridge/`, indefinitely, and
  P5 (retire the bridge) has no path forward until that changes. That is a real, deliberately
  accepted regression in the plan's own ambitions, not an oversight — recorded here so a future
  reader doesn't have to reconstruct it from a deleted directory. Revisiting D11's approach (or a
  different one — Desktop's own HTTP-config bug landing upstream would remove the whole question)
  is fair game whenever P5 actually needs to move; nothing here forecloses it.

**Rollback.** Each phase needs an off switch that does not require a downgrade:

- P1: `init_approval_ui()` — the seam itself. A config key selects native or web.
- P2: the HTTP listener is off unless configured; the bridge is untouched.
- P3: a config key restores blocking-only behaviour (hold window = pending TTL, no pending results).
  After P0 this is no longer just a rollback lever — it is the sane configuration for a single-user
  `local` deployment where the human is at the desktop anyway, so treat it as a **supported, tested
  configuration** with a documented default rather than a switch that exists only until P3 is stable.
  §5.4's "Claude may not re-call" is now a measured behavior, not a hypothetical.
- ~~P4b: the previous `.mcpb` is the off switch~~ — moot: P4b itself was the rollback target, and
  has been rolled back (D12). `PrivacyFence.mcpb` is the bridge again, unconditionally.
- P4c: nothing to roll back — a read-only display of values that already exist (`web.mcp.enabled`'s
  own off switch, from P2, still controls whether there's anything for the section to show).
- P6–P9: `mode: local` is the off switch for everything org-shaped.
- P10 is the one phase with no rollback — it deletes the fallback. That is why it is last.

### What ships as a beta

The beta channel already exists and needs no new machinery: `update_check.include_beta` selects
GitHub pre-releases, `update_checker.py` ranks `dev`/`alpha`/`beta`/`rc` below a bare `vX.Y.Z`, and
the on-disk cache records which channel produced it so switching channels behaves. That module's own
docstring anticipated this: "so a future beta-testing program can start without any further changes
here."

| Phase | Release | Why |
|---|---|---|
| P0 | none | Throwaway. |
| **P1** | **beta**, then stable | The ideal first beta: opt-in, the native popup remains, and the seam is the kill switch. Blast radius is one config key. |
| **P2** | **beta**, then stable | Dual-transport and reversible — the bridge still works, so a broken `/mcp` costs a user nothing. |
| **P3** | **beta, and it needs one — more so after P0** | P0's early read came back negative on Claude Code (§5.4): the model stops and asks rather than re-calling. Only a real beta cohort, on all four surfaces and with iterated copy, settles what that costs in practice. Do not ship this straight to stable, and do not size the phase as if the loop were silent. |
| P4 | stable with P1 | Same surface, same risk profile. |
| ~~**P4b**~~ | ~~beta, then stable~~ **reverted (D12)** | Shipped a beta and worked, but the maintenance cost of a second Node package for one platform's install path wasn't judged worth it against P4c's simpler answer. See D12. |
| P4c | stable with P4 | Read-only display of values `/settings` already computes; no new risk surface. |
| P5 | **blocked, not scheduled** | No longer has a resolved Desktop path (D12) — releasing it is not a beta-vs-stable question until it has one. |
| P6 | stable | No user-visible change by construction — local mode must be byte-identical. If it needs a beta, it is not done. |
| **P7–P9** | **beta, on a separate build target** | Per D4, org mode is a different artifact. Its "beta" is a tagged server/container build for one pilot organization, not a pre-release on the desktop DMG channel. Do not mix the two cohorts. |
| P10 | stable | Removes the fallback, so it goes out only after org mode has shipped for real. |

Version bumps follow `CLAUDE.md`: only when a branch is actually about to be released, in their own
commit. Most phase PRs carry no bump — a beta is tagged `vX.Y.Z-beta.N` at release time, not
per-phase.

Phases P1–P5, P4b/P4c included, are strictly additive to the existing security posture. P7 is where the
trust boundary actually moves, and it should get its own security review recorded in
`docs/security-and-compliance.md` rather than riding on this document.

---

## 13. Testing strategy

The bar is a **100% pass rate**, on `macos-latest`. Coverage is reported
(`--cov-report=term-missing`) but gates on nothing — `testing-policy.md` §1 says so explicitly, and
CI runs plain `pytest`. An earlier draft of this document claimed a 100% *coverage* bar; that was
wrong, and it materially over-stated the cost of the new web layer. What the new surface actually
owes is the DoD checklist in `coding-and-testing-guidelines.md` §2.7 plus the negative tests below,
not line-by-line coverage of every socket and TLS branch.

- **`web/` unit tests**: routes tested against an in-process ASGI/HTTP test client, no real socket.
  Auth middleware, CSRF, `Host`/`Origin` policy, principal authorization and the audience separation
  from §10.3 each get explicit negative tests. The audience-separation test is the one that must fail
  loudly if the middleware is ever reordered.
- **`approvals.py` unit tests**: hold-window expiry, TTL expiry, idempotent decisions, ledger
  single-consumption for writes, coalescing of identical pendings, the rules-changed re-evaluation
  broadcast, and cross-principal isolation.
- **`gate.py`**: existing tests stay valid for the decided-in-hold-window path, which is why that
  path is worth keeping. New tests cover pending → decide → re-call → release, and the two audit
  entries sharing one `request_id`.
- **HTML**: extend `test_approval_window_html.py` / `test_settings_window_html.py` with the new
  transport shim and the responsive breakpoints. Both files already assert "no external network
  references"; keep that assertion, it is the CSP guarantee in test form.
- **Integration**: `tests/integration/test_bridge_daemon_contract.py` currently drives the real Node
  bridge over real MCP-over-stdio using the `mcp` client. Its replacement drives the real HTTP MCP
  endpoint with the same client. The test's *purpose* — a wire-protocol change on one side without
  the other fails visibly — is preserved; only the transport changes. This also means the `mcp<2.0`
  cap in `pyproject.toml` gets revisited as part of P2, and the test stops needing Node at P5 —
  once P5 is actually unblocked (D12; it currently is not). P4b briefly added its own, much
  smaller Node-side contract test for the shim (`test_shim_mcp_contract.py`); it was deleted along
  with the shim when P4b was reverted, since there's nothing left for it to test.
- **Browser**: a headless-Chromium smoke test rendering both documents and clicking through one
  approval, as the successor to `scripts/qa_popup_smoke.py` (which loses its subject when the native
  window goes away at P10). §11 is a manual version of exactly this.
- **`org` mode**: a two-principal fixture asserting that user A can never see, decide, or read
  anything belonging to user B — approvals, previews, settings, audit entries, connectors.

---

## 14. Relationship to #55 and #121

Both issues are acted on **once this refactoring is complete**, not before — see D8. Until then they
stay open and unchanged: closing #55 while the thing that replaces it is still unbuilt would leave
mobile approval untracked.

**#55 — mobile remote approval** becomes unnecessary rather than superseded. Its entire architecture
existed to move an approval *off* a machine that must never accept inbound connections: a relay host,
a WireGuard tunnel, an encrypted mailbox, APNs as a content-free wake trigger, an Apple Developer
membership, a signed-and-pinned PWA bundle. If PrivacyFence itself serves the approval page over
HTTPS, every one of those components has nothing left to do. The requirements #55 was protecting are
carried forward explicitly in §10.1, including the one this design deliberately drops and why. On
completion it closes as **won't do**, pointing at this document.

**#121 — Windows version** is unblocked as a side effect, and is revisited on completion **together
with a potential Linux version** — the two share everything that matters here, since what made both
hard was the native UI and transport layer, not the core. #121's own analysis says the
connector/policy core is already portable and "the gap is entirely the native UI and transport
layer" — and it lists as prerequisites the two refactors (#119's `ApprovalUI` seam, #120's webview
config window) that this plan now consumes. After P10 the remaining work on either platform
is packaging, autostart and CI: no `pystray` tray backend and no `pywebview`/WebView2 host are
needed, because the UI is a browser. The Unix-domain-socket blocker #121 names is already gone (the
IPC transport is loopback TCP today), and P5 removes that transport entirely.

---

## 15. Decisions taken

These are settled, not open questions. Implementation proceeds on them; anything that turns out to be
wrong gets revisited against evidence from the phase that found it, rather than re-litigated up
front.

| # | Question | Decision |
|---|---|---|
| **D1** | `local` mode: loopback HTTP, or real HTTPS with a self-signed certificate? | **Loopback HTTP**, served on `localhost` rather than `127.0.0.1` so WebAuthn stays available (D7). A self-signed certificate would be rejected by MCP clients and risks training people through TLS warnings. TLS opt-in remains. §10.2 |
| **D2** | MCP server: official `mcp` SDK + starlette/uvicorn, or hand-rolled Streamable HTTP on asyncio? | **The official SDK**, accepting the deviation from the stdlib-first rule. Spec conformance with Claude's client is the acceptance criterion, and it moves. §8.2 |
| **D3** | Hold window and ledger TTL. | **Hold 30 s, pending TTL 15 min, ledger TTL 5 min, single-use for writes.** All configurable; these defaults are what P3's beta measures against. §5.2 |
| **D4** | Is `org` mode the same artifact as the desktop app? | **Same codebase, separate build target.** The desktop app must not ship an inbound-facing server it never binds. |
| **D5** | `org` MCP auth: own authorization server, or delegate to the org IdP? | **Own authorization server**, with IdP delegation as a supported configuration. It is what makes the browser session and the MCP token provably one identity. §9.4 |
| **D6** | Keep the native macOS popup after P10, or delete it? | **Delete it.** Two approval surfaces means two places for a security fix to land, and the `ApprovalUI` seam lets it come back if that proves wrong. |
| **D7** | Require step-up re-authentication before a *write* approval, and by what mechanism? | **Yes in `org` mode, scoped and configurable — via a WebAuthn platform authenticator** (Face ID / Touch ID / fingerprint / Hello), with IdP `acr_values` step-up as the alternative and OIDC re-auth as the no-passkey fallback. The mechanism is decided; whether the approval link opens somewhere that offers the platform authenticator is **still unverified** — P0 could not reach the real apps, and the ten-minute manual check is P9's entry condition. §10.6 |
| **D9** | What replaces `id(writer)` as the unattended-session key? | **The Streamable HTTP session identifier**, not a token claim — an MCP session is the exact successor to a connection, whereas a claim would make "unattended" a property of a credential that outlives the run. §9.4 |
| **D10** | Which HTTP stack does P1 bring in, given the MCP endpoint does not arrive until P2? | **starlette + uvicorn, from P1** — the same stack D2 commits to for P2. P1 has to stand up `web/server.py` and `routes_approvals.py` before any MCP code exists, and a stdlib asyncio server written at P1 would be thrown away at P2 — the waste §12 avoids by ordering P2 ahead of P3. This front-loads D2's single deviation from the stdlib-first rule by one phase rather than adding a second one. §3, §8.2 |
| **D11** | ~~After P5 deletes the bridge, how does Claude Desktop connect to `local` mode without a hand-edited config or a copied token?~~ **Reverted — see D12.** | ~~PrivacyFence ships its own thin stdio-to-Streamable-HTTP shim inside `PrivacyFence.mcpb`~~ — built in P4b, worked, then reverted. §8.1, §12, §16.9 |
| **D12** | D11's shim is built and works — is it worth keeping as a second Node package indefinitely for one platform's zero-config install? | **No — revert P4b.** `PrivacyFence.mcpb` goes back to wrapping the bridge, unconditionally; `mcpb/shim/` and its tests are deleted. In its place, **P4c**: the `/settings` page (already built by P4) gets a "Connect Claude" section showing the `/mcp` URL and bearer token, covering Claude Code and any other Streamable-HTTP-native client with no new code to maintain per client. This is explicitly not a Desktop replacement — D11's problem is open again, and P5 (retire the bridge) is blocked until it has some other answer. §12, §16.9 |
| **D8** | When are #55 and #121 acted on? | **Once this refactoring is complete, not before.** #55 then closes as won't-do pointing at this document; #121 is revisited then, together with a potential Linux version. Closing #55 earlier would leave mobile approval untracked while its replacement is still unbuilt. §14 |

---

## 16. P4 implementation plan: the web surfaces

**Status: plan, no code written.** It sequences two pieces of work that share one page, one push
channel and one design system:

- **§7.2** — settings on the web: every `SettingsController` action reachable from a browser.
- [`approval-list-ui-ux.md`](approval-list-ui-ux.md) — the parts of the approval-surface design that
  need only P1: the page shell, the return-to-list flow after a decision, and notification tiers 0
  and 1.

**P3 is not in here and is not a dependency in either direction.** P3 changes the approval
*protocol* (deferred results, concurrency); this phase changes the approval and settings *surfaces*.
The two meet at one interface — the state-push channel and the `__pfRender` re-render convention in
§16.3 — which this phase builds and P3's row list later renders into. P3 can land before, after, or
alongside this phase without either being rewritten. What this phase deliberately does not build is
anything that only makes sense with several approvals live at once: the multi-row list, groups, the
hold-window clock, the seven row states. Those are P3's, and [`approval-list-ui-ux.md`
§6](approval-list-ui-ux.md#6-where-this-lands-in-the-phase-plan) already says so.

§16.2 is the part worth reading first: eight things the code says that §7.2 doesn't, one of which
means P4's own goal is unreachable until a threading seam is fixed.

### 16.1 Scope and exit criteria

P4's exit criterion in §12 is *"Every `SettingsController` action reachable from a browser."* With
the P1-compatible half of the approval design folded in, the full bar:

- [ ] Every action in §16.4's table works from a browser, including the four §7.2 calls out as
      desktop-bound and the five connector authentication flows.
- [ ] Async outcomes — an OAuth flow completing, an update check finishing, a rule changed from
      another surface — reach an open page without a manual refresh.
- [ ] `/approvals` and `/settings` are one application: one header, one nav, one palette, one
      session, links both ways.
- [ ] Deciding an approval returns to the approvals page instead of a dead-end string, with a toast
      saying what happened.
- [ ] A new approval raises a Notification Center notification while the tab is open, with no push
      service involved and nothing leaving the machine.
- [ ] Nothing in the daemon shells out to `open`/`osascript` on behalf of an HTTP request.
- [ ] The settings surface is reachable only with the local token, every action is on an explicit
      allowlist, and every mutating request carries the CSRF double-submit the approvals routes
      already use.
- [ ] `pytest -v` green on macOS; the platform-independent subset green on Linux — and for the first
      time that subset includes `settings_controller.py`'s async paths (§16.2.1).

Out of scope, explicitly: P3's protocol and multi-row list; web push, the manifest and the iOS
install flow (org mode, P7+); principals and per-user settings (P6); replacing loopback OAuth with
server-side redirects (P8, §9.3); retiring the native settings window (P10 — this phase leaves it
working and selectable).

### 16.2 What the code says that the plan doesn't

§7.2 calls the settings port "close to mechanical" and names three controller paths needing real
replacements. The mechanical two-thirds is real. The rest is eight findings, checked against `main`
at `a6766d0`.

#### 16.2.1 Every async action is wired through AppKit, so the headless path is broken today

`settings_controller.py`'s module docstring says it is headless-first — "No unguarded AppKit/WebKit
imports at module level" — and it is, at *import* time. At *run* time it is not:

```python
def _run_async(work, on_done):
    def _runner():
        try:
            result = work()
            AppHelper.callAfter(on_done, True, result)      # AppHelper is None without PyObjC
        except Exception as exc:
            AppHelper.callAfter(on_done, False, exc)
    threading.Thread(target=_runner, daemon=True).start()
```

`AppHelper` is imported behind `try/except ImportError` and set to `None` when PyObjC is absent. So
on Linux — the platform this phase's whole point is to reach — `authenticate_connector`,
`check_for_updates_now`, `refresh_connectors`, `_resolve_names_async` (the background grant-name
lookup) and all four Telegram steps start their thread, do their work, and then die with
`AttributeError: 'NoneType' object has no attribute 'callAfter'` in a daemon thread, where nothing
surfaces it. The same applies to `_on_rules_changed` and `_on_unattended_changed`, which marshal
their snapshot push the same way.

This is not a UI problem and it cannot be worked around in the route layer. **The first PR of this
phase is a dispatcher seam**: one module-level `call_on_main(fn, *args)` that resolves to
`AppHelper.callAfter` when a native run loop is hosting, and to `loop.call_soon_threadsafe` against
the web server's event loop otherwise. Every current `AppHelper.callAfter` call site goes through it.

Nothing about it is web-specific — it is the change that makes the class genuinely headless, which
is what P4 has been assuming since it was written.

#### 16.2.2 The Atlassian picker is the same pattern P1 already solved

§7.2 lists the Atlassian multi-resource picker as needing "an in-page list picker". It needs less
than that, because the shape is already in the repo:

```python
def pick_resource(resources):
    # Runs on work()'s own background thread -- dialog_window.show_choice_dialog
    # handles marshaling the window build/show onto the main thread and blocking
    # this one until it resolves ...
    idx = dialog_window.show_choice_dialog(title=..., prompt=..., options=options)
    return resources[idx] if idx is not None else resources[0]
```

A worker thread blocked on a human's choice is exactly `WebApprovalUI`'s pending card plus
`threading.Event`. So the web replacement is not a new mechanism: generalize that single slot into a
small `web_prompt` helper (register a prompt document, block, resolve from an HTTP POST) and let both
the approval card and the picker use it. One mechanism, one set of tests, and the picker gets the
same "no longer pending" landing page an approval link already has.

Two behaviors to carry over deliberately rather than by accident: a cancelled picker falls back to
`resources[0]` (there was never an abort path), and the options are site URLs, not names.

#### 16.2.3 The two web pages are two different design languages

`settings_window_html.py` hardcodes its palette — `#0071e3` for the active nav item, `#8a8a8e` for
captions — with a system font stack. `approval_window/styles.css` has a nine-step token system,
Source Serif 4 embedded as base64, and a fully mirrored dark palette.

Served from one origin, under one nav, with links between them, that reads as two applications
bolted together. Unify by extracting the `:root` token block into `resources/tokens.css`, embedded by
both documents, and restyling the settings page onto it. This is the same extraction the approval
list needs later, which is a real argument for doing it once, here, rather than twice.

The dark palette matters more on the web than it did in a WKWebView: the browser follows the OS
theme, and a settings page that ignores `prefers-color-scheme` next to a card that honours it looks
broken rather than plain.

#### 16.2.4 Three actions shell out to `open`, and one runs `osascript`

| Action | Today | Why it cannot stay |
|---|---|---|
| `install_org_config` | `osascript` → "choose file" dialog | A native picker on the daemon's machine, triggered by an HTTP request. Invisible if the browser is anywhere else. |
| `export_audit_log` | writes an `.xlsx`, then `subprocess.run(["open", path])` | Opens Excel on the daemon's machine, not the user's browser. |
| `_show_update_available_alert` | `rumps.alert`, then `open <release url>` on "Download" | Blocking native modal from a background callback, plus a shell-out. |
| `open_repo` (in `settings_window.py`'s dispatch) | `subprocess.run(["open", REPO_URL])` | Same. |

Replacements: a multipart **upload** (validate it parses as JSON and carries a `version` key — the
checks already in the method — then write `0600`, as it already does); a **download** endpoint with
`Content-Disposition` and `no-store`; an in-page banner whose three buttons map to the existing
`mark_skipped` / `mark_remind_later` / a plain `<a href>`; and a plain `<a href>`.

Worth stating as a rule rather than four fixes: **after this phase, no code path reachable from an
HTTP request runs a subprocess on the host.** It is merely wrong in local mode; it is a remote
command-execution shape in org mode, and org mode is two phases away.

#### 16.2.5 `getattr(controller, action)` is fine in a webview and not fine over HTTP

The native dispatcher is a one-line `getattr`, and its docstring is right that this is what keeps it
from becoming an if/elif ladder. Its only possible sender is the app's own WKWebView.

Over HTTP the same line means *any authenticated request can call any attribute of the controller by
name* — including `_load_config`, `_save_config`, `_authenticate_google`, or anything a future
refactor adds. Nothing here is remotely reachable in local mode, and the token is required, so this
is defense in depth rather than a live hole; it is still the kind of thing a security review of the
org-mode phases will (correctly) refuse to inherit.

The web dispatcher gets an **explicit allowlist**: a module-level frozenset of action names (or a
`@web_action` decorator on the controller), rejecting anything else with 404 before `getattr` is
reached, plus the CSRF double-submit and `Origin` check `routes_approvals.py` already implements.

Related and smaller: the native dispatcher coerces `idx` to `int` and forces `str` on
`objc.pyobjc_unicode` values. Over JSON both are unnecessary — but the coercion is *also* what
currently protects `remove_rule_row(idx="2")` from a `TypeError`. The web path needs real per-action
argument validation returning 400, not a copy of the pyobjc workaround.

#### 16.2.6 The connector icons live on the wrong side of the seam

`settings_window.py._augment_connectors_with_icons()` fills each connector row's `icon_data_uri` by
calling `approval_window._connector_icon_path()` / `_icon_data_uri()` — AppKit-importing modules,
which is exactly why it lives in the window host and not the controller.

`approval_icons.py` (P1) is the PyObjC-free version of those two functions and already serves the
approval card. The web settings route does the same augmentation through it. Small, mechanical, and
easy to forget until the connectors page renders with no icons.

#### 16.2.7 Telegram's sign-in is already web-shaped; the OAuth flows are already local-only

Two pieces of good news worth recording so nobody re-designs them:

- `telegram_start_auth` / `submit_code` / `submit_2fa` / `cancel_auth` is already a resumable state
  machine (`self._telegram_auth`), each step opening its own short-lived client precisely "since a
  webview round trip can be arbitrarily far apart from the next one". It ports unchanged.
- `oauth_loopback.run_browser_oauth()` opens the system browser with `webbrowser.open` and binds
  `127.0.0.1` for the redirect. In local mode the daemon and the browser are the same machine, so it
  works as-is. That assumption should be *stated* in this phase (and surfaced in the page copy: "a
  browser window will open on the machine running PrivacyFence"), because it is the exact thing P8
  replaces with server-side redirect endpoints. An optional, cheap step toward that: return the
  authorize URL to the page and let the page open it, rather than the daemon calling
  `webbrowser.open` itself.

#### 16.2.8 Secrets cross the wire, and one action can kill the server

`telegram_submit_code` and `telegram_submit_2fa` carry a login code and an account password in a POST
body, over loopback HTTP (D1's accepted posture). Two things follow: the snapshot must never echo
them back — `_telegram_auth_state()` returns only `step` and `error` today, and that should be pinned
by a test rather than left as a happy accident — and no log line may carry them.

And `quit_app` calls `rumps.quit_application()`. From a browser, that is a button that kills the
server rendering the page. It stays local-mode only, behind an explicit confirmation, and is refused
outright once `mode: org` exists. The same posture covers any future action that acts on the host
rather than on config.

### 16.3 Target shape

#### The one interface this phase and P3 share

```
GET  /api/state/stream        SSE, one channel per session
       event: settings   data: <SettingsController.snapshot()>
       event: approvals  data: <approval summaries>        ← one row today, a list after P3
```

Both pages already re-render from a state dict: the settings document through
`window.__pfRender(state)`, and the approval list will through `window.__pfRenderApprovals(state)`.
The stream carries whichever the page subscribes to. That is the entire coupling between this phase
and P3: P3 adds rows to the payload of an event this phase already delivers.

This channel is not optional for P4. `SettingsController` pushes state asynchronously through
`on_change` for outcomes that arrive after the POST has already returned — an OAuth flow finishing,
an update check completing, a grant's resource names resolving in the background, a rule changed from
the MCP side. Without the stream, a browser shows a stale page for every one of them, and the
snapshot each action returns is a promise the page cannot keep.

#### New modules

| Module | What it owns |
|---|---|
| `web/routes_settings.py` | `GET /settings`, `POST /api/settings/{action}` (allowlisted), the org-config upload, the audit-log download. |
| `web/state_stream.py` | The SSE channel above, plus debounced snapshot pushes and reconnect-safe full-state delivery. |
| `web_shell.py` | The shared page chrome: header, nav between Approvals and Settings, connection indicator, toast host. One pure function both pages wrap themselves in. |
| `web_prompt.py` | §16.2.2's generalized "block a worker thread on a human's choice" helper, factored out of `web_approval_ui.py`. |
| `resources/tokens.css` | The `:root` light/dark token block, embedded by both stylesheets. |
| `resources/sw.js` | The service worker for notification tiers 0–1. |

#### Changed modules

| Module | Change |
|---|---|
| `settings_controller.py` | §16.2.1's `call_on_main` seam; the four host-bound actions split into a headless part plus a host-specific part; `quit_app` gated. |
| `settings_window_html.py` | Restyled onto the shared tokens; wrapped in the shared shell; nav rail fixed at narrow widths (§7.3's one follow-up). |
| `web_approval_ui.py` | Its single-slot pending mechanism generalized into `web_prompt.py`; behavior unchanged. |
| `web/routes_approvals.py` | The return-to-list flow replacing `document.body.innerHTML = "…close this tab"`; the shared shell around the list page. |
| `web/server.py` | Routes for settings, the stream, the service worker; the `SettingsController` handed in the way `WebApprovalUI` already is. |
| `daemon_main.py` | Wire the controller into `WebServer`; register the web dispatcher as a second `on_change` consumer. |
| `resources/settings.yaml.example` | The `web.settings` and `web.notifications` blocks (§16.6). |
| `tests/conftest.py` | Resets for whatever singleton `state_stream`/`web_prompt` introduces. |

Nothing in `connectors/`, `gate.py`, or `audit_log.py` changes. As in every phase here, editing one
of those is the signal that the work has left its scope.

### 16.4 The action surface, sorted by what it actually needs

`SettingsController` exposes about thirty public actions. Sorted by cost, so the "mechanical"
majority is visible as such and the exceptions are countable:

**Mechanical (POST → new snapshot → `__pfRender`), ~22 actions.** `toggle_pii_detection`,
`toggle_pii_category`, `toggle_update_check`, `toggle_update_check_beta`, `toggle_connector`,
`refresh_connectors`, `update_rule_row`, `add_rule_row`, `remove_rule_row`,
`toggle_grant_capability`, `add_grant_row`, `update_grant_row`, `remove_grant_row`,
`set_default_policy`, `set_category_policy`, `toggle_calendar_free_busy`, `set_log_level`,
`check_for_updates_now`, and the four Telegram steps. These are the "close to mechanical" part §7.2
promised — and they are only mechanical *after* §16.2.1, since several of them complete asynchronously.

**Needs a new transport shape, 4 actions.** `install_org_config` (upload), `export_audit_log`
(download), the update-available alert (in-page banner), `open_repo` (link). §16.2.4.

**Needs the blocking-prompt mechanism, 1 action.** `authenticate_connector` for Atlassian, via
`pick_resource`. §16.2.2.

**Needs a policy decision, 1 action.** `quit_app`. §16.2.8.

**Needs nothing but the icons, 1 path.** The connectors page's `icon_data_uri`. §16.2.6.

### 16.5 The PR sequence

Eight PRs. Each is independently mergeable and leaves the suite green; through PR 3 nothing is
user-visible unless a config key is flipped. Sizes are the plan's own S/M/L.

#### W1 — The main-thread dispatcher seam · **S**

§16.2.1. `call_on_main()` in `settings_controller.py`, every `AppHelper.callAfter` site routed through
it, and a test proving `_run_async`'s `on_done` actually runs with PyObjC absent. No UI change, no
new route. This is the PR that makes P4 possible rather than nominally possible.

*Done when:* `authenticate_connector`'s failure path surfaces an error on a machine with no AppKit,
in a test that would have raised `AttributeError` before.

#### W2 — The shared shell and tokens · **M**

§16.2.3. `resources/tokens.css` extracted; `web_shell.py`; `settings_window_html.py` restyled onto the
tokens and wrapped in the shell; dark mode honoured on both pages; the nav rail no longer eating a
third of a 375px screen. `/approvals` gets the same shell so the two pages visibly become one app.

*Done when:* both documents render correctly at 375px and 1200px in light and dark, and the existing
"no `http://` or `https://` in the document" assertions still hold for both.

#### W3 — Settings on the web, the mechanical two-thirds · **M**

`GET /settings` serving `build_html(snapshot())`; `POST /api/settings/{action}` with the §16.2.5
allowlist, CSRF, `Origin` check and per-action argument validation; the shim swapping
`postMessage` for `fetch` exactly as the approval document's already does; `web/state_stream.py`
with the settings event; `_augment_connectors_with_icons` ported onto `approval_icons.py`.

*Done when:* all ~22 mechanical actions work from a browser, and a rule changed over MCP updates an
open settings page with no refresh.

#### W4 — The four host-bound actions · **M**

§16.2.4: org-config upload (multipart, JSON-validated, `0600`), audit-log download, the update-available
banner with its three outcomes, the repo link. The standing rule lands with them: no subprocess on
the host from an HTTP request, asserted by a test that greps the reachable call graph.

#### W5 — Blocking prompts on the web · **S**

§16.2.2: `web_prompt.py` factored out of `web_approval_ui.py` (no behavior change to approvals), and the
Atlassian site picker served through it, cancelled-picker fallback preserved.

#### W6 — Connector authentication, end to end · **M**

The five flows from a browser: Google, Slack, Salesforce, Atlassian (on W5), Telegram's four steps.
Busy states from `_busy_connectors`, the error banner, and the copy §16.2.7 asks for about *which*
machine opens the OAuth browser window. Secrets never echoed into the snapshot, pinned by a test.

*Done when:* a connector can be authenticated from a browser on the same machine, start to finish,
with the page reflecting each step as it happens.

#### W7 — The approvals page: shell, states, and returning to the list · **M**

The P1-compatible slice of [`approval-list-ui-ux.md`](approval-list-ui-ux.md): the page shell, live
indicator, empty state, recently-decided section, and — the substantive part —
[§3's return-to-list flow](approval-list-ui-ux.md#3-after-a-decision-back-to-the-list) replacing the
`innerHTML` dead end: `replaceState`, restored scroll, the toast, focus moved without auto-opening
anything, and the 409 path.

Renders zero or one approval, by construction. The row markup is written so P3's list is *more rows*,
not a different page.

#### W8 — Notifications, tiers 0 and 1, and the docs · **S**

Title/favicon badge, `aria-live` announcement, the service worker,
`registration.showNotification()` fired from the state stream, permission requested after the first
decision behind a pre-prompt, the three detail levels, and the content invariant asserted by a test
rather than by convention ([§4.3](approval-list-ui-ux.md#43-what-a-notification-is-allowed-to-say)).
Plus `TECHNICAL_REFERENCE.md`'s web-surface section, `security-and-compliance.md`'s entry for the
action allowlist and the no-subprocess rule, and `testing-policy.md` for the browser smoke test.

No push, no VAPID, no manifest — those are org mode's.

#### Order

```
W1 ─▶ W2 ─┬─▶ W3 ─▶ W4 ─▶ W6
          │         └──▶ W5 ─┘
          └─▶ W7 ─▶ W8
```

W1 and W2 are the shared foundation; after them the settings track (W3–W6) and the approvals track
(W7–W8) are independent and can run in parallel. Total sizing lands on §12's M for P4 plus roughly
one M for the approval-surface half.

### 16.6 Configuration and rollback

```yaml
web:
  # P1's existing lever, unchanged: "native" turns the whole web surface off.
  approval_ui: native
  settings:
    # Serve /settings. Independent of approval_ui: a user can run the web
    # approval surface with the native settings window, or the reverse.
    enabled: false
    # rumps.quit_application() from a browser stops the server rendering the
    # page. Local mode only, confirmed in-page, refused once mode: org exists.
    allow_quit: true
  notifications:
    enabled: true          # tiers 0-1; no push service involved
    detail: standard       # minimal | standard | detailed
    sound: false
```

Rollback: `web.settings.enabled: false` returns configuration to the native window, which this phase
leaves fully working (P10 is what deletes it). `web.approval_ui: native` turns off the whole embedded
surface. Neither needs a downgrade.

No version bump on any PR here — `CLAUDE.md`'s rule.

### 16.7 Test plan

Beyond the standing `coding-and-testing-guidelines.md` §2.7 checklist:

**`settings_controller.py`** — the §16.2.1 seam: `_run_async`'s `on_done` runs with `AppHelper` absent;
`_on_rules_changed` pushes a snapshot on the web loop; the four host-bound actions no longer invoke
a subprocess in their headless form; `_telegram_auth_state()` never carries a code or password.

**`web/routes_settings.py`** — the allowlist rejects `_load_config`, `snapshot`, dunder names and
anything unlisted, with 404 and no `getattr` call; missing/invalid CSRF and a cross-origin `Origin`
are both rejected; a wrong-typed argument returns 400 rather than raising; the upload rejects
non-JSON, JSON without `version`, and oversized bodies, and writes `0600`; the download sets
`Content-Disposition` and `no-store`.

**`web/state_stream.py`** — a subscriber receives a settings snapshot on connect; a rules change
pushes to every subscriber; a reconnect delivers full state rather than a patch; pushes are debounced.

**`web_prompt.py`** — a blocked worker thread resolves from an HTTP POST; a second POST for the same
prompt is rejected; an abandoned prompt times out; the Atlassian cancel path still returns
`resources[0]`.

**HTML** — `settings_window_html`'s existing tests extended for the shared tokens and the narrow
breakpoint; both documents keep the no-external-references assertion.

**Notifications** — the payload builder against a marker-stuffed card: nothing gated appears at any
detail level.

**Browser** — headless Chromium: toggle a PII category on `/settings` and see the snapshot round
trip; decide an approval and land back on `/approvals`.

Everything above runs on Linux. This phase is where the Linux CI leg added at P1 starts carrying the
settings surface too — and §16.2.1 is the reason it could not before.

### 16.8 Risks and open questions

| # | Risk | Handling |
|---|---|---|
| 1 | The `call_on_main` seam changes threading in a module the native window also uses. | W1 is deliberately its own PR with no UI change; the native settings window's existing tests are the parity oracle, exactly as `gate.py`'s suite is for other refactors. |
| 2 | Two `on_change` consumers (native window + web stream) after this phase. | Same shape as the single-listener problem elsewhere: make it a list, test that both fire. Note `set_rules_changed_listener` is likewise a single slot already held by this controller. |
| 3 | OAuth's `webbrowser.open` fires on the daemon's machine. | Correct in local mode, stated in copy, and W6 optionally returns the URL to the page instead — which is the seam P8 needs anyway. |
| 4 | The settings page exposes more than the approvals page did: connector auth, config writes, quit. | The allowlist, the CSRF/Origin checks, `allow_quit`, and the no-subprocess rule are all in this phase rather than deferred to org mode, so P7's review inherits a surface that was designed for it. |
| 5 | Restyling `settings_window_html.py` risks visual regressions in the native window, which shares the document. | Same document, two hosts — that is the point. Screenshot the native window before and after in W2; the tokens are a palette swap, not a layout change. |

Open questions worth answering before W3 lands:

1. **Allowlist by frozenset or by decorator?** A decorator on the controller keeps the list next to
   the methods and survives renames; a frozenset in the route module keeps the controller free of
   web concerns. Mild preference for the decorator, but it puts a web-shaped concept into a class
   whose docstring is proud of not having one.
2. **Does `/settings` require a second confirmation for connector *de*-authentication or `quit_app`,
   or is the token enough?** Local mode says the token is enough; the answer changes at P7, so it is
   worth deciding now which way this phase leans.
3. **Should the state stream be one channel with typed events or two endpoints?** One channel is
   simpler to authenticate and to reconnect; two keep a settings page from holding a subscription to
   approval data it never renders. §16.3 assumes one; P6's principals may force the split anyway.

### 16.9 P4c: `/mcp` URL + token on `/settings` (D12, supersedes P4b)

**Status: implemented.** P4b (D11) is reverted — see the note at the top of this document and D11/D12
in §15. This section is what replaced it: not a second install artifact, but one more piece of the
`/settings` page P4 already built.

**Scope.** A "Connect Claude" card on the General page (same page the PII gate/update-check/org-config
cards already live on — no new nav entry), reachable from both the native settings window and
`/settings`, since both render `settings_window_html.build_html()`:

- The `/mcp` URL (`http://<host>:<port>/mcp`) when `web.mcp.enabled` is on and the daemon has actually
  bound it; otherwise a plain sentence saying `/mcp` is off and which config key turns it on.
- The bearer token, masked by default (a settings page rendered in a browser is exactly the kind of
  screen a screen-share or a meeting-room monitor can see — same reasoning §4.3 of
  `approval-list-ui-ux.md` applies to notification bodies), with a click-to-reveal/click-to-copy
  control. Never sent to the client pre-revealed in the initial snapshot's JSON — see the test
  requirement below.
- A ready-to-paste `claude mcp add --transport http privacyfence <url> --header "Authorization:
  Bearer <token>"` command, built server-side from the two values above so there's nothing to
  transcribe by hand.

**Explicitly not in scope**, per D12's own reasoning: Claude Desktop. Nothing here tries to make a
raw URL+token consumable by Desktop's stdio-only extension model — that is D11's problem, still open,
and P4c does not claim otherwise. A phone/other-device Claude Code session reaching a `local`-mode
daemon's loopback-only server also isn't in scope (D1 keeps `local` mode loopback-bound on purpose);
this card is for the same machine, same posture as every other `/settings` action.

**Where the values come from.** `WebServer` already computes `mcp_url`/`mcp_token` as live Python
attributes the moment it's constructed (§16.3's `build_app`, `mcp_dispatcher`/`mcp_token` params) —
the same process serving `/settings` has them in memory, so this needs no new discovery file the way
D11's shim did (that file existed for an *out-of-process* Node consumer; P4c's consumer is the
in-process settings route). `daemon_main._maybe_start_web_server` calls a new
`SettingsController.set_mcp_connection_info(url=, token=)` right after constructing `WebServer`,
passing `server.mcp_url`/`server.mcp_token` — regardless of `use_web_settings`, since both the native
window and `/settings` render the same `build_html()` and both need the values live. `snapshot()`'s
`general` state grows `mcp_enabled`/`mcp_url` (not a separate `connect_claude` key — this ended up
sitting alongside the PII gate/update-check/org-config fields the General page already carries there,
one dict, not a second one); the token itself is never in `general` at all, matching the Telegram-
secrets pattern exactly. A dedicated `reveal_mcp_token()` action — allowlisted like every other
`/api/settings/{action}`, but answering `{"mcp_token": ...}` rather than a fresh snapshot — is the only
way the token leaves the process, and both bridges (the web page's JS, the native window's
`_dispatch`) route that specific response to its own `window.__pfRevealMcpToken` callback instead of
the generic `window.__pfRender`, which expects a full state shape and would otherwise wipe every other
rendered section.

**Test requirements**, mirroring §16.7's own discipline: the masked-by-default state is asserted (the
token string does not appear in `build_html()`'s initial `window.__pfInitialState` in cleartext,
matching the Telegram-secrets-never-echoed pattern §16.2.8 already established — this landed as the
*stronger* guarantee floated above, not the CSS-only one: the token is fetched on demand via
`reveal_mcp_token()` and is never in `window.__pfInitialState`'s JSON at all, revealed or not, so
there's no weaker "hidden in the DOM" case to caveat); `/mcp` disabled renders the explanatory state,
not a broken URL; the command string round-trips through a copy-button click in the browser smoke test
(`qa_web_smoke.py`, §16.7's own "browser" row).

**Rollback.** None needed — see §12's Rollback list. Turning `web.mcp.enabled` off (P2's own lever)
already makes the card show its disabled state; turning `web.settings.enabled` off (P4's own lever)
removes the whole page, card included.
