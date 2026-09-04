# The pending-approval list: UI/UX design

**Status: design proposal, no code written.** A side quest to the (now-removed, P10-complete)
`https-connector-refactor-plan.md` — it designs the *human* half of that document's §7.1 in the
detail the plan deliberately doesn't go into, and it assumes that plan's P3
(`PendingApprovalRegistry`, `_popup_lock` retired, several approvals live at once).
Sections 2–3 have a subset that works on P1's single-slot `WebApprovalUI` today; §6 says which.

Nothing here changes the gate, the rule model, the PII detector, or what any tool does. It changes
what the human sees and how they are told there is something to see.

An interactive mockup of everything in §2–§4 lives at
[`mockups/approval-list.html`](mockups/approval-list.html) — a single self-contained file; open it
in a browser. It is a mockup, not a prototype: static data, no daemon, no network.

---

## Contents

1. [What this replaces](#1-what-this-replaces)
2. [The list](#2-the-list)
3. [After a decision: back to the list](#3-after-a-decision-back-to-the-list)
4. [Notifications](#4-notifications)
5. [Eleven things that weren't asked for and belong here anyway](#5-eleven-things-that-werent-asked-for-and-belong-here-anyway)
6. [Where this lands in the phase plan](#6-where-this-lands-in-the-phase-plan)
7. [Tests](#7-tests)
8. [Open questions](#8-open-questions)

---

## 1. What this replaces

`web/routes_approvals.py`'s `list_approvals` today is one sentence in a bare `<body>`:

> Approval pending — click to review

That is honest about P1's scope (one pending card at a time, §7.1's real list is P3) and it is the
whole of the list UI that exists. Everything below is what that page becomes once the registry can
hold more than one thing.

Three existing facts constrain the design, and all three are load-bearing:

- **The card itself is not being redesigned.** `approval_window_html.build_card_stack_html()` stays
  exactly as it is — its four sections, its fixed-height `.pf-kv` rows, its own Deny / Allow once /
  Always allow button row. The list is a layer *above* the card, not a replacement for it.
- **The page must render with zero network fetches.** The approval document's own test asserts it
  contains no `http://` or `https://` at all; icons are base64 data URIs from `approval_icons.py`,
  fonts are embedded in `resources/approval_window/styles.css`. The list page inherits that rule.
  It is served over HTTP now, so the reason is weaker than it was in a `loadHTMLString_baseURL_`
  WKWebView — but a privacy tool that phones out to a CDN to render its own approval prompt is
  not a thing worth shipping, and the constraint costs nothing.
- **The rendering convention already exists.** `settings_window_html.build_html(state)` is a pure
  function taking a snapshot dict and re-rendering client-side through `window.__pfRender(newState)`.
  The list follows it exactly: `approval_list_html.build_list_html(state)` for the first paint,
  `window.__pfRenderApprovals(state)` for every SSE update. No framework, no build step, no
  divergence between server-rendered and client-rendered markup.

---

## 2. The list

### 2.1 The row

One horizontal box per pending approval, full width, `--color-surface` on `--color-bg`, the same
1px/2px/4px radii and Source Serif 4 the card uses. Not a table: a table implies you scan columns,
and what you actually do is read one row and decide it.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│  ┌────┐                                                                          │
│  │Gmail│  Send email to alice@example.com                    ⟨WRITE⟩ ⟨PII⟩  0:24 │
│  └────┘  Gmail · gmail_send_message · 6 seconds ago                              │
│                                                                                  │
│          “Sending the Q3 summary you asked me to draft”                          │
│                                                                                  │
│          Seen 3 times this week                       [ Deny ]  [ Review → ]     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

| Element | Source | Notes |
|---|---|---|
| Connector icon, 28px | `approval_icons.connector_icon_path()` | The eleven bundled brand PNGs. Falls back to the shield mark when a connector has no icon — silent skip, same as the card. |
| Title | the card's `title` | Already written to be a one-line human sentence. Truncated with a real `text-overflow: ellipsis`, `title=` carries the full string. |
| Kicker | connector display name · tool name · relative age | The tool name is there for the person who wants to write a rule about it. Absolute timestamp in the `title=` attribute. |
| Direction badge | `is_read` | `READ` (neutral) / `WRITE` (accent-2). A write is the one that leaves a mark on the world; it should not look like a read. |
| PII badge | `pii_categories` non-empty | Category *names* only, never a matched value. |
| Countdown | `expires_at` / hold window | §2.3. |
| Reason quote | `claude_reason` | Model-authored text — see the escaping rule below. Omitted entirely when empty rather than rendering an empty quote. |
| Seen count | `seen_count` | Doubles as the rule affordance (§5.4). |
| Actions | — | **Deny is on the row. Allow is not.** §2.2. |

**`claude_reason` is untrusted text and must be rendered as such.** It is the one string on the page
written by the model whose request the human is judging. It is HTML-escaped (the card already does
this), clamped to two lines, rendered in italic inside typographic quotes, and never given the
page's own chrome styling. A row must not be able to grow a fake button, a fake badge, or a fake
"PrivacyFence says" line out of a model-supplied string. This is not hypothetical politeness: the
whole product is a defense against a model asking for something the human wouldn't want, and the
list is the first place model text and product chrome sit next to each other.

### 2.2 Deny from the row, never Allow

The asymmetry is deliberate and it is the single most important decision in this document.

- **Deny** is fail-closed. Denying without reading the card cannot leak anything; at worst the user
  re-asks Claude. So it is one tap from the list, no confirmation, undoable only in the sense that
  Claude can ask again.
- **Allow** releases data. It requires the card — the preview, the disclosure rows, the PII banner,
  the visibility sentences. Approving from a summary line is exactly the habituation failure the
  card was designed to prevent, and putting an "Allow" button on a list row is how a security
  product teaches people to stop reading.

Same reasoning kills **bulk approve**, which is the first feature anyone asks for once a list
exists. Bulk *deny* is fine and is offered ("Deny all", plus a "Deny everything and pause connectors
for 15 minutes" panic action). Bulk approve is not offered at any list size. If the list is
routinely long enough that approving one at a time is painful, the fix is a rule (§5.4), and the UI
should say so at that moment rather than growing a button that makes the pain tolerable.

### 2.3 Row states, and the two clocks

There are two clocks and they mean different things to the person reading:

**The hold window (default 30 s, §5.2 of the plan).** Inside it, `gated_call()` is still blocking
and the tool call is still live: deciding *now* costs nothing and Claude continues in the same turn.
Outside it, the call has already returned a pending result, and the plan's own P0 finding (§5.4:
zero of five sessions re-called autonomously) means the realistic cost of a late decision is *a
human turn in the conversation* — the user has to go back to Claude and say "go ahead".

That is worth surfacing, because it is the only thing on the page the user can act on to make their
own life easier, and nothing in the current design tells them the difference:

```
Claude is still waiting — 24s          ← inside the hold window, a thin depleting bar
Claude has moved on; decide anyway     ← after it. Not an error, not red.
```

The copy after the hold window matters. "Expired" it is not — the approval is perfectly live and
deciding it still works (the decision ledger releases the data on Claude's next identical call).
The honest sentence is that Claude stopped waiting and the user may need to nudge it. §5.9 has the
copy table.

**The TTL (default 15 min).** The approval dies at it. Show a countdown only under 5 minutes, in
`--color-danger` under 60 seconds, and label the state plainly when it lapses.

Full state set:

| State | Trigger | Row treatment |
|---|---|---|
| `waiting` | inside the hold window | depleting bar + "Claude is still waiting — Ns" |
| `pending` | hold window elapsed | quiet kicker line, no bar |
| `expiring` | < 5 min to TTL | countdown, danger under 60 s |
| `expired` | TTL reached | greyed, "Expired — Claude was told no", auto-removed after 10 s |
| `auto_accepted` | a rule created elsewhere now covers it (§6 of the plan) | fades out with "Auto-accepted by rule *Gmail reads*" |
| `decided_elsewhere` | resolved on another device/tab | fades out with the decision that landed |
| `released` | Claude re-called and got the data | shown in "Recently decided" only, not in the list |

The last four are what makes a stale tab honest. The tab in this design is expected to stay open for
hours — the user said so, and it is the whole reason notifications matter — so every way a row can
vanish that isn't "you clicked something" needs a visible reason, not a silent disappearance.

### 2.4 One ask, several approvals

§5.2's `await_approval` exists because Claude fires several gated calls and then tells the user
"3 steps need your approval". If those arrive as three unrelated rows, the human has to reconstruct
that they are one thing.

Group rows that share an MCP session and arrived within ~5 seconds of each other under one header:

```
   Claude asked for 3 things · Claude Code · 14:02
   ┌────────────────────────────────────────────────────┐
   │  row                                               │
   ├────────────────────────────────────────────────────┤
   │  row                                               │
   ├────────────────────────────────────────────────────┤
   │  row                                               │
   └────────────────────────────────────────────────────┘
```

The group header gets **Deny all 3** (fail-closed, so it is allowed) and a count. It does not get
an approve-all. Reviewing one and coming back leaves the group in place with two left, and the
group header's count updates.

Grouping is presentation only — the registry keys, the decision ledger and the audit entries stay
strictly per-approval. It never merges two approvals into one decision.

**Coalescing is a different thing and happens below this.** §6's rule that two pending approvals
with the same `(principal, connector, tool, args)` are one row is registry-level de-duplication; the
row shows `×2` and one decision resolves both, because they are literally the same call.

### 2.5 The page around the rows

```
  PrivacyFence                                        ● live      ⚙ Settings
  ───────────────────────────────────────────────────────────────────────────
  3 approvals pending                     [ Deny all ]  🔔 Notifications: on
  ───────────────────────────────────────────────────────────────────────────

  ...rows...

  ───────────────────────────────────────────────────────────────────────────
  Recently decided                                              View audit log
  ✓ Read Drive file "Q3 plan"        approved  ·  2 min ago  ·  released
  ✗ Send Slack message to #general   denied    ·  8 min ago
```

- **Live indicator.** A dot bound to the SSE connection state, not decorative. `● live` /
  `◌ reconnecting…` / `✕ can't reach PrivacyFence`. The last one is important: a page that shows
  "no approvals pending" while its stream is dead is actively lying, and this page is one a user
  will trust with "nothing is waiting for me".
- **Empty state.** "Nothing is waiting. PrivacyFence is watching." plus, when notifications are not
  enabled, the one place the enable affordance is offered (§4.4). An empty list is the goal, not a
  failure — the copy should not read like an error page.
- **Recently decided.** Last five, TTL'd out after ~30 minutes, decision + relative time + whether
  Claude has actually re-called and been given the data (`released`). This closes a real gap in the
  deferred protocol: after P3 there is a window where the human has approved and *nothing appears to
  happen*, because Claude hasn't re-called yet. Without this row the user's model is "I clicked
  Allow and it didn't work".
- **Notification toggle.** Visible state, one click, no settings-page round trip.

### 2.6 Opening a row

`Review →` navigates to `/approvals/{id}`, which renders the existing card. Not an inline
accordion:

- the card is a full document with its own scroll regions, its own `100vh`-derived layout and its
  own button row; nesting it inside a list row means either an iframe or a rewrite of its
  containment model, and §7.3's responsive work was scoped against it being the page;
- on a phone the card *is* the screen anyway;
- a real URL is what Claude's own approval link points at, so the deep link and the "expand" gesture
  land on exactly the same page, with no second rendering path to keep in sync.

The list's scroll position and expanded groups are restored on return (§3). On a wide desktop
viewport the list may stay visible as a left rail beside the card — presentation only, same two
endpoints, and explicitly a nice-to-have that ships after the phone layout works.

---

## 3. After a decision: back to the list

Today the decision POST rewrites the whole document to a string:

```python
document.body.innerHTML = r.ok ? "Decision recorded — you can close this tab." : ...
```

That is right for P1 (one card, native-dialog parity, the tab was opened for this one decision) and
wrong for a list the user keeps open. The replacement, in `web/routes_approvals.py`'s shim:

1. **Optimistic exit.** On a 2xx, navigate straight back to `/approvals` — `history.replaceState`,
   not a push, so the browser back button doesn't walk back into a card that no longer exists.
2. **Restore where they were.** Scroll offset and group expansion are stashed in `sessionStorage`
   before navigating away and restored on return. On a phone this is the difference between "back
   to my list" and "back to the top of a list I have to find my place in again".
3. **Say what happened.** A toast, `role="status"`, auto-dismissing after ~4 s: *"Approved — Gmail ·
   send email. Claude will get the data on its next attempt."* The second sentence is only shown
   when the hold window had elapsed, because that is the case where the user needs to know their
   click was not the end of the story.
4. **Move the focus, don't move the user.** Focus lands on the next pending row's `Review` button —
   keyboard and screen-reader users continue without hunting. The row is **not** auto-opened. Auto
   advancing into the next card is how you get a person clicking Allow on muscle memory, which is
   the failure mode §2.2 exists to prevent.
5. **Handle the race.** A 409 (`already_decided`) is not an error dialog: navigate back to the list
   and toast *"Already decided elsewhere — auto-accepted by rule 'Gmail reads'"* when the registry
   can say why. §6 of the plan makes this a genuinely common case, not an edge one: any rule created
   anywhere re-evaluates every pending approval for the principal, so a card open on screen can be
   resolved out from under the person looking at it. The card should notice this from the SSE stream
   and swap its button row for a resolved banner *before* they click, and handle the 409 gracefully
   when they beat it.
6. **The list was already right.** The SSE `removed` event clears the row independently of the
   navigation, so a decision made on a phone empties the row on the desktop tab too, and the
   optimistic removal and the authoritative one agree.

Denials get the same flow with different copy and no "Claude will get the data" line.

---

## 4. Notifications

The scenario is the stated one: the tab is open, has been open for hours, is not the frontmost
window, and something needs a human. Four tiers, in ascending order of how much machinery they cost.

### 4.1 The four tiers

| Tier | Works when | Mechanism | External dependency |
|---|---|---|---|
| **0. In-page** | tab open, no permission needed | title badge `(3) PrivacyFence`, favicon dot, a `role="status"` live region, optional sound | none |
| **1. Desktop notification** | tab open, permission granted | SSE event → `registration.showNotification()` from the page's service worker | **none** — no push service involved |
| **2. Web Push** | tab closed, device asleep, phone | VAPID push → service worker `push` handler | the browser's push service (Apple/Google/Mozilla) |
| ~~3. Native~~ | *(retired at P10 — see below)* | ~~the daemon posts a local notification / badges the menu bar~~ | none |

**Tier 1 is the answer to the question actually asked, and it is nearly free.** The page is open,
the SSE stream from §7.1 is already delivering the new approval, and `showNotification()` puts it in
macOS Notification Center. No VAPID keys, no subscription store, no push endpoint, nothing leaves
the machine. It works on `http://127.0.0.1` because loopback is a secure context, so `local` mode
gets it with no HTTPS work.

Use `registration.showNotification()` from the service worker, not `new Notification()`: the
constructor is unavailable in Chrome on Android and is the shakier path in Safari, while
`showNotification()` is the one call that works everywhere the product cares about. That means the
service worker exists from tier 1 onward, which is also what tier 2 needs later — so build it once.

**Tier 2 is where the platform bites**, and the constraints are worth writing down before anyone
schedules it:

- Push needs a **service worker and a secure context**. Loopback counts as secure, but a phone
  reaching a `local`-mode daemon does not have loopback — so in practice tier 2 belongs to `org`
  mode's HTTPS endpoint (§4 of the plan), which is the same conclusion the plan reaches for mobile
  generally.
- **iOS/iPadOS only allows push for Home Screen web apps.** A site open in a Safari tab cannot even
  request permission; `registration.pushManager` is undefined. So mobile push requires shipping a
  `manifest.webmanifest`, an install ("Add to Home Screen") flow, and copy explaining it. That is a
  real chunk of work and it is entirely invisible on desktop. Safari 18.4+ added Declarative Web
  Push, which drops the service-worker requirement for the simple case — worth a look at
  implementation time, not worth designing around now.
- **Every push must show a notification** (`userVisibleOnly: true`). There is no silent push, which
  is why a resolved approval cannot silently retract its notification on a closed tab (§4.6).
- Encryption is RFC 8291 (ECDH + HKDF + AES-GCM) and VAPID is an ES256 JWT. Both are ~150 lines
  against `cryptography`, **which is already a dependency** — no `pywebpush`, consistent with
  `CONTRIBUTING.md`'s "prefer the standard library over new dependencies".

**Tier 3 does not exist.** This section originally described it as "the local-mode answer for a
closed tab," positioned on the daemon's own bundled macOS menu bar (badge the title with `🛡 3`, or
post a real `NSUserNotification`/`UNUserNotificationCenter` notification), and flagged the tension
explicitly: tier 3 is a *notifier*, not a UI, and either it survives P10 as the one small
AppKit-touching module, or `local`-mode users lose closed-tab notifications entirely — "a decision
P10 should make deliberately rather than discover."

P10 made it: no carve-out. D6 (`docs/https-connector-refactor-plan.md` §15) is unconditional —
"two approval surfaces means two places for a security fix to land" applies exactly as much to a
tiny notifier as to a full dialog host, and keeping one small AppKit-touching module alive to badge
a menu bar that no longer exists (the menu bar itself is gone too, not just the approval/settings
windows) would have been keeping the dependency for its own sake. `local`-mode users on a closed tab
are on tier 2 (Web Push) if reachable, or nothing — same as `org` mode already was. Revisit only if
a real user reports this as a regression that matters in practice, not speculatively.

### 4.2 What actually fires

| Event | Tier 0 | Tier 1/2 |
|---|---|---|
| New approval, tab **focused** | badge + live region | **nothing** — you are looking at it |
| New approval, tab open, not focused | badge | one notification, tag `pf-approval-<id>` |
| New approval while another is unread | badge | replace with a summary: "3 approvals pending" |
| ≥2 approvals within 5 s (one ask, §2.4) | badge | one grouped notification, never one per row |
| Approval decided here | badge decrements | close that tag's notification |
| Approval decided elsewhere | row fades | close that tag's notification |
| Approval about to expire (60 s left) | countdown turns danger | one notification, once, only if never notified |
| Approval expired | row greys | none — a dead approval is not worth a buzz |

Rate limit: at most one notification per 5 seconds per principal, coalescing whatever arrives in the
window into a count. `renotify: false`, stable per-approval tags, so a re-render never restacks.

Click → `notificationclick` → `clients.matchAll({type:'window', includeUncontrolled:true})` → focus
the existing tab and `postMessage` it to `/approvals/{id}`; only open a new window if no tab exists.
The user's tab stays their one tab.

### 4.3 What a notification is allowed to say

This is a privacy product, so the notification body is a data-release surface. It appears on a lock
screen, in a screen share, on a watch, in a meeting-room mirror.

Three levels, a setting under `web.notifications.detail`:

| Level | Body | Default |
|---|---|---|
| `minimal` | "1 approval pending" | `org` mode |
| `standard` | "Gmail — send email · write" (connector + tool + direction) | `local` mode |
| `detailed` | adds the row's title line ("Send email to alice@example.com") | never a default |

Two hard invariants, enforced by construction and asserted by a test, not by care:

1. **No notification payload ever contains gated content** — no message body, no file content, no
   PII match value, no attachment name, no recipient at `standard` or below.
2. **A push payload contains no approval data at all.** Push sends a bare tickle (`{"v":1}`); the
   service worker then fetches `/api/approvals/summary` same-origin with credentials and builds the
   body locally. If that fetch fails (expired session, daemon down), it shows the `minimal` string —
   which also satisfies `userVisibleOnly` without inventing content.

That second one is what makes tier 2 acceptable for this product at all. The push service is a third
party. Payloads are end-to-end encrypted so it cannot read them, but the subscription endpoint is a
stable pseudonymous identifier for the device and the timing and size of pushes are visible to it —
a message per approval means the push service learns exactly when the user's Claude touches gated
data. A content-free tickle leaks the timing and nothing else, and the timing is unavoidable if
push is used at all. `docs/security-and-compliance.md` should carry this as an explicit entry when
tier 2 ships, along with the fact that tier 2 is off by default and per-principal.

### 4.4 Asking for permission

Never on page load. A cold `Notification.requestPermission()` on first paint is the pattern browsers
now actively penalize and users reflexively deny — and a denial is close to permanent, since
re-granting means digging through browser settings.

- Offer it **after the first successful decision**, in the return-to-list toast: *"Want PrivacyFence
  to notify you when Claude needs approval? [Enable]"* — at that moment the value is concrete and
  the user has just proven they want to be told.
- Always behind a **pre-prompt** the product owns, so a "not now" costs nothing and can be asked
  again; the browser prompt only ever fires from that click (a user gesture, which Safari requires).
- The header toggle is the permanent home for the state, including the dead end: if permission is
  `denied`, say so and link to the browser's own instructions rather than silently doing nothing.
- On iOS, the toggle explains the Home Screen requirement instead of offering a prompt that cannot
  work.

### 4.5 Failure modes

| Failure | Behavior |
|---|---|
| Permission denied | tiers 0 and 3 still work; header toggle shows the state and how to undo it |
| SSE dropped | reconnect with backoff, `● live` → `◌ reconnecting…`, full state refetch on reconnect (never patch onto a stale list) |
| Service worker unregistered / unsupported | tier 0 only, silently — never a broken-page error |
| Daemon down | `✕ can't reach PrivacyFence`, list dimmed and marked stale rather than emptied |
| Push subscription expired (410 from the push service) | drop it server-side, mark the device stale in settings, do not retry forever |
| Notification for an approval decided while the tab was closed | tag closed on next SSE connect; if the user taps it first, `/approvals/{id}` already says "no longer pending" — the graceful landing that route was built for |
| Two devices, one decision | both close the notification, both rows vanish, the loser gets the 409 path (§3.5) |

---

## 5. Eleven things that weren't asked for and belong here anyway

### 5.1 The hold-window countdown is the highest-value pixel on the page

Covered in §2.3. It is called out again here because it is the one piece of UI that turns the plan's
worst measured finding (P0: Claude stops and asks a human rather than re-calling) into something the
user can route around — decide inside 30 seconds and the round trip never happens. Nothing else on
the page changes the user's cost of an approval that much.

### 5.2 Never offer bulk approve

§2.2. Restated as a standing product rule rather than a screen detail, because it is the thing that
will be requested repeatedly and should be declined with a reason each time.

### 5.3 The list should be measured, locally, and only locally

P3's beta needs numbers the plan explicitly asks for: how often a decision lands inside the hold
window, time-to-decision distribution, expiry rate, how often Claude re-calls after approval. All of
it is already derivable from the audit log once §5.4's `decided_at` field lands — pending entry,
release entry, both carrying the same `request_id`.

So: a `scripts/approval_stats.py` over the local JSONL, printed as a report the user can choose to
paste into a beta issue. **No telemetry, no phone-home, not even opt-in.** A privacy product that
ships usage analytics has an argument to have with itself first, and this data can be had without
having it.

### 5.4 Make the rule the obvious exit from a repetitive list

`seen_count` ("Seen 3 times this week") is already computed and already on the card. On the row it
should be a link, not a caption: *"Seen 3 times this week — always allow calls like this"*, landing
on the card with the matching "Always allow" candidate pre-highlighted (the card already renders
those candidates and posts `accept_all` with a choice index; nothing new is needed under it).

The best version of this page is one nobody has to visit. Every design decision that makes a long
list more comfortable should be weighed against making the list shorter instead.

### 5.5 Approving is not the end — show the release

§2.5's "released" marker. The deferred protocol splits approval from delivery, and the gap is
invisible unless the UI shows it. This is also the honest place to surface the write-decision rule
from §5.4 of the plan: a write approval is single-consumption, so once released, an identical call
will ask again. A user who doesn't know that reads the second prompt as a bug.

### 5.6 Say which Claude is asking

`local` mode is single-user but not single-client: Claude Code in two terminals and Claude Desktop
can all be talking to the same daemon. When the registry knows the MCP session, the row's kicker
should name it ("Claude Code · started 14:02"), and in `org` mode this becomes the principal, which
is not optional there. Without it, "why is something asking for my Drive files" has no answer on
the page.

### 5.7 Accessibility is the same feature as notifications

A blind user's version of "tell me when something arrives" is an `aria-live="polite"` region
announcing "New approval: Gmail, send email". It is the same event, the same copy, a different
output device — build them together.

Also: focus is moved deliberately after every decision (§3.4); every interactive target is ≥44px on
touch; no colour-only state (each badge carries text); `prefers-reduced-motion` removes the
countdown bar's animation and the row fade; **no single-key approve shortcut** — `j`/`k` to move and
`Enter` to open are fine, a bare `a` for allow is exactly the habituation problem again.

### 5.8 Dark mode is already done — don't break it

`resources/approval_window/styles.css` carries a full mirrored dark palette and a 700px breakpoint.
The list must use the same tokens, which argues for lifting the `:root` token block into a shared
`resources/tokens.css` that both stylesheets embed, rather than copying nine colour ramps into a
second file where they will drift within two releases.

### 5.9 Copy, decided once, in a table

The words are the product here. A single table in the implementation PR, reviewed as copy rather
than invented per-branch:

| Situation | Copy |
|---|---|
| Inside hold window | "Claude is still waiting — 24s" |
| Hold window elapsed | "Claude has moved on — decide anyway, then ask it to continue" |
| Expiring | "Expires in 47s" |
| Expired | "Expired — Claude was told no" |
| Auto-accepted by rule | "Auto-accepted by rule *Gmail reads*" |
| Approved, hold window elapsed | "Approved. Claude will get the data on its next attempt." |
| Approved, write | "Approved. A future identical send will ask again." |
| Denied | "Denied. Nothing was sent." |
| Empty list | "Nothing is waiting. PrivacyFence is watching." |
| Stream down | "Can't reach PrivacyFence — this list may be out of date." |

Note what is *not* in the table: no "Are you sure?", no "Success!", no exclamation marks. The tone of
the card is plain and slightly formal; the list should not be chattier than the thing it wraps.

### 5.10 Install it as an app, on both ends

The `manifest.webmanifest` that iOS push requires (§4.1) also buys a standalone window on macOS, a
dock icon, and `navigator.setAppBadge(n)` for a pending count on the icon — feature-detected,
progressive enhancement, no fallback needed. Cheap, and it makes the always-open tab an always-open
*app*, which is what the user is actually describing when they say they leave it open.

### 5.11 Two smaller ones

- **Attachment previews 404 by design.** §7.1: preview bytes are gone the moment an approval is
  decided. A card left open on a phone will hit that. The image slot needs a real empty state
  ("preview no longer available — this approval was decided"), not a broken-image icon.
- **Relative time needs an absolute on hover.** "6 seconds ago" is right for the row; "14:02:31"
  belongs in the `title=` attribute, because the person cross-checking a row against the audit log
  needs the real timestamp.

---

## 6. Where this lands in the phase plan

The bulk of this is **P3**: the list is only meaningful once several approvals can be pending, and
§2.3's hold-window state, §2.4's grouping, §3.5's rule-race and §5.5's release marker are all
descriptions of P3's own protocol. It does not need a phase of its own.

Three pieces can land earlier, on P1's single-slot `WebApprovalUI`, and are worth landing early
because they are what makes the web surface usable day to day rather than demo-able:

| Piece | Why it works pre-P3 |
|---|---|
| §3's return-to-list flow | Pure shim change (`document.body.innerHTML = …` → navigate + toast). No registry needed. |
| §2.5's page shell, live indicator, empty state | Renders 0 or 1 rows perfectly well; the list just never has two. |
| §4 tier 0 + tier 1, at `minimal` only | Needs the SSE stream, which is small even against a single slot. This alone delivers the notification the user asked for, without needing §4.3's per-field allowlist below. |

§4.3's `standard`/`detailed` levels shipped at **P5**, alongside that phase's bridge retirement
(`https-connector-refactor-plan.md`'s own phase table has the rationale for the pairing): a real
per-field content allowlist (`web_shell.py`'s `notificationBody()`) lets a notification name a
connector/tool/direction, and — at `detailed` only — the row's own title, without violating
§4.3's no-gated-content invariant. Before P5, `web.notifications.detail` above `minimal` was a
config no-op — the shipped body was always the bare count, regardless of the setting.

Rough sizing, in the plan's own S/M/L terms: the list page and the return flow are **M** together;
tiers 0/1 plus the service worker are **S**; `standard`/`detailed`'s per-field allowlist (P5, above)
is its own **S** on top of that; tier 2 (VAPID, subscription store, manifest, iOS install flow) is a
solid **M** on its own and should be scheduled with `org` mode (P7+), not bolted onto P3. Tier 3 is
**S** and needs the P10 decision in §4.1 first.

Config keys, following the plan's rollback-per-phase discipline:

```yaml
web:
  notifications:
    enabled: true          # tiers 0-1
    detail: standard       # minimal | standard | detailed
    sound: false
    push: false            # tier 2, org mode only
    # No tier-3 key -- the native notifier this table once had was retired
    # at P10 along with the rest of the AppKit UI layer (§4.1).
```

No version bump on this work — `CLAUDE.md`'s rule, only at actual release.

## 7. Tests

The existing shape of the suite carries almost all of this, because the page is a pure function:

- `test_approval_list_html.py` — `build_list_html(state)` against fixture states: empty, one row,
  a group of three, each of §2.3's seven row states, a `claude_reason` containing `<script>` and
  markup that must come out escaped and inert.
- The no-network assertion the card already has, applied to the list document: no `http://` or
  `https://` anywhere in the output.
- **The notification-content invariant as a real test** (§4.3): build a notification payload from a
  card whose preview, PII categories and title are stuffed with marker strings, and assert that at
  each detail level nothing gated appears — and that a *push* payload contains no approval data at
  all, at any level.
- SSE route tests: connect, receive `added`/`removed`, reconnect and get a full state refresh.
- Headless Chromium, which §13 of the plan already establishes as the smoke path: decide a card,
  assert the browser ends up back on `/approvals` with the row gone; open two tabs, decide in one,
  assert the row disappears in the other.
- `tests/conftest.py` gets resets for whatever new module-level singleton the notification and
  subscription stores introduce — the standing §2.7 item.

Everything above is platform-independent, which is the Linux CI leg the plan already adds at P1.

## 8. Open questions

1. ~~Does the tier-3 native notifier survive P10?~~ Resolved: no. §4.1 has the full reasoning —
   D6's "two approval surfaces means two places for a security fix to land" applies to a notifier
   the same as a dialog host, so `local` mode has no closed-tab notification without a push service
   now, same as `org` mode always did.
2. **Which Claude surface actually opens the approval link, and does a service worker registered
   there stick?** The plan already has this question open for WebAuthn (§10.6). Tier 1 needs the
   same answer for a different reason: a link opened in an in-app webview may register a service
   worker in a storage partition that dies with the view, in which case notifications work in the
   system browser and silently do nothing in Claude's own.
3. **Does the hold window get raised?** §5.4's live option. If P2's measured tool-call timeout
   allows, say, 90 seconds, §2.3's countdown becomes the dominant path and §2.5's "released" marker
   becomes a rarity — the same page, a materially different centre of gravity.
4. **Is the desktop list-plus-card rail worth it** (§2.6), or does the phone-first single-column
   flow carry desktop well enough? Decide against a real build, not on paper.
