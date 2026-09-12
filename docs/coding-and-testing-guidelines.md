# Coding and testing guidelines

These are the repository's standing expectations for implementation and test changes. It is
descriptive before it is prescriptive: every rule below reflects a pattern already established in
`src/privacyfence/` and `tests/` — the Python daemon — not a generic style guide imported wholesale.
`mcpb/shim/` (the Node/TypeScript stdio-to-`/mcp` transport proxy Claude Desktop's `.mcpb` installs)
follows its own, separate conventions and is only referenced here where a change on its side of a
contract needs a matching Python-side check.

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for process (PRs, issues, license) and
[`security-and-compliance.md`](security-and-compliance.md) for the security model this code
implements. This document is about how to write and test the code correctly, not why it exists.

## 1. Coding guidelines

Keep policy/security decisions in shared layers rather than connector-specific shortcuts — a new or
changed connector tool must use the same gate, privacy-filter, audit, secure-file, and
principal-scoping mechanisms as equivalent existing tools. Prefer explicit, testable boundaries
over hidden global state: keep filesystem/network/provider behavior behind modules that can be
exercised independently.

### 1.1 Language baseline

- Python 3.11+. Every module starts with `from __future__ import annotations` (right after the
  module docstring, before other imports).
- Prefer the standard library over new dependencies (stated in `CONTRIBUTING.md`).
- Use modern union syntax, `X | None`, not `Optional[X]`. The codebase has no remaining
  `Optional[...]` call sites — keep it that way in new code.
- Type-hint function signatures, including return types (`-> None`, `-> Any`, etc.). Dataclass
  fields are always typed.

### 1.2 Module & docstring conventions

- Every module has a docstring as the first line of the file — even a one-liner
  (`"""Gmail connector."""`). For modules with non-obvious lifecycle or invariants (e.g.
  `daemon_main.py`, `gate.py`, `audit_log.py`), the docstring explains the *why*: threading model,
  ordering guarantees, what a caller must not assume.
- Default to no comments. Only add one when it captures a non-obvious *why* — a hidden constraint,
  a race that was fixed, a workaround for a specific API quirk — never a restatement of *what* the
  next line does.
- Long files use `# --- ... --- #` banner comments to separate phases (e.g. `gmail.py`'s method
  groups). Use this once a connector or client grows past ~5-6 methods; don't bother for short
  files.

### 1.3 Data modeling

- Use `@dataclass` for structured data crossing a boundary (API responses, tool specs, audit
  entries). Give collection fields `field(default_factory=...)`, never a mutable default.
- Dataclasses that normalize an external API's response document what they deliberately *don't*
  carry (e.g. `Attachment`: "Content is intentionally never carried here") when the omission is a
  privacy decision, not just an oversight.
- `Connector` subclasses (`connector.py`) are the unit of extension. Adding a service means: one
  file in `connectors/`, registered in `daemon_main.py`, nothing else changes. Don't special-case a
  connector's wiring elsewhere.

### 1.4 Error handling

- Every external-API client (`*_client.py`) defines its own `<Name>ClientError(Exception)` and
  raises only that (or lets it propagate) across its public methods. Internal-only clients that
  never leave the local trust boundary (talking over the embedded `/mcp` HTTP endpoint to the
  daemon the app itself controls) are the one accepted exception to this — external cloud APIs
  always get a dedicated error type.
- Connectors catch the client's specific error type at the boundary, log it, and re-raise as
  `RuntimeError(str(exc)) from exc` — never swallow it, never let the raw client exception or a
  bare `except Exception` leak past the connector into the tool-call response.
- Non-critical side effects (writing an audit entry) are wrapped in their own
  `try/except Exception: logger.warning(...)` so a logging failure never blocks the primary
  operation — see `gate.py::_audit` and every connector's `_auto_audit`.

### 1.5 The gate is load-bearing — treat it as a security boundary, not plumbing

This is the one area where "looks like a style rule" is actually a security invariant:

- Every tool call that touches real data must go through `gated_call()` (`gate.py`), **or** be a
  connector that deliberately auto-approves a whole tool and says so in its own docstring/comments
  (e.g. `contacts.py`'s unconditionally-auto-accepted tools, or the read-only listing tools every
  connector has). There is no third option — a tool that silently skips both the gate and an
  explicit auto-approve rationale is a bug.
- `gated_call()` must never return `raw_data` when `filtered_data` differs from it. This is stated
  verbatim in `tests/unit/test_gate.py`'s module docstring — it's the actual privacy boundary the
  whole gate exists to enforce.
- `preview` dicts (shown before approval) carry metadata only — sender, subject, size, destination
  path. Full body/content only ever goes into `details_text`, which the user must actively expand
  to see. Never put message bodies, file contents, or similar into `preview`.
- Every tool a connector exposes must leave an audit trail, one way or another — either through
  `gated_call` (which always audits, on every branch) or via a direct `_auto_audit`-style call for
  auto-approved tools. `tests/helpers.py::assert_all_tools_leave_an_audit_trail` enforces this
  mechanically; don't add a tool that this helper can't verify.
- Writes default to `gate="popup"`, reads of full content default to `gate="review"`; only
  low-sensitivity metadata listing calls (`list_messages`, `list_task_lists`, ...) default to
  `read_only=True` with no gate at all. If a new tool doesn't fit one of these three buckets
  cleanly, that's a design question worth raising explicitly, not silently defaulting to whichever
  is least effort.

### 1.6 Untrusted input

- Any filename, path fragment, or identifier that originates from a remote party (an email
  attachment name, a message header) is untrusted. Before it touches the filesystem, sanitize it —
  the established pattern is `os.path.basename(filename) or "<fallback>"`
  (`gmail_client.py::resolve_attachment_destination`, `download_staging.py::stage`), which is what
  stops a crafted name like `../../.ssh/authorized_keys` from writing outside the intended
  directory. Compute the destination path once, in one function, and reuse it for both the
  pre-approval preview and the actual write — never compute it twice, or the preview and the real
  write can silently disagree.

### 1.7 Async & concurrency

- Blocking/sync calls (the Google/Slack/Salesforce/Atlassian SDKs, file I/O in a hot path) are
  wrapped in `asyncio.to_thread(...)`, never called directly from an `async def`.
- Approval dialogs (the popup itself, the PII confirmation, the "Always allow" rule confirmation)
  run on their own dedicated thread-pool executor (`gate._popup_executor`), not `asyncio.to_thread`'s
  shared default pool — a slow connector call (a Slack rate-limit retry sleeping out a
  `Retry-After` window, say) can otherwise occupy every worker in that shared pool and starve a
  popup that has nothing to do with it.
- `WebApprovalUI` can show several pending approvals at once — there is no single lock serializing
  dialogs. Instead, each gate interaction re-checks the current auto-accept state immediately before
  resolving (see each branch's own `_interact` closure), so a rule created by one
  concurrently-resolving approval is picked up by another still in flight, and
  `approvals.PendingApprovalRegistry.reevaluate_all()` re-evaluates anything already parked in the
  pending/registry state.

### 1.8 Logging vs. `print`

- Library/application code (clients, connectors, the daemon's request handling) logs via
  `logging.getLogger(__name__)`. Do not log secrets, bearer/session/bootstrap tokens, connector
  credentials, full message bodies, document contents, or protected provider content, unless the
  existing audit/data model explicitly requires a safe representation — log identifiers, subjects,
  counts instead.
- `print()` is reserved for the handful of argparse CLI subcommands in `daemon_main.py`
  (`--oauth-setup`-style flows) that are invoked directly by a human at a terminal and need to
  print a human-facing confirmation. It does not belong inside a `*_client.py` method — those are
  library code, called from multiple contexts, and should only log; the CLI entry point that calls
  them is responsible for any terminal-facing `print`.

### 1.9 Formatting and static analysis

Run Ruff on changed Python code:

```bash
ruff check .
```

`pyproject.toml` is authoritative for Ruff, mypy, Bandit, pytest, and coverage configuration. Ruff
is a blocking CI check; mypy and Bandit are visible informational checks in the current test
workflow.

For Node/TypeScript changes under `mcpb/shim/`, run:

```bash
cd mcpb/shim
npm test
npm run typecheck
npm run build
```

## 2. Testing guidelines

### 2.1 Framework & layout

- `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed).
  `pytest-timeout` caps every test at 30s so a hung fixture fails loudly instead of stalling CI.
  `freezegun` for time-dependent tests, `openpyxl` for asserting against the audit log's Excel
  export.
- `tests/unit/` mirrors `src/privacyfence/`; connector tests live in `tests/unit/connectors/`,
  named `test_<connector>_connector.py`. One test module per source module. `tests/integration/`,
  `tests/system/`, and `tests/platform/` hold cross-boundary, full-scenario, and OS-level tests
  respectively — see [`testing-policy.md`](testing-policy.md) for the full seven-layer taxonomy and
  what runs where.
- New tests carry the marker that matches their layer: `unit`, `integration`, `system`, `platform`,
  `browser`, `packaged`, or `live`.
- Run the smallest relevant test set while developing, then the affected unit/integration suite
  before opening/updating a PR. CI runs the full suite on Linux, Windows, and macOS runners (see
  `testing-policy.md`); a 100% pass rate is required to merge on all of them.
- Keep ordinary unit/integration tests offline and deterministic. Do not add real connector
  credentials or unmocked provider calls to GitHub-hosted CI.

### 2.2 Module & class organization

- Every test module opens with a docstring naming the module under test and, where relevant, the
  one invariant that matters most (see `test_gate.py`, `test_gmail_connector.py`). If a whole test
  file exists to prevent one specific class of bug, say so up top.
- Group tests into `class TestScenario:` blocks by behavior (`TestAutoAcceptPath`,
  `TestReviewGateDecisions`, `TestAcceptAll`, ...), not by method-under-test or in one flat list.
  Bare module-level `def test_...` functions are the exception, reserved for
  structural/cross-cutting checks that aren't about one component's behavior (e.g.
  `test_readme_manifest_alignment.py`, which checks docs stay in sync with the tool manifest).
- Regression tests carry a docstring explaining the original bug, not just what they assert — see
  `TestGetMessagePreviewMinimization`'s "a prior real bug: reply-all only checked the original
  sender" note in `test_gmail_connector.py`. The point is that a future reader can tell *why* the
  test exists before deciding it's safe to delete or weaken.

### 2.3 Fixtures & isolation

- Module-level singletons (`auto_accept._INSTANCE`, `audit_log._INSTANCE`, and their supporting
  module globals) are reset in an `autouse=True` fixture in `tests/conftest.py`, before *and*
  after each test. Any new module-level singleton needs a matching reset added there, or state
  leaks between tests silently.
- Use the `tmp_path` fixture with `init_audit_logger(str(tmp_path))` to get an isolated audit log
  directory per test — never point tests at the real `logs/audit/` directory.
- A lock or other primitive that binds itself to whichever asyncio event loop first contends on it
  (e.g. `asyncio.Lock`) needs its own `autouse` per-test reset if tests exercise real contention on
  it.

### 2.4 Faking the gate and the approval UI

- Never let a test spawn a real interactive dialog. Stub `show_popup` / `show_read_popup` /
  `show_rule_confirmation_popup` via `monkeypatch.setattr` on the module under test, not by mocking
  at `WebApprovalUI`'s own import site of every caller.
- Connector tests stub `gated_call` itself (the `gated_call_spy` pattern used in
  `test_gmail_connector.py`, `test_jira_connector.py`, `test_confluence_connector.py`) to capture
  exactly what a tool sends into the gate — `preview`, `details_text`, `raw_data`, `filtered_data`,
  `args`, `gate` — and assert on those kwargs, rather than trying to drive the real gate end-to-end
  from a connector test. `test_gate.py` owns proving the gate's own state machine; connector tests
  own proving each tool calls it correctly.

### 2.5 Reuse shared helpers before writing new ad hoc ones

- `tests/helpers.py` provides `make_ctx` (a `ReviewContext` with sane defaults),
  `build_stub_args` (a minimal-but-plausible args dict from a `ToolSpec`),
  `assert_all_tools_leave_an_audit_trail`, and `assert_no_placeholder_fields` (fails loudly the
  moment a `_parse_*` field mapping silently degrades to a fallback value). Check there before
  writing a new stub-args builder, a new per-connector audit-trail sweep, or a new
  placeholder-field check — duplicating these tends to drift out of sync with the real
  `Connector`/`ToolSpec` shape over time.

### 2.6 New-connector checklist

A new connector's test module should include, at minimum:

1. `TestDispatch` — unknown tool name raises `ValueError`.
2. One test per auto-approved tool proving it never touches the gate and does write its own audit
   entry.
3. One test per gated tool's `preview` dict, asserting it contains *only* metadata (data
   minimization) — never full body/content.
4. A call to `assert_all_tools_leave_an_audit_trail` covering every tool the connector declares,
   with `arg_overrides` for any tool that validates its args before reaching the client/gate.
5. For at least one gated read tool, an end-to-end test that runs a fully-populated raw API
   response through the real `_parse_*` method and the real connector method that builds the popup
   preview (not a hand-built dataclass, unlike the rest of this checklist), checked with
   `assert_no_placeholder_fields` — see `TestFieldCompleteness` in
   `tests/unit/connectors/test_confluence_connector.py` for the pattern this catches (a `_parse_*`
   field mapping silently degrading to a fallback value).

### 2.7 Definition of done for a PR touching this repo

- [ ] `pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing
      --cov-report=json:coverage.json` passes at 100%, and `python scripts/check_coverage_floor.py
      coverage.json` passes (the coverage ratchet — see `testing-policy.md`).
- [ ] `ruff check .` passes (CI's `static-analysis` job blocks on this; `mypy`/`bandit` run in the
      same job but are informational only for now — see `[tool.mypy]`/`[tool.bandit]` in
      `pyproject.toml`).
- [ ] Every new/changed tool call still resolves through `gated_call` or an explicit
      always-auto-approve connector, and leaves an audit trail either way.
- [ ] No preview dict carries full content; no log line carries a credential or a message/document
      body.
- [ ] New client code has a matching `<Name>ClientError`; new connector code catches it and
      re-raises as `RuntimeError`.
- [ ] New module-level singletons have a reset added to `tests/conftest.py`.
- [ ] Comments only where the *why* is non-obvious; no restated-*what* comments.
- [ ] If this PR touches a `src/privacyfence/*_client.py` or `src/privacyfence/connectors/**` file:
      run `scripts/qa_fixture_recorder.py --check <connector>` locally against a real account per
      [`qa-environment-setup.md`](qa-environment-setup.md), and paste its report into the PR
      description — see [`testing-policy.md` §2.1](testing-policy.md#21-qa_fixture_recorderpy---check----record).
- [ ] If this PR changes `web/mcp_dispatch.py`, `web/routes_mcp.py`, `connector.py`'s
      `ToolSpec`/`ToolParam` shapes, or `web/server.py`'s socket-binding/lifecycle: run
      `pytest tests/integration -v` locally and confirm `test_mcp_daemon_contract.py` still passes
      — it drives a real, socket-bound daemon with the official `mcp` client, catching what the
      in-process `test_routes_mcp.py` transport can't.
- [ ] If this PR changes `web/mcp_auth.py`, `web/server.py`'s `mcp_url` discovery-file writing, or
      anything under `mcpb/shim/src/`: run `pytest tests/integration -v` locally (needs Node on
      PATH) and confirm `test_shim_mcp_contract.py` still passes — a change on one side of the
      shim<->`/mcp` contract without the other only fails there, not in either side's own unit
      tests.
- [ ] If this PR changes a dependency in `pyproject.toml` (a version bound, a new package, an
      extra): run `scripts/update_dependency_locks.sh` (needs `python3.13 -m pip install pip-tools`
      — Python 3.13 specifically, see the script's own comments for why) and commit the resulting
      `requirements/*.lock.txt` — `dependency-audit.yml`'s `lockfile-freshness` job fails the build
      otherwise.

## Test design

Prefer tests that assert observable contracts rather than implementation trivia. For gated connector operations, verify the complete relevant outcome:

- selected gate/policy path;
- whether the connector was called;
- returned result/error;
- approval state/decision;
- audit entry;
- rule/grant side effect when applicable.

Use parameterization where multiple connector/tool states share the same invariant.

Avoid fixed sleeps for synchronization when an event, condition poll, socket readiness check, or explicit signal can make the test deterministic. Every async/process test must fail in bounded time rather than hang indefinitely.

## Browser tests

Use the existing Playwright integration harness for behavior that only a real browser can prove: CSP enforcement, browser session behavior, JS/DOM ordering, approval interactions, responsive structure, service-worker/notification behavior, and org-mode WebAuthn UI.

Do not use fragile pixel-perfect screenshots as the primary correctness assertion. Subjective visual quality belongs in [`release-testing.md`](release-testing.md).

## Connector/provider tests

Parser/client unit tests may replay committed redacted live fixtures from `tests/fixtures/live/`; they must not require a network connection.

Real provider drift checks run through the dedicated self-hosted workflow and `scripts/qa_fixture_recorder.py`. See [`connector-live-check-setup.md`](connector-live-check-setup.md) and [`qa-environment-setup.md`](qa-environment-setup.md).

Never commit real account identifiers, access tokens, tenant URLs, private content, or unredacted provider payloads in fixtures.

## Security-sensitive changes

Changes to auth/session/CSRF/CSP, org identity/principal scoping, connector token storage, approval/gate behavior, PII filtering, audit integrity/forwarding, configuration trust, staged downloads, or MCP authentication require targeted negative tests in addition to the happy path.

Fail closed on malformed/unknown security configuration. A test should prove rejection rather than only proving that valid configuration works.

Use the existing secure filesystem helpers for credential/security-sensitive files instead of open-coding permissions.

## Packaging changes

When changing PyInstaller specs, installers, Debian metadata, startup registration, MCPB contents, or release workflows, run the corresponding package/build checks and update [`platform-support.md`](platform-support.md) if user-visible behavior changes.

Do not use a source checkout as proof that a packaged artifact works.

## System/packaged-artifact test diagnostics

New `pytest.mark.system`/`pytest.mark.packaged` tests get CI-diagnostics capture (`tests/diagnostics.py`, `docs/automated-test-strategy-plan.md` Phase 10) for free, without any per-test code, as long as the test's own daemon home/install directory lives under its `tmp_path` (directly or via a fixture it depends on — see `test_windows_packaged_smoke.py`'s `home = tmp_path / "home"`) and any subprocess log is named `daemon.log`, `install*.log`, or `uninstall*.log`, or is a `*.jsonl` audit log. A test that instead drives a real system-wide install (`dpkg -i`, not a `tmp_path`-scoped one) needs its own small capture call into `tests.diagnostics.failure_dir()`/`suite_name_for()` — see `test_deb_packaged_lifecycle.py`'s `_capture_installed_file_manifest` for the pattern.

## Documentation

Update standing documentation in the same PR as behavior changes. Standing docs describe current behavior, not implementation history. Do not add completed plans, phase narratives, migration diaries, or “previously/after X” explanations.

The sole active plan is [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md); new testing gaps should go there only when they represent actual planned work rather than current policy.
