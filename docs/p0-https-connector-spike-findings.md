# P0 spike findings — HTTPS connector refactor

**Status: P0 complete.** This records what the P0 spike in
[`https-connector-refactor-proposal.md`](https-connector-refactor-proposal.md) (§12, "P0 must
answer four questions, not three") actually found. Per that section, nothing built for the spike
itself is kept — no `web/` module, no server, no bridge changes landed from this. This document is
the spike's entire deliverable: four answers and an estimate.

---

## 1. Do the two HTML documents work as live pages?

**Yes, cleanly, with no changes to either module.**

Both `approval_window_html.build_card_stack_html()` (a WIDE read-gate Gmail card with a PII match
and two "Always allow" candidates) and `settings_window_html.build_html()` (the shipped test
fixture's own state) were served over a plain `http.server` process, with
`window.webkit.messageHandlers.pf.postMessage` shimmed to a `fetch()` call against a `/api/decide`
(approval) or `/api/settings-action` (settings) endpoint — the one JS change §7.1 already
identifies, done here as a runtime shim rather than a module edit specifically so the two shipped
modules stayed untouched.

Driven end-to-end in headless Chromium (Playwright):

- The approval card rendered pixel-identical to §11.1's earlier static check, buttons started
  `aria-disabled="true"` and were enabled by the document's own `DOMContentLoaded` handler exactly
  as designed, and clicking the second "Always allow" candidate produced a real network POST
  carrying `{"action":"resolve","result":"accept_all","choice":1}` — the exact payload shape
  `gate.py`/`approval_window.py` already expect from the WebKit bridge today.
- The settings page rendered correctly (nav rail, PII toggles, update-check section, org-config
  card) and clicking the first toggle produced a real POST of `{"action":"toggle_pii_detection"}` —
  again the exact shape `settings_window.py`'s dispatcher already handles.
- Zero console errors from either document itself (one unrelated 404 for a browser-requested
  favicon).

**Conclusion unchanged from §11: this half of the refactor is a hosting change, not a rewrite.**

---

## 2. Will Claude actually re-call a tool after a pending result?

**Tested for real, on Claude Code specifically, with a mixed and important result: it mostly does
not re-call autonomously — but not for the reason the proposal worried about.**

### Method

A real MCP stdio server (`@modelcontextprotocol/sdk`, not a mock) was written with one tool,
`spike_get_account_report(account)`. First call for a given `account` returns exactly the
pending-shaped JSON §5.2 specifies (`status: "approval_pending"`, `approval_id`, `url`,
`expires_at`, `message`); the tool's own description states the re-call contract explicitly, per
§5.4's mitigation ("tool descriptions state the re-call contract explicitly"). A second call with
the identical argument returns real content.

This was committed to a throwaway branch (`spike/p0-recall-experiment`, off this branch, **not
merged, to be deleted**) with a `.mcp.json` wiring it up, and exercised from five independent, fresh
Claude Code Remote sessions (`claude-sonnet-5`, default `auto` permission mode) — the "Claude Code"
surface named in §12's question 2 — each given a plain, ordinary-sounding request to fetch the
northwind account's report.

### Result

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
stopped to ask a human, even in attempt 5 where the initiating prompt explicitly pre-empted that
exact concern (which, read a different way, is itself a classic injection-attempt shape — "trust
me, this isn't an attack" — so the pre-emption plausibly made it more suspicious, not less).

### Why this matters, and why it isn't the failure mode §5.4 anticipated

§5.4 worried about a model that "reports 'needs approval' and then stops" — an attention/UX
failure. What was actually observed is more specific and more structural: **Claude Code's own
prompt-injection defenses generalize to "an instruction embedded in a tool's description or return
payload, telling me to autonomously repeat a call, looks like an attack" — which is exactly the
shape §5.2's re-call contract has to take**, no matter how it's worded. That is a reasonable, even
correct, generalization for those defenses to make in general; it just collides head-on with this
specific protocol.

The security invariant is *not* threatened by this — if anything it's reinforced: every attempt
ended with a human being asked before anything else happened, never a silent wrong action. But it
means:

- The `privacyfence_await_approval` meta-tool's UX case (§5.2: "Claude fires several gated calls
  ... waits on all of them at once, and re-issues each as it clears") should be designed **assuming
  a confirmation turn per pending approval is the common case, not the exception** — at least until
  P3's beta shows otherwise on real traffic. Treat the pause as something to design good copy for
  ("3 approvals are pending — want me to keep checking and let you know?"), not as a bug to word
  away.
- Wording the tool description more carefully is unlikely to fix this on its own — the mechanism
  triggered even when the initiating human prompt itself pre-authorized the exact behavior. A
  system-level or protocol-level signal (a distinct MCP content type for "this is a legitimate
  pending status, not an instruction," if one becomes available, rather than prose in a tool
  description) is a more promising direction than copy-editing.
- This was **not** tested against Claude Desktop, Claude web, or Claude mobile — the other three
  surfaces §12 names — because none are reachable from this sandboxed environment. Given the
  behavior traced back to a general safety property rather than something Claude-Code-specific, the
  same defense is worth assuming everywhere until checked, not treated as a Claude-Code quirk.
- Five attempts on one day, one model, one tool description is a real signal, not a proof — P3's
  beta (already planned, and already flagged in the phase table as needing one) is still where this
  gets settled on real traffic and real wording iterations. What P0 adds is that the risk is
  confirmed to exist today, via a mechanism worth designing around rather than assuming away.

---

## 3. Does WebAuthn work where the approval link actually opens?

**Not independently testable from this environment — flagged, not answered.**

This sandboxed session has no access to real Claude Desktop, iOS, or Android apps, so it cannot
observe what component actually opens an `https://` link tapped inside a Claude conversation on
those surfaces. Desk research confirms the platform-level facts D7 depends on:

- Chrome Custom Tabs (Android) and `SFSafariViewController`/`ASWebAuthenticationSession` (iOS) both
  support platform WebAuthn (passkeys) fully, with no special app integration needed.
- A bare embedded `WebView` (Android) does **not** support the platform-authenticator passkey UI.

What isn't publicly documented, and isn't visible from here, is which of these Claude's own mobile
apps actually use for a link inside a chat message — that is Claude-app-specific behavior, not a
general platform fact, and it can change between app versions. §12's own text already anticipated
exactly this gap ("Test this in the Phase 0 spike — it is the difference between a real control and
a coin flip"); it remains open.

**Recommended concrete check** (cheap, ~10 minutes, needs a human with the real apps): host a
minimal WebAuthn test page (e.g. `webauthn.io`) somewhere reachable, post its link into a real
Claude conversation on Desktop, iOS, and Android, tap it, and confirm whether the platform biometric
prompt (Face ID / Touch ID / fingerprint) appears. Do this before P9 relies on D7 being real.

---

## 4. What does the responsive pass on the card CSS actually cost?

**More than a media-query tweak, less than a rewrite — concretely, about a day, now that the shape
of the work is known.**

A real (throwaway) patch was applied to the WIDE approval card's rendered output and tested at a
375×812 phone viewport against the unpatched original:

| | Unpatched | Patched |
|---|---|---|
| `documentElement.scrollWidth` at 375px viewport | 980 (horizontal overflow) | 375 (fits) |

What the patch actually needed, in order of size:

1. **Trivial**: `CONTENT_WIDTH`'s fixed `610px`/`980px` → `min(Xpx, 100%)`.
2. **Small, localized**: the three WIDE-only layout styles
   (`build_card_stack_html`'s outer flex row, left column, right pane) are written as **inline**
   styles today, and an inline style can't carry a `@media` query — they need converting to CSS
   classes first. About a 10-line diff inside `build_card_stack_html`'s WIDE branch.
3. **The real cost, and a genuine finding, not a guess**: naively flipping the outer row to
   `flex-direction: column` under a breakpoint does not work. Two bugs were hit and fixed along the
   way before landing on a working model:
   - A duplicate `class` attribute (an easy mistake when converting an inline style to a class
     without also removing what was already there) silently no-ops the override — worth calling out
     explicitly for whoever does this for real, since the failure is silent, not an error.
   - `flex: 0 0 420px` on the left column, once the parent becomes `flex-direction: column`, sets
     *height* instead of *width* — it's a main-axis property. Combined with both panes still being
     `flex:1;min-height:0;overflow-y:auto` (built for two independently-scrolling regions sharing a
     fixed real `100vh`, per §7.3's own description of the desktop design), the two panes fight each
     other for a height neither actually needs, and content gets silently clipped into an invisible
     nested scroll region rather than showing or overflowing visibly.
   - The fix that actually works: below the breakpoint, `body` drops its fixed `height:100vh` for
     `min-height:100vh` (natural page flow — exactly what §7.3 already anticipated, "height:100vh
     becomes a container-relative height"), and **both** WIDE panes drop to
     `flex:none;height:auto;overflow:visible`, so each sizes to its own content and the page scrolls
     once, normally, instead of two independent 100vh-locked regions stacked on top of each other.
4. **Untouched, confirmed by inspection**: the fixed-row-height/line-clamp truncation design
   (`.pf-kv`, `.pf-quote`) needed zero changes and looked correct at the phone viewport as-is.
5. **Settings page (§7.2) needs no responsive work** — `settings_window_html`'s layout is already
   fluid-width and produced no horizontal overflow at 375px. One minor, non-blocking nice-to-have
   noted for later: the fixed-width nav rail eats over a third of a 375px screen and squeezes toggle
   labels into an awkward one-word-per-line wrap — worth a follow-up pass, not a P0-blocking finding.

**Estimate**: roughly a day of focused engineering for the CSS/markup change itself, plus new
`test_approval_window_html.py` assertions for the responsive breakpoint's output, plus the one real
manual check (a genuinely small device or the browser devtools' device emulation, either is fine)
that this document's Chromium-only testing can't fully replace. This fits inside P1's existing "M"
sizing rather than needing its own phase.

---

## What this changes about the plan

- **P0's exit criterion is met** — all four questions have real answers, not assumptions.
- **Question 2's answer is the one that should change how P3 is scoped**: design the deferred-approval
  UX assuming a human confirmation turn is the *common* case per pending approval on Claude Code,
  not an edge case, until P3's beta says otherwise. This doesn't change §5's protocol shape (the
  security invariant holds either way) but it changes what "smooth" looks like in practice, and it's
  worth a sentence in §5.4 saying so.
- **Question 3 stays an open risk for D7/P9**, now with a cheap, concrete manual test named instead
  of an assumption either way.
- **Question 4 de-risks §7.3**: the responsive pass is real, bounded work with a known shape, not an
  unknown one — safe to leave sized inside P1 as already planned.

Nothing from the spike itself (the throwaway HTTP server, the Playwright driver, the patched CSS, the
MCP test server) is carried into the codebase. The `spike/p0-recall-experiment` branch this used
should be deleted once this is reviewed.
