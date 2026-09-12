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
[Phase 1](#phase-1--complete-live-connector-ci-—-done)
below for what shipped, what deviated from the original design, and the one item still open (Apps
Script fixture coverage, blocked on a live QA Apps Script project to record against).
[Phase 0](#phase-0--establish-the-test-taxonomy) is also now done — see that section's own status
note. [Phase 2](#phase-2--cross-platform-core-ci) (cross-platform core CI) has since landed too, in
PR #293, and is now also fully done, including 2.4 — closed by an explicit decision (keep the full
core suite on `platform-windows`/`platform-macos` rather than narrow it to a targeted subset) rather
than by building the narrowing infrastructure that decision's own grounding pass found no safe
definition for; see that phase's own status note for the reasoning.
[Phase 3](#phase-3--canonical-cross-platform-system-test) (canonical cross-platform system test) is
also now done — `tests/system/test_local_mode_system.py`, collected by every job that already runs
the full suite, with no new CI wiring needed; see that phase's own status note for what shipped and
where it deviated from the original design (the bootstrap-link-redaction gotcha, and the real
"Quit PrivacyFence" action used for a genuinely clean shutdown rather than `proc.terminate()`).
[Phase 4](#phase-4--complete-browserui-automation) (browser/UI automation) is also now done, across
two PRs (#298 for item 4.1, a follow-up for items 4.2–4.5) — see that phase's own status note for
what shipped, including a real responsive-layout bug the new checks found and fixed
(`dialog_window_html.py`'s confirmation/choice dialogs overflowing a phone-width viewport) and the
new failure-artifact-capture infrastructure item 4.5 asked for, which didn't exist anywhere in this
repo before.
[Phase 5](#phase-5--exhaustive-gatepolicy-system-tests) (exhaustive gate/policy system tests) is
also now done — the audit this phase asked for (rather than a from-scratch build, since
`test_gate.py` already covered most of the matrix) found and closed two narrow gaps (an explicit
resource-grant/rule-mismatch case on both the review and popup gate branches) and confirmed one
matrix item that architecturally belongs to `tests/unit/connectors/*.py` instead of this file; see
that phase's own status note for the detail and the resulting `testing-policy.md`/
`connector-qa-testing.md` framing updates.
[Phase 6](#phase-6--packaged-artifact-lifecycle-tests) (packaged-artifact lifecycle tests) is also
now done, across several PRs (6.1 macOS gap-closing, 6.2 Windows, 6.3 Linux `.deb`, plus a final PR
closing both of the two items that were left open across all of 6.1–6.3: item 20's macOS/Windows
upgrade-in-place tests — Linux's own had already landed as part of 6.3 — and 6.4's cross-workflow
release gating) — see that phase's own status note and its new "Upgrade/state-preservation testing
(item 20)" subsection for what shipped and where it deviated from the original design (macOS/Windows
each get their own synthetically-relabeled "version N+1" built from the same already-built artifact
rather than a second real build, the same substitution 6.3's own Linux upgrade test already made;
6.4 landed as a same-repo REST-API polling job rather than a `needs:` edge, since GitHub Actions has
no such edge across separate workflow files). [Phase 7](#phase-7--graphical-sessionautostart-verification)
is now fully done — Linux (item 1) landed first as `tests/integration/test_linux_graphical_session_
autostart.py`, its own `.github/workflows/linux-graphical-session.yml`, closing the now-removed
`linux-local-deb-packaging-plan.md` P7.2 (one deliberate substitution: no real display manager in CI, so a real
`systemd --user` session brought up and pointed at `xdg-desktop-autostart.target` stands in for the
missing physical login); Windows (item 2) followed the same shape as `tests/integration/test_windows_
graphical_session_autostart.py`, its own `.github/workflows/windows-graphical-session.yml`, closing
the now-removed `windows-support-plan.md` 8.2 (two deliberate substitutions this time — see that phase's own status
note). Item 3 (macOS) remains deliberately not built, per the source strategy's own guidance. Phases
8–10 are otherwise unaffected by any of these merges and reflect this plan's original grounding pass.
Phase 11 (update branch-protection required checks) is new in this revision — added once this plan
was checked against `testing-policy.md`'s own "one job to merge" language and found not to close
that gap anywhere — and Phase 12 is the renumbered "retire the platform-specific plan docs" phase
(previously Phase 11), pushed one slot later so doc retirement stays the true last step.

## Relationship to existing planning docs

- [`testing-policy.md`](testing-policy.md) — the current tiered description of what runs in CI
  versus by hand, now including the §0 runner-local live tier Phase 1 added. Phase 0 below further
  rewrites its framing into the finer-grained seven-layer taxonomy this plan needs; every later
  phase updates its "Quick reference" table as new automated tiers come online.
- `connector-ci-integration-plan.md` — **removed from
  `docs/` by [PR #284](https://github.com/privacyfence/privacyfence/pull/284)**, once the
  live-connector-CI infrastructure it designed was confirmed
  working end to end on the real self-hosted runner. It is kept only as git history now, not as
  a file to link to. [`connector-live-check-setup.md`](connector-live-check-setup.md) is the
  document that replaced `connector-ci-integration-plan.md` — it carries forward only the pieces
  that still need standing documentation (account acquisition, runner provisioning/troubleshooting)
  now that the workflow file itself is the authoritative source for what the live-check tier does.
  This plan's references below to that removed document are historical — describing what the
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
so later phases don't duplicate checks `testing-policy.md`/the now-removed `manual-pre-release-test-plan.md` still
describe as needing a human.

### Status: done

`testing-policy.md` now opens with a "Test taxonomy: the seven layers" section (the table below,
carried over verbatim except its "Runs" column was filled in with what's *actually* running today
per-layer rather than left as a Phase-number placeholder — three layers, 3/6/(most of)7, are still
partially or fully open, and the table says so inline instead of pointing only at a future phase),
a "Test ownership: failure type → layer" table plus the governing rule stated verbatim, and a new
"Checked against the now-removed `manual-pre-release-test-plan.md`" cross-check mapping each of that document's five
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
- the now-removed `manual-pre-release-test-plan.md` and `connector-qa-testing.md` already exist as the two manual
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

4. Cross-check the now-removed `manual-pre-release-test-plan.md` against the table above — every checklist item
   there should map to exactly one layer. Where an item doesn't map to any layer, that's this plan's
   signal that the layer needs a phase (it does — see Phases 1–8 below); don't remove the manual
   item until the corresponding phase actually lands automation for it.

### Exit criteria (met)

- ✅ `testing-policy.md` describes the seven-layer target architecture and the test-ownership table.
- ✅ `pyproject.toml` has the marker list registered (even if lightly used so far).
- ✅ No existing test behavior changes.

---

## Phase 1 — Complete live connector CI

### Objective

Finish the scheduled live-connector workflow and TST-08 through TST-13.

### Status: done

Landed on `main` in three PRs after this plan's initial draft, in order:
[#283](https://github.com/privacyfence/privacyfence/pull/283) "Add connector-live-check.yml (Phase
C) and update testing-policy.md (Phase D)", [#278](https://github.com/privacyfence/privacyfence/pull/278)
"TST-08/09/10/11/12/13: Systemic test coverage for security invariants" (in two commits, `9d3ef19`
covering TST-08–TST-11/TST-13 and `e5b5f21` adding TST-12 as a deliberate follow-up once adding
`hypothesis` as a dependency was flagged rather than bundled silently), and
[#284](https://github.com/privacyfence/privacyfence/pull/284) (closing out the completed remediation
plan and connector-ci-integration-plan.md), which verified every finding in the remediation plan's
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
  "mark complete": the completed remediation plan and `docs/connector-ci-integration-plan.md`
  are both deleted from `main`, with every dangling cross-reference elsewhere in `docs/` (
  `security-and-compliance.md`, `org-mode-operational-readiness.md`, `testing-policy.md`,
  `coding-and-testing-guidelines.md`, the now-removed `windows-linux-support-plan.md`, `adr/0001`,
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
   `CONNECTOR_CHECKS` and `EXPECTED_FIXTURES` in `scripts/qa_fixture_recorder.py` and record its
   first fixture once a QA Apps Script project exists. Small, standalone follow-up — no dependency
   on anything else in this plan. Blocked on a live QA Apps Script project existing to record
   against (both edits have to land together — `EXPECTED_FIXTURES`/`CONNECTOR_CHECKS` self-check at
   import time, so adding `apps_script` to one without a fixture already committed for the other
   fails every PR, not just this connector's). Since Phase 9's rewrite of
   the now-removed `manual-pre-release-test-plan.md` no longer enumerates connectors by name or count, this item no
   longer needs a matching doc edit there.
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
- ✅ The remediation plan's Phase 3.12, and the whole plan, are complete — the document
  itself is removed rather than left marked-complete in place, per PR #284's own judgment call that
  a fully-landed tracking document is better retired than kept as dead weight.

---

## Phase 2 — Cross-platform core CI

### Objective

Prove the runtime works on Ubuntu, Windows, and macOS without tripling the full suite.

### Status note (2026-09-11)

2.1 (Windows promotion), 2.2 (macOS job), and 2.3 (`tests/platform/` suite + marker) were already
done. 2.4 (the target CI shape) is now closed too, but by an explicit decision rather than by
building the narrowing infrastructure its own table originally described: `platform-windows`/
`platform-macos` keep running the full core suite, on purpose, rather than being narrowed to
"`tests/platform/` + core sanity subset."

That narrowing was a real, separate risk/cost tradeoff, not a mechanical follow-up — this repo's
own history is the deciding evidence, not a hypothetical: 2.1's first real Windows run (promoting
the job from `workflow_dispatch`-only to every-PR) found four genuine, narrow, cross-platform-safe
bugs (a POSIX-only `strftime` directive, a registry-dependent `mimetypes.guess_type()` call, a bare
`"npm"` instead of its resolved path, and a test missing `$USERPROFILE`) that the full suite caught
and a "core sanity subset" — under any definition this grounding pass could construct — would have
missed, since none of the four failing tests lived in `tests/platform/`'s own subject matter
(atomic-write concurrency, cross-process locking, browser-launch defaults, daemon process
lifecycle) or in any other single, nameable "platform-sensitive" corner of the suite. They surfaced
in `settings_controller.py`, `drive.py`, a shim-contract test, and an audit-log test — ordinary
modules with no platform marker, reached only because the *whole* suite ran on Windows. Narrowing
to any subset defined ahead of time, by module or by marker, would as a structural matter only ever
catch categories of bug someone already thought to name; the value 2.1 actually demonstrated was
running everything and letting the OS itself decide what's platform-sensitive. Given that concrete
evidence and no offsetting evidence that the CI-time cost of the full suite is actually a problem
in practice (`platform-windows`/`platform-macos` run in parallel with `test`, not serially after
it), the decision is to keep running the full suite on both jobs indefinitely rather than trade a
demonstrated detection capability for an unmeasured CI-time saving. 2.4's original table (still
shown below for the record) and this phase's exit criteria are updated accordingly: "narrowed" is
no longer the target shape.

The new `tests/platform/` tests still run on every PR exactly as 2.3 wants (nothing extra needed —
`pyproject.toml`'s `testpaths = ["tests"]` already collects them as part of the existing full-suite
`pytest` invocation both jobs run) — they add targeted coverage for the specific OS-level behaviors
Phase 2.3 identified as otherwise-uncovered, on top of the full suite, not instead of most of it.

### Already in this repo

- `.github/workflows/tests.yml`'s `test` job already runs the comprehensive Ubuntu suite (pytest +
  coverage + coverage floor, `npm test`, `npm run typecheck`) plus `static-analysis` (`ruff`,
  informational `mypy`/`bandit`) on every PR — this is already the "Ubuntu stays comprehensive"
  half of the key decision.
- `platform-windows` (renamed from `test-windows` by 2.1 below) runs the full pytest suite with
  coverage on `windows-latest`, on every PR — no longer gated to `workflow_dispatch` only. Its
  comment trail now records that promotion decision instead of merely flagging it as pending.
- A `test-python-compat` job (3.11/3.12 matrix, reduced suite, `ubuntu-latest` only) already exists
  — Python-version compatibility is already Linux-only, matching §2.4's target.
- `platform-macos` now exists (2.2 below), `runs-on: macos-latest`, on every PR — `build.yml` still
  separately builds and signs the DMG on its own `macos-latest` job, tag-triggered only; the two are
  independent (source/runtime portability on every PR vs. the packaged release artifact).
- `tests/platform/` and the `platform` pytest marker now exist (2.3 below). Grounding 2.3 found that
  several of the areas it originally listed were already thoroughly covered elsewhere — see 2.3's
  own text for exactly which, and what genuinely new coverage `tests/platform/` adds instead.

### 2.1 Promote Windows CI — done

`tests.yml`'s Windows job now runs on every PR instead of `workflow_dispatch`-only, and is renamed
`platform-windows` for clarity (the existing Windows-specific comment trail was kept, extended
in place with the promotion decision rather than replaced). It still runs the full core suite
rather than a `platform`-marked subset: Phase 2.3's `tests/platform/` directory and `platform`
marker don't exist yet, and 2.1 always said to narrow later once that subset lands rather than
block promotion on it — so the full-suite version landed first. the now-removed `windows-support-plan.md`'s own
Phase 6.2 (the item that originally left "permanent leg vs. release-time-only" as an open decision)
is updated to record that this is the decision made.

Turning this job on for real (rather than the `workflow_dispatch`-only leg it had been, which had
in fact never actually been dispatched and passed) surfaced 55 test failures + 1 error on the very
first run — exactly the kind of gap a full-suite promotion exists to find, not a regression from
this change's own (CI-config-only) diff. Four were genuine, narrow, cross-platform-safe bugs and
are fixed (a POSIX-only `strftime` directive, a `mimetypes.guess_type()` call whose result for a
handful of known extensions shouldn't depend on the Windows registry, a bare `"npm"` passed to
`subprocess.run()` instead of its resolved `npm.cmd` path, and a test that only set `$HOME` instead
of also `$USERPROFILE`). The rest split into two buckets, both left red-skipped rather than papered
over: the already-known-and-accepted POSIX file-permission gap (the now-removed `windows-linux-support-plan.md`
Track B3), and a new finding — POSIX-style path strings (`"credentials/telegram.session"`,
a `"/tmp"` destination_dir) colliding with `ntpath`'s `os.path.join()`/`os.path.isabs()`, which in
one case (`daemon_main._resolve_path`) silently resolves to a different on-disk location entirely
on Python 3.13/Windows, not just a cosmetic separator mismatch. See the now-removed `windows-support-plan.md`
Phase 6.3 for the full breakdown and the design question the path finding raises.

### 2.2 Add macOS platform CI — done

New `platform-macos` job in `tests.yml`, `runs-on: macos-latest`, Python 3.13 (matching
`platform-windows`/`test`), running the identical full core suite `platform-windows` runs — not yet
"the same targeted subset as `platform-windows`" as originally worded, since neither job has been
narrowed to a subset (see this phase's status note above). Explicitly **not**
`scripts/build_dmg.sh`/signing/notarization, which stay in `build.yml`'s release path — this job
runs from source, on every PR, the way `platform-windows` does.

### 2.3 Create the targeted platform suite — done

`tests/platform/` now exists (preferred over a bare `-m platform` filter scattered across existing
files, so new platform tests have an obvious home), and the `platform` pytest marker is registered
in `pyproject.toml` — resolving Phase 0's own open naming question (its status note) as a marker
distinct from `system`, since Phase 3's canonical daemon/MCP/approval/audit scenario is a different
concept from this directory's OS-level path/process/locking/daemon-discovery tests.

Grounding this item against the areas it originally listed found most of them already thoroughly
covered by existing tests that already run cross-platform (including on `platform-windows` today,
and now `platform-macos` too) — adding near-duplicate coverage under `tests/platform/` for these
would have been pure churn:

- **State/config path resolution** (`paths.py`) and **environment discovery** —
  `tests/unit/test_paths.py`'s `TestIsBundled`/`TestIsInstalledPackage`/`TestDataDir`/`TestOrgDir`/
  `TestUserDir`/`TestDownloadsDir`/`TestBundleMacosDir`/`TestAppBundlePath` already cover every
  dev/bundled/installed-package branch combination.
- **Secure directory creation** (SEC-09's `secure_mkdir`) — the same module's directory-creation/
  permission-re-tightening cases, plus `tests/unit/test_secure_files.py` directly.
- **Path-separator handling** — already covered for the case that actually works correctly
  cross-platform (a relative path joined via `pathlib`/`os.path.join` against a native, non-hardcoded
  root, e.g. `tests/unit/test_daemon_main.py`'s `TestResolvePath::
  test_relative_path_for_a_non_local_principal_uses_its_own_storage_root`). The one case that does
  **not** work correctly on Windows today — a hardcoded POSIX-style path literal (e.g.
  `"credentials/telegram.session"`, `"/etc/hosts"`) run through `os.path.join()`/`os.path.isabs()` —
  is a known, already-tracked open design question (the now-removed `windows-support-plan.md` Phase 6.3's "new
  finding, tracked, not fixed"), not something this item re-litigates or works around with a new
  test; the existing `@pytest.mark.skipif(sys.platform == "win32", ...)` cases stay exactly as they
  are.

What `tests/platform/` actually adds — genuine gaps this grounding pass found, none of them
previously covered anywhere in the suite:

- **File handling** — `test_atomic_write_concurrency.py`: `secure_files.atomic_write_bytes()`'s
  atomicity claim (a reader only ever sees the old complete file or the new complete file) proven
  against two genuinely separate OS processes racing writes to the same destination, not just
  same-process sequential calls.
- **Single-instance locking** — `test_single_instance_lock_cross_process.py`: the existing
  `tests/unit/test_daemon_main.py::TestInstanceLock` already proves the lock is OS-level (a second
  file descriptor in the *same* process is rejected), but not that it holds and releases correctly
  across a real process boundary — proven here with a real `subprocess.Popen` holder, released both
  cleanly and by being killed outright.
- **The browser-launch abstraction** — `test_browser_launch_default.py`: `oauth_loopback.
  run_browser_oauth()`'s injectable `open_browser` parameter is exercised by every existing test via
  its own stand-in, leaving the real production default (`opener is None` → lazily-imported
  `webbrowser.open`) never actually reached by anything. Proven here by patching `webbrowser.open`
  itself rather than injecting a callback.
- **Process spawning, daemon startup, shim daemon discovery (`mcp_url` file), and process cleanup on
  shutdown** — `test_daemon_process_lifecycle.py`: every other daemon-startup test in this repo
  drives `daemon_main.run_app()`/`main()` as a plain function call inside the test's own process;
  nothing previously started `python -m privacyfence.daemon_main` as a genuinely separate OS process
  the way the packaged app/a systemd unit/mcpb/shim's own spawn call all do. This module does, using
  a lighter isolation technique than `tests/integration/test_org_ubuntu_release_smoke.py`'s real
  `pip install --target` (which that module needs for a different reason — proving installation
  itself works): faking `sys.frozen`/`sys._MEIPASS` before importing `privacyfence.daemon_main` in
  the spawned process flips `paths.is_bundled()` the same way a real packaged `.app` would, without
  a real package build. Proves the daemon binds a real socket, writes the `mcp_url` file `mcpb/
  shim/src/protocol.ts` reads to discover it, and that `proc.terminate()` frees both the port and the
  instance lock for an immediately-following fresh launch — deliberately a small slice of Phase 3's
  future full daemon/MCP/approval/audit scenario (`tests/system/test_local_mode_system.py`, not yet
  built), not a duplicate of it; Phase 3 should reuse this module's spawn/isolation pattern rather
  than reinventing it, the same way its own text already says to reuse
  `test_mcp_daemon_contract.py`'s.

All four new `tests/platform/` modules carry `pytestmark = pytest.mark.platform` and run wherever
the full suite already runs (`test`, `test-python-compat`, `platform-windows`, `platform-macos`) —
no CI wiring beyond the new `platform-macos` job itself was needed, since `pyproject.toml`'s
`testpaths = ["tests"]` already collects everything under `tests/platform/`.

### 2.4 Avoid matrix explosion — done (decision: keep the full suite, don't narrow)

Original target shape from the source strategy, superseded by the decision recorded in this
phase's status note above:

| Runner | Suite | Originally proposed | Actual, and now the deliberate target |
|---|---|---|---|
| `ubuntu-latest` (`test`) | Full pytest, coverage, `npm test`, typecheck, Chromium, static analysis | ✅ matches | ✅ matches |
| `ubuntu-latest` (`test-python-compat`) | 3.11/3.12 reduced core suite | ✅ matches | ✅ matches |
| `windows-latest` (`platform-windows`) | `tests/platform/` + core sanity subset | ⚠️ full core suite | ✅ full core suite, on purpose — see status note |
| `macos-latest` (`platform-macos`) | `tests/platform/` + core sanity subset | ⚠️ full core suite | ✅ full core suite, on purpose — see status note |

The "avoid matrix explosion" objective is still met without narrowing these two jobs: the
Cartesian product this plan's core testing principle warns against is `OS × connector × operation
× gate × browser × package type`, not "the same OS-independent core suite running on more than one
OS." Running one already-deduplicated suite (no connector/gate/browser/package permutation, since
those stay OS-independent by construction) on three runners is linear in the number of OSes, not
exponential in anything — matrix explosion was never actually a risk here once `test-python-compat`
already handles the one genuinely combinatorial axis (Python version) Linux-only. Full-suite
promotion costs CI minutes, not combinatorial growth, and 2.1's own evidence says that cost buys
real detection the narrowed alternative would not.

### Exit criteria

- ✅ Runtime-relevant PRs run meaningful tests on all three OS families.
- ✅ The Windows/macOS jobs don't duplicate Ubuntu's Node/Chromium/coverage-floor steps — revised
  from the original "stay materially smaller" wording once 2.4's own grounding pass found that
  wording assumed narrowing was the right call without evidence either way; 2.1's evidence (real
  bugs a narrowed subset would have missed) settled it against narrowing. What "avoid matrix
  explosion" actually requires — no OS × connector/gate/browser/package permutation — was true
  before this decision and stays true now; it never depended on the Windows/macOS jobs being
  smaller than `test`, only on them not re-deriving Node/Chromium/coverage-floor results `test`
  already establishes once.
- ✅ A platform-specific regression fails before release, not after — already true today (2.1's own
  first-run findings are the proof), and is *strengthened*, not weakened, by keeping the full suite
  on both jobs rather than narrowing it.

---

## Phase 3 — Canonical cross-platform system test

### Objective

One scenario — real daemon → MCP → approval → audit — proven identical on Linux, Windows, and
macOS.

### Status: done

`tests/system/test_local_mode_system.py` (`@pytest.mark.system`) landed, collected by every job
that runs the full suite (`test` on `ubuntu-latest`, `platform-windows`, `platform-macos`) via
`pyproject.toml`'s existing `testpaths = ["tests"]` — no new CI wiring needed, the same shape
Phase 2.3's `tests/platform/` established. What shipped, versus what this phase originally
described:

- **Real process boundary, not an in-process call.** Every other daemon/MCP/approval test in this
  repo (`test_mcp_daemon_contract.py`, `test_deferred_approval_round_trip.py`) drives `WebServer`/
  `daemon_main` functions as a plain call inside the test's own process. This module instead reuses
  Phase 2.3's own spawn technique (`tests/platform/test_daemon_process_lifecycle.py`'s `python -c
  <bootstrap>`, monkeypatching `privacyfence.paths.data_dir` to an isolated sandbox before
  `daemon_main` is ever imported) to run the real `daemon_main.main([])` entry point as a genuinely
  separate OS process — exactly the reuse Phase 2.3's own text called for ("Phase 3 should reuse
  this module's spawn/isolation pattern rather than reinventing it, the same way its own text
  already says to reuse `test_mcp_daemon_contract.py`'s").
- **Synthetic connector, injected differently than the in-process tests.** Since the daemon here is
  a real subprocess, there's no shared Python object to hand a fake connector to the way
  `test_deferred_approval_round_trip.py`'s in-process `WebServer` construction does. The bootstrap
  script instead monkeypatches `daemon_main.build_connectors` to return one minimal real (not
  mocked) `Connector` — same shape as that module's own `GatedTestConnector`, necessarily
  reproduced rather than imported since it has to exist inside the spawned process's own `-c`
  script — with a single write (`gate="popup"`) tool. No live provider needed, same as every other
  daemon/MCP test in this repo.
- **`GET /settings`/`GET /approvals` via a minted bootstrap code, not the logged link.** The
  original design ("verify discovery/state files exist," then "GET /settings, GET /approvals")
  undersold a real gotcha this phase's implementation found: the daemon's own startup log lines
  that print those bootstrap links (`WebServer.mint_bootstrap_url()`) get their `?bootstrap=<code>`
  query string redacted by `safe_errors.SecretRedactingFormatter` before it ever reaches the log
  file — that formatter's key=value pattern matches the literal word "bootstrap," which is exactly
  the point of the redaction (a leaked log line shouldn't be a usable credential) but also means a
  test can't scrape a working link out of the log the way it might naively expect to. This module
  instead mints its own code via `POST /api/bootstrap` (`Authorization: Bearer <web_token>`, the
  same "still have filesystem access, no valid link handy" path `unauthorized_html`'s own 401 page
  already recommends to a human), then follows the real `?bootstrap=` redirect to get a real session
  cookie.
- **Steps 1–8 as originally scoped**, plus one addition: the deferred-approval protocol (hold
  window elapses → `approval_pending` → HTTP decide → a second identical call releases from the
  ledger) is reused wholesale from `test_deferred_approval_round_trip.py` rather than re-derived,
  for both the Allow and the Deny path — the same reasoning that reused Phase 2.3's spawn pattern
  applies to reusing this in-process test's already-proven protocol shape.
- **Step 9 (clean shutdown) via the real "Quit PrivacyFence" action, not `proc.terminate()`.**
  `test_daemon_process_lifecycle.py`'s own termination test uses `proc.terminate()`
  (SIGTERM/`TerminateProcess`) and deliberately only asserts "some exit code," not a clean one —
  because `run_app()`'s own `finally` block (which closes the audit logger and releases the
  instance lock) only runs if `_wait_for_shutdown()` returns normally, and an unhandled SIGTERM
  doesn't reach that far. This module instead calls the real `POST /api/settings/quit_app` action
  (the same route a human's "Quit PrivacyFence" button in `/settings` posts to), which calls
  `SettingsController.quit_app()` → `daemon_main.request_shutdown()` → `_wait_for_shutdown()`
  returns → the real `finally` block runs → `main()` returns `0` → the subprocess exits with a
  genuinely clean code, not just a code.
- **Platform-specific assertions, kept deliberately light.** The original design's per-OS bullets
  (Windows path handling/single-instance lock/process spawning, Linux XDG paths/permissions/process
  cleanup, macOS state paths/process discovery) turned out to already be Phase 2.3's own territory —
  `tests/platform/`'s four modules (state/config path resolution, secure-directory creation,
  cross-process single-instance locking, a real spawned-daemon-process lifecycle) cover exactly
  those, cross-platform, already. Duplicating them here would have been pure churn (the same
  reasoning Phase 2.3's own grounding pass used against re-covering ground `tests/unit/test_paths.py`
  already had). What this module adds *on top* of that existing coverage, genuinely new: a
  POSIX-only permission assertion (`0o700`, skipped on Windows per `secure_mkdir`'s own "best effort
  on non-POSIX" docstring) against the audit-log directory this exact end-to-end scenario just wrote
  to — not a directory some other test created for the purpose, but the one this contract's own
  Allow/Deny decisions landed in.

### Exit criteria (met)

- ✅ The same daemon→MCP→approval→audit contract passes on `ubuntu-latest`, `windows-latest`, and
  `macos-latest` — one module, collected by all three jobs, no per-OS fork of the test itself.

---

## Phase 4 — Complete browser/UI automation

### Objective

Move every objectively testable browser behavior out of manual QA.

### Status: done

Item 1 (`TestApprovalListBehavior`) landed first, in PR #298 (Phase 4.1). Items 2-5 landed in a
follow-up PR that also extended `test_browser_smoke.py` directly (still the one home for this
layer, no new file):

- **PII behavior** (`TestPiiApprovalUi`): a review-gate card carrying `pii_categories` renders the
  "Possible PII detected" risk card with every matched category visible as its own tag; an ordinary
  card with no PII match never renders it at all (the negative case only means something next to
  the positive one); and the separate PII/rule confirmation dialog
  (`show_pii_confirmation_popup`/`dialog_window_html.build_confirmation_html`) actually resolves
  Proceed → `True` and Cancel → `False` from a real click, with the right post-decision toast on
  each.
- **Responsive layout** (`TestResponsiveLayout`): the three named viewports (375×812, 768×1024,
  1280×800), asserting no horizontal page scroll on both the approval list and a pending WIDE-layout
  card, the WIDE two-column split actually computing `flex-direction: column` below
  `approval_window_html.py`'s 700px breakpoint and `row` above it, primary Allow/Deny actions
  staying visible, and the confirmation dialog fitting a phone viewport too. **Found and fixed a
  real bug in the process**: `dialog_window_html.py`'s `_document()` (the confirmation/choice-picker
  shape `show_pii_confirmation_popup`/`show_rule_confirmation_popup`/`show_rule_choice_popup` all
  render) gave its `<body>` a bare fixed `width: {width}px` — correct for the native host, which
  sizes its own window frame to exactly that width, but an unconditional horizontal overflow once
  the identical document is served into an ordinary (narrower) browser tab, which the web approval
  UI does for exactly this shape. Fixed to `width: min({width}px, 100%)` plus the same
  `@media (max-width: 700px)` height override `approval_window_html.py`'s own card documents already
  use, for the same reason — the same "no test catches this" pattern `build_csp()`
  (`TestSecurityHeadersCsp`) and the PDF `<embed>` fix (`TestPdfPreview`) already closed elsewhere in
  this file, this time caught by a real-browser layout check instead of a real-browser security
  check. `tests/unit/test_dialog_window_html.py`'s two width-assertion tests were updated to match
  the new responsive CSS shape rather than the old bare-pixel one.
- **Light/dark mode** (`TestColorScheme`): both `prefers-color-scheme` values render correctly
  (element presence) on the approval list and on a pending PII card, plus one positive-and-negative
  pairing test proving the dark tokens actually take effect — `document.body`'s own computed
  background color genuinely differs between the two `page.emulate_media()` states in the same
  browser context — rather than the dark case silently falling back to the light palette in a way
  bare element-presence assertions can't tell apart. Structural assertions only, no pixel
  comparison, per this phase's own text; subjective visual quality stays manual
  (`docs/testing-policy.md`'s governing rule).
- **Failure-artifact capture**: neither this file nor `tests.yml`'s own Playwright step had any of
  screenshot/console/DOM/daemon-log capture on a failing test before this landed — checked, not
  assumed, so this is new infrastructure, not a duplicate. `tests/integration/conftest.py`'s
  (moved up to the repo-wide `tests/conftest.py` by Phase 10, so `tests/system/` could reuse it too)
  `pytest_runtest_makereport` hook (hook implementations are only ever collected from
  `conftest.py`/plugins, never an ordinary test module) stashes each phase's own outcome onto the
  test item; `test_browser_smoke.py`'s `page` fixture now buffers every `console`/`pageerror` event
  as it happens (read only after the fact, from teardown, would miss anything printed before a
  failure); and the new autouse `_capture_failure_artifacts` fixture writes a screenshot, the page's
  current DOM, that console/pageerror transcript, and — since this suite runs `WebServer` in-process
  on a background thread rather than as a separate OS process, so there's no daemon log file to
  reach for — whatever `privacyfence.*` logged during the test (via `caplog`) to
  `test-results/browser-smoke/<test id>.*`, but only for a test that actually failed; nothing is
  written on a passing run. `.github/workflows/tests.yml` uploads that directory as a build artifact
  on `failure()`, and `.gitignore` excludes it as a local/CI-only scratch directory, never something
  to commit.

### Exit criteria (met)

Manual browser QA is reduced to subjective visual inspection — every objectively-checkable behavior
above has a passing automated assertion.

---

## Phase 5 — Exhaustive gate/policy system tests

### Objective

Retire the giant live-connector QA flow as the normal way to prove gate behavior.

### Status: done

`tests/unit/test_gate.py` was already, per its own class list, most of the matrix the source
strategy asks for (`auto→allowed`, `review→Allow/Deny`, `review+PII→Proceed/Cancel`,
`popup/write→Allow/Deny`, "Always allow"→proposed rule, matching/non-matching rule,
unattended allowed/forbidden) — this phase's own framing said to audit and close gaps, not build
from scratch, and that's what the audit found once carried out: two narrow, genuinely missing cases,
plus one matrix item that turns out to belong to a different test file entirely by this codebase's
own architecture, not a gap in this one.

- **Resource-grant match/mismatch** — already exercised end to end at the evaluator level
  (`tests/unit/test_resource_grants.py`, `tests/unit/test_auto_accept.py`'s grant-backed rule
  classes, e.g. `TestDriveRules`/`TestSheetsFolderScopedRules`), since a grant compiles into an
  ordinary rule-shaped entry before `AutoAcceptEvaluator.should_auto_accept()` ever sees it
  (`settings_controller.py`'s own comment: "a compiled entry... not hand-authored") — `gate.py`
  itself, and therefore `test_gate.py`'s `FakeEvaluator`, is architecturally agnostic to whether a
  match came from a plain rule or a grant. What `test_gate.py` itself was missing was the
  *gate-level* half of that binary: every existing review/popup-gate test already reached the popup
  under `FakeEvaluator()`'s default no-match result, but none of them asserted that reaching the
  popup was actually *caused* by the evaluator being consulted and returning no match, as opposed to
  an accident of the fixture default. Added `TestReviewGateDecisions::
  test_no_matching_grant_or_rule_falls_through_to_popup_with_evaluator_consulted` and
  `TestPopupGateWrites::test_no_matching_rule_falls_through_to_popup_with_evaluator_consulted`,
  each asserting the evaluator's `calls` list is non-empty (gated_call re-checks it a second time
  right before showing the popup, for the documented concurrent-approval coalescing race — see
  `TestCoalescing` — so this is `>= 1`, not `== 1`) and that the popup only ran because of that
  no-match result, alongside the pre-existing full-tuple assertions (result, audit decision,
  `auto_accept_rule`).
- **"Policy denial before connector execution"** — confirmed already proven, but not inside
  `test_gate.py`, because it structurally can't be: `gated_call()` never holds a reference to a
  connector's provider client at all. For every popup-gated write, the calling connector method
  (e.g. `GmailConnector._create_draft`) is written as `await gated_call(...)` followed by the real
  provider call on the next line — a raised denial (this file's own
  `test_deny_raises_and_audits_rejected` cases, both gate branches) already prevents that second
  line from running by ordinary Python control flow, with no separate flag for this module to
  intercept. The actual "provider client mock asserted not called" proof already exists, one
  assertion per write tool, in `tests/unit/connectors/*.py` (10 connector test modules, e.g.
  `test_gmail_connector.py`'s `client.create_draft.assert_not_called()` /
  `client.create_label.assert_not_called()`) — the only layer that actually holds a reference to the
  client to assert against. Documented this explicitly in `test_gate.py`'s own module docstring
  rather than adding a test that can't exist in this file, so the next person auditing this matrix
  doesn't rediscover the same architectural fact from scratch.
- **Full-tuple assertions** — spot-checked broadly (every class in the file, plus a script checking
  every `test_*` function for a missing `audit_dir`/audit-entry assertion): of the 144 cases now in
  the file, the only 8 with no audit assertion are pre-validation `ValueError` cases in
  `TestProposeRuleChange` that raise before any audit-worthy event exists to record, and
  `TestRunInPopupExecutor`'s four executor-mechanism cases, which aren't gate-decision cases at all.
  Every actual gate-decision case already asserts the meaningful subset of the tuple for its own
  branch (gate selection is implicit in which popup function is stubbed and asserted
  called/not-called; connector call state is out of scope per the point above; result, audit
  decision, and rule/grant side effect are asserted directly) — no case found checking only a
  subset that should be extended.
- **`testing-policy.md`/`connector-qa-testing.md` framing** — both updated. `testing-policy.md`'s
  §3 intro and its the now-removed `manual-pre-release-test-plan.md` cross-check table (§3 row) now say Phase 5
  confirmed `test_gate.py`'s coverage rather than "is auditing" it.
  `connector-qa-testing.md`'s §3 ("Gate behavior") now states outright that `test_gate.py` is the
  primary, deterministic proof for gate-state coverage and this tier is no longer required to
  re-verify it — narrowing §3's checklist to what it's still needed for: a live provider response
  proving a given tool is actually wired to the gate/metadata its implementation intends, which
  `test_gate.py`'s synthetic contexts and `test_systemic_gate_invariants.py`'s source-level scan
  don't reach.

`pytest --collect-only` reports 5120 tests collected (up from Phase 0's 5037, consistent with what
landed in Phases 1–4 since); `tests/unit/test_gate.py` alone runs its 144 cases (142 pre-existing +
2 new) in ~2 seconds. `ruff check` is clean on the touched file.

### Exit criteria (met)

- ✅ Every gate-state transition in the matrix has confirmed, deterministic automated coverage — the
  two gaps this audit found are closed; the one matrix item that doesn't apply to this file is
  documented as such, not left silently unaddressed.

---

## Phase 6 — Packaged-artifact lifecycle tests

### Objective

Prove what users actually download.

### Status note (2026-09-12)

6.1 (macOS), 6.2 (Windows), and 6.3 (Linux `.deb`) all landed as their own PRs and are done, per
their own subsections below. The two items left open across all three of those PRs are now also
closed: item 20 (upgrade/state-preservation testing for macOS and Windows — Linux's own landed
inside 6.3 itself, see that subsection) has its own new subsection below, and 6.4 (release gating)
is done as a same-repo REST-API polling job, not literal cross-workflow `needs:` (GitHub Actions has
no such thing) — see that subsection's own text for why. Phase 6 as a whole is now done.

### Already in this repo

- **macOS**: done as of this phase's own §6.1 below — `tests/integration/test_macos_packaged_smoke.py`
  already mounted the built DMG, extracted the app, launched the frozen daemon with an isolated
  `$HOME`, connected via the real built `mcpb/shim/dist/shim.js`, and drove a headless-Chromium
  approval round trip; §6.1 closed the two gaps this phase's own audit found (signature/notarization
  validation, and state-outside-package survival) rather than rebuilding any of the above.
- **Linux**: done as of this phase's own §6.3 below — `tests/integration/test_deb_packaged_
  lifecycle.py` turns the now-removed `linux-local-deb-packaging-plan.md` Phase 7's install/autostart-file/
  remove/purge lifecycle (P7.1) and upgrade-in-place test (P7.3), both previously only manually
  verified, into a repeatable `build.yml` CI job. P7.2 (graphical-session autostart) is explicitly
  still open there and belongs to this plan's Phase 7, not here.
- **Windows**: done as of this phase's own §6.2 below — `tests/integration/test_windows_packaged_
  smoke.py` runs a real silent install (autostart Task Scheduler registration included) of
  `dist/PrivacyFence-*-setup.exe`, the daemon → MCP → approval → audit round trip against the real
  installed daemon, and a real silent uninstall (package removal + scheduled-task removal + untouched
  user state), wired into `build.yml`'s `build-windows` job right after `scripts/build_installer.ps1`.

### 6.1 macOS — close remaining gaps only — done

Auditing `test_macos_packaged_smoke.py` in full (rather than assuming, per this item's own
instruction) found the round trip it already ran was solid but genuinely missing both gaps this
item asked to check for — neither signature/notarization validation nor state-outside-package
survival existed anywhere in the file. Both are closed, narrowly, without touching the existing
round trip's own steps 1–5:

- **State lives outside the package** — the existing round-trip test now has one addition at its
  end: delete the installed `PrivacyFenceApp.app` (macOS's actual "uninstall" gesture — there's no
  installer/uninstaller pair the way Windows/Linux have one) and confirm the auto-accept rule the
  test just applied is still readable from `$HOME/.privacyfence/config/settings.yaml` — the same
  "user state survives package removal" property `test_deb_packaged_lifecycle.py`'s `dpkg -r`/
  `dpkg -P` and `test_windows_packaged_smoke.py`'s silent uninstall already assert for their own
  platforms' removal gesture. Deleting the bundle's files out from under its own already-running
  process is ordinary POSIX unlink semantics, not an error, so this needed no change to the daemon
  fixture's own lifecycle.
- **Signature/notarization** — a new, separate test (`test_packaged_app_signature_and_notarization`,
  deliberately its own test so a signature failure is never conflated with an approval-protocol
  failure in one report) asserts `codesign --verify --deep --strict` on the installed bundle, that
  the identity is a real `Authority=Developer ID Application` rather than ad-hoc, and that
  Gatekeeper's own policy engine accepts it (`spctl --assess --type execute`) — the same question a
  real first launch asks, without actually driving the interactive "are you sure" dialog a human
  sees (that part of the story stays manual, per the source strategy). Notarization is checked
  against the DMG itself via `spctl --assess --type open --context context:primary-signature`'s own
  `source=Notarized Developer ID` line — chosen over `xcrun stapler validate` specifically so this
  never needs network access to ask Apple directly. `scripts/build_dmg.sh`'s `--sign`/
  `NOTARIZE_PROFILE` are both optional (a local dev build with no Developer ID identity is
  legitimate and common), so this test skips outright, rather than failing, on an unsigned or
  signed-but-unnotarized local build; `build.yml`'s real release job always sets both, so this
  asserts the full chain there.
- **`packaged` marker** — added to the module's existing `pytestmark` list, closing the one
  inconsistency `testing-policy.md`'s own layer-6 note already flagged (`test_macos_packaged_smoke.py`
  was the one packaged-artifact module not yet carrying it, "same posture as `browser`'s own note").

Gatekeeper UX itself — the interactive dialog a human actually clicks through — stays manual per the
source strategy; nothing in this item automates that.

**A latent test-ordering bug found while adding item 20's own upgrade test (below), not part of
6.1's original scope**: the round-trip test's own "state lives outside the package" step deletes
the shared, module-scoped `installed_app` fixture's copy of the bundle — which meant
`test_packaged_app_signature_and_notarization`, running after it in pytest's default
file-definition order, always found nothing on disk and silently *skipped* (`codesign` against a
missing path exits non-zero the same way an unsigned bundle does, and the skip logic couldn't tell
the two apart) rather than actually verifying anything. Fixed by decoupling that test onto its own
private `signed_app_copy` fixture (a second, independent extraction from the DMG) instead of
sharing `installed_app` with the round-trip test — the round-trip test's own deletion step is
otherwise unchanged, including deleting the exact copy its own daemon process is still running
from. `_copy_app_from_dmg()` (the DMG-mount-and-extract logic, previously inlined in the
`installed_app` fixture) is now a standalone helper both fixtures — and item 20's own upgrade test —
share.

### 6.2 Windows — new packaged installer smoke — done

`tests/integration/test_windows_packaged_smoke.py` landed, marked `packaged`, collected wherever the
rest of `tests/integration/` is but self-skipping unless it finds a real Windows host and a built
`dist/PrivacyFence-*-setup.exe` — the same posture `test_macos_packaged_smoke.py`/`test_deb_
packaged_lifecycle.py` already established for the DMG/`.deb` — so it never runs in `tests.yml`'s
per-PR jobs, only in `build.yml`'s `build-windows` job right after `scripts/build_installer.ps1`
(that job's own new "Run packaged installer smoke test" step, gating the installer's upload/R2/
release steps the same way the DMG/`.deb` are already gated in the `build`/`build-deb` jobs).

One test, exactly the base-smoke scenario this item named:

- **`test_windows_install_validate_scenario_uninstall_lifecycle`**: a real silent install
  (`Setup.exe /VERYSILENT /SUPPRESSMSGBOXES`, `/DIR=` overridden to a scratch directory this test's
  own user already owns — `installer/privacyfence.iss`'s `PrivilegesRequired=lowest` is what lets
  that skip admin elevation entirely) → asserts `PrivacyFenceApp.exe`/`privacyfence-app.exe`/the
  bundled `.mcpb` all exist at the installed path → asserts the autostart Task Scheduler task
  (`schtasks /query`) the `.iss`'s `[Run]` section registers → starts the real installed
  `privacyfence-app.exe` alias (not the main exe directly — proving the alias itself resolves,
  same reasoning the `.deb`'s wrapper-script assertion gives) with an isolated `%USERPROFILE%` and
  runs the shared Phase 3 daemon→MCP→approval→audit contract shape against it → a real silent
  uninstall (`unins000.exe /VERYSILENT /SUPPRESSMSGBOXES`) → asserts the install directory and the
  Task Scheduler task are both gone, and that the isolated `%USERPROFILE%\.privacyfence\` user state
  (the auto-accept rule the scenario just applied) survived untouched, per the `.iss`'s own
  `[UninstallDelete]` scoping comment.

**The same deliberate substitution** `test_macos_packaged_smoke.py`/`test_deb_packaged_lifecycle.py`
already made, for the same reason: this module drives `privacyfence_propose_auto_accept_rule_
change`, the one built-in meta-tool that always blocks on a confirmation dialog with no connector/
credential behind it, and resolves the pending card via a direct HTTP POST to
`/api/approvals/<id>/decide` with the bootstrap-minted session cookie as CSRF — same as the `.deb`
module, so this job needs no Node/Playwright dependency beyond what `scripts/build_installer.ps1`
itself already needs.

Upgrade/state-preservation testing (install N, create state, install N+1, verify state survives) was
explicitly deferred here, per this item's own "don't build both in one PR" — now closed, see
"Upgrade/state-preservation testing (item 20)" below.

### 6.3 Linux `.deb` — automate the already-manually-proven lifecycle — done

`tests/integration/test_deb_packaged_lifecycle.py` landed, marked `packaged` (the first module to
actually apply that marker — `testing-policy.md`'s own table update below), collected wherever the
rest of `tests/integration/` is but self-skipping unless it finds a real Linux host, a built
`dist/privacyfence_*.deb`, `dpkg`/`dpkg-deb`/`desktop-file-validate` on `PATH`, and passwordless
root — the same posture `test_macos_packaged_smoke.py` already established for the DMG, so it never
runs in `tests.yml`'s per-PR jobs, only in `build.yml`'s `build-deb` job right after
`scripts/build_deb.sh` (that job's own new "Run packaged .deb lifecycle test" step, gating the
`.deb`'s upload/R2/release steps the same way `test_macos_packaged_smoke.py` already gates the DMG's
in the `build` job).

Two tests, exactly the two lifecycles P7.1/P7.3 named:

- **`test_deb_install_validate_scenario_remove_purge_lifecycle`**: real `dpkg -i` → asserts
  `/usr/bin/privacyfence-app`/`/opt/privacyfence/PrivacyFenceApp`/the autostart `.desktop` file all
  exist → `desktop-file-validate` against the installed `.desktop` file → starts the real installed
  daemon (the wrapper script, not the PyInstaller binary directly) with an isolated `$HOME` and runs
  the shared Phase 3 daemon→MCP→approval→audit contract shape against it → `dpkg -r` (package files
  gone, the autostart file — a conffile — correctly survives a plain remove, `$HOME` untouched) →
  `dpkg -P` (conffile now gone too, `$HOME` still untouched).
- **`test_upgrade_in_place_preserves_user_state`**: installs version N, applies a real auto-accept
  rule change through the daemon's own MCP/approval round trip (not a hand-written settings.yaml),
  installs a version N+1 — the identical PyInstaller bundle, relabeled to a version
  `dpkg --compare-versions` orders strictly after N (a second genuine PyInstaller build would
  multiply this module's already-heavy setup cost for no coverage the file-level `$HOME` isolation
  claim actually needs — see that test's `_synthetic_next_version_deb` docstring) — over it, and
  confirms the state survived and the upgraded binary still starts and serves. Closes the exact gap
  P7.3's own "partially checked" note left open: a real version transition, not a same-version
  reinstall.

**One deliberate substitution from Phase 3's own scenario**, for the same reason
`test_macos_packaged_smoke.py` already made it: Phase 3's `SystemTestConnector` is injected by
monkeypatching `daemon_main.build_connectors` *before* `daemon_main` is imported — only possible
when the test controls the Python import itself, which a packaged, frozen daemon started as its own
binary never does. This module instead drives `privacyfence_propose_auto_accept_rule_change`, the
one built-in meta-tool that always blocks on a confirmation dialog with no connector/credential
behind it — same tool, same reasoning, `test_macos_packaged_smoke.py` already uses. Unlike that
module (a real headless-Chromium click), this one resolves the pending card the same way Phase 3
itself does — a direct HTTP POST to `/api/approvals/<id>/decide` with the bootstrap-minted session
cookie as CSRF — so this job needs no Node/Playwright dependency, only what `scripts/build_deb.sh`
itself already needs.

the now-removed `linux-local-deb-packaging-plan.md`'s P7.1 and P7.3 are marked CI-automated (not just
manually-verified-once); P7.2 (graphical-session autostart) is closed by this phase's own item 1,
below.

### Upgrade/state-preservation testing (item 20) — done

Linux's own upgrade test landed inside 6.3 already (`test_upgrade_in_place_preserves_user_state`,
above). macOS and Windows each get the analogous test now, in the same shape 6.3 already
established — install version N, apply real state through the daemon's own MCP surface (not a
hand-written `settings.yaml`), replace it with a synthetically-relabeled version N+1 built from the
*same* already-built artifact (never a second genuine PyInstaller/build pass — see each test's own
docstring for why that would only add cost, not coverage, for what this item needs proven), confirm
the state survived and the upgraded binary still starts and serves:

- **macOS** (`test_macos_upgrade_preserves_user_state` in `test_macos_packaged_smoke.py`) — macOS
  has no installer/uninstaller pair at all (6.1's own §6: "uninstalling" is deleting the `.app`
  bundle), so "upgrading" here is just deleting the old bundle and copying in a fresh extraction
  from the same DMG, relabeled via a rewritten `Contents/Info.plist` `CFBundleShortVersionString`
  (`_bump_bundle_version`) — the macOS analogue of 6.3's own `_synthetic_next_version_deb`. Unlike
  the primary round-trip test in this same module, this test's own state-creation step doesn't use
  the real Node shim or a real browser click — it calls the daemon's `/mcp` endpoint directly and
  resolves the pending card with a raw HTTP POST, the same lighter substitution the Linux/Windows
  modules already use for their own scenarios (the shim artifact itself is already proven by the
  primary round-trip test; this test's own job is proving state survival across a bundle swap, not
  re-proving the shim a second time). Landed alongside a real fix to a latent bug this addition
  surfaced in 6.1's own code — see 6.1's own text above for the `signed_app_copy` fixture and why it
  exists.
- **Windows** (`test_windows_upgrade_in_place_preserves_user_state` in
  `test_windows_packaged_smoke.py`) — a second, standalone `iscc.exe` invocation against the *same*
  already-built `dist/PrivacyFenceApp` onedir output, `.mcpb`, and icon 6.2's own installer build
  already produced, with a bumped `/DAppVersion` and a scratch `/DOutputDir`
  (`_synthetic_next_version_installer`) — not a second PyInstaller build, same reasoning as the
  macOS/Linux cases. Unlike `dpkg --compare-versions`, Inno Setup's own upgrade detection keys off
  `AppId` (fixed in `installer/privacyfence.iss`), not a version-string comparison, so the relabeled
  version doesn't need to be *orderable*, only different. The second silent install targets the same
  `/DIR=` as the first, and `installer/privacyfence.iss`'s `[Run]` section (unconditional on every
  install, upgrades included) re-registers the Task Scheduler task, which this test asserts is still
  present afterward. This addition also fixed a real, if narrower, bug of its own:
  `test_windows_packaged_smoke.py`'s `_prepare_home` unconditionally overwrote `settings.yaml` on
  every boot (fine for the original single-boot test, silently fatal for a test that needs to boot
  the same `$HOME` twice) — fixed to match `test_deb_packaged_lifecycle.py`'s own
  load-existing-then-patch-two-fields pattern, the same fix 6.1's own `signed_app_copy` addendum
  above made for a different reason in the macOS module.

Both additions carry the module's own `packaged` marker (inherited from each module's
`pytestmark`) and run wherever their respective test 1 already does — no new CI wiring: `build.yml`'s
existing `pytest tests/integration/test_macos_packaged_smoke.py -v` /
`pytest tests/integration/test_windows_packaged_smoke.py -v` steps already collect every test in
each file, this addition included.

### 6.4 Release gating — done

Wire all three into the release build workflows (`build.yml`, `publish-pypi.yml`) ahead of the
publish step — build → package smoke → signature/notarization validation → publish. A failed
packaged-artifact test blocks publication.

Within `build.yml` this was already true before this item — each of the `build` (macOS),
`build-windows`, and `build-deb` jobs already ran its own packaged-artifact test as an ordinary step
between building its own artifact and that same job's own R2-upload/GitHub-Release-attach steps (6.1
predates this whole plan; 6.2/6.3 built it in from the start), so an ordinary failed step already
stopped each job before its own publish steps could run — no `needs:` edge needed for that part,
since it's all sequencing within one job. Confirmed rather than re-built.

The genuinely open part was `publish-pypi.yml`: its sdist/wheel has no packaged-artifact test of its
own to sequence after, and — since `build.yml`/`publish-pypi.yml` are two independent listeners on
the same `push: tags: ['v*']` event, not one workflow — nothing connected a failure in `build.yml`
(a broken DMG/installer/`.deb`) to whether `publish-pypi.yml` still went ahead and published. This
landed differently than the original wording ("a change to those workflows' job dependencies")
literally implied: GitHub Actions has no `needs:` across separate workflow files at all, so instead
`publish-pypi.yml` gained its own new first job, `wait_for_build`, which polls the REST API for
`build.yml`'s own run against the exact same commit SHA and fails (blocking every `publish-*` job
below it — `publish-testpypi`, `publish-pypi`, and `publish-r2`, via an ordinary same-workflow
`needs:` edge onto this new job) unless that run completed successfully. A `workflow_run`-triggered
redesign (having `publish-pypi.yml` fire only after `build.yml` finishes, instead of independently on
the same tag push) was considered and rejected: it would have silently broken every existing
`startsWith(github.ref, 'refs/tags/')`/`GITHUB_REF` check already in that workflow, since a
`workflow_run`-triggered run's own `github.ref` refers to the *triggering* workflow's default-branch
context, not the tag — the polling job keeps the original trigger (and therefore every existing
ref-based check) completely unchanged. See `publish-pypi.yml`'s own `wait_for_build` job comment and
this repo's `CLAUDE.md` ("Packaged-artifact release gating") for the full reasoning, including how a
`workflow_dispatch` rerun is gated the same way (keyed on commit SHA, not run recency).
`publish-r2` — not originally named in this item's own wording, but "publication" — is gated the
same way as the two PyPI jobs: R2 is meant to be a complete record of what a tag actually shipped,
not a place a broken build's bits land anyway just because nothing public depends on them.

### Exit criteria (met)

- ✅ Every published DMG/EXE/DEB has been started and exercised on its native OS, automatically,
  before release.
- ✅ Package upgrade tests prove user state survives, on all three platforms (item 20).
- ✅ A failed packaged-artifact test blocks publication on every channel this plan publishes to
  (GitHub Release, PyPI/TestPyPI, the private R2 archive), not just its own platform's artifact
  (6.4).

---

## Phase 7 — Graphical-session/autostart verification

### Objective

Automate whether PrivacyFence actually starts after a normal desktop login — deliberately last,
since GUI-session infrastructure is the most expensive, flakiest tier here.

### Already in this repo

the now-removed `linux-local-deb-packaging-plan.md` already tracks this exact gap as its own open item, **P7.2**:
"Real desktop-session test... install on a real or VM Ubuntu/Debian desktop, log out/in, confirm the
daemon is running post-login... confirm the OAuth loopback browser flow opens correctly." Nothing
in this repo implements it yet. the now-removed `windows-support-plan.md` already has a home for the Windows
equivalent too — its own manual-QA item **8.2** ("Log out/in (or reboot), confirm the daemon
autostarts via the Task Scheduler task") — just not yet automated.

**Status note:** items 1 (Linux) and 2 (Windows) are now **done** —
`tests/integration/test_linux_graphical_session_autostart.py`/`.github/workflows/linux-graphical-
session.yml` and `tests/integration/test_windows_graphical_session_autostart.py`/`.github/workflows/
windows-graphical-session.yml` respectively. Item 3 (macOS, deliberately not built) remains as
originally planned.

### Remaining work

1. ~~**Linux**: a graphical Ubuntu VM scenario implementing the now-removed `linux-local-deb-packaging-plan.md` P7.2
   exactly as already specified there — install, ensure stopped, logout/reboot, login, wait for
   session startup, verify daemon, exercise the system request from Phase 3, and where practical the
   OAuth loopback browser-opening flow. Close P7.2 in that document once this lands.~~ **Done** —
   `tests/integration/test_linux_graphical_session_autostart.py`. There's no real display manager to
   actually log into on a CI runner, so this makes one deliberate substitution instead of a
   hand-rolled one: it brings up a real `systemd --user` manager for the account (the same
   `user@<uid>.service` unit `pam_systemd` starts at a real login) and starts
   `xdg-desktop-autostart.target` — the same target a real GNOME/KDE/Sway session's own
   compositor/session manager pulls in once it comes up, standing in for the missing physical login.
   Everything downstream of that one substitution is the real, unmocked OS mechanism:
   `systemd-xdg-autostart-generator` (confirmed present, never hand-parsed) turns the installed
   `/etc/xdg/autostart/privacyfence.desktop` into a transient `app-privacyfence@autostart.service`
   unit, gated behind that same target exactly the way a real desktop session's own autostart works
   today; starting the target is confirmed to actually launch the real packaged binary
   (`/proc/<pid>/exe` checked against the installed binary, not assumed), which then serves a real
   daemon/MCP/approval/audit round trip (Phase 3's own contract shape), and "Quit PrivacyFence"
   is confirmed to cleanly stop the real systemd unit, not just the process. A second test in the
   same module closes the "OAuth loopback browser-opening flow" half of this item's own "where
   practical" text for real too, under a genuine Xvfb `$DISPLAY`: unlike
   `tests/platform/test_browser_launch_default.py` (Phase 2.3), which proves `oauth_loopback.py`'s
   default path reaches `webbrowser.open` by monkeypatching that function itself, this test lets the
   stdlib `webbrowser` module's own real browser-detection-and-subprocess-launch logic run unmocked,
   via a real launched subprocess that performs the actual loopback HTTP round trip. The now-removed
   `linux-local-deb-packaging-plan.md` P7.2 is now marked closed accordingly. Scheduled on packaging-related
   `main` pushes, weekly, and on demand via its own workflow — deliberately kept out of both
   `tests.yml`'s per-PR jobs and `build.yml`'s tag-triggered release pipeline, since a flaky run in
   this tier (the flakiest and most expensive in this plan's whole taxonomy, per this phase's own
   objective) must never block an actual release.
2. ~~**Windows**: equivalent scenario on a Windows desktop VM/session — install, sign out/reboot, sign
   in, verify autostart, exercise the Phase 3 system contract. Add this as a new tracked item in
   the now-removed `windows-support-plan.md` if that document doesn't already have a home for it.~~ **Done** —
   `tests/integration/test_windows_graphical_session_autostart.py`, closing the now-removed `windows-support-plan.md`
   8.2 (that document already had a home for this item — it just needed converting from manual QA to
   automated CI). There's no physical sign-in to drive on a GitHub-hosted Windows runner, so this
   makes two deliberate substitutions rather than one hand-rolled workaround: a throwaway local
   account stands in for "someone signs in" (this test mints and knows its password, unlike the CI
   runner's own already-logged-on account, and `installer/privacyfence.iss`'s own `schtasks /create`
   call passes `/ru "BUILTIN\Users"`, the built-in group rather than one specific account, so the
   trigger fires for *any* interactive logon — the same "whichever account is at the keyboard" scope
   the macOS LaunchAgent and Linux XDG autostart already have, so a throwaway account is a valid
   stand-in, not a special case. This line originally omitted `/RU` entirely on the mistaken
   assumption that the unqualified default already meant "any user" — Microsoft's own documentation
   says the opposite, that it scopes the task to whichever account ran `schtasks /create`; this
   module's own first real run against a real Windows runner is what caught that, alongside the
   separate `/ri`/`/du` registration bug Phase 12's own verification pass found and fixed — see that
   phase's own status note and `platform-support.md`'s "Known open items"); and that throwaway
   account is added to local Administrators purely to satisfy
   this Windows Server base image's default "Log on locally" policy (denied to plain standard
   accounts), not to change how the daemon itself runs — the scheduled task's own `/rl limited`
   still governs that regardless of the account's own group membership. Everything else is the real,
   unmocked mechanism: a genuine interactive Windows logon (`LOGON32_LOGON_INTERACTIVE` via
   `CreateProcessWithLogonW`, driven through PowerShell's `Start-Process -Credential` — the same
   primitive `runas.exe` is built on, and the standard documented way to manually test an "At log on"
   Task Scheduler trigger without a physical sign-in), Task Scheduler's own real trigger evaluation of
   the real installed task, the real packaged `privacyfence-app.exe` alias it launches (confirmed
   running as the account that just logged on, via `Win32_Process`'s `GetOwner`, not assumed), a real
   daemon/MCP/approval/audit round trip against it (Phase 3's own contract shape, reusing
   `test_windows_packaged_smoke.py`'s own helpers), and "Quit PrivacyFence" confirmed to actually end
   that real process. Scheduled the same way as the Linux module — its own
   `.github/workflows/windows-graphical-session.yml`, packaging-related `main` pushes, weekly, and on
   demand, deliberately out of both `tests.yml`'s per-PR jobs and `build.yml`'s tag-triggered release
   pipeline.
3. **macOS**: do not build dedicated login-launch infrastructure unless the existing packaged smoke
   (Phase 6.1) proves insufficient in practice — the source strategy is explicit on this, and nothing
   found while grounding this plan suggests macOS autostart is currently a live gap.

### Exit criteria

Normal local-mode login/autostart is verified without owning physical Windows/Linux hardware. Met for
both Linux and Windows; item 3 (macOS) is exit criteria met by design, not left open — see its own
remaining-work entry above.

---

## Phase 8 — Org-mode system CI

### Objective

Treat org mode as its own deployment shape, provisioned and exercised from scratch, entirely in
automated Linux infrastructure.

### Status: done

`tests/integration/test_org_ubuntu_release_smoke.py` already did almost exactly what this phase
described, per its own docstring: `daemon_main.main()` end to end, a real synthetic Ed25519-signed
`org_config.json`, a real loopback mocked IdP (`tests/integration/mock_idp.py`), strict fail-closed
startup on a malformed/unsigned/incomplete bundle, reverse-proxy Host-header handling, org-only
route mounting, and per-principal session isolation — the single largest instance in this whole
plan of a proposed deliverable already substantially built. This phase closed the three gaps its
own "Remaining work" named:

1. **The four-scenario check.** Unauthenticated request rejected and authenticated MCP request
   resolving to the correct principal were already covered. Added: **app-level authz policy**
   (`TestAppLevelAuthzPolicy`, its own daemon since `authz.allowed_domains` is fixed at startup) —
   an allowed-domain principal signs in, one outside every allowed domain is turned away with no
   session; **an approval exercised with audit-principal correctness**
   (`test_an_approval_is_exercised_by_the_correct_principal_and_audited_there`) — a real MCP
   `privacyfence_propose_auto_accept_rule_change` call blocks on a human confirmation the way a
   gated tool's popup does, a different principal can't decide it, the right one can, and the
   resulting audit entry lands under that principal's own per-principal log directory; and
   **persisted state surviving a restart** (`test_persisted_state_survives_a_restart`) — a
   confirmed rule and its audit trail (byte-identical up to that point, then longer) both outlive a
   SIGTERM/restart cycle. Grounding these against the real subprocess (not an in-process shortcut)
   surfaced two real, previously-uncaught bugs, both fixed as part of this phase:
   `gate._run_in_popup_executor` ran its callable in a bare thread-pool thread with no `contextvars`
   propagation, so a confirmation dialog registered with no pre-registered `PendingApproval`
   (`show_rule_confirmation_popup`, `show_pii_confirmation_popup` — the main gated-call popup path
   was unaffected, since it always pre-registers before this function is ever called) silently
   attributed itself to the wrong principal and could never be decided by anyone; and neither
   `McpDispatcher.propose_rule_change` nor `.list_rules` forced their principal's
   `ConnectorRegistry` entry (and the `auto_accept.init_config_path()` side effect of building it)
   to exist first, so either raised "auto_accept config path not initialized" if called as a
   principal's very first MCP interaction. Both single-principal-only in-process tests could never
   have caught, by construction.
2. **CI promotion.** This test was dispatch/tag-only (`build.yml`'s `build-deb` job), the same gap
   `test-windows` had before Phase 2.1 — promoted the same way, as its own permanent `org-mode-smoke`
   job in `tests.yml`, on every PR (Ubuntu only; `build-deb` still runs it too, unchanged, at release
   time).
3. **Readiness doc.** `org-mode-operational-readiness.md`'s "Automated evidence" section now cites
   this module by name for each claim it backs, instead of a generic pointer to "broader unit/
   security tests."

### Exit criteria (met)

Org mode can be provisioned and exercised from scratch entirely in automated Linux CI, with no real
Google/Microsoft identity login required for routine coverage.

---

## Phase 9 — Rewrite release QA around automation

### Objective

Shrink manual release validation to minutes, now that Phases 0–8 have replaced most of what
the now-removed `manual-pre-release-test-plan.md` and `connector-qa-testing.md` currently ask a human to do by hand.

### Status: done

1. **the now-removed `manual-pre-release-test-plan.md`**: rewritten from a five-section, half-day walkthrough into a
   three-section, minutes-long checklist — §1 "Automated prerequisites" (confirm `tests.yml`'s
   merge-gate jobs and `connector-live-check.yml`'s scheduled run are green/recent, no unresolved
   `chore/connector-live-fixture-drift` PR, no open gate/auto-accept/approval-UI PR that skipped the
   required full `connector-qa-testing.md` pass, nothing known-broken in `build.yml`'s
   packaged-artifact path), §2 "Human QA" (visual UI sanity if the web surface changed, one real
   MCP-client compatibility smoke — unconditional, since no automated test in this repo drives a
   real third-party MCP client — OS-native UX smoke if packaging/autostart changed, a drift-PR
   re-skim), and §3 "Tag and release" (unchanged release mechanics). The old §0's
   `pre_release_check.py` step is gone outright, not just folded — that script's own docstring
   already dropped its version-consistency check once `setuptools_scm` replaced the hand-bumped
   scheme (see this repo's `CLAUDE.md`), so by this phase it reran nothing the merge gate hadn't
   already run on the same commit; §1 points at confirming that merge gate directly instead. The old
   §1 fixture-recording walkthrough folded into a single §1 bullet, since Phase 1's scheduled
   recorder (`connector-live-check.yml`) already does this weekly. The old §3 live-Cowork prompt
   folded away entirely — Phase 5 confirmed `test_gate.py` already proves gate-state coverage
   deterministically, so re-proving it by hand added nothing; what survives from that section is
   only the genuinely unautomatable part (a real MCP client's compatibility), generalized rather than
   run as a connector-specific popup script.
2. **`connector-qa-testing.md`**: reframed. Its title now parenthesizes "Extended Connector/Gate
   Exploratory QA," and it opens with a "When to use this" section stating outright that routine
   releases need none of it, naming the four cases that do (new connector, material connector/gate
   change, unexplained regression, or the broad gate/auto-accept/approval-UI change
   `testing-policy.md` §3 already required this for).
3. **`testing-policy.md`**: consistency pass done. The "Checked against the now-removed `manual-pre-release-test-plan.md`"
   section (Phase 0) now maps each *old* section to where its coverage lives post-rewrite instead of
   describing a still-pending rewrite; §3's closing paragraph and the Quick-reference table's
   `connector-qa-testing.md` row both dropped their "before a release" framing in favor of the same
   four trigger conditions as item 2 above.

### Exit criteria (met)

Routine manual release validation takes minutes, not hours; both documents above accurately
describe a *reduced*, not aspirational, manual surface.

---

## Phase 10 — CI observability and maintenance controls

### Objective

Make test failures diagnosable entirely from cloud CI, since this project is developed
cloud-first without dedicated physical test machines.

### Status: done

New shared module `tests/diagnostics.py`, wired in via `tests/conftest.py`'s own
`pytest_runtest_makereport` hook (moved up from `tests/integration/conftest.py`, which this phase
deleted, to cover `tests/system/` too — see that hook's own comment). On a failing
`pytest.mark.packaged`/`pytest.mark.system` test that took `tmp_path` (directly, or indirectly via
a fixture it depends on — confirmed empirically: `item.funcargs` carries it either way), the hook
writes, under `test-results/<suite>/<nodeid>/`:

- `environment.txt` — OS/runtime versions and a test-run identifier (`GITHUB_RUN_ID`-
  `GITHUB_RUN_ATTEMPT` in CI, a random id locally);
- `manifest.txt` — a flat `<relative path>\t<size>` listing of everything under that test's own
  `tmp_path`, covering item 1's "installed-file manifest" for every module whose install
  directory already lives there (`test_windows_packaged_smoke.py`, `test_org_ubuntu_release_
  smoke.py`, `tests/system/test_local_mode_system.py`) *without* also re-uploading the installed
  binaries themselves as a CI artifact (a listing, never a copy — see that function's own
  docstring for why: a PyInstaller onedir build or an extracted `.deb` easily runs to hundreds of
  MB);
- `logs/` — every file under that `tmp_path` named `daemon.log`, `install*.log`, `uninstall*.log`,
  or `*.jsonl` (an audit log), copied out in full and flattened by path so two same-named logs
  from the same test (e.g. an upgrade test's N and N+1 boots) don't collide.

Nothing is written on a passing run — matching the posture `test_browser_smoke.py`'s own Phase 4.5
`_capture_failure_artifacts` fixture already established for the browser suite, which this phase
deliberately left untouched (its own screenshot/DOM/console capture already satisfies item 1's
"browser console... reuse Phase 4's existing capture"). The hook also appends a report section
naming exactly where the diagnostics landed — CI's own failure output now answers item 2's "where
its diagnostic artifacts landed" directly, not just "what failed" (pytest's own assertion-rewriting
already gives the expected-vs-actual half of that for every plain `assert`, and every module this
phase touches already embeds it explicitly wherever it raises `AssertionError` by hand).

This generic, per-`tmp_path` mechanism needed no per-test-module code for
`test_windows_packaged_smoke.py`, `test_org_ubuntu_release_smoke.py`, and `tests/system/
test_local_mode_system.py` — all three already isolated their own daemon home/install directory
under `tmp_path` and already named their subprocess log `daemon.log`/`install*.log`.
`test_macos_packaged_smoke.py`'s `running_packaged_daemon` fixture was switched from a bare
`tempfile.mkdtemp()` + manual `shutil.rmtree()` to `tmp_path` for exactly this reason (matching
`test_macos_upgrade_preserves_user_state`'s own `home`, which already used `tmp_path`), and its
`_running_daemon_at` helper now redirects the daemon's stdout/stderr straight to a `daemon.log`
file instead of an in-memory buffer read only from the exception message itself.

Three modules needed their own small, explicit capture call instead, because their installed/
runtime state genuinely isn't under any single test's `tmp_path`:

- `test_deb_packaged_lifecycle.py` installs via a real `dpkg -i` into real system paths — its
  `_clean_package_state` fixture now calls `dpkg -s`/`dpkg -L` on a failing test, before its own
  teardown purge, and writes both into the same `test-results/` directory (via `tests.diagnostics
  .failure_dir()`/`suite_name_for()`, so a module needing this doesn't have to recompute that path
  by hand).
- `test_linux_graphical_session_autostart.py`'s own `_real_home_state` fixture deliberately boots
  into the *real* `$HOME` (see that fixture's own docstring — the whole point of that test is a
  real, un-injected login), and its daemon is started by a real `systemd --user` unit, which logs
  to the user's own journal, not a file — its teardown now writes a manifest of the real
  `$HOME/.privacyfence` plus a `journalctl --user -u <unit>` dump.
- `test_windows_graphical_session_autostart.py`'s daemon boots into a real throwaway Windows
  user's own profile directory (not known until the test body calls `_wait_for_profile_dir`,
  and a different profile than the test's own `tmp_path`) via a Scheduled Task, which likewise has
  no stdout log file of its own — a small `_graphical_diagnostics` fixture (a mutable dict the test
  registers `home` into once known, then reads back from teardown — the same "register, then
  capture from teardown" shape as `test_deb_packaged_lifecycle.py`'s own pair above) captures a
  manifest of that profile's `.privacyfence` directory plus a `schtasks /query ... /v` dump of the
  task's own last-run result, before `_throwaway_user`'s own finalizer deletes the account and its
  profile (confirmed, not assumed: pytest fixture teardown order is the reverse of setup order, and
  this fixture is requested after `_throwaway_user` in the test's own signature).

Every CI job that runs one of these suites (`tests.yml`'s `test`, `test-python-compat`,
`platform-windows`, `platform-macos`, `org-mode-smoke`; `build.yml`'s `build`, `build-windows`,
`build-deb`; `linux-graphical-session.yml`, `windows-graphical-session.yml`) now has its own
`if: failure()` / `if-no-files-found: ignore` / `retention-days: 30` upload step for `test-results/`
— the same bounded-retention convention `connector-live-check.yml`'s own report upload already
established (item 1's "bounded retention... matching the existing pattern"), and the same posture
`tests.yml`'s pre-existing browser-smoke upload step already had (now widened to cover the whole
tree rather than just `test-results/browser-smoke/`, since both live under the same root and one
step covers both). `tests.yml`'s `test-python-compat` matrix folds its own Python version into the
artifact name, since `actions/upload-artifact` needs a unique name per matrix leg in the same job.

Known, deliberately accepted gap: `test_macos_packaged_smoke.py`'s `installed_app`/`signed_app_
copy` fixtures (the mounted/copied `.app` bundle itself, as opposed to the daemon's own runtime
`home`) stay on `tempfile.mkdtemp()`, module-scoped and shared across several tests in that file —
not one test's own `tmp_path`, so a failure there doesn't get an installed-file manifest of the
`.app` bundle itself the way Windows/`.deb`/org-mode's own install directories do. Revisit only if
a real failure in that fixture ever turns out to need one; the daemon's own `home`/`daemon.log`
(what actually varies at runtime, and what every other failure in this module needs) is already
fully covered.

Item 3 needed no code change: nothing in this repo's CI auto-retries a deterministic test today —
`scripts/qa_fixture_recorder.py`'s own narrowly-scoped retry/backoff (rate limiting, transient
network failure) is, and remains, the only retry logic anywhere in this test suite, confirmed by
grep across every workflow file rather than assumed.

### Exit criteria (met)

Most CI failures are diagnosable without local reproduction.

---

## Phase 11 — Update branch-protection required status checks

### Objective

Keep GitHub's required-status-checks list (Settings → Branches, the branch protection rule on
`main`) in step with which jobs in `.github/workflows/tests.yml` actually run, and are actually
trustworthy, on every PR — so a job this plan promotes to per-PR (Phase 2's `platform-windows`, its
`platform-macos` sibling, `test-python-compat`, the org-mode job from Phase 8) can't go red and
still let a PR merge. This was a real, open gap: `testing-policy.md` stated plainly, "This `test`
job is the one a PR needs to pass to merge" — singular — and nothing landed by Phase 2.1's
promotion of `platform-windows` (or by any later phase) had updated that setting or that sentence
to match.

### Status note (2026-09-12)

The *policy* — the target required-check set, made explicit and reviewable — and the
*documentation* consistency pass (remaining-work items 2 and 3 below) are done. Actually flipping
the GitHub Settings → Branches toggle (item 1) and then confirming enforcement with a deliberately
red scratch branch (item 4) are not: this plan's own automation has no GitHub credential scoped to
branch-protection administration (that's a repo-admin action on GitHub itself, not something a
commit or a PR merge can carry out), so those two steps are the explicit hand-off to a maintainer
described below, not skipped work.

### Already in this repo

- `scripts/update_branch_protection.py` is the reviewable, applied-the-same-way-every-time record
  of the target required-status-checks set — `REQUIRED_STATUS_CHECKS` names exactly `test`,
  `platform-windows`, `platform-macos`, both `test-python-compat` matrix legs ("Test (Python 3.11,
  core suite)" / "Test (Python 3.12, core suite)"), `static-analysis`, and `org-mode-smoke`, with
  its own comment explaining why the Phase 6/7 packaged-artifact and graphical-session jobs are
  deliberately excluded (they run in `build.yml` / their own scheduled workflows, never on
  `pull_request`, so they can never be a per-PR required check). `show` prints the live diff against
  a repo's actual setting; `apply` (optionally `--dry-run` first) sets it. This is the mechanism
  remaining-work item 1 below hands to a maintainer, and the same file to edit, in the PR that adds
  or renames a job, the next time this list needs to change.
- `testing-policy.md`'s §1 names the full six-job required set (with the `test-python-compat`
  matrix and `static-analysis`'s blocking-`ruff`-only nature called out explicitly) instead of the
  old singular "`test` job" sentence, and its Quick-reference table has rows for
  `test-python-compat` and `static-analysis` (splitting `ruff check .`, which gates, from
  `mypy`/`bandit`, which don't).
- Confirms remaining-work item 2 below by inspection rather than a live GitHub trial: reading
  `.github/workflows/tests.yml`'s `static-analysis` job shows `mypy`/`bandit` each carry their own
  `continue-on-error: true`, which is GitHub Actions' documented mechanism for letting a step fail
  without changing the job's own conclusion — and a job's conclusion, not any individual step, is
  what a required status check evaluates (Actions reports one check run per job to the Checks API).
  Requiring the `static-analysis` job is therefore already exactly "require `ruff check .`," with no
  need to split it into a separate blocking-only job.
- Confirmed, while doing this audit, that `testing-policy.md` had never documented
  `test-python-compat` or `static-analysis` at all (not just under-scoped required-check language) —
  both are now covered in §1's prose and the Quick-reference table alongside the required-check fix.

### Remaining work

1. A repo admin runs `python scripts/update_branch_protection.py show` against the live repo to see
   the current vs. target diff, then `apply` (a `GITHUB_TOKEN` with branch-protection admin rights
   is required; see the script's own docstring) to actually update `main`'s branch protection rule
   to the target set above. This is the one step here that has to happen outside of a commit/PR.
2. ~~Confirm required-check granularity~~ — done above, by inspection.
3. ~~Update `testing-policy.md`~~ — done above.
4. After item 1 is applied, confirm enforcement rather than trusting the setting alone: push a
   scratch branch with a deliberately failing test in one of the newly-required jobs and confirm
   GitHub actually blocks that PR from merging.

### Exit criteria

Every job in `tests.yml` that runs on every PR and is meant to gate correctness (`test`,
`platform-windows`, `platform-macos`, both `test-python-compat` matrix legs, `org-mode-smoke`,
`static-analysis`'s blocking `ruff` step) is a required status check on `main`'s branch protection
rule (pending remaining-work item 1); `testing-policy.md` names the real required set instead of
"the `test` job" (done); a deliberately red job on one of those checks has been confirmed, not
assumed, to block merge (pending remaining-work item 4, which depends on item 1).

---

## Phase 12 — Retire the platform-specific plan docs

### Objective

Return to one active plan doc under `docs/`, as this document's own docs-audit pass intended
before `windows-support-plan.md`, `windows-linux-support-plan.md`, `linux-local-deb-packaging-
plan.md`, and `manual-pre-release-test-plan.md` all had to be restored because they still tracked
open work `main` had landed against them.

### Status: done

Verified against the current source tree and live CI rather than trusting each of the four
documents' own checkbox state, which turned out to be stale in both directions: several genuinely-
landed items were never checked off in their own source document (`portalocker`, the Windows
installer's `schtasks` registration, the README's Windows/Linux install sections), while two real,
still-open items existed that this phase's original "every open item is already tracked above"
claim did not actually account for:

- **Windows**: closing [privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121)
  stays gated on a real signed Windows release actually shipping (none has, since this packaging
  work landed), plus the inherently-manual Windows QA items (a real installer run, OAuth loopback
  through the installed app, crash-restart, a clean uninstall, `.mcpb` in a real Claude Desktop) —
  `TECHNICAL_REFERENCE.md`'s missing Windows section is fixed as part of this same pass, see below.
  A new finding from this verification pass, not previously known: `windows-graphical-session.yml`
  was not green — the installed Task Scheduler autostart task was missing immediately after a real
  silent install reporting success, on every real run to date, including the run against `main`
  after PR #315's own fix (which addressed a different bug in the same test module). **Two
  independent real root causes found and fixed, not just theorized, each confirmed by an actual
  `workflow_dispatch` run rather than assumed**: the `PrivilegesRequired=lowest`/non-elevation guess
  this note originally carried was wrong on both counts.
  1. `installer/privacyfence.iss`'s `schtasks /create` call passed `/ri 1 /du 9999:59`, trying to
     get crash-restart behavior out of plain CLI flags that Microsoft documents as "not applicable"
     to an `ONLOGON` schedule — `schtasks.exe` rejected the whole call outright, silently, since an
     Inno `[Run]` entry's nonzero exit code doesn't abort Setup by default. Fixed by dropping the
     invalid flags. This does not restore crash-restart behavior, which was never actually working —
     tracked as the new Phase 13 below.
  2. Once that fix let registration itself succeed for the first time, the next real run exposed a
     second bug: the same `schtasks /create` call omitted `/RU` entirely, on the mistaken assumption
     (this document's own text included) that Microsoft's unqualified default for `/SC ONLOGON`
     already meant "any interactive logon." It doesn't — per Microsoft's own documentation, omitting
     `/RU` scopes the task to whichever account ran `schtasks /create`, i.e. the installing account
     only. The real test run caught this directly: registration succeeded, but the throwaway
     account's logon never fired the trigger within 30s. Fixed by adding `/ru "BUILTIN\Users"`, the
     standard technique for "any interactive logon, in that user's own session."
- **Linux**: a real end-to-end org-mode run against a live Ubuntu server with a real identity
  provider and a real live connector remains undone, distinct from `org-mode-smoke`'s synthetic,
  mocked-IdP CI coverage (Phase 8).

Neither is phase-shaped implementation work belonging in a plan document, so both are migrated into
[`platform-support.md`](platform-support.md)'s new "Known open items" section instead of left to
disappear with the retired docs. `manual-pre-release-test-plan.md` needed no such migration —
`release-testing.md` already stands alone as its evergreen replacement, confirmed by reading it,
exactly matching this phase's original design.

All four documents are deleted; `docs/README.md`'s doc index and "active implementation plans" note
drop the four retired entries; every cross-reference to the four documents elsewhere in the repo —
this file included — is converted to a plain "now-removed" citation, the same convention Phase
1.10's removal of the completed remediation plan and `connector-ci-integration-plan.md` already
established, rather than left as a link to a deleted file.

### Exit criteria (met)

`docs/` contains exactly one `*plan*.md`: this document.

---

## Phase 13 — Windows Task Scheduler real crash-restart-on-failure

### Objective

Give the installed Windows autostart task real crash-restart behavior — parity with the macOS
LaunchAgent's `KeepAlive`/`SuccessfulExit=false` and the Linux `.deb`'s systemd `Restart=on-failure`,
which Phase 3's original design intent wanted but never actually shipped.

### Already in this repo

Phase 12's own verification pass found and fixed two independent reasons
`windows-graphical-session.yml` was red on every run — see that phase's own status note for the
detail on both. Neither fix restores restart-on-failure, which is what this phase is for: the task
now registers correctly and fires for any interactive logon, but it is logon-triggered only — no
restart-on-failure of any kind, since there was never a working implementation of it to begin with.
`platform-support.md`'s "Known open items" and `TECHNICAL_REFERENCE.md`'s "Windows" subsection both
already name this as a known, expected-to-fail check.

### Remaining work

1. Author a Task Scheduler task-definition XML (`<Triggers><LogonTrigger>` with no `<UserId>`, same
   "any interactive logon" scope the current flag-based registration has; `<Principals><Principal>`
   with `<RunLevel>LeastPrivilege</RunLevel>`, matching today's `/rl limited`; `<Settings>` with
   `<RestartOnFailure><Interval>PT1M</Interval><Count>...</Count></RestartOnFailure>`) with a
   placeholder for the installed `privacyfence-app.exe` path.
2. Wire it into `installer/privacyfence.iss`'s `[Code]` section: extract the template at install time
   (`[Files]` with `Flags: dontcopy`), substitute the real `{app}` path, and register it via
   `schtasks /create /tn "PrivacyFence" /xml <file> /f` from a `CurStepChanged(ssPostInstall)` hook,
   replacing the current plain `[Run]` entry.
3. **Get the task-definition XML's file encoding right.** `schtasks /create /xml` is documented to
   require the file to actually be UTF-16-encoded, not just UTF-8 text with a matching `encoding=`
   declaration — Inno Setup's stock Pascal-Script `SaveStringToFile` writes plain (ANSI/UTF-8) text,
   not UTF-16, so this needs either a real UTF-16 write path in the `[Code]` section or writing the
   template's placeholder-substitution step differently (e.g. baking the real path in at build time
   in `scripts/build_installer.ps1`, which already has real PowerShell available to write a correctly
   -encoded file, rather than doing the substitution in Pascal Script at all). This is exactly the
   kind of byte-level detail that needs a real Windows host or a `workflow_dispatch` run to get
   right — don't guess at it from a Linux dev loop, the same lesson Phase 12's own fix drew from
   getting the `/ri`/`/du` root cause wrong on the first pass.
4. Extend `tests/integration/test_windows_graphical_session_autostart.py` (or
   `test_windows_packaged_smoke.py`) with a real crash-restart assertion: start the installed daemon,
   kill its process, wait, and confirm Task Scheduler actually relaunched it.
5. Validate via `workflow_dispatch` on `windows-graphical-session.yml` before merging, same as every
   other Windows-flag change in this plan — this tier has already shown once that a flag/XML syntax
   assumption can silently break autostart registration entirely.

### Exit criteria

The installed Windows autostart task actually restarts the daemon after a crash, proven by an
automated CI test (not just a manual QA step), with the fix's own correctness confirmed on a real
`windows-latest` runner before merging, not assumed from documentation alone.

---

## Revised sequencing

```
Phase 0  Taxonomy / doc foundation                               (DONE — testing-policy.md's
   ↓                                                               seven-layer section + ownership
   ↓                                                               table, pyproject.toml markers)
Phase 1  Live connector CI + Security Remediation 3.12 closure   (DONE — PR #283/#278/#284;
   ↓                                                               1.8/1.9 also done as a follow-up;
   ↓                                                               Apps Script fixture still open)
Phase 2  Cross-platform core CI                                  (DONE — Windows job promoted/
   ↓                                                               renamed, macOS job added,
   ↓                                                               tests/platform/ suite + marker;
   ↓                                                               2.4 closed by decision (keep the
   ↓                                                               full suite, don't narrow it))
Phase 3  Canonical cross-platform system test                    (DONE — tests/system/test_local_
   ↓                                                               mode_system.py, collected by every
   ↓                                                               job that runs the full suite, no
   ↓                                                               new CI wiring needed)
Phase 4  Browser/UI automation                                   (DONE — extended existing
   ↓                                                               test_browser_smoke.py, not a
   ↓                                                               new file; found/fixed a real
   ↓                                                               phone-viewport overflow bug;
   ↓                                                               new failure-artifact capture)
Phase 5  Gate/policy matrix                                       (DONE — audit of the existing
   ↓                                                               2,600-line test_gate.py found and
   ↓                                                               closed two narrow gaps; one matrix
   ↓                                                               item confirmed to live in the
   ↓                                                               connector tests instead)
Phase 6  Packaged-artifact lifecycle                              (DONE — macOS gap-closing, Linux
   ↓                                                               .deb lifecycle, Windows installer
   ↓                                                               smoke, macOS/Windows upgrade tests
   ↓                                                               (item 20), and publish-pypi.yml's
   ↓                                                               cross-workflow release gating (6.4))
Phase 7  Graphical-session/autostart                              (Done for both Linux (P7.2) and
   ↓                                                               Windows (8.2); macOS deliberately
   ↓                                                               not built.)
Phase 8  Org-mode system CI                                       (DONE — audit/extend of
   ↓                                                               test_org_ubuntu_release_smoke.py,
   ↓                                                               promoted to a permanent per-PR job)
Phase 9  Retire obsolete manual QA                                (DONE — manual-pre-release-
   ↓                                                                test-plan.md rewritten to a
   ↓                                                                minutes-long checklist,
   ↓                                                                connector-qa-testing.md reframed
   ↓                                                                as exploratory-only, testing-
   ↓                                                                policy.md consistency pass)
Phase 10 Observability and maintenance polish                    (DONE — tests/diagnostics.py's
   ↓                                                               generic per-tmp_path capture, wired
   ↓                                                               into every packaged/system CI job)
Phase 11 Update branch-protection required checks                (policy/script/docs DONE — the
   ↓                                                               live GitHub setting itself is a
   ↓                                                               repo-admin hand-off, see Phase
   ↓                                                               11's own status note)
Phase 12 Retire the platform-specific plan docs                  (DONE — all four docs deleted;
   ↓                                                                their two still-real open items
   ↓                                                                (Windows issue #121 gating +
   ↓                                                                QA, a live Linux org-mode run)
   ↓                                                                live in platform-support.md now;
   ↓                                                                the verification pass also found
   ↓                                                                and fixed a real regression --
   ↓                                                                see Phase 13 below)
Phase 13 Windows Task Scheduler real crash-restart-on-failure    (new, found by Phase 12's own
                                                                    verification pass -- not yet built)
```

Phases 4 and 5 may proceed in parallel once Phase 3 is stable, as in the source strategy. Phase 7
stays last for the same infrastructure-cost reason the source strategy gives. Phase 11 landed once
Phases 2, 3, 6, 7, and 8 (the jobs its target required-check set names) were all done — its own
status note explains why the GitHub-side toggle itself is a separate repo-admin hand-off rather
than something this PR's own merge can complete. Phase 12 was meant to stay last, deleting docs only
once every phase above it had actually shipped — and did, except that its own verification pass
found a real, previously-unknown regression (Windows autostart registration silently broken) and
fixed it, which is where Phase 13 came from: not a residual gap in the original plan, but new scope
this pass's own audit surfaced.

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
11. ~~Windows permanent portability CI~~ — **done** (Phase 2.1), rename/promote only
12. ~~macOS portability CI + targeted platform suite~~ — **done** (Phase 2.2–2.3): new
    `platform-macos` job, `tests/platform/` directory, `platform` pytest marker. Narrowing
    `platform-windows`/`platform-macos` down to that suite (Phase 2.4) is also done, but as a
    decision *not* to narrow — see Phase 2's own status note.
13. ~~Cross-platform daemon/MCP/approval/audit test~~ — **done** (Phase 3):
    `tests/system/test_local_mode_system.py`, a real spawned daemon process reusing Phase 2.3's own
    spawn pattern and test_deferred_approval_round_trip.py's deferred-approval protocol shape.
14. ~~Browser approval-flow coverage gaps: "Always allow," multi-card, idempotency~~ — **done**,
    PR #298 (Phase 4.1)
15. ~~Browser PII/responsive/light-dark coverage + failure-artifact capture~~ — **done** (Phase
    4.2–4.5): found and fixed a real phone-viewport overflow bug in the process (see Phase 4's own
    status note)
16. ~~Gate matrix audit + close any real gap found~~ — **done** (Phase 5): two narrow gaps closed
    in `test_gate.py`; the matrix was already mostly there, as expected
17. ~~Linux packaged lifecycle~~ — **done** (Phase 6.3): automated the already-manually-proven
    P7.1/P7.3, `test_deb_packaged_lifecycle.py`, wired into `build.yml`'s `build-deb` job
18. ~~Windows packaged lifecycle~~ — **done** (Phase 6.2): new `test_windows_packaged_smoke.py`,
    wired into `build.yml`'s `build-windows` job
19. ~~macOS packaged additions~~ — **done** (Phase 6.1): closed the two gaps this phase's audit
    found (signature/notarization, state-outside-package)
20. ~~Package upgrade/state-preservation testing, where not already covered by 17–19~~ — **done**:
    macOS/Windows upgrade tests (Linux's own already landed inside 6.3/item 17 above), plus 6.4's
    `publish-pypi.yml` cross-workflow release gating, landed together in one PR — see Phase 6's own
    "Upgrade/state-preservation testing (item 20)" and "6.4 Release gating" subsections
21. ~~Linux graphical-session/autostart CI~~ — **done** (Phase 7 item 1): closes the already-tracked
    P7.2, `tests/integration/test_linux_graphical_session_autostart.py`, its own
    `.github/workflows/linux-graphical-session.yml`
22. ~~Windows graphical-session/autostart CI~~ — **done** (Phase 7 item 2): closes
    the now-removed `windows-support-plan.md` 8.2, `tests/integration/test_windows_graphical_session_autostart.py`,
    its own `.github/workflows/windows-graphical-session.yml`
23. ~~Org-mode system test audit/extension~~ — **done** (Phase 8): closed the four-scenario gap,
    promoted `test_org_ubuntu_release_smoke.py` to a permanent `org-mode-smoke` per-PR job, and
    updated `org-mode-operational-readiness.md`
24. ~~Manual QA documentation reduction~~ — **done** (Phase 9): the now-removed `manual-pre-release-test-plan.md`
    rewritten to a three-section checklist, `connector-qa-testing.md` reframed as exploratory-only,
    `testing-policy.md` consistency pass
25. ~~CI diagnostic/observability polish~~ — **done** (Phase 10): `tests/diagnostics.py`'s generic
    per-`tmp_path` failure capture, wired in via `tests/conftest.py`, plus the three modules
    (`test_deb_packaged_lifecycle.py`, both graphical-session-autostart modules) that needed their
    own explicit capture call for real installed/runtime state no `tmp_path` isolates
26. ~~Update branch-protection required status checks~~ — **done** (Phase 11):
    `scripts/update_branch_protection.py` names and applies the target required-check set,
    `testing-policy.md` names the real required set instead of "the `test` job." Actually running
    `apply` against the live repo, and the enforcement check that follows it, are a repo-admin
    hand-off outside of what a PR merge can do — see Phase 11's own status note.
27. ~~Retire `windows-support-plan.md`, `windows-linux-support-plan.md`,
    `linux-local-deb-packaging-plan.md`, and `manual-pre-release-test-plan.md`~~ — **done** (Phase
    12) — last PR in this plan

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
  `workflow_dispatch` (Phase 2, fully done — 2.4 closed by the decision to keep the full suite on
  both jobs rather than narrow it, per that phase's own status note).
- A canonical daemon/MCP/approval/audit scenario passes on all three desktop platforms (Phase 3,
  done).
- Browser behavior is tested automatically against real Chromium, covering PII, responsive, and
  light/dark surfaces, not just the approval round trip already covered (Phase 4, done).
- Every connector is periodically exercised against dedicated QA accounts (Phase 1, done for ten of
  eleven — Apps Script fixture coverage is the one open item).
- Provider API drift is detected automatically and produces a reviewable PR (Phase 1, done).
- The Security & Quality Remediation Plan's Phase 3.12 is complete and the overall plan is closed
  (Phase 1, done — the plan document itself was removed from `docs/` rather than left
  marked-complete in place).
- Every published DMG/EXE/DEB is exercised before publication, and a failed packaged-artifact test
  blocks publication on every channel this plan publishes to, not just its own platform's artifact
  (Phase 6, done).
- Package upgrade tests prove user state survives, on all three platforms (Phase 6, done).
- Local-mode autostart has automated platform-specific coverage (Phase 7, done for Linux and Windows;
  macOS deliberately not built).
- Org mode executes an authenticated synthetic end-to-end request in CI, on every PR (Phase 8,
  done).
- `connector-qa-testing.md` is exploratory, not mandatory, for routine releases (Phase 9, done).
- Routine manual release validation takes minutes, not hours (Phase 9, done).
- A system/packaged-artifact test failure is diagnosable from its own CI run alone — daemon/install/
  audit logs, an installed-file manifest, OS/runtime versions, and a test-run identifier, all
  uploaded as a bounded-retention build artifact — without re-running it locally (Phase 10, done).
- PrivacyFence can be confidently released without owning physical Windows, Linux, or macOS
  development machines.
- GitHub's required-status-checks list on `main` names every blocking per-PR job, not just `test` —
  a red `platform-windows`/`platform-macos`/`test-python-compat`/`org-mode-smoke`/`static-analysis`
  run actually blocks merge, confirmed rather than assumed (Phase 11's target set is defined and
  scripted; applying it live and confirming enforcement is the repo-admin hand-off its status note
  describes).
- ✅ `docs/` contains exactly one `*plan*.md` — this document. `windows-support-plan.md`,
  `windows-linux-support-plan.md`, `linux-local-deb-packaging-plan.md`, and
  `manual-pre-release-test-plan.md` are retired (Phase 12); their still-real open items live in
  `platform-support.md`'s "Known open items" instead.
- The Windows autostart task actually survives a daemon crash, the same crash-restart parity Windows
  has had on every other platform's own autostart mechanism from the start (Phase 13, new, not yet
  done).
