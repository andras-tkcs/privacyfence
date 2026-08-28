# PrivacyFence as an HTTPS connector — architecture & functional refactoring proposal

**Status: design agreed (§15). Nothing here is implemented yet.**

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
12. [Phasing](#12-phasing)
13. [Testing strategy](#13-testing-strategy)
14. [Relationship to #55 and #121](#14-relationship-to-55-and-121)
15. [Decisions taken](#15-decisions-taken)

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

What disappears: `bridge/` entirely, `ipc.py` / `ipc_server.py` as the Claude-facing transport,
`approval_popup.py`, `approval_window.py`, `dialog_window.py`, `settings_window.py`, `menu_bar.py`
(the last one optionally kept as a convenience tray on desktop).

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

**Claude may not re-call.** A model that reports "needs approval" and then stops is a UX failure, not
a security failure — fail-closed holds. Mitigations: the hold window covers the fast case entirely;
tool descriptions state the re-call contract explicitly; `await_approval` gives the model a natural
thing to do instead of ending the turn. This should be measured in the Phase 2 spike rather than
assumed.

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

**Unattended sessions** lose their `id(writer)` key. They rebind to the MCP session identifier from
the Streamable HTTP session, or to a token claim, with the same lifecycle: entered explicitly,
cleared when the session ends, audited on both transitions, and still gated behind
`unattended_sessions.enabled` in `org_config.json`.

---

## 10. Security analysis

### 10.1 The posture change, stated plainly

Issue #55's design was built on a requirement this proposal deliberately drops:

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
| 5. Desktop popup keeps working through rollout | Held through Phase 1–3; Phase 6 retires it deliberately (decision D6). |

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
- **Where the link opens decides whether this is dependable.** The approval URL arrives inside a
  Claude conversation. Passkeys work in the system browser and in Safari View Controller / Android
  Custom Tabs; a plain embedded webview may not offer the platform authenticator at all. Test this in
  the Phase 0 spike — it is the difference between a real control and a coin flip.
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
   100vh}` are native-window assumptions. §7.3 — real work, not a tweak.
2. **Mobile requires `org` mode** — verified against Claude's remote-connector requirements rather
   than assumed. §2. Confirmed as the intended design; the README should say so explicitly.
3. **Per-user service OAuth is the largest single work item**, larger than the HTTP server itself.
   §9.3.
4. **Unattended sessions lose their identity key** when the TCP connection goes away. §9.4.
5. **Three `SettingsController` paths assume a local desktop** and need real replacements. §7.2.
6. **100% coverage is the standing bar** (`coding-and-testing-guidelines.md` §2.7) and a new
   HTTP/auth/TLS layer is a large surface to bring to it. §13.

---

## 12. Phasing

Each phase is independently shippable and independently valuable. The `ApprovalUI` seam is what
makes Phase 1 possible without touching `gate.py` at all.

| Phase | Scope | Ships |
|---|---|---|
| **0. Spike** | Serve both existing HTML documents over a local asyncio HTTP server; swap `post()` to `fetch()`; drive one real approval end to end. Throwaway. | Confidence + the responsive-CSS estimate |
| **1. `WebApprovalUI`** | `web/server.py`, `web_approval_ui.py` implementing `ApprovalUI`. Single user, still blocking, bridge unchanged, `gate.py` unchanged. Approvals happen in a browser. | Cross-platform approvals; native popup as fallback |
| **2. Concurrency + deferred protocol** | `approvals.py` registry, hold window, decision ledger, `await_approval` tool, retire `_popup_lock`, rules-changed broadcast, audit additions, responsive CSS. | Multiple pending approvals; the approval link in Claude |
| **3. Settings on the web** | `routes_settings.py`, the three desktop-assuming controller paths, admin/user split. | Config UI in the browser |
| **4. MCP over HTTP** | `routes_mcp.py`, local bearer auth, bind/Host/Origin policy. Bridge retired; `ipc.py`/`ipc_server.py` retired. | One process, no Node, no bridge |
| **5. `org` mode** | `principal.py`, per-principal registries and storage, OIDC login, OAuth 2.1 AS/RS, server-side service-OAuth redirects, rate limits. | **Multi-user; mobile Claude works** |
| **6. Retire native UI** | Delete `approval_popup.py`, `approval_window.py`, `dialog_window.py`, `settings_window.py`; `rumps`/PyObjC become optional extras; menu bar optional. | Windows/Linux viability (#121) |

Phases 1–4 are strictly additive to the existing security posture. Phase 5 is where the trust
boundary actually moves, and it should get its own security review under
`docs/security-and-compliance.md`.

---

## 13. Testing strategy

The bar is `pytest --cov=src/privacyfence` at **100%**, and the new surface is large.

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
  cap in `pyproject.toml` gets revisited as part of Phase 4.
- **Browser**: a headless-Chromium smoke test rendering both documents and clicking through one
  approval, as the successor to `scripts/qa_popup_smoke.py` (which loses its subject when the native
  window goes away in Phase 6). §11 is a manual version of exactly this.
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
config window) that this proposal now consumes. After Phase 6 the remaining work on either platform
is packaging, autostart and CI: no `pystray` tray backend and no `pywebview`/WebView2 host are
needed, because the UI is a browser. The Unix-domain-socket blocker #121 names is already gone (the
IPC transport is loopback TCP today), and Phase 4 removes that transport entirely.

---

## 15. Decisions taken

These are settled, not open questions. Implementation proceeds on them; anything that turns out to be
wrong gets revisited against evidence from the phase that found it, rather than re-litigated up
front.

| # | Question | Decision |
|---|---|---|
| **D1** | `local` mode: loopback HTTP, or real HTTPS with a self-signed certificate? | **Loopback HTTP**, served on `localhost` rather than `127.0.0.1` so WebAuthn stays available (D7). A self-signed certificate would be rejected by MCP clients and risks training people through TLS warnings. TLS opt-in remains. §10.2 |
| **D2** | MCP server: official `mcp` SDK + starlette/uvicorn, or hand-rolled Streamable HTTP on asyncio? | **The official SDK**, accepting the deviation from the stdlib-first rule. Spec conformance with Claude's client is the acceptance criterion, and it moves. §8.2 |
| **D3** | Hold window and ledger TTL. | **Hold 30 s, pending TTL 15 min, ledger TTL 5 min, single-use for writes.** All configurable; these defaults are what Phase 2 measures against. §5.2 |
| **D4** | Is `org` mode the same artifact as the desktop app? | **Same codebase, separate build target.** The desktop app must not ship an inbound-facing server it never binds. |
| **D5** | `org` MCP auth: own authorization server, or delegate to the org IdP? | **Own authorization server**, with IdP delegation as a supported configuration. It is what makes the browser session and the MCP token provably one identity. §9.4 |
| **D6** | Keep the native macOS popup after Phase 6, or delete it? | **Delete it.** Two approval surfaces means two places for a security fix to land, and the `ApprovalUI` seam lets it come back if that proves wrong. |
| **D7** | Require step-up re-authentication before a *write* approval, and by what mechanism? | **Yes in `org` mode, scoped and configurable — via a WebAuthn platform authenticator** (Face ID / Touch ID / fingerprint / Hello), with IdP `acr_values` step-up as the alternative and OIDC re-auth as the no-passkey fallback. §10.6 |
| **D8** | When are #55 and #121 acted on? | **Once this refactoring is complete, not before.** #55 then closes as won't-do pointing at this document; #121 is revisited then, together with a potential Linux version. Closing #55 earlier would leave mobile approval untracked while its replacement is still unbuilt. §14 |
