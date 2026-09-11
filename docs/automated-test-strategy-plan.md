# Automated Test Strategy — Implementation Plan

This is the only active implementation plan under `docs/`. It tracks test automation that is not yet part of the current repository baseline. Completed work is removed from this document rather than kept as project history.

## Current automated baseline

The repository already has:

- full Ubuntu pull-request CI with pytest, branch coverage, coverage-floor enforcement, Playwright/Chromium, Node shim tests, TypeScript type-checking, and shim build;
- Python 3.11 and 3.12 compatibility jobs;
- blocking Ruff plus informational mypy and Bandit;
- registered pytest markers for unit, integration, system, browser, packaged, and live layers;
- a scheduled self-hosted live-provider workflow that checks/redacts recorded fixtures, reports fixture age, re-records drift, exercises supported provider write lifecycles, and opens a fixture-drift PR when required;
- integration coverage for the real daemon/MCP/approval flow;
- browser smoke coverage for the embedded web UI;
- macOS packaged smoke coverage and Linux org-mode release smoke coverage.

[`testing-policy.md`](testing-policy.md) describes what currently runs. This document describes only the gaps below.

## 1. Apps Script live-fixture coverage

Apps Script is implemented as a connector but does not have the same committed live-response fixture coverage as the connectors handled by `scripts/qa_fixture_recorder.py`.

Add an Apps Script QA resource, recorder/check support, a redacted committed fixture, and the same fixture-presence guard used for the other recorded providers.

## 2. Cross-platform source CI

The main suite runs on Ubuntu. A Windows pytest job exists but is manual-dispatch-only, and there is no equivalent permanent macOS source/runtime job.

Create a small platform-focused suite for path resolution, secure directory creation, instance locking, process spawning/cleanup, browser launching, daemon discovery, and OS-specific filesystem behavior. Run that targeted suite on Windows and macOS for pull requests without duplicating the complete Ubuntu matrix.

## 3. Canonical local-mode system test on all desktop OSes

Create one `system`-marked scenario that starts the real daemon, connects through MCP, creates an approval, resolves Allow and Deny decisions, verifies audit output, and shuts down cleanly.

Run the same scenario on Ubuntu, Windows, and macOS, with only the platform-specific state/path assertions varying by runner.

## 4. Browser/UI automation gaps

Extend `tests/integration/test_browser_smoke.py` rather than creating a parallel browser harness. Add objective assertions for:

- empty and multi-item approval-list behavior;
- return-to-list toast and stale/double-decision handling;
- PII banner/confirmation behavior with deterministic synthetic PII;
- responsive layout at representative phone/tablet/desktop viewports;
- structural light/dark-mode behavior;
- useful failure artifacts such as screenshot, console output, relevant DOM, and daemon log.

Subjective visual quality remains manual.

## 5. Gate/policy coverage audit

Audit the existing parameterized gate tests against the complete policy state machine. Ensure each path asserts the gate selected, connector execution or non-execution, result/error, approval state, audit decision, and rule/grant side effect.

Add only genuinely missing cases; extend existing parameterized suites instead of duplicating coverage.

## 6. Packaged-artifact lifecycle automation

### macOS

Keep the existing packaged smoke as the base. Add only any missing assertions for package ownership, signature/notarization validation, state location, and cleanup behavior.

### Windows

Add an installer smoke that performs install, daemon startup, settings/MCP discovery, an approval round trip, audit verification, uninstall, and state-preservation checks. Add upgrade-state preservation as a separate follow-up once the base lifecycle is reliable.

### Debian/Ubuntu

Automate `.deb` install/remove/purge and state-preservation checks against the package built by `scripts/build_deb.sh`. Add a repeatable N→N+1 state-preserving upgrade test.

Release workflows should block publication when their native package smoke fails.

## 7. Login/autostart verification

Automate desktop-session startup only where packaging smoke cannot prove it:

- Debian/Ubuntu: install the `.deb`, start a real graphical session/login, verify XDG autostart launches the daemon, and exercise the system contract;
- Windows: install, sign out/reboot, sign in, verify the Task Scheduler startup path, and exercise the system contract.

Keep this tier periodic/release-oriented rather than per-PR if the infrastructure cost is high.

## 8. Org-mode system coverage audit

Use `tests/integration/test_org_ubuntu_release_smoke.py` as the canonical org-mode system harness. Confirm it covers unauthenticated rejection, authenticated principal/policy application, approval + audit principal correctness, restart/state survival, signed configuration, and reverse-proxy behavior. Extend that module only where a gap exists.

## 9. Release QA reduction

As automation above lands, keep [`release-testing.md`](release-testing.md) limited to checks automation cannot judge reliably: subjective visual review, first-time external consent screens, OS-native security presentation, and focused exploratory investigation.

Routine releases should not require manually replaying assertions already covered by CI.

## 10. CI diagnostics

For system and packaged-artifact failures, upload enough bounded diagnostic artifacts to debug from CI: daemon/audit logs, pytest output, browser console/DOM/screenshots where relevant, installed-file manifests, and OS/runtime versions.

Avoid blanket retries for deterministic tests. Retries for live-provider checks should be narrowly scoped to known transient conditions such as rate limiting or temporary network failure.

## Completion criteria

This plan is complete when ordinary pull requests and release workflows provide automated evidence for the supported source/runtime/package surfaces on macOS, Windows, Linux local mode, Linux org mode, the browser UI, MCP clients, and supported live providers, leaving only genuinely subjective or externally controlled checks for humans.

When those criteria are met, remove this plan from `docs/` or convert any still-useful stable material into standing reference documentation.
