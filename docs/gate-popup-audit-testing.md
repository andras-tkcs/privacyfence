# Automated Gate, Popup-UI, and Audit-Log Testing (Design Proposal)

**Status: Parts A and B implemented** (`tests/unit/test_gate_real_evaluator.py`,
`tests/unit/test_approval_window.py`, and the `approval_window.py` build/run split below). **Part C
remains proposed, not implemented** — see that section for why it's kept optional. This closes the
gap
[`external-api-contract-testing.md`](external-api-contract-testing.md) names explicitly in its own
"Test coverage" evaluation:

> Nothing here exercises the gate, the popup UI, or the audit log.
> [`connector-qa-testing.md`](connector-qa-testing.md) remains the only thing that does, and
> remains necessary before releases.

That statement is still true after this design ships — see [Relationship to
`connector-qa-testing.md`](#relationship-to-connector-qa-testingmd) — but it should become true for
a much narrower reason than it is today.

## Problem

`external-api-contract-testing.md` closes the *connector-client* blind spot: real API response
shape vs. the hand-authored fixtures `tests/unit/` mocks against. It says nothing about the three
things `connector-qa-testing.md` actually spends its twelve phases on, and today only a human
running that manual prompt exercises any of them:

1. **The gate** (`gate.py`) — auto-accept evaluation, the PII-detection override, `Accept All`
   rule creation, `Accept for 5 min` temp-accept, and the audit entry each path produces — but only
   as a **real, end-to-end pipeline** wired to a **real** `AutoAcceptEvaluator` loaded from a real
   `settings.yaml`. `tests/unit/test_gate.py` already exercises `gated_call`'s state machine
   thoroughly, but the large majority of its ~50 tests drive it with `FakeEvaluator`, a stand-in
   that returns a canned `(bool, str)` from `should_auto_accept()` with no rule-matching logic of
   its own. Exactly one class, `TestApprovedObjectTypesNeverPopsUp`, uses the real
   `AutoAcceptEvaluator` — added as a regression test after a live QA run turned up a genuine
   contradiction (a popup the operator saw, next to an audit entry claiming `auto_accepted` for
   the same call) that couldn't be resolved from the log alone. That one test proves the pattern is
   valuable; it doesn't generalize it. Every other auto-accept rule `connector-qa-testing.md` checks
   by hand across ten connectors (`approved_folder`, `trusted_sender_domain`, `i_am_organizer`,
   `i_am_author`, `i_am_reporter`/`i_am_assignee`, `approved_task_list`, `no_contact_info_change`,
   `session_temp_accept`, the PII-gate override of all of the above) has no equivalent
   real-evaluator test today.
2. **The popup UI** (`approval_window.py`) — the actual AppKit window `ApprovalWindowController`
   builds: which buttons appear for which combination of `allow_accept_all` /
   `allow_temp_accept` / `pii_categories`, whether the PII wash/banner renders when it should (and
   only then), whether the summary box shows the right preview fields, whether the scrollable
   `NSTextView` holds the full `details_text`. `tests/unit/test_approval_popup.py` and
   `test_menu_bar.py` both mock `show_native_approval` itself — by design, so no test run ever pops
   a real interactive dialog — which means **zero** test in the suite ever imports
   `approval_window.py`, let alone instantiates `ApprovalWindowController` or looks at what it
   builds. A bug in `_pii_banner_text`'s wording, a missing `Accept All` button when
   `allow_accept_all=True`, or a PII wash that fails to render would only ever be caught by a human
   looking at the screen during a `connector-qa-testing.md` run.
3. **The audit log's field-level contract** — not `audit_log.py`'s read/write mechanics
   (`test_audit_log.py` covers those directly), but the *specific claims*
   `connector-qa-testing.md`'s Phase 12 exists to verify by hand: that a PII-overridden read logs
   `"decision": "approved"` (not `"auto_accepted"`) with `"auto_accept_rule": ""` and
   `"pii_detected": true`; that the matching write logs `"pii_detected": false` regardless of
   identical content; that `Accept for 5 min`'s second call logs `"auto_accepted"` with
   `"auto_accept_rule": "session_temp_accept"`. These are gate.py behaviors already partially
   covered in `test_gate.py` (see `TestPIIGate`, `TestTempAccept`) — the actual gap here is narrower
   than it sounds once Part A lands: it's making sure the same claims hold when the auto-accept
   side is the *real* evaluator, not `FakeEvaluator`, since that's the only way a rule-vs-PII
   interaction is genuinely exercised rather than asserted by construction.

None of this is a defect in the existing unit suite — mocking the native popup layer is the right
call for testing `gated_call`'s state machine in isolation, exactly as mocking `*_client.py` is the
right call for connector logic. The blind spot is structural, the same shape as the one
`external-api-contract-testing.md` describes: everything downstream of the mock boundary is
unverified by anything except a human, on a cadence of "whenever someone next runs
`connector-qa-testing.md`."

## Design principles

1. **Reuse the mock boundary that already works; extend what's on the real side of it.**
   `test_gate.py`'s pattern of monkeypatching `approval_popup.show_read_popup` /
   `show_popup` to return a scripted decision, without ever touching `approval_window.py`, is
   correct and stays exactly as it is. What changes is swapping `FakeEvaluator` for a real
   `AutoAcceptEvaluator` fed a real (temp-file) `settings.yaml`-shaped rules dict — turning
   `TestApprovedObjectTypesNeverPopsUp`'s one-off pattern into the default for every rule
   `connector-qa-testing.md` currently checks by hand.
2. **Popup-UI tests build the window; they never run the modal loop.** `runApproval_` in
   `approval_window.py` already separates window construction (private helper methods:
   `_summary_rows`, `_summary_height`, `_pii_banner_height`, `_build_details_view`,
   `_build_button`, `_compute_layout`) from driving it (`makeKeyAndOrderFront_`,
   `activateIgnoringOtherApps_`, `runModalForWindow_`, `orderOut_`). A test that instantiates
   `ApprovalWindowController`, sets its fields, and inspects the constructed `NSView`/`NSButton`/
   `NSTextView` tree needs no human and no modal session — the same principle the existing
   `test_approval_popup_escaping.py` already applies (real `osascript`, but a non-interactive
   `-e` expression, never a blocking `display dialog`).
3. **No new CI environment.** `tests.yml` already runs on `macos-latest` specifically because this
   is a PyObjC / AppKit project; that runner already has everything Part B needs (a real
   `NSApplication`, real `AppKit` classes) without provisioning anything new. Nothing here asks for
   a differently-configured runner, an Accessibility-permission grant, or a logged-in interactive
   session beyond what already backs the existing `osascript`-based tests.
4. **The one piece that genuinely needs a human pressing a real button — the modal event loop
   itself blocking and a real click resolving it — stays out of CI**, same as
   `external-api-contract-testing.md`'s fixture recorder stays out of CI. It's designed as an
   optional, explicitly-invoked local script (Part C), not a pytest test, and not a gate on anything.
5. **Every new test earns its place by mapping to a specific manual step this design intends to
   stop needing a human for.** The [Rollout plan](#rollout-plan) cross-references
   `connector-qa-testing.md` phases so it stays legible which manual steps this narrows, and which
   don't move at all.

## Part A — Real-evaluator gate integration tests

### What's new relative to `test_gate.py` today

A new test module, `tests/unit/test_gate_real_evaluator.py`, alongside the existing
`test_gate.py` (which keeps its `FakeEvaluator`-based state-machine tests as-is — those aren't
being replaced, just supplemented). Each test:

1. Builds a real `AutoAcceptEvaluator` from a rules dict shaped exactly like one
   `connector-qa-testing.md` fixture's slice of `settings.yaml` (e.g.
   `{"gmail.read_message": [{"rule": "trusted_sender_domain", "value": "trusted.com"}]}`).
2. Calls `gate.gated_call(...)` with `args`/`raw_data` shaped the way the real connector module
   builds them (matching the existing convention in `TestApprovedObjectTypesNeverPopsUp`, which
   already cites `connectors/salesforce.py::_get_record` as its args shape reference).
3. Asserts both the return value **and** the resulting `AuditEntry` fields — not just "a popup
   would/wouldn't have been shown," but the exact `decision` / `auto_accept_rule` / `pii_detected`
   triple `connector-qa-testing.md`'s Phase 12 reconciliation is checking for that scenario.

### Scenarios ported from `connector-qa-testing.md`'s manual checks

Each maps to one or more numbered steps in that doc, so it's traceable which manual "should NOT
prompt" / "should still prompt" assertions become a deterministic test instead of a per-release
human judgment call:

| Scenario | `connector-qa-testing.md` reference | Assertion |
|---|---|---|
| `trusted_sender_domain` matches a subdomain | Phase 1 step 6 | `auto_accepted`, rule name present |
| `approved_folder` auto-accepts a plain read | Phase 2 step 3 | `auto_accepted` |
| PII content overrides a matching `approved_folder` rule | Phase 2 steps 21–23 | `approved` (not `auto_accepted`), `auto_accept_rule=""`, `pii_detected=true` |
| Same content on the write side never scans | Phase 2 step 21 | `pii_detected=false` regardless of body |
| `session_temp_accept` covers a second call, same file, within TTL | Phase 2 steps 5/13/16 | first call `approved`/interactive, second call `auto_accepted` with rule `session_temp_accept` |
| `i_am_organizer` / `i_am_author` / `i_am_reporter` / `i_am_assignee` | Phase 4 step 5, Phase 9 step 5, Phase 10 step 5 | `auto_accepted` when ctx matches `my_email`, normal path otherwise |
| `no_contact_info_change` allows a name/note edit, not an email/phone edit | Phase 5 steps 5–6 | `auto_accepted` for the former, normal path for the latter |
| `approved_task_list` / `approved_project_keys` / `approved_space_keys` / `approved_report_ids` / `approved_object_types` | Phases 6, 8, 9, 10 | `auto_accepted` inside the allowlist, normal path outside it (contrast case) |
| `Accept All` persists a rule that then covers a second, different-but-matching item | Phase 2 step 12 pattern | second call `auto_accepted` with the newly created rule name, without a second popup |

Every row already has a `gate="review"` vs. `gate="popup"` contrast built into
`connector-qa-testing.md`'s own script (an in-allowlist call next to an out-of-allowlist one) — the
test for each row should keep that pairing, the same way
`TestApprovedObjectTypesNeverPopsUp.test_object_type_outside_allowlist_still_shows_the_popup`
already does, so a rule that's vacuously never reachable doesn't silently pass.

### What this does not attempt

This does not re-derive `suggest_rule`'s rule-shape logic or `auto_accept.py`'s per-rule matching
functions from scratch — those already have direct unit coverage in `test_auto_accept.py`. Part A
is specifically about the *integration* seam: real evaluator + real `gated_call` + real audit
write, together, for the exact scenarios a human currently has to drive through a live Cowork
session to observe.

## Part B — Popup-UI construction tests

### The refactor this needed — done

`ApprovalWindowController.runApproval_` used to build the entire window **and** drive the modal
loop in one method. Testing the construction half without ever blocking on `runModalForWindow_`
needed that split made explicit — a small, mechanical refactor, no behavior change, now shipped:

- New method `build_panel() -> NSPanel`: everything `runApproval_` used to do up to and including
  `panel.setContentView_(content)` and populating `content` with the kicker, icon, title, PII
  wash/banner, summary box, details scroll view, and button row — returns the fully-populated
  `panel` (its `contentView()` carries the whole subview tree tests walk).
- `runApproval_` itself shrunk to: activation-policy handling, call `build_panel()`, then exactly
  the window-driving part it always did (`makeKeyAndOrderFront_`, `setLevel_`,
  `activateIgnoringOtherApps_`, `runModalForWindow_`, `orderOut_`).

This is the same shape of change `external-api-contract-testing.md`'s recorder makes to
`daemon_main.build_connectors()` (reusing existing construction logic rather than duplicating it) —
here it's splitting "build" from "run" inside a single class that already kept them logically
separate, just not yet callable separately. `tests/unit/test_approval_popup.py` (mocked) passed
unchanged after the split, confirming `show_native_approval`'s external contract didn't move.

### New tests: `tests/unit/test_approval_window.py` — implemented

Marked `skipif sys.platform != "darwin"`, matching `test_approval_popup_escaping.py`'s existing
precedent for tests that touch real macOS frameworks rather than mocking them. No test in this
module calls `runApproval_` or `runModalForWindow_` — every assertion works against the real
`NSView` tree `build_panel().contentView()` holds, walked via `subviews()`.

Coverage, each traceable to a `connector-qa-testing.md` step or ground rule that currently asks a
human to eyeball the popup:

- **Button set per gate configuration** (ground rule: "Deny / Accept / Accept All / Accept for 5
  min"): `allow_accept_all=True` → an `NSButton` titled exactly `"Accept All"` is present;
  `allow_accept_all=False` → it is absent. Same pattern for `allow_temp_accept` →
  `"Accept for 5 min"`. `Accept`/`Deny` are present unconditionally, and `Accept`'s
  `keyEquivalent()` is `"\r"` (Enter defaults to Accept, matching every "I'll click Accept" step)
  while `Deny`'s is `"\x1b"` (Escape).
- **PII tint and banner** (Phase 2 steps 18–19, 21–23): with `pii_categories=[]`, no subview has
  the `_PII_RED`-derived fill color anywhere in the tree (the "plain, untinted popup" a write
  always gets). With `pii_categories=["Email-like", "Phone-like"]` non-empty, a full-window wash
  box is present with `_PII_BACKGROUND_ALPHA`, a banner box with `_PII_BANNER_FILL_ALPHA`, and a
  banner label whose `stringValue()` matches `_pii_banner_text()` verbatim (catching a wording
  regression the same class of bug the PII-category cross-check in
  `connector-qa-testing.md`'s Phase 2 step 20 currently only catches by a human reading the popup).
- **Summary box contents** (every "confirm the summary box shows X" instruction across all ten
  phases): for a given `preview` dict, walking the built overlay's label/value `NSTextField`
  pairs reproduces the dict's keys and `str(value)`s in order.
- **Details pane holds the full content, not a truncated preview**: `NSTextView.string()` on the
  scroll view's `documentView()` equals `details_text` exactly, including a multi-KB body — the
  thing "the user always sees what they're approving before they can click Accept" (the module
  docstring's own claim) actually depends on.
- **Layout doesn't overflow or collapse for edge-shaped input**: `_compute_layout` (already a pure
  function of `title`/`pii_categories`/`preview`) returns a sane, monotonically-increasing height
  for an empty `preview`, a single-row `preview`, a very long title needing wrap, and PII banner
  text at various category-list lengths — cheap regression coverage for a bug in the details pane
  quietly clipping the last line, which no interactive QA step currently checks quantitatively
  (a human eyeballing the popup would only notice an *obvious* clip, not an off-by-one).
- **`buttonClicked_` maps every button title to the right `result`**: construct a controller,
  call `buttonClicked_` directly with a fake `sender` object exposing only `.title()` (no real
  `NSButton` needed for this one), for each of `"Accept"`, `"Deny"`, `"Accept All"`,
  `"Accept for 5 min"`, and confirm `controller.result` and that
  `NSApplication.sharedApplication().stopModalWithCode_` was invoked (mockable, since it's a
  no-op when no modal session is running) — no refactor needed for this part, it's already
  callable in isolation today.

### What Part B does not attempt

Pixel-level rendering, exact on-screen positioning, or anything requiring the window to actually
be visible. Those are exactly the things a human glancing at the popup during
`connector-qa-testing.md` is good at and a headless assertion on frame-rects would be a poor,
brittle substitute for — this part targets *content and structure* (right buttons, right text,
right tint), not visual fidelity.

## Part C — Optional: scripted full-modal smoke test (local only, never CI)

The one thing Parts A and B still don't touch: does `runModalForWindow_` actually block until a
real click, and does a real click on the real "Accept All" button actually resolve
`show_native_approval()` to `"accept_all"`? This is the AppKit equivalent of
`external-api-contract-testing.md`'s live-fixture recorder — a tool a developer runs locally,
never wired into CI, never required for a PR.

- A new script, `scripts/qa_popup_smoke.py`, run manually (`python3 scripts/qa_popup_smoke.py`):
  calls `show_native_approval` with a representative set of arguments (plain popup, PII-tinted
  popup, `allow_accept_all=True`, `allow_temp_accept=True`), and for each one, instead of waiting
  for a human, drives a real click via `System Events` (`osascript -e 'tell application "System
  Events" to click button "Accept All" of window 1 of process "Python"'`) fired from a short
  delayed background thread started just before `runApproval_` is invoked — the same "real
  `osascript`, no human" pattern `test_approval_popup_escaping.py` already established, just now
  clicking a real button on a real window instead of round-tripping a string through a headless
  `-e` expression.
- Requires Accessibility permission granted to the terminal/IDE running it, same as any
  `System Events`-driven automation — this is exactly why it's a local opt-in script and not a CI
  job: granting Accessibility access to a CI runner (which GitHub's hosted macOS runners don't do
  by default and provisioning it would be a real, ongoing maintenance burden) buys detection of a
  failure mode narrow enough — "the modal loop itself is wired to the wrong window" — that it
  doesn't justify that cost. A developer changing `approval_window.py`'s modal-loop plumbing
  (rare — Part B already covers everything about window *contents*) runs this once, locally,
  the same way the fixture recorder's `--check` mode is run before a `*_client.py` PR.
- Prints a pass/fail table, one row per scenario, to stdout — same "paste into the PR description"
  convention `external-api-contract-testing.md`'s local-check report already establishes, reused
  rather than inventing a second format.

This part is explicitly optional and decoupled from Parts A/B — nothing in the rollout plan blocks
on it.

## File layout

```
tests/
  unit/
    test_gate.py                    # unchanged — FakeEvaluator state-machine tests stay as-is
    test_gate_real_evaluator.py      # done — Part A, real AutoAcceptEvaluator + real gated_call
    test_approval_window.py          # done — Part B, build_panel() structure assertions
    test_approval_popup.py          # unchanged
    test_approval_popup_escaping.py # unchanged
    test_audit_log.py               # unchanged
    test_auto_accept.py             # unchanged
src/privacyfence/
  approval_window.py                 # done — build_panel() split out of runApproval_
scripts/
  qa_popup_smoke.py                  # new, optional — Part C, run locally only, never in CI
```

## Rollout plan

1. **`approval_window.py`'s build/run split** — done. Mechanical, no behavior change. Confirmed the
   existing (mocked) `test_approval_popup.py`, `test_gate.py`, and the new
   `test_gate_real_evaluator.py` suites all still pass unchanged, proving the split didn't alter
   `show_native_approval`'s external contract.
2. **Part B tests** — done, in `tests/unit/test_approval_window.py`, per the coverage list above;
   each test class docstring names which `connector-qa-testing.md` phase/ground-rule it narrows.
3. **Part A tests** — done, in `tests/unit/test_gate_real_evaluator.py`, one class per scenario in
   the table above, each directly citing the phase/step it replaces judgment for.
   `TestApprovedObjectTypesNeverPopsUp` (in `test_gate.py`) already set the pattern; salesforce's
   `approved_object_types` isn't duplicated here for that reason — everything else in the table is.
4. **`docs/connector-qa-testing.md` cross-references** — not yet done. Once picked up: a short note
   at each covered phase's start (not a deletion of the phase — see below) pointing at the new
   automated test, so a human running the manual prompt knows which parts of what they're about to
   click through already have a deterministic guardrail behind them.
5. **Part C script** — not implemented; still optional and decoupled per the design principles
   above (Accessibility-permission requirement, narrow additional coverage). Pick up whenever
   convenient; nothing here blocks on it.

No step here removes a phase from `connector-qa-testing.md` — see below for why.

## Relationship to `connector-qa-testing.md`

This design **narrows**, but does not remove, why `connector-qa-testing.md` remains necessary.
After Parts A and B ship:

- **What moves to CI, deterministic, every PR**: whether a given auto-accept rule fires or doesn't
  for a given context (Part A), and whether the popup that *would* show carries the right buttons,
  tint, and content for a given gate configuration (Part B). These are exactly the two things a
  human running `connector-qa-testing.md` is currently trusted to notice by eye and cross-check
  against the audit log by hand.
- **What stays manual, and why it has to**: `connector-qa-testing.md` drives *real* connector calls
  against *real* accounts — Gmail, Slack, Jira, etc. — which is the only way to catch the class of
  bug `external-api-contract-testing.md`'s own Problem section documents (a provider's API moving
  out from under the client code) **at the same time** as observing the gate/popup/audit behavior
  around that real call, in the one combined pass that's actually representative of what a Cowork
  session experiences end to end. Parts A and B each test one layer in isolation — real evaluator
  and scripted popup answer (Part A), or a constructed window with no real connector call behind it
  (Part B) — deliberately, the same way `external-api-contract-testing.md`'s Part B unit tests
  exercise real fixture data through the real parser without a live call. Nothing here reproduces
  the single, combined, live-account pass `connector-qa-testing.md` is for.
- **What Part C would add if a team decided to invest in it further**: a real modal loop resolved
  by a real click closes the very last gap (does the window actually *appear*, become key, and
  respond to a click the way `runApproval_`'s activation-policy comment describes) — but per the
  design principles above, that's judged not to be worth CI-grade investment given how rarely
  `approval_window.py`'s modal-loop plumbing itself changes, versus its *contents*, which Part B
  already covers on every PR.

Net effect: `connector-qa-testing.md` keeps its place as "the thing that runs before a release and
catches what nothing else can" — but what "nothing else can" catch, after this design ships, should
be limited to genuinely live-account, cross-layer behavior, not a gate rule or a popup button that
a deterministic test could have already caught on the PR that introduced the regression.

## Evaluation

### Test coverage

**What this buys, concretely:**

- Every auto-accept rule `connector-qa-testing.md` currently checks by hand, across all ten
  connectors, gets a real-evaluator regression test — a rule that silently stops matching (or
  starts matching too broadly) fails a PR's test run instead of surfacing only when someone next
  runs the manual prompt.
- The PII-gate override — the specific, easy-to-regress interaction between a matching auto-accept
  rule and content-based PII detection — gets locked in as a real-evaluator test rather than only
  ever being demonstrated by a human clicking through Phase 2 steps 21–23.
- `approval_window.py` goes from zero test coverage to direct coverage of every button/tint/content
  combination it can render, closing the one file in the gate/popup/audit trio that today has
  *no* automated test at all, not even a partial one.
- Costs nothing in CI reliability for the existing suite: Part A adds new test classes without
  touching `test_gate.py`'s existing `FakeEvaluator` tests, and Part B's `skipif darwin` guard and
  no-modal-loop design mean it runs exactly as deterministically as
  `test_approval_popup_escaping.py` already does on `macos-latest`.

**What this deliberately does not cover, matching the honesty of
`external-api-contract-testing.md`'s own "what this doesn't cover" section:**

- **No live-account behavior.** Every scenario here uses synthetic `args`/`raw_data` shaped like a
  real connector call, never an actual Gmail/Jira/Slack/etc. round trip — that's
  `external-api-contract-testing.md`'s job, and `connector-qa-testing.md`'s job for the combined
  pass. A provider-side behavior change that happens to also change what gate context looks like
  (a new field connectors start passing into `ReviewContext`) isn't caught by Part A unless someone
  updates the synthetic `args` to match — same caveat `external-api-contract-testing.md`'s fixture
  recorder already carries for its own recorded shapes.
- **No pixel-level or actually-visible-on-screen verification.** Part B proves the right `NSView`s
  exist with the right properties; it doesn't prove they render correctly at a given screen
  resolution, under a given macOS appearance mode, or that nothing overlaps. A human glancing at
  the popup during a release QA pass is still the only check for genuine visual regressions.
- **No proof the modal loop itself still blocks and resolves correctly on a real click** unless
  Part C is adopted and run — and even then, only whenever a developer remembers to run it, the
  same "however often a human runs it" caveat `external-api-contract-testing.md` names for its own
  optional staleness reminder.
- **Coverage is bounded by the scenarios enumerated in Part A's table.** A rule shape
  `connector-qa-testing.md` doesn't currently exercise (because no fixture for it exists yet) has
  no automated equivalent here either — this design ports what's already being checked by hand, it
  doesn't invent new gate behavior to test.

**Net assessment**: closes the specific, named gap — automated coverage of the gate's real
rule-evaluation behavior and the popup's actual constructed content — using the same
mock/real-boundary discipline the existing suite already applies elsewhere, without asking
`connector-qa-testing.md` to shrink past the one thing only a live, combined, human-observed pass
can actually verify.

### Cybersecurity

No new external surface: Part A tests run entirely in-process against synthetic data (no network,
no credentials — the same profile as every other `tests/unit/` module). Part B tests touch real
AppKit classes but only to construct object graphs and read their properties back — no window is
ever made key, activated, or shown on screen, so there is no new automation surface an attacker
could target through the test suite itself. Part C (optional, local-only) requires Accessibility
permission on the machine that runs it, exactly the same trust boundary a developer already grants
their own terminal/IDE for any local automation — no new credential or account access of any kind,
and it never runs in CI, so no CI secret or permission grant is introduced by it either.
