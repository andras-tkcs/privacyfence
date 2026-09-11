# Automated Test Strategy — Implementation Plan

Phased plan to get PrivacyFence to a state where it can be released with confidence — across
macOS/Windows/Linux local mode, Linux org mode, the browser-based approval UI, MCP clients, and
ten live third-party connectors (eleven once Apps Script has a fixture — see Phase 1's residual
gap) — without the maintainer manually reproducing that whole matrix by hand on every release.
This document is deliberately an *implementation* plan, not a restatement of the strategy: every
phase below is checked against what this repo already has today (a script, a test module, a CI
job) before describing new work, so the plan says only what's actually left to build.

**Status note (2026-09-11):** Phase 1 — the largest single body of work in this plan — landed on
`main` in three PRs ([#283](https://github.com/privacyfence/privacyfence/pull/283),
[#278](https://github.com/privacyfence/privacyfence/pull/278),
[#284](https://github.com/privacyfence/privacyfence/pull/284)) between this plan's initial draft
and this revision. Its residual work (1.8 bounded lifecycle tests, 1.9 fixture freshness reporting)
has since landed too, in a follow-up PR — as has the pytest-marker backfill on the TST-08–13
modules, which landed as part of Phase 0 rather than as a Phase 1 follow-up (see below). See
[Phase 1](#phase-1--complete-live-connector-ci-and-close-security-remediation-plan-312-—-done)
below for what shipped, what deviated from the original design, and the one item still open (Apps
Script fixture coverage, blocked on a live QA Apps Script project to record against).
[Phase 0](#phase-0--establish-the-test-taxonomy) is also now done — see that section's own status
note. Phases 2–10 are unaffected by either merge and reflect this plan's original grounding pass.

## Relationship to existing planning docs

- [`testing-policy.md`](testing-policy.md) — the current tiered description of what runs in CI
  versus by hand, now including the §0 runner-local live tier Phase 1 added. Phase 0 below further
  rewrites its framing into the finer-grained seven-layer taxonomy this plan needs; every later
  phase updates its "Quick reference" table as new automated tiers come online.
- `security-remediation-plan.md` and `connector-ci-integration-plan.md` — **both removed from
  `docs/` by [PR #284](https://github.com/privacyfence/privacyfence/pull/284)**, once every finding
  in the former's coverage matrix (SEC-01..23, TST-01..16, DOC-01..04, ORP-01..06) had a landed
  commit on `main` and the live-connector-CI infrastructure the latter designed was confirmed
  working end to end on the real self-hosted runner. Both are kept only as git history now, not as
  files to link to. [`connector-live-check-setup.md`](connector-live-check-setup.md) is the
  document that replaced `connector-ci-integration-plan.md` — it carries forward only the pieces
  that still need standing documentation (account acquisition, runner provisioning/troubleshooting)
  now that the workflow file itself is the authoritative source for what the live-check tier does.
  This plan's references below to either removed document are historical — describing what the
  work looked like when planned, before Phase 1 below records what it looked like once shipped.

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

### Status: done

`testing-policy.md` now opens with a "Test taxonomy: the seven layers" section (the table below,
carried over verbatim except its "Runs" column was filled in with what's *actually* running today
per-layer rather than left as a Phase-number placeholder — three layers, 3/6/(most of)7, are still
partially or fully open, and the table says so inline instead of pointing only at a future phase),
a "Test ownership: failure type → layer" table plus the governing rule stated verbatim, and a new
"Checked against `manual-pre-release-test-plan.md`" cross-check mapping each of that document's five
sections to a layer (item 4 below) — no item there turned out to map to zero layers, so no new phase
gap was found beyond what Phases 1–8 already cover. `pyproject.toml` has the marker list registered
verbatim. The five TST-08–13 modules got backfilled as planned — as class-level `@pytest.mark.unit`
decorators for the two modules that only gained one new class each alongside pre-existing, differently-
scoped classes (`test_qa_fixture_recorder.py`'s `TestFixturePresence`, `test_routes_security.py`'s
`TestCrossPrincipalIsolation`), and as module-level `pytestmark` for the three modules dedicated
wholly to their own TST item (`test_deferred_approval_round_trip.py` → `integration`,
`test_parser_properties.py` and `test_systemic_gate_invariants.py` → `unit`) — rather than blanket-
marking whole files that also contain unrelated pre-existing classes, since that would have overclaimed
what the marker means for content this phase didn't itself audit. `pytest --collect-only` (5037 tests)
and a targeted run of all five backfilled modules were both used to confirm no collection or behavior
change; `ruff check` is clean on every touched file.

One pre-existing inconsistency in this plan surfaced while implementing this phase, not introduced
by it: Phase 2.3 below says to "Register the `platform` pytest marker from Phase 0," but this
phase's own marker list (item 2 below), registered verbatim, has no `platform` entry — only `system`
("full daemon/MCP/approval/audit scenario," Phase 3's canonical scenario, a different concept from
Phase 2.3's OS-level `tests/platform/` suite). Left as-is rather than guessed at here: whoever
implements Phase 2 should decide whether `platform` becomes an eighth registered marker or
`tests/platform/` reuses `system`, and update `pyproject.toml` accordingly at that point.

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

   Apply markers to *new* test modules as later phases add them — Phase 1's TST-08–13 modules
   already exist unmarked (they landed before this marker work did; backfill their markers as part
   of this phase rather than leaving them the one unmarked cohort), plus Phase 3's system test and
   Phase 6's packaged-artifact tests as those land. Do not retroactively mark every other existing
   test in this phase — that's churn with no payoff until something actually needs to select on the
   marker (e.g. `pytest -m "not live"`).

3. Add the test-ownership table (failure type → layer) to `testing-policy.md`, and state the
   governing rule explicitly: *a test stays manual only when automated observation cannot reliably
   determine pass/fail* — visual judgment and first-time third-party consent screens are the two
   recurring cases that meet that bar in this project; nothing else should.

4. Cross-check `manual-pre-release-test-plan.md` against the table above — every checklist item
   there should map to exactly one layer. Where an item doesn't map to any layer, that's this plan's
   signal that the layer needs a phase (it does — see Phases 1–8 below); don't remove the manual
   item until the corresponding phase actually lands automation for it.

### Exit criteria (met)

- ✅ `testing-policy.md` describes the seven-layer target architecture and the test-ownership table.
- ✅ `pyproject.toml` has the marker list registered (even if lightly used so far).
- ✅ No existing test behavior changes.

---

## Phase 1 — Complete live connector CI and close Security Remediation Plan 3.12

### Objective

Finish the scheduled live-connector workflow and TST-08 through TST-13, then close out
`security-remediation-plan.md` entirely — it had no other open item.

### Status: done

Landed on `main` in three PRs after this plan's initial draft, in order:
[#283](https://github.com/privacyfence/privacyfence/pull/283) "Add connector-live-check.yml (Phase
C) and update testing-policy.md (Phase D)", [#278](https://github.com/privacyfence/privacyfence/pull/278)
"TST-08/09/10/11/12/13: Systemic test coverage for security invariants" (in two commits, `9d3ef19`
covering TST-08–TST-11/TST-13 and `e5b5f21` adding TST-12 as a deliberate follow-up once adding
`hypothesis` as a dependency was flagged rather than bundled silently), and
[#284](https://github.com/privacyfence/privacyfence/pull/284) "Close out security-remediation-plan.md
and connector-ci-integration-plan.md", which verified every finding in the remediation plan's
coverage matrix had a landed commit and removed both source-planning documents. What actually
shipped, versus what was originally planned here:

- **1.1 (live connector workflow)** — `.github/workflows/connector-live-check.yml` exists, runs on
  `schedule` (weekly) + `workflow_dispatch` only, targets a self-hosted runner (label
  `privacyfence-test`, not `privacyfence-qa-live` as originally named), and is confirmed green
  end-to-end against real data for all ten covered connectors. The implementation fixed three real
  bugs the original design (`connector-ci-integration-plan.md` §C) got wrong once someone actually
  built it: `actions/checkout`'s default `clean: true` would have wiped runner-local state before
  every run (fixed by making the checkout fully ephemeral instead of trying to persist state inside
  the Actions workspace); `--ephemeral` runner registration is incompatible with a
  systemd-managed always-listening runner (the plan's B.2 recommended it); and a hardcoded
  `python3.13` doesn't match every runner's actual install (switched to plain `python3`, matching
  `pyproject.toml`'s real `>=3.11` floor). See
  [`connector-live-check-setup.md`](connector-live-check-setup.md) for the corrected Phase B and a
  Troubleshooting section covering these.
- **1.2 (TST-08)** — done differently than planned: instead of a standalone
  `tests/unit/test_fixture_coverage.py`, the guard is `TestFixturePresence` inside the existing
  `tests/unit/test_qa_fixture_recorder.py`, checked against a new `EXPECTED_FIXTURES` static
  manifest in `scripts/qa_fixture_recorder.py` itself (which also self-checks at import time that
  `EXPECTED_FIXTURES`'s keys equal `CONNECTOR_CHECKS`'s, so a connector added to one without the
  other fails loudly). **Apps Script was not added** — `EXPECTED_FIXTURES`/`CONNECTOR_CHECKS` cover
  exactly the same ten connectors as before (`confluence`, `jira`, `salesforce`, `gmail`, `drive`,
  `calendar`, `contacts`, `tasks`, `slack`, `telegram`); `src/privacyfence/connectors/apps_script.py`
  still ships with no `tests/fixtures/live/apps_script/` directory and no recorder entry. This is
  the one residual gap from this phase's original scope — see below.
- **1.3 (TST-09)** — `tests/integration/test_deferred_approval_round_trip.py` landed as planned,
  same posture as `test_mcp_daemon_contract.py`, covering both the accept and the deny outcome of
  the full deferred-approval protocol (hold-window timeout → `approval_pending` → HTTP decide → a
  second identical call finds the ledger and releases without a second prompt).
- **1.4 (TST-10)** — landed as `TestCrossPrincipalIsolation` in `tests/unit/web/test_routes_security.py`,
  proving the WebAuthn step-up credential stores' per-principal binding (a second signed-in
  principal can't see another's enrolled passkey, can't delete another principal's credential,
  can't complete a registration ceremony another principal began) — the same binding property this
  plan asked for, expressed against the actual step-up mechanism rather than a generic scaffold.
- **1.5 (TST-11)** — landed narrower than originally scoped: six fixed-sleep sites were replaced
  with `threading.Event` signals plus `@pytest.mark.timeout(5)` — three in `test_gate.py` and three
  in `test_audit_forwarding.py` (not the `test_approvals.py`/`test_webauthn_stepup.py` sites this
  plan originally listed; the implementer's own investigation found the real flakiness risk lived
  in `test_gate.py` instead). One of the three `test_gate.py` fixes also surfaced and fixed a real
  latent bug in `TestRunInPopupExecutor` — a pool-saturation test whose "occupier" coroutines were
  never actually scheduled before the popup dispatch it claimed to race against, so it was passing
  without exercising the scenario it claimed to cover. A handful of `time.sleep(...)` calls remain
  elsewhere (`test_approvals.py`, `test_webauthn_stepup.py`, and several `interval`-driven
  reload-polling helpers) — PR #284's own verification treated this as within the "small
  process-settle sleeps... may remain unless they demonstrate actual flakiness" carve-out the
  original design already allowed for, not as an open item.
- **1.6 (TST-12)** — `tests/unit/test_parser_properties.py` (not
  `test_parser_roundtrip_properties.py` as originally named), covering `html_to_text.py`,
  `markdown_to_html.py`, `email_markdown.py`, and `text_extraction.py`; `hypothesis>=6.100` added
  to `pyproject.toml`'s `test` extra.
- **1.7 (TST-13)** — `tests/unit/test_systemic_gate_invariants.py`, extending the same
  parameterized source-scanning pattern `test_readme_manifest_alignment.py` already used, for all
  three invariants this plan asked for: `reason` on every gated tool, `pii_scan_text` on every
  `review`-gated call (with a documented, individually-justified exemption for Salesforce's three
  record/report reads, whose `details_text` has no separate metadata envelope to strip), and all
  eleven token-writer call sites going through the shared `secure_files.py` helpers.
- **1.8/1.9 (bounded lifecycle tests, fixture freshness reporting)** — not part of this batch of
  PRs; still open, tracked below.
- **1.10 (close the remediation plan)** — done, but as a full removal rather than an in-place
  "mark complete": `docs/security-remediation-plan.md` and `docs/connector-ci-integration-plan.md`
  are both deleted from `main`, with every dangling cross-reference elsewhere in `docs/` (
  `security-and-compliance.md`, `org-mode-operational-readiness.md`, `testing-policy.md`,
  `coding-and-testing-guidelines.md`, `windows-linux-support-plan.md`, `adr/0001`,
  `requirements/README.md`) converted to plain "(now-removed)" citations rather than real links.
  `connector-live-check-setup.md` is the new home for the parts of the removed
  `connector-ci-integration-plan.md` that still need standing documentation.
- **1.11 (testing documentation)** — done: `testing-policy.md` gained a new §0 ("Runner-local live
  tier (scheduled, not per-PR)") describing the workflow, updated its §1/§2/§2.1 framing to the
  "GitHub-hosted vs. any other GitHub Actions runner" distinction, and its Quick-reference table
  gained the new row. `pyproject.toml`'s `[tool.pytest.ini_options]` did **not** gain a `markers`
  list as part of this work — that's still Phase 0's job, not touched here.

### Residual work

1. **Apps Script fixture coverage** — genuinely still open. Add `apps_script` to both
   `CONNECTOR_CHECKS` and `EXPECTED_FIXTURES` in `scripts/qa_fixture_recorder.py`, record its first
   fixture once a QA Apps Script project exists, and update `manual-pre-release-test-plan.md` §1's
   connector count accordingly. Small, standalone follow-up — no dependency on anything else in
   this plan. Blocked on a live QA Apps Script project existing to record against (both edits have
   to land together — `EXPECTED_FIXTURES`/`CONNECTOR_CHECKS` self-check at import time, so adding
   `apps_script` to one without a fixture already committed for the other fails every PR, not just
   this connector's).
2. **1.8 — bounded lifecycle tests for write-capable providers** (create/read/update/delete a
   uniquely-tagged QA object, verify cleanup) — done. `scripts/qa_fixture_recorder.py`'s
   `--lifecycle` mode (`LIFECYCLE_CHECKS`) covers `calendar`, `confluence`, `jira`, and `tasks` — the
   four connectors whose client exposes a full create/get/update triple. `contacts` is deliberately
   excluded (`ContactsClient.create_contact()`'s own docstring: "Contact deletion is not
   supported", and unlike Confluence below there's no update step either to make a create-only check
   worth running on its own); `gmail` has no `get_draft()`/`update_draft()` to exercise; `drive`/
   `slack` have writes but no matching update-in-place pair; `salesforce`/`telegram` are read-only
   from PrivacyFence's side. For calendar/jira/tasks, cleanup reaches past each client's public API
   into its internal request/service choke point (the same pattern `RawCapture`/`RawCaptureExecute`
   already use), since no `*_client.py` exposes a `delete_*()` method and no `connectors/**` tool
   ever deletes anything by design. Confluence is the one exception to actually verifying cleanup:
   deleting a page needs its own `delete:page:confluence` OAuth scope, and granting that to the
   org-wide app every real user authenticates through — just so this internal QA script can clean up
   after itself — was considered and rejected; `lifecycle_confluence()` verifies create/get/update
   only and deliberately leaves the page behind (`[QATEST-LIFECYCLE]`-tagged pages accumulate in the
   QA Confluence space and need occasional manual cleanup there). `connector-live-check.yml` runs
   `--lifecycle` on the same weekly schedule as `--check`/`--record`, and — unlike drift — fails the
   job outright on any failure, including a calendar/jira/tasks cleanup call that ran but didn't
   actually remove what it created. `tests/unit/test_qa_fixture_recorder.py` covers the sequencing
   (create → verify → update → verify → delete → confirm gone, cleanup always attempted even when an
   earlier step fails, for the three connectors that clean up; create → verify → update → verify only
   for Confluence) against in-memory fakes, fully offline.
3. **1.9 — fixture freshness/age reporting** (`< 60 days` healthy / `60–90 days` warning /
   `> 90 days` refresh required) — done. `_fixture_freshness_lines()` in `scripts/
   qa_fixture_recorder.py` now tags each connector's freshness line with `[healthy]`/`[warning]`/
   `[refresh required]`; a connector with no recorded fixture at all reports `[refresh required]`.
4. **Pytest markers on the TST-08–13 modules** — still folds into Phase 0 below once that phase's
   marker list is registered; these five modules are the first backfill candidates. Not done here —
   genuinely depends on Phase 0 landing first, not just deferred for scope reasons like the other
   three items above were.

Of these four, only Apps Script fixture coverage and the Phase 0-dependent marker backfill remain
open — treat what's left as a small, independent follow-up rather than reopening Phase 1 as a
whole.

### Exit criteria (met, except where noted)

- ✅ `connector-live-check.yml` runs successfully on the self-hosted runner; no connector credential
  is ever a GitHub Actions secret; it cannot execute from an untrusted PR.
- ⚠️ Every connector *except Apps Script* has at least one recorded live fixture, and deleting a
  connector's last fixture fails ordinary PR CI (`TestFixturePresence`). Apps Script itself is the
  one residual gap above.
- ✅ TST-09 through TST-13 pass.
- ✅ `testing-policy.md` describes both CI trust tiers.
- ✅ `security-remediation-plan.md`'s Phase 3.12, and the whole plan, are complete — the document
  itself is removed rather than left marked-complete in place, per PR #284's own judgment call that
  a fully-landed tracking document is better retired than kept as dead weight.

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
   `.github/workflows/connector-live-check.yml` already establishes for that job).
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
Phase 0  Taxonomy / doc foundation                               (DONE — testing-policy.md's
   ↓                                                               seven-layer section + ownership
   ↓                                                               table, pyproject.toml markers)
Phase 1  Live connector CI + Security Remediation 3.12 closure   (DONE — PR #283/#278/#284;
   ↓                                                               1.8/1.9 also done as a follow-up;
   ↓                                                               Apps Script fixture still open)
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

1. ~~Test taxonomy + policy foundation~~ — **done** (Phase 0)
2. ~~Connector live workflow~~ — **done**, PR #283 (Phase 1.1)
3. ~~TST-08 fixture completeness + coverage guard~~ — **done**, PR #278 (Phase 1.2); Apps Script
   fixture coverage itself is not, and is small enough to fold into PR 3 below rather than stay its
   own row
4. ~~TST-09 deferred approval test~~ — **done**, PR #278 (Phase 1.3)
5. ~~TST-10 cross-principal step-up tests~~ — **done**, PR #278 (Phase 1.4)
6. ~~TST-11 deterministic synchronization~~ — **done** for the two files that turned out to need it,
   PR #278 (Phase 1.5)
7. ~~TST-12 property tests~~ — **done**, PR #278 (Phase 1.6)
8. ~~TST-13 systemic invariant tests~~ — **done**, PR #278 (Phase 1.7)
9. ~~Security remediation closure/documentation~~ — **done**, PR #284 (Phase 1.10–1.11)
10. ~~Connector fixture freshness/reporting + bounded lifecycle tests~~ — **done** (Phase 1's
    residual work, items 1.8/1.9). Apps Script fixture coverage itself is not — blocked on a live QA
    Apps Script project to record against, and folded into whichever future PR sets that up rather
    than staying its own tracked row.
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
- Every connector is periodically exercised against dedicated QA accounts (Phase 1, done for ten of
  eleven — Apps Script fixture coverage is the one open item).
- Provider API drift is detected automatically and produces a reviewable PR (Phase 1, done).
- The Security & Quality Remediation Plan's Phase 3.12 is complete and the overall plan is closed
  (Phase 1, done — the plan document itself was removed from `docs/` rather than left
  marked-complete in place).
- Every published DMG/EXE/DEB is exercised before publication (Phase 6).
- Package upgrade tests prove user state survives, on all three platforms (Phase 6).
- Local-mode autostart has automated platform-specific coverage (Phase 7).
- Org mode executes an authenticated synthetic end-to-end request in CI (Phase 8, mostly already
  true — confirm and close remaining gaps).
- `connector-qa-testing.md` is exploratory, not mandatory, for routine releases (Phase 9).
- Routine manual release validation takes minutes, not hours (Phase 9).
- PrivacyFence can be confidently released without owning physical Windows, Linux, or macOS
  development machines.
