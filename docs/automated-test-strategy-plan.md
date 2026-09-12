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
repo before. Phases 5–10 are unaffected by any of these merges and reflect this plan's original
grounding pass.
Phase 11 (update branch-protection required checks) is new in this revision — added once this plan
was checked against `testing-policy.md`'s own "one job to merge" language and found not to close
that gap anywhere — and Phase 12 is the renumbered "retire the platform-specific plan docs" phase
(previously Phase 11), pushed one slot later so doc retirement stays the true last step.

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
block promotion on it — so the full-suite version landed first. `windows-support-plan.md`'s own
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
over: the already-known-and-accepted POSIX file-permission gap (`windows-linux-support-plan.md`
Track B3), and a new finding — POSIX-style path strings (`"credentials/telegram.session"`,
a `"/tmp"` destination_dir) colliding with `ntpath`'s `os.path.join()`/`os.path.isabs()`, which in
one case (`daemon_main._resolve_path`) silently resolves to a different on-disk location entirely
on Python 3.13/Windows, not just a cosmetic separator mismatch. See `windows-support-plan.md`
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
  is a known, already-tracked open design question (`windows-support-plan.md` Phase 6.3's "new
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

## Phase 11 — Update branch-protection required status checks

### Objective

Keep GitHub's required-status-checks list (Settings → Branches, the branch protection rule on
`main`) in step with which jobs in `.github/workflows/tests.yml` actually run, and are actually
trustworthy, on every PR — so a job this plan promotes to per-PR (Phase 2's `platform-windows`, its
`platform-macos` sibling, `test-python-compat`, the new system/packaged/org-mode jobs from Phases 3,
6, 7, 8) can't go red and still let a PR merge. This is a real, currently-open gap, not a
hypothetical one: `testing-policy.md:141` already states plainly, "This `test` job is the one a PR
needs to pass to merge" — singular — and nothing landed by Phase 2.1's promotion of
`platform-windows` (or by any later phase) has updated that setting or that sentence to match.

### Already in this repo

- Only the `test` job (ubuntu-latest: pytest + coverage floor + `npm test` + `npm run typecheck`) is
  a required status check today, per `testing-policy.md`'s §1 and its Quick-reference table (only
  those four checks are marked "this is the merge gate" there).
- `platform-windows` now runs on every PR (Phase 2.1, done) but is not required — a PR can merge
  with it red.
- `test-python-compat` and `static-analysis` (`ruff check .`, blocking; `mypy`/`bandit`, deliberately
  `continue-on-error`, informational) also run on every PR and are also not required.
- The branch protection rule itself is GitHub repo configuration, not a file this repo tracks — there
  is no commit history or diff to inspect for it, which is exactly why it's easy for it to silently
  fall behind the workflow file as new jobs get added. This phase exists to make that catch-up an
  explicit, named step instead of an implied one.

### Remaining work

1. As each per-PR job lands and proves itself stable (i.e. no flaky-red history over a normal
   run of PRs, not just one green run) — `platform-windows` now, `platform-macos` once Phase 2.2
   lands, `test-python-compat`, the canonical system test once Phase 3 lands it on all three OSes,
   the packaged-artifact jobs from Phase 6, the org-mode job from Phase 8 — add it to `main`'s
   required-status-checks list. Do this incrementally, alongside the phase that introduces the job,
   rather than batching every addition into this phase's own single PR: this phase's own scope is
   the *policy* (require every blocking per-PR job) and the final consistency pass, not re-doing the
   stability wait each earlier phase already did before its job was safe to gate on.
2. If GitHub's required-check granularity turns out to be job-level rather than step-level, requiring
   `static-analysis` would also require its still-informational `mypy`/`bandit` steps (they use
   `continue-on-error`, which keeps the *job* green even when they fail — so requiring the job is
   safe as-is). Confirm that behavior rather than assuming it; only split `static-analysis` into a
   separate blocking-only job if `continue-on-error` turns out not to isolate them the way intended.
3. Update `testing-policy.md`'s §1 "This `test` job is the one a PR needs to pass to merge" sentence
   and its Quick-reference table (the "Runs in CI?" / "this is the merge gate" language) to name the
   actual required set once it's more than one job, rather than leaving singular language that
   predates Phase 2's promotion of `platform-windows`.
4. After each addition, confirm enforcement rather than trusting the setting alone: push a scratch
   branch with a deliberately failing test in the newly-required job and confirm GitHub actually
   blocks that PR from merging.

### Exit criteria

