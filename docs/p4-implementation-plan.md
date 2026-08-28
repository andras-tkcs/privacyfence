# P4 implementation plan: the web surfaces

**Status: plan, no code written.** It sequences two pieces of work that share one page, one push
channel and one design system:

- [`https-connector-refactor-plan.md`](https-connector-refactor-plan.md) **P4** — settings on the
  web (§7.2): every `SettingsController` action reachable from a browser.
- [`approval-list-ui-ux.md`](approval-list-ui-ux.md) — the parts of the approval-surface design that
  need only P1: the page shell, the return-to-list flow after a decision, and notification tiers 0
  and 1.

**P3 is not in here and is not a dependency in either direction.** P3 changes the approval
*protocol* (deferred results, concurrency); this phase changes the approval and settings *surfaces*.
The two meet at one interface — the state-push channel and the `__pfRender` re-render convention in
§3 — which this phase builds and P3's row list later renders into. P3 can land before, after, or
alongside this phase without either being rewritten. What this phase deliberately does not build is
anything that only makes sense with several approvals live at once: the multi-row list, groups, the
hold-window clock, the seven row states. Those are P3's, and [`approval-list-ui-ux.md`
§6](approval-list-ui-ux.md#6-where-this-lands-in-the-phase-plan) already says so.

§2 is the part worth reading first: eight things the code says that §7.2 doesn't, one of which means
P4's own goal is unreachable until a threading seam is fixed.

---

## Contents

1. [Scope and exit criteria](#1-scope-and-exit-criteria)
2. [What the code says that the plan doesn't](#2-what-the-code-says-that-the-plan-doesnt)
3. [Target shape](#3-target-shape)
4. [The action surface, sorted by what it actually needs](#4-the-action-surface-sorted-by-what-it-actually-needs)
5. [The PR sequence](#5-the-pr-sequence)
6. [Configuration and rollback](#6-configuration-and-rollback)
7. [Test plan](#7-test-plan)
8. [Risks and open questions](#8-risks-and-open-questions)

---

## 1. Scope and exit criteria

P4's exit criterion in §12 is *"Every `SettingsController` action reachable from a browser."* With
the P1-compatible half of the approval design folded in, the full bar:

- [ ] Every action in §4's table works from a browser, including the four §7.2 calls out as
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
      time that subset includes `settings_controller.py`'s async paths (§2.1).

Out of scope, explicitly: P3's protocol and multi-row list; web push, the manifest and the iOS
install flow (org mode, P7+); principals and per-user settings (P6); replacing loopback OAuth with
server-side redirects (P8, §9.3); retiring the native settings window (P10 — this phase leaves it
working and selectable).

---

## 2. What the code says that the plan doesn't

§7.2 calls the settings port "close to mechanical" and names three controller paths needing real
replacements. The mechanical two-thirds is real. The rest is eight findings, checked against `main`
at `a6766d0`.

### 2.1 Every async action is wired through AppKit, so the headless path is broken today

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

### 2.2 The Atlassian picker is the same pattern P1 already solved

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

### 2.3 The two web pages are two different design languages

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

### 2.4 Three actions shell out to `open`, and one runs `osascript`

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

### 2.5 `getattr(controller, action)` is fine in a webview and not fine over HTTP

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

### 2.6 The connector icons live on the wrong side of the seam

`settings_window.py._augment_connectors_with_icons()` fills each connector row's `icon_data_uri` by
calling `approval_window._connector_icon_path()` / `_icon_data_uri()` — AppKit-importing modules,
which is exactly why it lives in the window host and not the controller.

`approval_icons.py` (P1) is the PyObjC-free version of those two functions and already serves the
approval card. The web settings route does the same augmentation through it. Small, mechanical, and
easy to forget until the connectors page renders with no icons.

### 2.7 Telegram's sign-in is already web-shaped; the OAuth flows are already local-only

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

### 2.8 Secrets cross the wire, and one action can kill the server

`telegram_submit_code` and `telegram_submit_2fa` carry a login code and an account password in a POST
body, over loopback HTTP (D1's accepted posture). Two things follow: the snapshot must never echo
them back — `_telegram_auth_state()` returns only `step` and `error` today, and that should be pinned
by a test rather than left as a happy accident — and no log line may carry them.

And `quit_app` calls `rumps.quit_application()`. From a browser, that is a button that kills the
server rendering the page. It stays local-mode only, behind an explicit confirmation, and is refused
outright once `mode: org` exists. The same posture covers any future action that acts on the host
rather than on config.

---

## 3. Target shape

### The one interface this phase and P3 share

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

### New modules

| Module | What it owns |
|---|---|
| `web/routes_settings.py` | `GET /settings`, `POST /api/settings/{action}` (allowlisted), the org-config upload, the audit-log download. |
| `web/state_stream.py` | The SSE channel above, plus debounced snapshot pushes and reconnect-safe full-state delivery. |
| `web_shell.py` | The shared page chrome: header, nav between Approvals and Settings, connection indicator, toast host. One pure function both pages wrap themselves in. |
| `web_prompt.py` | §2.2's generalized "block a worker thread on a human's choice" helper, factored out of `web_approval_ui.py`. |
| `resources/tokens.css` | The `:root` light/dark token block, embedded by both stylesheets. |
| `resources/sw.js` | The service worker for notification tiers 0–1. |

### Changed modules

| Module | Change |
|---|---|
| `settings_controller.py` | §2.1's `call_on_main` seam; the four host-bound actions split into a headless part plus a host-specific part; `quit_app` gated. |
| `settings_window_html.py` | Restyled onto the shared tokens; wrapped in the shared shell; nav rail fixed at narrow widths (§7.3's one follow-up). |
| `web_approval_ui.py` | Its single-slot pending mechanism generalized into `web_prompt.py`; behavior unchanged. |
| `web/routes_approvals.py` | The return-to-list flow replacing `document.body.innerHTML = "…close this tab"`; the shared shell around the list page. |
| `web/server.py` | Routes for settings, the stream, the service worker; the `SettingsController` handed in the way `WebApprovalUI` already is. |
| `daemon_main.py` | Wire the controller into `WebServer`; register the web dispatcher as a second `on_change` consumer. |
| `resources/settings.yaml.example` | The `web.settings` and `web.notifications` blocks (§6). |
| `tests/conftest.py` | Resets for whatever singleton `state_stream`/`web_prompt` introduce. |

Nothing in `connectors/`, `gate.py`, or `audit_log.py` changes. As in every phase here, editing one
of those is the signal that the work has left its scope.

---

## 4. The action surface, sorted by what it actually needs

`SettingsController` exposes about thirty public actions. Sorted by cost, so the "mechanical"
majority is visible as such and the exceptions are countable:

**Mechanical (POST → new snapshot → `__pfRender`), ~22 actions.** `toggle_pii_detection`,
`toggle_pii_category`, `toggle_update_check`, `toggle_update_check_beta`, `toggle_connector`,
`refresh_connectors`, `update_rule_row`, `add_rule_row`, `remove_rule_row`,
`toggle_grant_capability`, `add_grant_row`, `update_grant_row`, `remove_grant_row`,
`set_default_policy`, `set_category_policy`, `toggle_calendar_free_busy`, `set_log_level`,
`check_for_updates_now`, and the four Telegram steps. These are the "close to mechanical" part §7.2
promised — and they are only mechanical *after* §2.1, since several of them complete asynchronously.

**Needs a new transport shape, 4 actions.** `install_org_config` (upload), `export_audit_log`
(download), the update-available alert (in-page banner), `open_repo` (link). §2.4.

**Needs the blocking-prompt mechanism, 1 action.** `authenticate_connector` for Atlassian, via
`pick_resource`. §2.2.

**Needs a policy decision, 1 action.** `quit_app`. §2.8.

**Needs nothing but the icons, 1 path.** The connectors page's `icon_data_uri`. §2.6.

---

## 5. The PR sequence

Eight PRs. Each is independently mergeable and leaves the suite green; through PR 3 nothing is
user-visible unless a config key is flipped. Sizes are the plan's own S/M/L.

### W1 — The main-thread dispatcher seam · **S**

§2.1. `call_on_main()` in `settings_controller.py`, every `AppHelper.callAfter` site routed through
it, and a test proving `_run_async`'s `on_done` actually runs with PyObjC absent. No UI change, no
new route. This is the PR that makes P4 possible rather than nominally possible.

*Done when:* `authenticate_connector`'s failure path surfaces an error on a machine with no AppKit,
in a test that would have raised `AttributeError` before.

### W2 — The shared shell and tokens · **M**

§2.3. `resources/tokens.css` extracted; `web_shell.py`; `settings_window_html.py` restyled onto the
tokens and wrapped in the shell; dark mode honoured on both pages; the nav rail no longer eating a
third of a 375px screen. `/approvals` gets the same shell so the two pages visibly become one app.

*Done when:* both documents render correctly at 375px and 1200px in light and dark, and the existing
"no `http://` or `https://` in the document" assertions still hold for both.

### W3 — Settings on the web, the mechanical two-thirds · **M**

`GET /settings` serving `build_html(snapshot())`; `POST /api/settings/{action}` with the §2.5
allowlist, CSRF, `Origin` check and per-action argument validation; the shim swapping
`postMessage` for `fetch` exactly as the approval document's already does; `web/state_stream.py`
with the settings event; `_augment_connectors_with_icons` ported onto `approval_icons.py`.

*Done when:* all ~22 mechanical actions work from a browser, and a rule changed over MCP updates an
open settings page with no refresh.

### W4 — The four host-bound actions · **M**

§2.4: org-config upload (multipart, JSON-validated, `0600`), audit-log download, the update-available
banner with its three outcomes, the repo link. The standing rule lands with them: no subprocess on
the host from an HTTP request, asserted by a test that greps the reachable call graph.

### W5 — Blocking prompts on the web · **S**

§2.2: `web_prompt.py` factored out of `web_approval_ui.py` (no behavior change to approvals), and the
Atlassian site picker served through it, cancelled-picker fallback preserved.

### W6 — Connector authentication, end to end · **M**

The five flows from a browser: Google, Slack, Salesforce, Atlassian (on W5), Telegram's four steps.
Busy states from `_busy_connectors`, the error banner, and the copy §2.7 asks for about *which*
machine opens the OAuth browser window. Secrets never echoed into the snapshot, pinned by a test.

*Done when:* a connector can be authenticated from a browser on the same machine, start to finish,
with the page reflecting each step as it happens.

### W7 — The approvals page: shell, states, and returning to the list · **M**

The P1-compatible slice of [`approval-list-ui-ux.md`](approval-list-ui-ux.md): the page shell, live
indicator, empty state, recently-decided section, and — the substantive part —
[§3's return-to-list flow](approval-list-ui-ux.md#3-after-a-decision-back-to-the-list) replacing the
`innerHTML` dead end: `replaceState`, restored scroll, the toast, focus moved without auto-opening
anything, and the 409 path.

Renders zero or one approval, by construction. The row markup is written so P3's list is *more rows*,
not a different page.

### W8 — Notifications, tiers 0 and 1, and the docs · **S**

Title/favicon badge, `aria-live` announcement, the service worker,
`registration.showNotification()` fired from the state stream, permission requested after the first
decision behind a pre-prompt, the three detail levels, and the content invariant asserted by a test
rather than by convention ([§4.3](approval-list-ui-ux.md#43-what-a-notification-is-allowed-to-say)).
Plus `TECHNICAL_REFERENCE.md`'s web-surface section, `security-and-compliance.md`'s entry for the
action allowlist and the no-subprocess rule, and `testing-policy.md` for the browser smoke test.

No push, no VAPID, no manifest — those are org mode's.

### Order

```
W1 ─▶ W2 ─┬─▶ W3 ─▶ W4 ─▶ W6
          │         └──▶ W5 ─┘
          └─▶ W7 ─▶ W8
```

W1 and W2 are the shared foundation; after them the settings track (W3–W6) and the approvals track
(W7–W8) are independent and can run in parallel. Total sizing lands on §12's M for P4 plus roughly
one M for the approval-surface half.

---

## 6. Configuration and rollback

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

---

## 7. Test plan

Beyond the standing `coding-and-testing-guidelines.md` §2.7 checklist:

**`settings_controller.py`** — the §2.1 seam: `_run_async`'s `on_done` runs with `AppHelper` absent;
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
settings surface too — and §2.1 is the reason it could not before.

---

## 8. Risks and open questions

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
   approval data it never renders. §3 assumes one; P6's principals may force the split anyway.
