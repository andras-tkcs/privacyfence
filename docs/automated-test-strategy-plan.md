# Automated Test Strategy — Implementation Plan

Phased plan to get PrivacyFence to a state where it can be released with confidence — across
macOS/Windows/Linux local mode, Linux org mode, the browser-based approval UI, MCP clients, and
eleven (soon: still eleven, once Apps Script has a fixture — see Phase 1) live third-party
connectors — without the maintainer manually reproducing that whole matrix by hand on every
release. This document is deliberately an *implementation* plan, not a restatement of the
strategy: every phase below is checked against what this repo already has today (a script, a test
module, a CI job) before describing new work, so the plan says only what's actually left to build.

## Relationship to existing planning docs

This repo already has three documents that overlap with pieces of this plan. This plan **extends
and sequences** them; it does not replace or duplicate their content:

- [`testing-policy.md`](testing-policy.md) — the current three-tier description of what runs in CI
  versus by hand. Phase 0 below rewrites its framing into the finer-grained taxonomy this plan
  needs; every later phase updates its "Quick reference" table as new automated tiers come online.
- [`security-remediation-plan.md`](security-remediation-plan.md) — Phase 3.12 of that plan
  (`tests/tst-08-through-13-remaining-test-depth`, TST-08–TST-13) is the *only* remaining open item
  in the whole remediation plan as of this writing. Closing it is this plan's Phase 1, and doing so
  also closes out `security-remediation-plan.md` entirely (see [Phase
  1.10](#110-close-the-security-remediation-plan)).
- [`connector-ci-integration-plan.md`](connector-ci-integration-plan.md) — already fully designs
  the self-hosted-runner live-connector-CI infrastructure (its Phases A–E) *and* already designs
  TST-08 through TST-13 in detail (its Phase E). Phase 1 below does not re-derive that design — it
  tracks completion status against it and calls out exactly what Phase A–E work remains unbuilt in
  this repository today.

**Assumption carried over from `connector-ci-integration-plan.md`:** Phase A (dedicated QA
accounts) and Phase B (the self-hosted, credential-holding runner) are treated as already complete
— acquiring accounts and provisioning a VM are infrastructure/credential actions with no trace in
this git history, so this plan can't verify them from the repository alone. **What the repository
*does* show, and what this plan verifies below:** neither `connector-ci-integration-plan.md`'s
Phase C workflow file (`.github/workflows/connector-live-check.yml`) nor any of its Phase E test
modules (TST-09 through TST-13) exist yet. If Phase A/B turn out not to be done, Phase 1.1 below
(standing up the workflow) blocks until they are — flag that back rather than silently building a
workflow with nowhere to run.

## Core testing principle

Restated from the source strategy because every phase below depends on it: **do not test the full
Cartesian product** (`OS × connector × operation × gate × browser × package type`). Test each
dimension independently, and cross a boundary only for a small number of genuinely high-value
end-to-end tests:

| Dimension | Proven by |
|---|---|
| Provider correctness | Live connector CI (Phase 1) |
| Gate/policy correctness | Synthetic deterministic integration tests (Phase 1, Phase 5) |
| OS/runtime portability | Cross-platform system CI (Phase 2, Phase 3) |
| Browser behavior | Playwright (Phase 4) |
| Installer/package correctness | Native artifact CI (Phase 6, Phase 7) |
| Org-mode deployment shape | Synthetic OIDC system CI (Phase 8) |
| Real UX / subjective compatibility | Small manual checks (Phase 9) |

## How to read the phase tables below

Each phase has an **Already in this repo** block before its **Remaining work** block. Where a
proposed deliverable already exists, the remaining work is "extend" or "wire into CI," not "write
from scratch" — several phases in the original strategy turn out to be substantially, or entirely,
already implemented once checked against the current tree.

---

## Phase 0 — Establish the test taxonomy

### Objective

Make the repository describe tests by what they prove, not by the historical "CI vs manual" split,
so later phases don't duplicate checks `testing-policy.md`/`manual-pre-release-test-plan.md` still
describe as needing a human.

### Already in this repo

- `testing-policy.md` already has a three-tier structure (§1 automated/§2 local-manual/§3 full
  manual QA) and a "Quick reference" table — the right shape, but it doesn't yet name the seven
  layers this plan needs (cross-platform system, browser system, and packaged-artifact aren't
  distinguished from each other or from "integration" today), and there are no pytest markers at
  all (`pyproject.toml`'s `[tool.pytest.ini_options]` has no `markers` list).
- `manual-pre-release-test-plan.md` and `connector-qa-testing.md` already exist as the two manual
  documents this plan's Phase 9 eventually rewrites.

### Remaining work

1. Rewrite `testing-policy.md` to define the seven layers:

   | # | Layer | What it proves | Runs |
   |---|---|---|---|
   | 1 | Unit | Python logic in isolation | `tests/unit/`, every PR |
   | 2 | Integration | Real internal stack, no external network | `tests/integration/`, every PR |
   | 3 | Cross-platform system | OS path/process/locking/daemon behavior | Phase 3, Linux+Windows+macOS |
   | 4 | Browser system | JS/CSP/rendering in a real browser | Phase 4, Chromium |
   | 5 | Live connector | Provider API drift | Phase 1, self-hosted runner, scheduled |
   | 6 | Packaged-artifact | Installer/package correctness | Phase 6, release workflows |
   | 7 | Manual exploratory/UX | Subjective judgment, first-time auth flows | Phase 9, human |

2. Add pytest markers in `pyproject.toml`:

   ```toml
   [tool.pytest.ini_options]
   markers = [
       "unit: fast, fully offline",
       "integration: real internal stack, no external network",
       "system: full daemon/MCP/approval/audit scenario",
       "browser: real Chromium via Playwright",
       "packaged: runs against a built installer/artifact",
       "live: touches a real third-party provider (self-hosted runner only)",
   ]
   ```

   Apply markers to *new* test modules as later phases add them (Phase 1's TST-08–13 modules,
   Phase 3's system test, Phase 6's packaged-artifact tests). Do not retroactively mark every
   existing test in this phase — that's churn with no payoff until something actually needs to
   select on the marker (e.g. `pytest -m "not live"`).

3. Add the test-ownership table (failure type → layer) to `testing-policy.md`, and state the
   governing rule explicitly: *a test stays manual only when automated observation cannot reliably
   determine pass/fail* — visual judgment and first-time third-party consent screens are the two
   recurring cases that meet that bar in this project; nothing else should.

4. Cross-check `manual-pre-release-test-plan.md` against the table above — every checklist item
   there should map to exactly one layer. Where an item doesn't map to any layer, that's this plan's
   signal that the layer needs a phase (it does — see Phases 1–8 below); don't remove the manual
   item until the corresponding phase actually lands automation for it.

### Exit criteria

- `testing-policy.md` describes the seven-layer target architecture and the test-ownership table.
- `pyproject.toml` has the marker list registered (even if lightly used so far).
- No existing test behavior changes.

---

## Phase 1 — Complete live connector CI and close Security Remediation Plan 3.12

### Objective

Finish `connector-ci-integration-plan.md`'s Phase C (the scheduled live-connector workflow) and
Phase E (TST-08 through TST-13), then close out `security-remediation-plan.md` entirely — it has no
other open item.

### Already in this repo

- `scripts/qa_fixture_recorder.py`, its own unit tests (`tests/unit/test_qa_fixture_recorder.py`),
  and the full local-manual workflow around it (`testing-policy.md` §2.1,
  `manual-pre-release-test-plan.md` §1) — this is the thing Phase 1.1 below schedules instead of
  requiring a human to remember to run it.
- `tests/fixtures/live/<connector>/` for ten of eleven connectors: `calendar`, `confluence`,
  `contacts`, `drive`, `gmail`, `jira`, `salesforce`, `slack`, `tasks`, `telegram`. **`apps_script`
  has no fixture directory yet**, even though `src/privacyfence/connectors/apps_script.py` exists
  and ships — this is exactly the TST-08 gap `connector-ci-integration-plan.md` §E.1 already
  identified.
- `tests/unit/web/test_routes_security.py` has the `_app(*, step_up=None, sessions=None)` scaffold
  TST-10 needs, but no test yet proves the cross-principal binding property itself.
- `pyproject.toml` has no `hypothesis` dependency yet (TST-12 not started).
- No `tests/integration/test_deferred_approval_round_trip.py`, no
  `tests/unit/test_fixture_coverage.py`, no `tests/unit/test_parser_roundtrip_properties.py` —
  TST-09, TST-08's CI guard, and TST-12 are all unbuilt.
- No `.github/workflows/connector-live-check.yml` — Phase C of the connector CI plan is unbuilt.
- `time.sleep(...)` calls remain at every site `connector-ci-integration-plan.md` §E.4 lists
  (`tests/unit/test_approvals.py` ×6, `test_audit_forwarding.py` ×3, `test_webauthn_stepup.py` ×1,
  plus one reload-polling sleep each in `test_settings_controller.py`, `test_daemon_main.py`,
  `test_web_prompt.py`, `tests/unit/web/test_routes_settings.py`, and one in
  `tests/unit/web/test_routes_approvals.py`) — TST-11 not started.

### 1.1 Stand up the live connector workflow

Implement `connector-ci-integration-plan.md` §C's `.github/workflows/connector-live-check.yml`
verbatim (that document already has the full YAML, the concurrency/permissions rationale, and the
drift-PR mechanics worked out) — `schedule` + `workflow_dispatch` only, targeting the
`privacyfence-qa-live` self-hosted runner label, never `pull_request`/`pull_request_target`, never a
GitHub-hosted runner. If Phase A/B credentials and runner registration aren't actually in place yet
(see the assumption note above), this step blocks on that, not on anything in this repository.

### 1.2 TST-08 — complete live fixture coverage

1. Add `apps_script` to `scripts/qa_fixture_recorder.py`'s connector registry if not already wired
   (check its `CONNECTOR_CHECKS`-equivalent mapping first — `connector-ci-integration-plan.md` §E.1
   already flags this as the one missing entry).
2. Record the first Apps Script fixture once a QA Apps Script project exists under the QA Workspace
   from Phase A.
3. Add `tests/unit/test_fixture_coverage.py`: enumerate connector modules under
   `src/privacyfence/connectors/` (mirroring how `connector_host.py` already builds its
   `{name: Connector}` map, so the enumeration can't silently miss a registered connector), and for
   each assert `tests/fixtures/live/<connector>/` exists, contains at least one `.json` file, and
   that file is non-empty. Add this as a step in `tests.yml`'s existing `test` job.
4. Update `manual-pre-release-test-plan.md` §1's "omit connector names to run all ten" line to
   eleven once Apps Script is wired in.

### 1.3 TST-09 — deferred approval round-trip

New `tests/integration/test_deferred_approval_round_trip.py`, same posture as the existing
`tests/integration/test_mcp_daemon_contract.py` (real loopback socket, official `mcp` Python
client, no external network, no Node needed): a synthetic gated tool call creates a pending
approval, the original MCP call stays unresolved, a later `/approvals` HTTP decision resolves it,
and the original call completes — Allow returns the expected result, Deny returns the expected MCP
error, and the audit record carries the final decision. Mark `@pytest.mark.integration`.

### 1.4 TST-10 — cross-principal step-up binding

Extend `tests/unit/web/test_routes_security.py` using its existing `_app(...)` scaffold. Add cases
proving: principal A's step-up credential satisfies A's own requirement; A's credential cannot
satisfy a requirement raised for principal B; B satisfies B's requirement with B's own credential;
a stale or mismatched binding is rejected. This is the specific binding property, not general
"step-up works" — the existing scaffold already proves the latter.

### 1.5 TST-11 — deterministic synchronization

Replace each `time.sleep(...)` call site listed above with a `threading.Event`/`asyncio.Event` the
code under test can signal, `.wait(timeout=...)` on the test side, plus a `pytest.mark.timeout(N)`
where the global `pyproject.toml` timeout isn't already sufficient. Leave the process-startup-settle
sleeps in `test_browser_smoke.py`, `test_shim_mcp_contract.py`, and `test_mcp_daemon_contract.py`
alone — those wait on a subprocess binding a socket, not a signal this plan is scoped to redesign.

Once done, revisit `scripts/check_coverage_floor.py`'s recorded floor for any module whose coverage
fluctuated because of the timing races these sleeps papered over, and tighten it to the now-stable
value.

### 1.6 TST-12 — parser property tests

Add `hypothesis>=6.100` to `pyproject.toml`'s `test` extra. New
`tests/unit/test_parser_roundtrip_properties.py`, focused on the `html_to_text` →
`markdown_to_html` round trip (and the sibling `email_markdown.py`/`markdown_to_html.py` pair),
using a constrained strategy (a realistic tag/character subset, not unconstrained `st.text()`).
Properties to cover: no transformation ever introduces a URL scheme outside the shared allowlist
from SEC-01 (`fix/sec-01-approval-window-url-scheme-allowlist`); sanitization survives a round
trip; accepted structures retain their textual meaning; malformed-but-supported input never raises
instead of degrading gracefully.

### 1.7 TST-13 — systemic parameterized invariants

Three parameterized tests, each iterating every relevant tool/site rather than hand-listing them:

1. **`reason` on every gated tool** — introspect each connector's `ToolSpec`/`ToolParam`
   registrations (the same shape `test_mcp_tools.py` already exercises) and assert every `review`/
   `popup`-gated operation declares a `reason` parameter.
2. **`pii_scan_text` on every review-gated tool** — reuse the call-capturing fixture pattern
   `coding-and-testing-guidelines.md` §2.5 documents for gate-argument assertions; assert every
   `review`-gated call passes `pii_scan_text`.
3. **Token writers use the secure-write helpers** — AST-based check (prefer over grep, per the
   source strategy) over every module writing a credential/token file, asserting each goes through
   `atomic_write_text`/`atomic_write_json` (the SEC-09 helper) rather than a raw `open(...).write()`.

### 1.8 Extend live connector CI beyond fixture-shape checks

Once 1.2's minimum is met, add bounded lifecycle tests for write-capable providers to
`scripts/qa_fixture_recorder.py` (or a sibling script it calls into): create a uniquely-tagged QA
object, read it back, update where the provider supports it, delete/archive it, verify cleanup.
Runs only on the self-hosted runner (Phase A/B), on the same schedule as 1.1.

### 1.9 Fixture freshness and reporting

Extend the report `qa_fixture_recorder.py` already prints (per `testing-policy.md` §2.1) with an
age column: `< 60 days` healthy, `60–90 days` warning, `> 90 days` refresh required — this is a
small addition to a script that already produces "a small, deterministic Markdown report," not new
infrastructure.

### 1.10 Close the Security Remediation Plan

Once 1.2–1.7 are green:

1. Update `security-remediation-plan.md`'s Phase 3.12 row to mark TST-08 through TST-13 complete
   individually, each referencing its actual test file (`test_fixture_coverage.py`,
   `test_deferred_approval_round_trip.py`, the extended `test_routes_security.py`, the diffed
   `time.sleep` sites, `test_parser_roundtrip_properties.py`, the three TST-13 invariant tests).
2. Remove any wording stating 3.12 remains outstanding.
3. Walk the rest of the document (Phase 0 through Phase 3) confirming no other row is still open —
   as of this writing 3.12 is the only unchecked item, so this should be a read-through, not a
   rediscovery.
4. If 3.12 is confirmed the last open item, mark the whole Security & Quality Remediation Plan
   complete at the top of the document, and state explicitly that it's kept as a historical record
   from here on, not a live tracker.

### 1.11 Update testing documentation

Fold `connector-ci-integration-plan.md` Phase D's `testing-policy.md` changes in alongside this
plan's own Phase 0 rewrite (same PR, to avoid two conflicting rewrites of the same section):
describe the two CI trust tiers explicitly — credential-free PR CI (GitHub-hosted, includes TST-08's
guard and TST-09–13) versus credential-bearing connector CI (self-hosted runner only, real provider
checks, fixture recording, drift detection, bounded lifecycle tests) — and state plainly that
GitHub-hosted runners and every `pull_request`-triggered workflow remain credential-free, full stop.

### Exit criteria

- `connector-live-check.yml` runs successfully on the self-hosted runner; no connector credential is
  ever a GitHub Actions secret; it cannot execute from an untrusted PR.
- Every connector, Apps Script included, has at least one recorded live fixture, and deleting a
  connector's last fixture fails ordinary PR CI (`test_fixture_coverage.py`).
- TST-09 through TST-13 pass, and the TST-11 coverage-floor cleanup is done.
- `testing-policy.md` describes both CI trust tiers.
- `security-remediation-plan.md` marks 3.12, and the whole plan, complete.

---

## Phase 2 — Cross-platform core CI

### Objective

Prove the runtime works on Ubuntu, Windows, and macOS without tripling the full suite.

### Already in this repo

- `.github/workflows/tests.yml`'s `test` job already runs the comprehensive Ubuntu suite (pytest +
  coverage + coverage floor, `npm test`, `npm run typecheck`) plus `static-analysis` (`ruff`,
  informational `mypy`/`bandit`) on every PR — this is already the "Ubuntu stays comprehensive"
  half of the key decision.
- `test-windows` already exists in `tests.yml`, running the full pytest suite with coverage on
  `windows-latest` — but gated `if: github.event_name == 'workflow_dispatch'`, i.e. it is **not**
  yet a permanent per-PR check. Its own comment already frames promoting it to permanent as a
  pending decision.
- A `test-python-compat` job (3.11/3.12 matrix, reduced suite, `ubuntu-latest` only) already exists
  — Python-version compatibility is already Linux-only, matching §2.4's target.
- No `platform-macos` job exists. `build.yml` runs on `macos-latest`, but that job builds and signs
  the DMG (tag-triggered release build) — it is not source/runtime portability verification on
  every PR.
- No `tests/platform/` directory or `platform` pytest marker exists yet.

### 2.1 Promote Windows CI

Change `test-windows`'s trigger from `workflow_dispatch`-only to running on every PR (or narrow it
to a `platform`-marked subset per 2.3 below, rather than the full suite, once that subset exists —
whichever lands first). Rename the job `platform-windows` for clarity, keeping the existing
Windows-specific comment trail intact.

### 2.2 Add macOS platform CI

New `platform-macos` job in `tests.yml`, `runs-on: macos-latest`, the primary supported CI Python
version, running the same targeted subset as `platform-windows` (2.3) — explicitly **not**
`scripts/build_dmg.sh`/signing/notarization, which stay in `build.yml`'s release path.

### 2.3 Create the targeted platform suite

`tests/platform/` (preferred over a bare `-m platform` filter scattered across existing files, so
new platform tests have an obvious home): state/config path resolution (`paths.py`), secure
directory creation (SEC-09's `secure_mkdir`), file handling, single-instance locking, process
spawning, the browser-launch abstraction, daemon startup, environment discovery, shim daemon
discovery (`mcp_url` file), path-separator handling, and process cleanup on shutdown. Register the
`platform` pytest marker from Phase 0.

### 2.4 Avoid matrix explosion

Target shape once 2.1–2.3 land:

| Runner | Suite |
|---|---|
| `ubuntu-latest` (`test`) | Full pytest, coverage, `npm test`, typecheck, Chromium, static analysis |
| `ubuntu-latest` (`test-python-compat`) | 3.11/3.12 reduced core suite |
| `windows-latest` (`platform-windows`) | `tests/platform/` + core sanity subset |
| `macos-latest` (`platform-macos`) | `tests/platform/` + core sanity subset |

### Exit criteria

- Runtime-relevant PRs run meaningful tests on all three OS families.
- The Windows/macOS jobs stay materially smaller than the Ubuntu `test` job.
- A platform-specific regression fails before release, not after.

---

## Phase 3 — Canonical cross-platform system test

### Objective

One scenario — real daemon → MCP → approval → audit — proven identical on Linux, Windows, and
macOS.

### Already in this repo

- `tests/integration/test_mcp_daemon_contract.py` already drives a real, socket-bound `WebServer`
  with the official `mcp` Python client (initialize → `tools/list` → gated tool call → resolve via
  HTTP → result), and already runs wherever the full suite runs (including `platform-windows` per
  Phase 2, once promoted) — but it is one module inside the general suite, not a dedicated,
  platform-assertion-bearing system scenario, and it currently has no macOS leg at all (no
  `platform-macos` job yet).
- `tests/integration/test_org_ubuntu_release_smoke.py` and
  `tests/integration/test_macos_packaged_smoke.py` already prove very similar daemon→MCP→
  approval→audit scenarios, but scoped to org-mode-on-Ubuntu and packaged-macOS-DMG respectively —
  neither is the *local-mode-on-all-three-desktop-OSes* scenario this phase asks for.
- No `tests/system/` directory exists yet.

### Implementation

New `tests/system/test_local_mode_system.py`, `@pytest.mark.system`, run on `ubuntu-latest`,
`windows-latest`, and `macos-latest`. Reuse `test_mcp_daemon_contract.py`'s daemon-startup and MCP
client patterns rather than reinventing them; this module's job is to add the platform-specific
assertions and the full audit-log inspection those two nearby files don't need for their own
narrower purposes:

1. Isolated temporary PrivacyFence home/config (already a pattern in `tests/conftest.py`).
2. Start the real daemon process; wait for readiness (an `Event`/health-check poll per Phase 1.5's
   deterministic-sync preference, not a fixed sleep).
3. Verify discovery/state files exist (`mcp_url`, config dir).
4. `GET /settings`, `GET /approvals`.
5. Connect with the official MCP client; `tools/list`; call a synthetic gated tool.
6. Confirm an approval is created; resolve Allow via HTTP; confirm the MCP result returns.
7. Repeat with a second synthetic call resolved Deny; confirm the MCP error.
8. Inspect the audit log; confirm both the allowed and denied decisions are present.
9. Shut the daemon down; verify clean process exit and expected state paths.

Platform-specific assertions layered on top of the shared scenario:

- **Windows**: path handling, single-instance lock, process spawning, expected state location.
- **Linux**: XDG/home paths, permissions where applicable, process cleanup.
- **macOS**: expected state paths, process discovery, source-tree assumptions that don't require
  signing (this module runs from source, not the packaged `.app` — that's Phase 6's job).

Use fake/synthetic connectors behind the real gate, same as `test_mcp_daemon_contract.py` already
does — no live provider needed.

### Exit criteria

The same daemon→MCP→approval→audit contract passes on `ubuntu-latest`, `windows-latest`, and
`macos-latest`.

---

## Phase 4 — Complete browser/UI automation

### Objective

Move every objectively testable browser behavior out of manual QA.

### Already in this repo

`tests/integration/test_browser_smoke.py` (TST-06) already covers substantially more than a bare
minimum: `TestBootstrapLogin`, `TestApprovalDecisionFlow`, `TestPdfPreview`,
`TestSecurityHeadersCsp` (a real no-inline-script CSP check, not a skip), and
`TestOrgModeWebAuthnUi`. What's **not** yet covered, checked against the classes above:

- Empty approval list, "Always allow," return-to-list toast behavior beyond one Allow/Deny round
  trip, live SSE refresh under multiple pending cards, and stale/idempotent decision handling
  (double-submitting a decision).
- PII-specific banner/highlight/confirmation assertions against deterministic synthetic PII (the
  existing classes exercise the approval flow generally, not the PII gate path specifically).
- Responsive-layout assertions at named viewports (375×812, 768×1024, desktop).
- `prefers-color-scheme: light`/`dark` structural assertions.
- Systematic failure-artifact capture (screenshot/console/DOM/daemon log) — check
  `tests.yml`'s Playwright step and `test_browser_smoke.py`'s own fixtures for what already uploads
  on failure before adding more; extend rather than duplicate.

### Remaining work

Extend `test_browser_smoke.py` (new test classes, not a new file — it's already the established
home for this layer) with:

1. **Approval behavior** (`TestApprovalListBehavior` or similar): empty list, "Always allow" →
   proposed-rule side effect, return-to-list + toast, live SSE refresh, multiple pending cards,
   double-decision idempotency.
2. **PII behavior** (`TestPiiApprovalUi`): deterministic synthetic PII triggers the banner/tint,
   Proceed and Cancel both work, an unrelated operation is never highlighted.
3. **Responsive layout** (`TestResponsiveLayout`): the three viewports above; assert no horizontal
   overflow, wide cards stack, primary actions stay visible, dialogs fit the viewport.
4. **Light/dark mode** (`TestColorScheme`): both `prefers-color-scheme` values on the approval list
   and a pending card; structural assertions only (element presence/class), not pixel comparison —
   the source strategy is explicit that subjective visual quality stays manual.
5. Confirm failure-artifact capture (screenshot, browser console, relevant DOM, daemon log) already
   happens on any new failing test in this file the same way it does for the existing ones; add
   whichever of those isn't already wired into the shared fixture.

### Exit criteria

Manual browser QA is reduced to subjective visual inspection — every objectively-checkable behavior
above has a passing automated assertion.

---

## Phase 5 — Exhaustive gate/policy system tests

### Objective

Retire the giant live-connector QA flow as the normal way to prove gate behavior.

### Already in this repo

`tests/unit/test_gate.py` is already a 2,600+-line parameterized suite covering, by class name
alone: auto-accept, review-gate decisions, delivery-audit fields, "Accept all" (single and multiple
choices, writes), propose-rule-change, popup-gate writes, the PII upload gate, request
fingerprinting, write-content flags, temp-accept, the PII review gate broadly (categories/match
details in the audit log, "already reviewed," `pii_scan_text` itself), concurrent approvals,
coalescing, the deferred-approval protocol, "approved object types never pop up," request IDs,
audit-gap safety, unattended mode, Claude's stated reason, default details, and cancellation. This
is already most of the matrix the source strategy asks for (`auto→allowed`, `review→Allow/Deny`,
`review+PII→Proceed/Cancel`, `popup/write→Allow/Deny`, "Always allow"→proposed rule,
matching/non-matching rule, unattended allowed/forbidden) — this phase is **audit and close gaps**,
not build from scratch.

### Remaining work

1. Cross-check the existing `test_gate.py` classes against the full matrix in the source strategy
   (resource-grant match/mismatch, "policy denial before connector execution" specifically —
   confirm the connector mock is asserted *not called* in the denial path, not just that the result
   is an error). Add any genuinely missing case as a new parameterized case in the matching
   existing class, not a new file — this suite's own organization is already the right shape.
2. Confirm every case asserts the full tuple the strategy calls for: gate selected, connector
   called/not called, result/error, approval state, audit decision, rule/grant side effect. Where an
   existing case only checks a subset, extend it rather than adding a near-duplicate test.
3. Once this audit is done, update `testing-policy.md` and `connector-qa-testing.md`'s own framing
   (this document's Phase 9 covers the actual rewrite) to state that `connector-qa-testing.md` is no
   longer required as routine release proof for gate-state coverage specifically — it remains
   required for connector-specific tool-to-gate-metadata mapping, which this phase deliberately does
   not duplicate per connector.

### Exit criteria

Every gate-state transition in the matrix has confirmed, deterministic automated coverage, with no
gap found in step 1 above left unaddressed.

---

## Phase 6 — Packaged-artifact lifecycle tests

### Objective

Prove what users actually download.

### Already in this repo

- **macOS**: `tests/integration/test_macos_packaged_smoke.py` already mounts the built DMG,
  extracts the app, launches the frozen daemon with an isolated `$HOME`, connects via the real
  built `mcpb/shim/dist/shim.js`, drives a headless-Chromium approval round trip, and (per its own
  docstring) covers most of what §6.1 below asks for. Confirm it also asserts signature/notarization
  validation and state-outside-package — extend narrowly if either is missing rather than assuming.
- **Linux**: `docs/linux-local-deb-packaging-plan.md` Phase 7 already has a manually-verified
  install/autostart-file/remove/purge lifecycle (P7.1, checked off) and a partially-checked upgrade
  test (P7.3) — but it ran once, by hand, on the environment available while implementing that plan,
  not as a repeatable CI job. P7.2 (graphical-session autostart) is explicitly still open there and
  belongs to this plan's Phase 7, not here.
- **Windows**: no packaged-installer smoke harness found in this repo or its plans.

### 6.1 macOS — close remaining gaps only

Read `test_macos_packaged_smoke.py` in full before writing anything new; add only what its own
docstring says is out of scope (e.g. explicit signature/notarization assertions if not already
present, package cleanup/removal if not already exercised). Gatekeeper UX itself stays manual per
the source strategy.

### 6.2 Windows — new packaged installer smoke

Build the installer, perform a silent install, verify: installed executable exists at the expected
path, autostart/startup registration exists, the daemon starts, `/settings` responds, MCP discovery
works, an Allow/Deny round trip passes, audit is written. Silently uninstall; verify package-owned
files are removed and user state (`%APPDATA%`-equivalent config/tokens) is preserved. Add an
upgrade test (install N, create state, install N+1, verify state survives) once the base smoke is
green — don't build both in one PR.

### 6.3 Linux `.deb` — automate the already-manually-proven lifecycle

Turn `linux-local-deb-packaging-plan.md` P7.1's manual container run into a CI job:
`dpkg -i` → `desktop-file-validate` → start daemon → the shared system smoke scenario from Phase 3
→ `dpkg -r` (verify user state remains) → `dpkg -P` (verify expected purge behavior). Add the
upgrade test P7.3 already partially checked, made repeatable: install N, create state, install N+1,
verify state survives. Mark P7.1 and P7.3 as CI-automated (not just manually-verified-once) in
`linux-local-deb-packaging-plan.md` once this lands.

### 6.4 Release gating

Wire all three into the release build workflows (`build.yml`, `publish-pypi.yml`) ahead of the
publish step — build → package smoke → signature/notarization validation → publish. A failed
packaged-artifact test blocks publication; this is a change to those workflows' job dependencies,
not new test logic beyond 6.1–6.3.

### Exit criteria

Every published DMG/EXE/DEB has been started and exercised on its native OS, automatically, before
release.

---

## Phase 7 — Graphical-session/autostart verification

### Objective

Automate whether PrivacyFence actually starts after a normal desktop login — deliberately last,
since GUI-session infrastructure is the most expensive, flakiest tier here.

### Already in this repo

`linux-local-deb-packaging-plan.md` already tracks this exact gap as its own open item, **P7.2**:
"Real desktop-session test... install on a real or VM Ubuntu/Debian desktop, log out/in, confirm the
daemon is running post-login... confirm the OAuth loopback browser flow opens correctly." Nothing
in this repo implements it yet. No equivalent Windows item exists in `windows-support-plan.md`/
`windows-linux-support-plan.md` as of this writing.

### Remaining work

1. **Linux**: a graphical Ubuntu VM scenario implementing `linux-local-deb-packaging-plan.md` P7.2
   exactly as already specified there — install, ensure stopped, logout/reboot, login, wait for
   session startup, verify daemon, exercise the system request from Phase 3, and where practical the
   OAuth loopback browser-opening flow. Close P7.2 in that document once this lands.
2. **Windows**: equivalent scenario on a Windows desktop VM/session — install, sign out/reboot, sign
   in, verify autostart, exercise the Phase 3 system contract. Add this as a new tracked item in
   `windows-support-plan.md` if that document doesn't already have a home for it.
3. **macOS**: do not build dedicated login-launch infrastructure unless the existing packaged smoke
   (Phase 6.1) proves insufficient in practice — the source strategy is explicit on this, and nothing
   found while grounding this plan suggests macOS autostart is currently a live gap.

Schedule these on packaging-related `main` changes, nightly/periodic runs, and release candidates —
not on every PR; this tier's infrastructure cost doesn't justify per-PR cadence.

### Exit criteria

Normal local-mode login/autostart is verified without owning physical Windows/Linux hardware.

---

## Phase 8 — Org-mode system CI

### Objective

Treat org mode as its own deployment shape, provisioned and exercised from scratch, entirely in
automated Linux infrastructure.

### Already in this repo

`tests/integration/test_org_ubuntu_release_smoke.py` already does almost exactly what this phase
describes, per its own docstring: `daemon_main.main()` end to end, a real synthetic Ed25519-signed
`org_config.json`, a real loopback mocked IdP (`tests/integration/mock_idp.py`), strict fail-closed
startup on a malformed/unsigned/incomplete bundle, reverse-proxy Host-header handling, org-only
route mounting, and per-principal session isolation. This is the single largest instance in this
whole plan of a proposed deliverable already substantially built.

### Remaining work

1. Read the rest of `test_org_ubuntu_release_smoke.py` (it was only partially read while grounding
   this plan) and check it against the full scenario the source strategy lists: unauthenticated
   request rejected, authenticated MCP request with identity/policy applied, an approval exercised
   with audit principal correctness, daemon restart with state survival. Add whichever of those
   isn't already present as a case in this file — extend it, don't fork a second org-mode system
   test module.
2. Confirm this test already runs as a permanent Ubuntu PR job (not dispatch-gated) — if it's
   currently dispatch-only like `test-windows` was before Phase 2.1, promote it the same way.
3. Update `org-mode-operational-readiness.md` to reference this test module as the automated
   evidence for whatever readiness claims it makes, rather than leaving org-mode readiness resting
   on a claim with no cited test.

### Exit criteria

Org mode can be provisioned and exercised from scratch entirely in automated Linux CI, with no real
Google/Microsoft identity login required for routine coverage.

---

## Phase 9 — Rewrite release QA around automation

### Objective

Shrink manual release validation to minutes, now that Phases 0–8 have replaced most of what
`manual-pre-release-test-plan.md` and `connector-qa-testing.md` currently ask a human to do by hand.

### Remaining work

Do this only after Phases 1–8 actually ship — rewriting these documents first would leave them
describing automation that doesn't exist yet.

1. **`manual-pre-release-test-plan.md`**: reduce to an automated-prerequisites checklist (PR CI
   green, cross-platform system CI green, connector live CI recent and green, packaged-artifact
   tests green, no unresolved provider-drift PR) plus a short human-QA section (visual UI sanity if
   UI changed, one real MCP-client compatibility smoke, OS-native UX smoke if packaging/autostart
   changed, review of any provider/fixture drift). Its current §0 (`pre_release_check.py`) and the
   fixture-recording section fold into the automated-prerequisites list once Phase 1 lands the
   scheduled recorder; its live-Cowork sections fold away once Phase 5's gate matrix and Phase 1's
   connector CI cover what they currently prove by hand.
2. **`connector-qa-testing.md`**: reframe from a routine release checklist to "Extended
   Connector/Gate Exploratory QA," used only for a new connector, a major gate/approval
   architecture change, or an unexplained integration regression — its opening "When to use this"
   section already gestures at this; make it explicit that routine releases no longer require it.
3. **`testing-policy.md`**: by this point it should already describe all seven layers, both CI trust
   tiers, and which checks are release-blocking versus manual (Phases 0, 1, 2–8 each touch it
   incrementally) — this step is a final consistency pass, not new content.

### Exit criteria

Routine manual release validation takes minutes, not hours; both documents above accurately
describe a *reduced*, not aspirational, manual surface.

---

## Phase 10 — CI observability and maintenance controls

### Objective

Make test failures diagnosable entirely from cloud CI, since this project is developed
cloud-first without dedicated physical test machines.

### Remaining work

1. On every system/packaged-artifact test failure, capture and upload as CI artifacts: daemon logs,
   audit logs, pytest output, browser console (where applicable, reuse Phase 4's existing capture),
   screenshots where applicable, an installed-file manifest (packaged tests), OS/runtime versions,
   and a test-run identifier. Bounded retention (`retention-days`, matching the existing pattern in
   `connector-ci-integration-plan.md`'s workflow YAML).
2. Every failure message states what failed, expected vs. actual state, and where its diagnostic
   artifacts landed — a small convention to apply across the new test modules from Phases 3, 4, 6,
   7, 8, not a framework to build.
3. Do not auto-retry deterministic tests. For live-provider tests specifically (Phase 1), a narrowly
   scoped retry/backoff for known-transient conditions (rate limiting, transient network failure) is
   acceptable — `scripts/qa_fixture_recorder.py` is the place for that, not a blanket CI-level retry.

### Exit criteria

Most CI failures are diagnosable without local reproduction.

---

## Revised sequencing

```
Phase 0  Taxonomy / doc foundation
   ↓
Phase 1  Live connector CI + Security Remediation 3.12 closure   (largest remaining gap)
   ↓
Phase 2  Cross-platform core CI                                  (promote existing Windows job;
   ↓                                                               add new macOS job)
Phase 3  Canonical cross-platform system test                    (new module, reuses existing
   ↓                                                               daemon/MCP test patterns)
Phase 4  Browser/UI automation                                   (extend existing test_browser_
   ↓                                                               smoke.py, not a new file)
Phase 5  Gate/policy matrix                                      (mostly an audit of the existing
   ↓                                                               2,600-line test_gate.py)
Phase 6  Packaged-artifact lifecycle                              (macOS: close small gaps.
   ↓                                                               Linux: automate an already-
   ↓                                                               manually-proven lifecycle.
   ↓                                                               Windows: new work.)
Phase 7  Graphical-session/autostart                              (Linux has a tracked open item
   ↓                                                               already, P7.2; Windows is new.)
Phase 8  Org-mode system CI                                       (mostly an audit/extend of
   ↓                                                               test_org_ubuntu_release_smoke.py,
   ↓                                                               already largely built)
Phase 9  Retire obsolete manual QA
   ↓
Phase 10 Observability and maintenance polish
```

Phases 4 and 5 may proceed in parallel once Phase 3 is stable, as in the source strategy. Phase 7
stays last for the same infrastructure-cost reason the source strategy gives.

## Suggested PR boundaries

Kept close to the source strategy's 27-PR breakdown, with entries removed or shrunk where this
plan's grounding pass found the work already done, and a note on which remain genuinely large:

1. Test taxonomy + policy foundation (Phase 0)
2. Connector live workflow — `connector-live-check.yml` only; Phase A/B are a prerequisite, not a PR
   in this repo (Phase 1.1)
3. TST-08 fixture completeness + Apps Script fixture + coverage guard (Phase 1.2)
4. TST-09 deferred approval test (Phase 1.3)
5. TST-10 cross-principal step-up tests (Phase 1.4)
6. TST-11 deterministic synchronization + coverage-floor tightening (Phase 1.5)
7. TST-12 property tests (Phase 1.6)
8. TST-13 systemic invariant tests (Phase 1.7)
9. Connector fixture freshness/reporting + bounded lifecycle tests (Phase 1.8–1.9)
10. Security remediation closure/documentation (Phase 1.10–1.11)
11. Windows permanent portability CI — rename/promote only, small PR (Phase 2.1)
12. macOS portability CI — new job (Phase 2.2–2.3)
13. Cross-platform daemon/MCP/approval/audit test — new module, reuses existing patterns (Phase 3)
14. Browser approval-flow coverage gaps: "Always allow," multi-card, idempotency (Phase 4.1)
15. Browser PII/responsive/light-dark coverage (Phase 4.2–4.4)
16. Gate matrix audit + close any real gap found (Phase 5) — likely small, since the matrix is
    mostly already there
17. Linux packaged lifecycle — automate the already-manually-proven P7.1/P7.3 (Phase 6.3)
18. Windows packaged lifecycle — new work (Phase 6.2)
19. macOS packaged additions — close small gaps only (Phase 6.1)
20. Package upgrade/state-preservation testing, where not already covered by 17–19
21. Linux graphical-session/autostart CI — closes the already-tracked P7.2 (Phase 7)
22. Windows graphical-session/autostart CI — new work (Phase 7)
23. Org-mode system test audit/extension — likely small (Phase 8)
24. Manual QA documentation reduction (Phase 9)
25. CI diagnostic/observability polish (Phase 10)

Each PR should leave the repository green.

## What deliberately remains manual

Unchanged from the source strategy — visual judgment, first-time OAuth/consent-screen onboarding,
one real external MCP-client compatibility smoke, OS-native UX presentation (SmartScreen,
Gatekeeper, UAC) when the corresponding platform integration changes, and human review of
detected-but-not-judged provider drift. None of these get multiplied across every OS × connector
combination.

## Definition of success

- Every PR receives comprehensive Ubuntu testing (already true).
- Runtime-relevant changes execute on real Windows and macOS runners on every PR, not just
  `workflow_dispatch` (Phase 2).
- A canonical daemon/MCP/approval/audit scenario passes on all three desktop platforms (Phase 3).
- Browser behavior is tested automatically against real Chromium, covering PII, responsive, and
  light/dark surfaces, not just the approval round trip already covered (Phase 4).
- Every connector is periodically exercised against dedicated QA accounts, including Apps Script
  (Phase 1).
- Provider API drift is detected automatically and produces a reviewable PR (Phase 1).
- `security-remediation-plan.md`'s Phase 3.12 is complete and the overall remediation plan is closed
  (Phase 1).
- Every published DMG/EXE/DEB is exercised before publication (Phase 6).
- Package upgrade tests prove user state survives, on all three platforms (Phase 6).
- Local-mode autostart has automated platform-specific coverage (Phase 7).
- Org mode executes an authenticated synthetic end-to-end request in CI (Phase 8, mostly already
  true — confirm and close remaining gaps).
- `connector-qa-testing.md` is exploratory, not mandatory, for routine releases (Phase 9).
- Routine manual release validation takes minutes, not hours (Phase 9).
- PrivacyFence can be confidently released without owning physical Windows, Linux, or macOS
  development machines.
