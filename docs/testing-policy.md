# Testing policy

PrivacyFence separates tests by what they prove and by the trust boundary they require. Automated checks should carry objective correctness; manual checks are reserved for human judgment and first-time third-party consent behavior.

## Test layers

| Layer | Purpose | Current execution |
|---|---|---|
| Unit | Python logic in isolation | `tests/unit/`, ordinary CI |
| Integration | Real internal stack without external provider credentials | `tests/integration/`, ordinary CI |
| Cross-platform system | OS-specific path/process/locking/daemon behavior | Windows job available through `workflow_dispatch`; broader automation tracked in the active test plan |
| Browser system | Real browser/CSP/DOM behavior | Playwright/Chromium integration coverage in ordinary CI |
| Live connector | Provider API/response drift | scheduled self-hosted runner plus manual dispatch |
| Packaged artifact | Installer/package correctness | release build/smoke paths; expansion tracked in the active test plan |
| Manual exploratory/UX | Subjective presentation and first-time consent screens | human-only when relevant |

Pytest markers are registered in `pyproject.toml` for `unit`, `integration`, `system`, `browser`, `packaged`, and `live`.

## Ordinary pull-request CI

`.github/workflows/tests.yml` runs the main test suite on Ubuntu for pull requests and pushes to `main`.

The primary job installs the test dependencies, installs Chromium for Playwright, builds/tests/type-checks the Node MCP shim, then runs pytest with branch coverage and the coverage-floor ratchet:

```bash
npm test
npm run typecheck
pytest -v --cov=src/privacyfence --cov-branch --cov-report=term-missing --cov-report=json:coverage.json
python scripts/check_coverage_floor.py coverage.json
```

The same workflow also runs reduced Python compatibility coverage for Python 3.11 and 3.12, and a separate static-analysis job. Ruff is blocking. Mypy and Bandit are currently informational in that workflow.

The main CI jobs do not require live connector credentials and must remain safe to run for ordinary pull requests.

## Browser coverage

`tests/integration/test_browser_smoke.py` uses Playwright with Chromium to exercise browser behavior that unit-level HTML tests cannot prove, including authentication/session behavior, approval interactions, security headers/CSP, previews, and org-mode WebAuthn UI paths.

Subjective visual quality is not treated as an automated pass/fail signal. Use [`release-testing.md`](release-testing.md) when a change needs human visual review.

## Live-provider checks

`.github/workflows/connector-live-check.yml` runs only on its schedule or by explicit manual trigger. It targets the project-controlled self-hosted runner labeled `privacyfence-test`.

Real connector credentials live on that runner, outside the GitHub-hosted workspace. The job builds a fresh virtual environment, copies the runner-local QA state into the ephemeral checkout, runs the fixture recorder/checker, exercises the configured lifecycle checks, and opens a fixture-drift pull request when re-recording changes committed fixtures.

Provisioning and recovery procedures are in [`connector-live-check-setup.md`](connector-live-check-setup.md).

## Windows test job

`tests.yml` contains a `windows-latest` pytest job. It is currently gated to `workflow_dispatch`, so it is not a permanent per-PR merge gate. Windows packaging is separately built in `.github/workflows/build.yml`.

## Release/package tests

Release workflows build the native artifacts described in [`platform-support.md`](platform-support.md). Artifact-specific smoke coverage lives with the release/build path where practical. Remaining cross-platform, installer, and package-lifecycle automation is tracked only in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).

## Coverage policy

Coverage is a ratchet rather than a single repository-wide target percentage. `scripts/check_coverage_floor.py` records/enforces floors for overall coverage and selected security-critical modules. A pull request that improves a protected module can raise its floor; lowering a floor should be treated as a real regression requiring explicit justification.

Coverage reports are uploaded as CI artifacts so failures can be diagnosed without reproducing the run locally.

## What stays manual

A recurring test should remain manual only when automation cannot reliably determine pass/fail. In this repository that normally means:

- subjective visual judgment such as spacing, contrast, or OS-native presentation;
- first-time third-party consent/authentication screens controlled by an external provider;
- focused exploratory investigation of a new or unexplained provider behavior.

Everything else should be automated or tracked as automation work in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).
