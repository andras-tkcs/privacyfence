# Testing Policy

What runs where, and when. This repo has three tiers of testing, plus one narrow scheduled
exception (§0) that only ever runs on infrastructure this project owns. See
[`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) for how to *write* tests;
this document is about which ones run automatically versus which ones a human has to run. The
tiers below (§0–§3) are organized by *trust boundary* (what infrastructure/credentials a check
needs); the taxonomy immediately below organizes the same checks by *what they prove* — the two
are complementary, not competing, framings of the same suite.

## Test taxonomy: the seven layers

[`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) (Phase 0) introduced this
finer-grained taxonomy because "CI vs. manual" alone doesn't say what a check actually catches: a
`tests/unit/` module and `tests/integration/test_mcp_daemon_contract.py` are both "automated, every
PR" per §1 below, but they prove different things and fail for different reasons. Every check in
this repo, present or planned, is exactly one of:

| # | Layer | What it proves | Where it runs today | Pytest marker |
|---|---|---|---|---|
| 1 | Unit | Python logic in isolation, fully offline | §1 below — `tests/unit/`, every PR | `unit` |
| 2 | Integration | Real internal stack (real sockets, servers, the real daemon process), no external network | §1 below — `tests/integration/`, every PR | `integration` |
| 3 | Cross-platform system | OS path/process/locking/daemon behavior, identical on Linux/Windows/macOS | Done. `test_mcp_daemon_contract.py` (§1 below) already proves the daemon→MCP→approval→audit *transport* contract on `ubuntu-latest`; `automated-test-strategy-plan.md` Phase 3 added the canonical scenario this layer actually names — `tests/system/test_local_mode_system.py`, a real `daemon_main.main()` subprocess (not an in-process call) driven through startup/discovery, the real `/approvals`/`/settings` web surfaces via a real bootstrap exchange, an MCP-client tool call resolved both Allow and Deny through the real HTTP decide route, the audit log read back from disk, and a graceful shutdown via the real "Quit PrivacyFence" action — collected by every job that runs the full suite (`test`, `platform-windows`, `platform-macos`), so it's proven identical on all three OSes with no extra CI wiring. `platform-windows` (renamed from `test-windows` by Phase 2.1) and `platform-macos` (Phase 2.2) both run the full core suite on every PR, deliberately, not narrowed to a targeted subset — Phase 2.4's own grounding pass found the full-suite run itself is what catches real cross-platform bugs (2.1's first run found four that a subset defined ahead of time would have missed), so the decision was to keep it rather than narrow it. `tests/platform/` (Phase 2.3) adds targeted coverage on top of that full run for OS path/process/locking/daemon-discovery behavior the full suite didn't otherwise reach (atomic-write concurrency, cross-process single-instance locking, the browser-launch default path, and a real spawned-daemon-process lifecycle) — deliberately not duplicated by Phase 3's own module, which adds only what neither `test_mcp_daemon_contract.py` nor `tests/platform/` covers: the full contract, end to end, as a real process, identically on all three OSes. Org mode's own equivalent canonical system-layer scenario is a separate module, `tests/integration/test_org_ubuntu_release_smoke.py` (TST-16) — same real-subprocess shape, but against a signed `org_config.json` and a mocked IdP instead of local mode's bootstrap secret: strict fail-closed startup, reverse-proxy Host handling, per-principal session/MCP-identity/authz-policy resolution, an approval exercised and audited under the *correct* principal, and persisted state (a confirmed rule, its audit trail) surviving a clean restart (`automated-test-strategy-plan.md` Phase 8). Runs on `ubuntu-latest` only (no Windows/macOS org-mode job exists), in its own `org-mode-smoke` CI job, promoted the same way `platform-windows` was (Phase 8 item 2) from build.yml's release-tag-only `build-deb` job (which still runs it too, unchanged). | `system` (registered, applied to `tests/system/test_local_mode_system.py`, Phase 3, and to `test_org_ubuntu_release_smoke.py`, Phase 10 — see below) and `platform` (registered, applied to every module under `tests/platform/`) |
| 4 | Browser system | JS/CSP/rendering in a real browser | Done. `tests/integration/test_browser_smoke.py` (§1 below) drives real Chromium via Playwright on every PR — login, approval decisions (including "Always allow," live SSE refresh with multiple pending cards, and double-decision idempotency, Phase 4.1), PDF preview, CSP, org-mode WebAuthn UI, PII banner/confirmation-dialog behavior, responsive layout at three named viewports, and structural light/dark assertions (Phase 4.2–4.5, `automated-test-strategy-plan.md`). §2.2's `qa_web_smoke.py` still covers what a pytest-collected browser test structurally can't — subjective visual judgment (contrast, "does this look right" in light vs. dark, at a phone width) — and still only runs by hand; that's the genuinely layer-7 sliver Phase 4 deliberately left manual, not a residual automation gap. | `browser` (registered; not yet applied — `test_browser_smoke.py` predates this marker work, see the note below) |
| 5 | Live connector | Provider API drift | §0 below — `connector-live-check.yml`, self-hosted runner, scheduled. Done. | `live` (registered; nothing in the pytest suite carries it today, since this tier is a standalone script invocation, `qa_fixture_recorder.py --check`/`--record`, not a pytest-collected test — see §0/§2.1) |
| 6 | Packaged-artifact | Installer/package correctness | Done. `test_macos_packaged_smoke.py` (Phase 6.1, done) runs in `build.yml`'s tag-triggered release path — a real DMG mount/install, daemon→MCP→approval round trip, code-signature/notarization chain (`codesign --verify`, `spctl --assess`), and a state-outside-package check (deleting the installed `.app` never touches `$HOME/.privacyfence`); `test_deb_packaged_lifecycle.py` (Phase 6.3, done) runs the Linux `.deb` install/validate/remove/purge and upgrade-in-place lifecycles (the now-removed `linux-local-deb-packaging-plan.md` P7.1/P7.3) the same way, in `build.yml`'s `build-deb` job; `test_windows_packaged_smoke.py` (Phase 6.2, done) runs a real silent install/autostart-task-registration/uninstall lifecycle against the Windows installer the same way, in `build.yml`'s `build-windows` job. Upgrade/state-preservation testing across all three platforms is also done (item 20) — macOS/Windows each got their own version-N→N+1 upgrade test, Linux's own having already landed inside Phase 6.3. `publish-pypi.yml` additionally gates every one of its own publish steps (TestPyPI/PyPI/R2) on `build.yml`'s run for the same commit actually succeeding (Phase 6.4) — a broken packaged artifact on any platform now blocks the whole tag's release, not just its own platform's upload. Linux's own graphical-session/autostart verification (`automated-test-strategy-plan.md` Phase 7 item 1, the now-removed `linux-local-deb-packaging-plan.md` P7.2) is also done — `test_linux_graphical_session_autostart.py` brings up a real `systemd --user` login-equivalent session and starts `xdg-desktop-autostart.target` (standing in for the missing physical login), then confirms the real `systemd-xdg-autostart-generator`-produced unit actually launches the packaged daemon and that "Quit PrivacyFence" stops the real unit; a second test in the same module proves the real (unmocked) OAuth loopback browser-opening flow under a genuine Xvfb `$DISPLAY`. Deliberately scheduled in its own `linux-graphical-session.yml` workflow (packaging-related `main` pushes, weekly, on demand), not per-PR or wired into `build.yml`'s release pipeline — the most expensive, flakiest tier here, so a flaky run must never block a release. Windows' equivalent (`automated-test-strategy-plan.md` Phase 7 item 2, the now-removed `windows-support-plan.md` 8.2) is built the same way — `test_windows_graphical_session_autostart.py` drives a real interactive Windows logon (`Start-Process -Credential`, the same primitive `runas.exe` is built on) for a throwaway local account standing in for "someone signs in," confirms the real installed Task Scheduler `ONLOGON` trigger actually launches the packaged `privacyfence-app.exe` alias under that account (via `Win32_Process`'s `GetOwner`, not assumed), runs the same daemon/MCP/approval/audit round trip, and confirms "Quit PrivacyFence" ends the real process — scheduled the same way, in its own `windows-graphical-session.yml` workflow. Was red on every run (Task Scheduler task never actually registering — see `platform-support.md`'s "Known open items" for the root cause and fix); confirmed green again via `workflow_dispatch` as part of the same fix. What it still does not cover: crash-restart behavior, since the installed task never actually implemented that (`automated-test-strategy-plan.md` Phase 13, not yet built). | `packaged` (registered; applied to all three packaged-artifact modules — `test_macos_packaged_smoke.py` picked it up as part of Phase 6.1 — and to `test_linux_graphical_session_autostart.py`/`test_windows_graphical_session_autostart.py`) |
| 7 | Manual exploratory/UX | Subjective judgment, first-time auth/consent flows | §3 below, [`connector-qa-testing.md`](connector-qa-testing.md), [`release-testing.md`](release-testing.md) | — (never automated, by definition — see the governing rule below) |

**Governing rule for what stays manual:** a test stays manual only when automated observation
cannot reliably determine pass/fail. In this project that bar is met by exactly two recurring
cases — visual/subjective judgment (contrast, spacing, "does this look right" in light vs. dark
mode) and a first-time third-party consent/OAuth screen (rendered by the provider, outside this
codebase's control, and different account-to-account) — and nothing else should be left manual on
that basis. Anything else currently manual (§2 below) is there because it needs a real credential
or a real browser binary CI doesn't provision, not because it can't be judged automatically; that's
a cost-of-infrastructure reason; the phases in `automated-test-strategy-plan.md` exist to remove
those, one at a time.

**Pytest markers**: seven markers are now registered in `pyproject.toml`'s
`[tool.pytest.ini_options]` — the original six plus `platform`
(`automated-test-strategy-plan.md` Phase 2.3), added as its own marker rather than reusing `system`
per Phase 0's own status note: `system` was reserved for, and is now applied to,
`tests/system/test_local_mode_system.py` (Phase 3) — the canonical daemon/MCP/approval/audit
scenario, a different concept from `tests/platform/`'s OS-level path/process/locking/daemon-
discovery tests. `test_org_ubuntu_release_smoke.py` (Phase 8's org-mode equivalent of that same
canonical scenario) picked up `system` too, by Phase 10 — not when it first landed, since at the
time nothing yet selected on the marker; Phase 10's own CI-diagnostics capture
(`tests/diagnostics.py`) is what now does, for exactly the `packaged`/`system`-marked tests it
targets. Consistent with Phase 0's own scope note, markers are *not* retroactively applied
across every existing test — that would be churn with no payoff until something actually needs to
select on the marker (e.g. `pytest -m "not live"`). The five modules that landed as part of closing
out `security-remediation-plan.md`'s Phase 3.12 (`test_qa_fixture_recorder.py`'s
`TestFixturePresence`, `test_deferred_approval_round_trip.py`, `test_routes_security.py`'s
`TestCrossPrincipalIsolation`, `test_parser_properties.py`, `test_systemic_gate_invariants.py`)
landed before this marker work existed and were the first backfill; every module under
`tests/platform/` carries `platform` from the day it was added, per the rule below; everything else
keeps whatever marker (none, today) it already had. New test modules should carry the marker that
matches their layer from the day they're added.

## Test ownership: failure type → layer

| Failure type | Owning layer |
|---|---|
| Wrong business logic, parsing bug, or malformed output from a pure function | 1. Unit |
| Regression in real internal wiring — daemon ↔ web routes ↔ gate ↔ audit — with no external network involved | 2. Integration |
| OS-specific bug: works on Linux, breaks on Windows/macOS (path separators, locking, process spawning, daemon discovery) | 3. Cross-platform system |
| Bug only a real browser exposes: script-order/DOM-timing, a CSP directive that silently blocks something, real rendering at a given viewport or color scheme | 4. Browser system |
| Provider API drift: a field renamed, an endpoint removed, a response shape changed | 5. Live connector |
| Installer/package bug: files not registered at the expected path, autostart missing, uninstall/purge leaves stray files, upgrade loses user state | 6. Packaged-artifact |
| Subjective visual defect (contrast, spacing, layout "looking right") | 7. Manual exploratory/UX |
| First-time third-party consent/OAuth screen behaving unexpectedly | 7. Manual exploratory/UX |

Every layer but the last is, by the governing rule above, a candidate to *stay* automated — a
failure type that currently lands in layer 7 only belongs there if it's one of the two named
exceptions; otherwise it's a sign the corresponding phase in `automated-test-strategy-plan.md`
hasn't landed yet, not that the failure type is inherently manual.

### Checked against `release-testing.md`

Every item that used to require manual release QA maps to exactly one layer above; where a layer's
automation isn't done yet, the corresponding phase in `automated-test-strategy-plan.md` is what
closes it. What's left genuinely manual today is exactly layer 7's two named exceptions (visual/
subjective judgment, first-time third-party consent) plus a small set of infrastructure-cost items
(a real third-party MCP client, OS-native trust-prompt presentation) — all covered by
[`release-testing.md`](release-testing.md), the evergreen standing reference for what stays manual
and why. `connector-qa-testing.md` covers the narrower, non-routine case: a new connector, a
material connector/gate change, or an unexplained regression — not a routine release.

## 0. Runner-local live tier (scheduled, not per-PR)

*Layer 5 (live connector) in the taxonomy above.*

`.github/workflows/connector-live-check.yml` runs `qa_fixture_recorder.py --check` (and, on
drift, `--record`), then `qa_fixture_recorder.py --lifecycle` (§2.1's bounded lifecycle mode --
create/read/update/delete a fresh QA object per write-capable connector), against the four
dedicated test accounts (Google, Slack, Atlassian, Salesforce) on a weekly schedule (plus
`workflow_dispatch`) — never on `pull_request`, and never on a GitHub-hosted runner. Unlike
`--check`'s drift (data, not a failure), a `--lifecycle` failure fails the job outright: a failed
write/read/update is a real provider-contract regression, and a cleanup call that ran but didn't
actually remove what it created would otherwise silently accumulate objects in the QA accounts
forever. It targets a self-hosted runner this project provisions and controls
(label `privacyfence-test`), provisioned per
[`connector-live-check-setup.md`](connector-live-check-setup.md) Phase B. The four OAuth
token files and `org/org_config.json` live only as local files on that runner — they are never
added as GitHub Actions secrets, and are never transmitted to GitHub at all. See
[`connector-live-check-setup.md`](connector-live-check-setup.md) for the full account/runner setup
and the reasoning behind isolating this tier from every GitHub-hosted job.

On drift, the job re-records the affected fixtures and opens an ordinary PR
(`chore/connector-live-fixture-drift`) with the redacted diff; a maintainer reviews it exactly as
they would review a manually-run `--record` per §2.1 below. That PR then runs through §1's normal
GitHub-hosted `tests.yml` merge gate like any other PR — the live-credential job and the
credential-free merge gate never share a runner or a trigger.

## 1. Automated suite — every PR, in CI

*Layers 1 (unit) and 2 (integration) in the taxonomy above, plus most of what layer 4 (browser
system) already has — see `test_browser_smoke.py` below.*

`.github/workflows/tests.yml` runs on every push to `main` and every pull request:

```bash
npm test              # mcpb/shim/, Node's built-in test runner
npm run typecheck     # mcpb/shim/, tsc --noEmit
pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json
python scripts/check_coverage_floor.py coverage.json
```

on an `ubuntu-latest` runner. A 100% pass rate is required to merge, for both suites. Coverage
itself is a ratchet, not a specific percentage a PR must hit (TST-03, Phase 2.1 of the now-removed
`docs/security-remediation-plan.md`): `scripts/check_coverage_floor.py` fails the build
if overall branch+line coverage, or the coverage of any module on its security-critical list (the
URL-scheme allowlist, identity-matching, audit-export, org-config/bundle-trust, session/token-
lifetime, privacy-filter, secure-write, OIDC-discovery-trust, and MCP-error-taxonomy code paths —
see that script's `MODULE_FLOORS` for the exact list), drops below where it was recorded. A PR that
raises coverage on one of those modules should bump its floor in the same PR; a PR that needs to
*lower* one is a real regression, not a config edit. `pytest`'s own `--cov-report=json`/`html`
output is uploaded as a `coverage-report` CI artifact on every run (pass or fail) so a regression
can be inspected without re-running locally.

`tests.yml` runs six jobs on every PR, and `main`'s branch protection rule requires all of them to
pass before a PR can merge: this `test` job; `platform-windows`/`platform-macos` (the full core
suite again on real Windows/macOS runners — see §3's system-layer row above for what these add);
`test-python-compat` (the same core suite, Node-free, against Python 3.11 and 3.12 — reported as
two separate checks, one per Python version, since each matrix leg is its own GitHub check);
`org-mode-smoke` (`test_org_ubuntu_release_smoke.py`, see §3's row above); and `static-analysis`'s
blocking `ruff check .` step (its `mypy`/`bandit` steps are `continue-on-error` and stay
informational — see `coding-and-testing-guidelines.md`). `scripts/update_branch_protection.py`
is the reviewable, applied-the-same-way-every-time record of exactly that set
(`docs/automated-test-strategy-plan.md` Phase 11) — update its `REQUIRED_STATUS_CHECKS` list, in
the same PR, whenever a job here is added, renamed, or removed, then have a repo admin run
`apply` against the live branch protection rule.

Through P9 this ran on `macos-latest` instead, and a second, non-blocking `test-linux` job carried
the platform-independent subset (everything under `web/`, `web_approval_ui.py`, `card_builder.py`,
and `approval_icons.py`) on `ubuntu-latest`, `--ignore`-ing the handful of test modules that imported
an AppKit-tainted module (`approval_popup.py`/`approval_window.py`/`dialog_window.py`/`menu_bar.py`)
directly at module scope. P10 (see `https-connector-refactor-plan.md` §12, decision D6, the design
document that shipped this and was removed from `docs/` once fully implemented) deleted all
of that — the native menu bar/approval dialogs/settings window — so nothing in this repo depends on
real AppKit/PyObjC behavior any more, the whole suite is platform-independent, and the two-job split
collapsed back into one.

This tier is fully self-contained: no network calls to Gmail/Slack/Jira/etc., no credentials, no
manual steps. It includes:

- Every module under `tests/unit/`, one test module per `src/privacyfence/` module — with one
  deliberate exception: `connector_host.py`'s `ConnectorHost` (the live `{name: Connector}` map) has
  no `test_connector_host.py` of its own; its behavior is exercised through its three consumers'
  own test modules instead (`test_settings_controller.py`, `test_daemon_main.py`,
  `tests/unit/web/test_routes_settings.py`, `tests/unit/web/test_server.py`), since it's a thin
  enough holder that testing it in isolation would just re-mock what those already cover for real.
- Each connector's `TestLiveFixtureParsing` class (in `tests/unit/test_<connector>_client.py`),
  which replays a **previously recorded** fixture from `tests/fixtures/live/<connector>/` through
  the real `_parse_*` method — still fully offline, since it's reading a committed JSON file, not
  making a live API call. See [§2.1](#21-qa_fixture_recorderpy---check----record) below for how those
  fixtures get recorded in the first place. A connector with no recorded fixture yet has its
  `TestLiveFixtureParsing` tests skip (not fail) with a message pointing at the recorder.
- `tests/unit/test_qa_fixture_recorder.py` — unit tests for the recorder script itself
  (`scripts/qa_fixture_recorder.py`), exercised against mocked/offline API responses. This is
  different from actually running the recorder: these tests prove the recorder's own logic
  (redaction, capture mechanisms, the tag guardrail) is correct without touching any real account.
- `tests/unit/test_approval_window_html.py`, `tests/unit/test_dialog_window_html.py` — construction-
  only coverage for the pure HTML builders behind every card/confirmation dialog (content, buttons,
  PII tint/banner, summary rows, details text). Through P9 these were rendered inside a real native
  AppKit view tree too (`test_approval_window.py`/`test_dialog_window.py`, covering the modal-loop
  host around the same HTML); P10 deleted that host, so this construction-only tier is now the whole
  of it.
- `tests/unit/test_web_approval_ui.py`, `tests/unit/test_card_builder.py`,
  `tests/unit/test_approval_icons.py`, `tests/unit/web/` — the web approval surface's own coverage,
  added at P1: `WebApprovalUI`'s blocking contract (the sole `ApprovalUI` implementation since P10 —
  see `approval_ui.py`'s ABC), the pure gate-args-to-card-HTML translation, shared icon-asset loading,
  and the approval routes themselves against an in-process ASGI test client (auth, CSRF, Host
  allowlist, security headers, idempotent decisions — no real socket, see
  `https-connector-refactor-plan.md` §13).
- `tests/unit/web/test_mcp_dispatch.py`, `tests/unit/web/test_routes_mcp.py` — the `/mcp` endpoint's
  own coverage, added at P2: `McpDispatcher`'s dedupe/staleness/gating dispatch and meta-tools
  (`test_mcp_dispatch.py`) and the wire-protocol/auth layer on top of it (`test_routes_mcp.py`),
  driven with the real official `mcp` Python client over an in-process ASGI transport — no real
  socket, same posture as the approval routes above. `TestAudienceSeparation` in
  `tests/unit/web/test_server.py` is the one required to fail loudly if the MCP bearer-token and
  approval-surface session-cookie middleware are ever reordered (§10.3 of the refactor plan).
- `tests/unit/web/test_mcp_tools.py` — added at the now-removed security-remediation-plan.md's
  phase 1.9 (TST-02):
  `mcp_tools.py`'s own `ToolSpec`-to-`Tool`/`CallToolResult` schema translation (untested by either
  file above, which exercise dispatch and wire framing, not this mapping layer), plus end-to-end
  coverage over the real `/mcp` transport for three narrow behaviors: an unattended session denying
  `privacyfence_propose_auto_accept_rule_change` before any confirmation popup can be shown (a real
  gap this phase found and fixed — see `McpDispatcher.propose_rule_change`'s own comment in
  `mcp_dispatch.py`), `privacyfence_begin_unattended_session` refusing when disabled by
  configuration, and `privacyfence_list_auto_accept_rules` always leaving an audit entry for its own
  disclosure.
- `mcpb/shim/test/*.test.ts` (`npm test`, run from `mcpb/shim/`) — the .mcpb shim's own suite (D11 in
  `docs/https-connector-refactor-plan.md` §12): daemon discovery/launch (`daemon.test.ts`, against
  `mcp_url` file discovery) and the stdio<->Streamable HTTP message proxy (`proxy.test.ts`,
  `index.test.ts` — the latter against a real fake `/mcp` server built on the official SDK's own
  server classes, not a hand-mocked transport). The only Node suite left in this repo since P5
  retired the original bridge and its own `bridge/test/*.test.ts`.
- `npm run typecheck` (`tsc --noEmit`, run from `mcpb/shim/`) — catches type errors across
  `mcpb/shim/src/*.ts` that `npm test`'s runtime coverage wouldn't necessarily hit (an unreachable
  branch, a type mismatch in an untested code path).
- `tests/integration/test_mcp_daemon_contract.py` — spawns a real `privacyfence.web.server.WebServer`
  bound to a real loopback socket and drives it with the official `mcp` Python client over a real
  TCP connection (not the in-process ASGI transport `test_routes_mcp.py` above uses), so a
  real-network-stack bug (uvicorn startup, real TCP binding, real HTTP framing) can't slip through
  either. Needs no Node — since P5 there is no longer a second, independently-maintained protocol
  implementation to cross-check against (both client and server here are the official `mcp` SDK).
  Uses the official `mcp` Python client, a runtime dependency since P2 (`pyproject.toml`'s
  `[project.dependencies]` — see `docs/https-connector-refactor-plan.md` §8.2/D2) rather than a
  test-only one.
- `tests/system/test_local_mode_system.py` (`automated-test-strategy-plan.md` Phase 3, layer 3
  cross-platform system) — the one module in this tier that spawns the real
  `python -m privacyfence.daemon_main` entry point as a genuinely separate OS process (every module
  above drives daemon/server code as a plain in-process call), and runs it through the full
  contract: `mcp_url`/lock-file discovery, `/approvals`/`/settings` reached via a real one-time
  bootstrap exchange (`POST /api/bootstrap`, never a link scraped out of the log — `safe_errors.
  SecretRedactingFormatter` deliberately redacts those), a gated MCP tool call resolved both Allow
  and Deny through the real HTTP decide route, the audit log read back from disk to confirm both
  decisions, and a graceful shutdown via the real "Quit PrivacyFence" action. Collected by every job
  that runs the full suite (`test`, `platform-windows`, `platform-macos`), so this one module proves
  the scenario identical on Linux, Windows, and macOS with no extra CI wiring — the same
  `pyproject.toml` `testpaths = ["tests"]` mechanism `tests/platform/` (Phase 2.3) already relies on.
- `tests/integration/test_shim_mcp_contract.py` — spawns the real built `mcpb/shim/dist/shim.js`
  against that same real, socket-bound `WebServer` and drives *it* with the official `mcp` Python
  client over real MCP-over-stdio. A passthrough test, not a schema test — the shim carries no
  tool-schema knowledge, so "one `initialize` and one `tools/call` round-trip, with the bearer
  header attached and `mcp_url` honoured" is the whole of what there is to assert. Skips
  automatically if Node isn't on `PATH`; CI installs it, so this runs there.
- `tests/integration/test_browser_smoke.py` (TST-06) — drives the real web approval surface in a
  real headless Chromium via Playwright: bootstrap login, an Allow/Deny decision round trip, PDF
  preview actually rendering, a real no-inline-script CSP check, and the org-mode WebAuthn UI. The
  one place in this tier that exercises what only a real browser does — the CSP header actually
  enforced, cookie `SameSite` semantics on a real `fetch()`, the page's own JS event loop
  (`EventSource`, `navigator.credentials`) — rather than a scripted transport (`test_routes_
  approvals.py`) or a real socket driven by a non-browser client (`test_mcp_daemon_contract.py`
  above). This is layer 4 (browser system) in the taxonomy at the top of this document; it already
  runs here in tier 1 because Playwright/Chromium install cleanly on `ubuntu-latest` with no
  external credential, unlike the connector accounts §0/§2.1 need. `automated-test-strategy-plan.md`
  Phase 4 extends this file's coverage (empty-list/multi-card/idempotency behavior, PII-specific
  banner assertions, responsive layout, light/dark structural assertions) — it does not need to
  create the layer, which is already here.

## 2. Local-only checks — run manually before opening/updating a relevant PR, never in CI

*§2.1 is a local-only slice of layer 5 (live connector), run by hand between §0's scheduled runs.
§2.2 is a local-only slice of layer 4 (browser system), covering the subjective/visual-judgment
sliver `test_browser_smoke.py`'s own structural checks in §1 can't reach (light/dark contrast,
phone-width "does this look right") — neither is layer 7 (manual exploratory/UX) in the
`qa_fixture_recorder.py`/deterministic-report sense: both are fully deterministic, just not yet safe
or fast enough to run on a GitHub-hosted PR runner.*

Two scripts exist specifically because some failure classes can't be caught by a fully-mocked,
fully-offline suite. Both are excluded from CI on purpose — one needs real, authenticated
third-party accounts; the other needs a real browser — and both print the same kind of small,
deterministic Markdown report meant to be pasted into the PR description so a reviewer doesn't have
to re-run anything or have access to the same accounts/hardware themselves.

Through P9 a third script, `qa_popup_smoke.py`, covered the one thing `test_approval_window.py`/
`test_dialog_window.py`'s construction-only tests couldn't reach: whether the real native modal loop
actually blocked and a real click actually reached it. P10 (`https-connector-refactor-plan.md` §12,
D6) deleted the native popup itself along with that script — there is no modal loop left to smoke-
test. `qa_web_smoke.py` below is this tier's own (Chromium-driven, not AppKit-driven) equivalent for
the web approval surface that replaced it, and already existed before this phase; nothing new was
needed to fill the gap.

### 2.1 `qa_fixture_recorder.py --check` / `--record`

Every `tests/unit/test_<connector>_client.py` module mocks the connector's `*_client.py` (or the
third-party SDK object one layer inside it), which is correct for testing this codebase's own
parsing logic in isolation — but it has a structural blind spot: a hand-authored mock fixture can
drift out of sync with what the real provider API actually returns (a field renamed, an endpoint
removed, a response shape changed) while the mocked test suite stays green. `scripts/
qa_fixture_recorder.py` closes that gap by calling the real, targeted read methods against a real,
already-authenticated account.

**Never run on a GitHub-hosted runner, or on any `pull_request`-triggered workflow.** It reuses the
exact OAuth token files `privacyfence-app --<connector>-oauth` writes to the git-ignored
`credentials/` directory, and only ever targets one specific, `[QATEST]`-tagged seed artifact per
connector — set up once per environment via [`qa-environment-setup.md`](qa-environment-setup.md),
resolved through the non-secret, git-ignored manifest `tests/fixtures/qa_environment.yaml` (see
[`qa_environment.yaml.example`](../tests/fixtures/qa_environment.yaml.example) for the template). No
credential is ever provisioned to a GitHub-hosted runner or any other shared cloud service to make
this possible.

The instructions below are for running this by hand, from your own machine, between scheduled
runs — still valid and still the right thing to do for a PR that touches a `*_client.py` or
`connectors/**` file. §0 above describes the one place this also now runs automatically: a
project-owned self-hosted runner, on a schedule, per
[`connector-live-check-setup.md`](connector-live-check-setup.md).

Three modes:

- `--check [connector ...]` — calls each connector's read methods against its seed artifact,
  asserts non-empty/expected results, prints a report. Never writes a file. Safe to run any time.
  The printed report also includes a **fixture freshness** line per connector checked: `< 60 days`
  healthy, `60–90 days` warning, `> 90 days` refresh required (1.9,
  `docs/automated-test-strategy-plan.md` Phase 1 residual work) — a quick signal for whether a
  connector's recorded fixture is due for a fresh `--record`, without doing the day-count
  arithmetic by hand.
- `--record [connector ...]` — the same calls, plus identity-field redaction (author email, account
  id, display name, ...) and structural de-identification (opaque resource ids, decorative URLs —
  neither of which any test actually depends on the specific value of), then writes the result to
  `tests/fixtures/live/<connector>/<method>.json`.
- `--lifecycle [connector ...]` — 1.8 (`docs/automated-test-strategy-plan.md` Phase 1 residual
  work). For each write-capable connector that supports it (`calendar`, `confluence`, `jira`,
  `tasks` — see the comment above `LIFECYCLE_CHECKS` in `qa_fixture_recorder.py` for why
  `contacts`/`gmail`/`drive`/`slack`/`salesforce`/`telegram` aren't included), creates a fresh,
  uniquely-tagged QA object, reads it back, updates it, reads it back again, then deletes it and
  confirms the deletion actually took, using the same run's redaction-free internal SDK access
  `RawCapture`/`RawCaptureExecute` already use — never a tool any MCP client can reach, since
  nothing in `connectors/**` ever registers a delete tool at all. Never writes a fixture file. Runs
  on the same weekly self-hosted-runner schedule as `--check`/`--record` (§0 above); safe to run by
  hand too, same caveats as `--check`/`--record`.

**When to run this**: only when a PR touches `src/privacyfence/*_client.py` or
`src/privacyfence/connectors/**` — not every PR. Scope it to the connector(s) touched, using the
project's own venv (a bare system `python3` won't have the third-party clients this imports):

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --check confluence
```

- **Passes, live shape unchanged**: nothing else to do. `--check` never writes a file.
- **Fails, or the fix was specifically in response to a provider shape change**: run
  `--record <connector>`, inspect the diff under `tests/fixtures/live/<connector>/*.json` — it
  should be a small, meaningful shape change, with identity fields already redacted to placeholders
  (if anything in the diff looks like a real email, name, or account-specific id, the redaction
  logic needs a fix before committing, not after) — then commit the updated fixtures alongside the
  code fix, in the same PR.
- Paste the printed report (or the file from `--report-file <path>`) into the PR description under
  a `## Local QA check` heading.

### 2.2 `qa_web_smoke.py`

`tests/unit/web/`, `test_web_shell.py`, and `test_approval_list_html.py` cover the web surfaces'
(`/settings`, `/approvals`) HTML/JSON construction and route behavior — CSRF, the settings action
allowlist, argument validation — on every PR, entirely against Starlette's in-process `TestClient`.
That deliberately leaves one thing untested: a **real browser** actually parsing and running the JS
those routes emit. A script tag referencing a DOM element or another script's global defined *later*
in the document silently no-ops instead of raising in a real browser — this script's own "card
decide → return-to-list toast" scenario is a regression test for exactly that bug (approval_list_
html.py's toast/notification-prompt code read `#pf-shell-toast`, defined by web_shell.py *after* it
in document order, before that element existed — found by running this script during P4's own
development, not by any unit test). It also confirms the CSP (web/server.py's `_CSP`) actually
permits what a page needs — e.g. that `worker-src 'self'` really does let `resources/sw.js` register,
not just that the header string contains the right token.

**Never run in CI.** It needs `playwright` (`pip install playwright` — not a project dependency,
install it locally) and a Chromium binary, and drives a real embedded HTTP server + real browser
end to end, which is slower and flakier than the route-level suite that already runs on every PR.

**When to run this**: whenever `web_shell.py`, `approval_list_html.py`, the JS-emitting functions in
`web/routes_approvals.py`/`web/routes_settings.py`, `resources/sw.js`, or `web/server.py`'s CSP
changes. Not for a `settings_controller.py`/`settings_window_html.py` change with no web-shell/CSP
involvement — those are covered by `test_settings_window_html.py`'s construction-only assertions.

```bash
.venv/bin/pip install playwright
.venv/bin/python scripts/qa_web_smoke.py
```

If Playwright's own bundled Chromium isn't installed, pass `--chromium-path` at a Chromium/Chrome
binary already on disk instead of downloading one. Paste the printed report into the PR description
under a `## Web smoke check` heading, same convention as §2.1.

## 3. Full manual QA pass — before a release, not per-PR

*Layer 7 (manual exploratory/UX) in the taxonomy above — this is the one tier the governing rule
says should stay manual. `automated-test-strategy-plan.md` Phase 5 cross-checked how much of what
this tier used to be the only proof for is actually deterministic gate behavior that belongs in
`test_gate.py` (layer 2) instead, and confirmed `test_gate.py` already covers nearly all of it —
this tier has shrunk accordingly, to the parts that genuinely need a human watching a real popup
render against real account data (or a live provider's own tool-to-gate-metadata mapping, which
`test_gate.py` deliberately doesn't duplicate per connector).*

[`connector-qa-testing.md`](connector-qa-testing.md) drives every tool through a live Claude
Cowork/Desktop session connected to the real `privacyfence` daemon, against real accounts, watching
what actually prompts. It is no longer the primary proof for gate-state correctness — that's
`test_gate.py`'s job now, confirmed exhaustive by Phase 5 — but it's still the only thing that
exercises the gate, the popup UI, and the audit log against a live provider end to end, and it stays
the route to catch a connector's tool wired to the wrong gate/metadata in the first place. Run it
before a release, or after any change to `gate.py`/`auto_accept.py`/`resource_grants.py`/the web
approval UI broadly, not on every PR.

A routine release no longer runs §2.1/§2.2 across every connector on principle —
[release-testing.md](release-testing.md)'s own "before release" check just confirms §0's scheduled
fixture check is recent and green, and reserves this full manual pass for the same trigger as
above: a broad gate/auto-accept/approval-UI change since the last release, not the mere fact that a
release is happening.

## Quick reference

| Check | Layer | Runs in CI? | When |
|---|---|---|---|
| `pytest` (full suite, incl. the mcp/daemon, shim/mcp contract, canonical system, and browser-smoke tests) | 1, 2, 3, 4 | Yes, every PR | Always — this is the merge gate |
| `pytest` on `platform-windows`/`platform-macos` (incl. `tests/platform/`, `-m platform`, and `tests/system/test_local_mode_system.py`) | 1, 2, 3 | Yes, every PR | Always — this is also a merge gate |
| `test_org_ubuntu_release_smoke.py` (`org-mode-smoke` job) | 3 | Yes, every PR (Ubuntu only) — also re-run in `build.yml`'s `build-deb` job at release time | Always — this is also a merge gate |
| `pytest` on `test-python-compat` (core suite, Node-free, Python 3.11 and 3.12) | 1, 2 | Yes, every PR (Ubuntu only) — two checks, one per Python version | Always — both are also a merge gate |
| `ruff check .` (`static-analysis` job) | — (static check, not a layer) | Yes, every PR | Always — this is also a merge gate |
| `mypy`/`bandit` (`static-analysis` job) | — (static check, not a layer) | Yes, every PR, but `continue-on-error` | No — informational only, doesn't gate the job or the merge |
| `check_coverage_floor.py` (coverage ratchet, TST-03) | — (a quality gate on layers 1–2, not a layer itself) | Yes, every PR | Always — this is also a merge gate |
| `npm test` (mcpb/shim/'s own suite) | 1 | Yes, every PR | Always — this is the merge gate |
| `npm run typecheck` (mcpb/shim/) | — (static check, not a layer) | Yes, every PR | Always — this is the merge gate |
| `qa_fixture_recorder.py --check` / `--record` (`connector-live-check.yml`) | 5 | Yes, but only on a project-owned self-hosted runner, never GitHub-hosted, never on `pull_request` | Weekly schedule + manual dispatch |
| `qa_fixture_recorder.py --lifecycle` (`connector-live-check.yml`) | 5 | Yes, same runner/trigger restrictions as `--check`/`--record` above; unlike drift, a failure fails the job | Weekly schedule + manual dispatch |
| `qa_fixture_recorder.py --check` (manual) | 5 | No | PR touches a `*_client.py`/`connectors/**` file, between scheduled runs |
| `qa_web_smoke.py` | 4 | No | PR touches `web_shell.py`, `approval_list_html.py`, web routes' JS, `resources/sw.js`, or the CSP |
| `test_linux_graphical_session_autostart.py` (`linux-graphical-session.yml`) | 6 | Yes, but its own dedicated workflow — never `pull_request`, never `build.yml`'s release pipeline | Packaging-related `main` pushes + weekly schedule + manual dispatch |
| `test_windows_graphical_session_autostart.py` (`windows-graphical-session.yml`) | 6 | Yes, but its own dedicated workflow — never `pull_request`, never `build.yml`'s release pipeline | Packaging-related `main` pushes + weekly schedule + manual dispatch |
| `connector-qa-testing.md`'s live Cowork pass | 7 | No | A new connector, a material connector/gate change, an unexplained regression, or a broad gate/auto-accept/approval-UI change — not routine releases (Phase 9) |

Layer 3 (cross-platform system) is now fully settled here (the two rows above) — `tests/platform/`
gives it a real, named subset of its own, and the canonical daemon/MCP/approval/audit scenario on
all three OSes (`automated-test-strategy-plan.md` Phase 3, `tests/system/test_local_mode_system.py`)
has landed too. Layer 6 (packaged-artifact) is now also fully settled — see the taxonomy table
above (`automated-test-strategy-plan.md` Phase 6, done): all three platforms' install/lifecycle/
upgrade tests are wired into `build.yml`, `publish-pypi.yml`'s own publish steps are gated on
`build.yml`'s run for the same commit succeeding (Phase 6.4), and both Linux's and Windows's own
graphical-session/autostart verification (Phase 7 items 1–2) are wired into their own scheduled
`linux-graphical-session.yml`/`windows-graphical-session.yml` workflows rather than `build.yml`,
since a flaky run in this tier must never block a release.

None of the "No" rows require a credential or secret to ever be granted to a **GitHub-hosted**
runner or any `pull_request`-triggered workflow — that part of the policy is unchanged and still
absolute. The `qa_fixture_recorder.py` "Yes" rows above are GitHub Actions workflows too,
technically, but only ever execute on infrastructure this project itself owns and controls (§0) —
not a GitHub-hosted runner, and not reachable from a fork PR or any untrusted trigger.
`linux-graphical-session.yml`'s and `windows-graphical-session.yml`'s rows are a different case:
they do run on a GitHub-hosted `ubuntu-latest`/`windows-latest` runner, same as `build.yml`, but
never on `pull_request` (a fork PR can't trigger either), and the one secret each bakes in
(`TELEGRAM_API_ID`/`TELEGRAM_API_HASH`) is the same shared, non-per-account app identity `build.yml`
itself already bakes into every distributed release build on a GitHub-hosted runner — not a live
per-account third-party OAuth credential of the kind this policy exists to keep off
GitHub-hosted/`pull_request` infrastructure.
