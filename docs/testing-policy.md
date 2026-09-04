# Testing Policy

What runs where, and when. This repo has three tiers of testing, only the first of which runs in
GitHub Actions — the other two need a real macOS machine, real screen, and/or real authenticated
accounts, none of which CI has or should have. See
[`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) for how to *write* tests;
this document is about which ones run automatically versus which ones a human has to run.

## 1. Automated suite — every PR, in CI

`.github/workflows/tests.yml` runs on every push to `main` and every pull request:

```bash
npm test              # mcpb/shim/, Node's built-in test runner
npm run typecheck     # mcpb/shim/, tsc --noEmit
pytest -v --cov=src/privacyfence --cov-report=term-missing
```

on an `ubuntu-latest` runner. A 100% pass rate is required to merge, for both suites; the coverage
report is informational only — nothing gates on a specific percentage. This `test` job is the one a
PR needs to pass to merge.

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

- Every module under `tests/unit/`, one test module per `src/privacyfence/` module.
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
- `tests/integration/test_shim_mcp_contract.py` — spawns the real built `mcpb/shim/dist/shim.js`
  against that same real, socket-bound `WebServer` and drives *it* with the official `mcp` Python
  client over real MCP-over-stdio. A passthrough test, not a schema test — the shim carries no
  tool-schema knowledge, so "one `initialize` and one `tools/call` round-trip, with the bearer
  header attached and `mcp_url` honoured" is the whole of what there is to assert. Skips
  automatically if Node isn't on `PATH`; CI installs it, so this runs there.

## 2. Local-only checks — run manually before opening/updating a relevant PR, never in CI

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

**Never run in CI.** It reuses the exact OAuth token files `privacyfence-app --<connector>-oauth`
writes to the git-ignored `credentials/` directory, and only ever targets one specific,
`[QATEST]`-tagged seed artifact per connector — set up once per environment via
[`qa-environment-setup.md`](qa-environment-setup.md), resolved through the non-secret, git-ignored
manifest `tests/fixtures/qa_environment.yaml` (see
[`qa_environment.yaml.example`](../tests/fixtures/qa_environment.yaml.example) for the template). No
credential is ever provisioned to GitHub Actions or any other cloud service to make this possible.

Two modes:

- `--check [connector ...]` — calls each connector's read methods against its seed artifact,
  asserts non-empty/expected results, prints a report. Never writes a file. Safe to run any time.
- `--record [connector ...]` — the same calls, plus identity-field redaction (author email, account
  id, display name, ...) and structural de-identification (opaque resource ids, decorative URLs —
  neither of which any test actually depends on the specific value of), then writes the result to
  `tests/fixtures/live/<connector>/<method>.json`.

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

[`connector-qa-testing.md`](connector-qa-testing.md) drives every tool through a live Claude
Cowork/Desktop session connected to the real `privacyfence` daemon, against real accounts, watching
what actually prompts. This is the only thing that exercises the gate, the popup UI, and the audit
log end to end — none of tiers 1 or 2 do. Run it before a release, or after any change to
`gate.py`/`auto_accept.py`/`resource_grants.py`/the web approval UI broadly, not on every PR.

Before a release specifically, run tiers 1 and 2 across every connector too, not just the ones a
recent PR touched — see [manual-pre-release-test-plan.md](manual-pre-release-test-plan.md) for the
full release-time checklist tying all three tiers together.

## Quick reference

| Check | Runs in CI? | When |
|---|---|---|
| `pytest` (full suite, incl. the mcp/daemon and shim/mcp contract tests) | Yes, every PR | Always — this is the merge gate |
| `npm test` (mcpb/shim/'s own suite) | Yes, every PR | Always — this is the merge gate |
| `npm run typecheck` (mcpb/shim/) | Yes, every PR | Always — this is the merge gate |
| `qa_fixture_recorder.py --check` | No | PR touches a `*_client.py`/`connectors/**` file |
| `qa_web_smoke.py` | No | PR touches `web_shell.py`, `approval_list_html.py`, web routes' JS, `resources/sw.js`, or the CSP |
| `connector-qa-testing.md`'s live Cowork pass | No | Before a release, or a broad gate/auto-accept change |

None of the "No" rows require a credential or secret to ever be granted to GitHub Actions or any
other cloud CI — they exist specifically because that's not something this project is willing to do,
not as a stopgap until it is.
