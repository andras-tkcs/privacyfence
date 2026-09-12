# Coding and testing guidelines

These are the repository's standing expectations for implementation and test changes.

## General code changes

Keep policy/security decisions in shared layers rather than connector-specific shortcuts. A new or changed connector tool must use the same gate, privacy-filter, audit, secure-file, and principal-scoping mechanisms as equivalent existing tools.

Prefer explicit, testable boundaries over hidden global state. Keep filesystem/network/provider behavior behind modules that can be exercised independently.

Do not log secrets, bearer/session/bootstrap tokens, connector credentials, or protected provider content unless the existing audit/data model explicitly requires a safe representation.

## Formatting and static analysis

Run Ruff on changed Python code:

```bash
ruff check .
```

`pyproject.toml` is authoritative for Ruff, mypy, Bandit, pytest, and coverage configuration. Ruff is a blocking CI check; mypy and Bandit are visible informational checks in the current test workflow.

For Node/TypeScript changes under `mcpb/shim/`, run:

```bash
cd mcpb/shim
npm test
npm run typecheck
npm run build
```

For changes under `cloudflare/downloads/` (the downloads.privacyfence.eu Worker,
[`release-publishing-kpi-plan.md`](release-publishing-kpi-plan.md) Phase 1), run:

```bash
cd cloudflare/downloads
npm test
npm run typecheck
npm run dry-run
```

`npm test` runs entirely against local Miniflare/workerd -- no Cloudflare credentials or network
access to the real R2/D1 resources involved.

## Python tests

Run the smallest relevant test set while developing, then the affected unit/integration suite before opening/updating a PR. The full CI command is described in [`testing-policy.md`](testing-policy.md).

New tests should use the marker that matches their layer when applicable: `unit`, `integration`, `system`, `browser`, `packaged`, or `live`.

Keep ordinary unit/integration tests offline and deterministic. Do not add real connector credentials or unmocked provider calls to GitHub-hosted CI.

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