Every job in `tests.yml` that runs on every PR and is meant to gate correctness (`test`,
`platform-windows`, `platform-macos`, `test-python-compat`, `static-analysis`'s
blocking `ruff` step) is a required status check on `main`'s branch protection rule; `testing-
policy.md` names the real required set instead of "the `test` job"; a deliberately red job on one of
those checks has been confirmed, not assumed, to block merge.

---

## Phase 12 — Retire the platform-specific plan docs

### Objective

Return to one active plan doc under `docs/`, as this document's own docs-audit pass intended
before `windows-support-plan.md`, `windows-linux-support-plan.md`,
`linux-local-deb-packaging-plan.md`, and `manual-pre-release-test-plan.md` all had to be restored
because they still tracked open work `main` had landed against them.

### Already in this repo

Every open item in those four documents is already tracked above, owned by the phase that covers
it: `windows-support-plan.md`'s remaining phases and its Phase 6.3 open path-handling finding under
Phase 2/Phase 6 above; `windows-linux-support-plan.md`'s Track B3 (the accepted POSIX
file-permission gap) referenced from Phase 2.1 and Phase 6; `linux-local-deb-packaging-plan.md`'s
P7.2/P7.3 under Phase 7/Phase 6.3; `manual-pre-release-test-plan.md` under Phase 9's reduction.
This phase adds no new scope — it is bookkeeping once that scope is closed.

### Remaining work

1. Confirm every open item in `windows-support-plan.md`, `windows-linux-support-plan.md`,
   `linux-local-deb-packaging-plan.md`, and `manual-pre-release-test-plan.md` has actually landed —
   i.e. Phases 2, 6, 7, and 9 above are done, not just summarized as done here.
2. Delete all four files from `docs/`.
3. Update `docs/README.md`'s doc index and "active implementation plans" note to drop the four
   retired entries, leaving `automated-test-strategy-plan.md` as the only plan doc again.
4. Remove this document's own cross-references to the four retired documents (the "Already in this
   repo" paragraph above, and any other mention elsewhere in this file) once they're gone, so
   nothing here links to a deleted file.

### Exit criteria

`docs/` contains exactly one `*plan*.md`: this document.

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
   ↓
Phase 11 Update branch-protection required checks                (incremental — starts as soon as
   ↓                                                               platform-windows is stable, keeps
   ↓                                                               picking up each phase's job as it
   ↓                                                               lands; final consistency pass once
   ↓                                                               Phases 2, 3, 6, 7, and 8 are done)
Phase 12 Retire the platform-specific plan docs                  (bookkeeping only, once Phases 2,
                                                                    6, 7, and 9 above are actually
                                                                    done — last step in this plan)
```

Phases 4 and 5 may proceed in parallel once Phase 3 is stable, as in the source strategy. Phase 7
stays last for the same infrastructure-cost reason the source strategy gives. Phase 11 runs
incrementally alongside whichever phase just promoted a job to per-PR (its own remaining-work item 1
says so explicitly) rather than waiting for everything else to finish — only its final consistency
pass (items 2-3) waits on the rest. Phase 12 stays last of all: it only deletes docs once every phase
above it has actually shipped.

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
26. Update branch-protection required status checks (Phase 11) — not one PR but a small addition
    riding alongside each of PRs 12, 17-18, 13, 23 above as their job proves stable, plus a final
    documentation-consistency PR once every addition has landed
27. Retire `windows-support-plan.md`, `windows-linux-support-plan.md`,
    `linux-local-deb-packaging-plan.md`, and `manual-pre-release-test-plan.md` once 12–24 above are
    actually done (Phase 12) — last PR in this plan, bookkeeping only

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
- Every published DMG/EXE/DEB is exercised before publication (Phase 6).
- Package upgrade tests prove user state survives, on all three platforms (Phase 6).
- Local-mode autostart has automated platform-specific coverage (Phase 7).
- Org mode executes an authenticated synthetic end-to-end request in CI (Phase 8, mostly already
  true — confirm and close remaining gaps).
- `connector-qa-testing.md` is exploratory, not mandatory, for routine releases (Phase 9).
- Routine manual release validation takes minutes, not hours (Phase 9).
- PrivacyFence can be confidently released without owning physical Windows, Linux, or macOS
  development machines.
- GitHub's required-status-checks list on `main` names every blocking per-PR job, not just `test` —
  a red `platform-windows`/`platform-macos`/`test-python-compat`/`static-analysis` run actually
  blocks merge, confirmed rather than assumed (Phase 11).
- `docs/` contains exactly one `*plan*.md` — this document — with `windows-support-plan.md`,
  `windows-linux-support-plan.md`, `linux-local-deb-packaging-plan.md`, and
  `manual-pre-release-test-plan.md` retired once the work they track has actually shipped
  (Phase 12, last).
