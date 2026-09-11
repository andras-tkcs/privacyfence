# PrivacyFence documentation

This directory documents PrivacyFence as it works in the current source tree. Runtime code, build scripts, configuration examples, and CI workflows are the source of truth when behavior changes.

## Start here

- [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md) — architecture, runtime, MCP transport, configuration, state, approvals, connectors, audit logging, and packaging.
- [`security-and-compliance.md`](security-and-compliance.md) — security boundaries, authentication, authorization, privacy controls, audit integrity, and deployment considerations.
- [`testing-policy.md`](testing-policy.md) — test layers, CI execution, live-provider checks, and what remains manual.
- [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) — the only active implementation plan in `docs/`; tracks remaining test-automation work.

## User and operator guides

- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) — deploy and configure centralized org mode.
- [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md) — backup, restore, upgrades, restart behavior, availability, and operations.
- [`org-mode-download-delivery.md`](org-mode-download-delivery.md) — org-mode inline and staged file delivery.
- [`platform-support.md`](platform-support.md) — macOS, Windows, and Linux packaging/support matrix.
- [`dev-vs-live-setup.md`](dev-vs-live-setup.md) — isolate source-development and packaged installations.
- [`release-testing.md`](release-testing.md) — current release validation that still requires a human.

## Approval, policy, and data handling

- [`always-allow-rules-reference.md`](always-allow-rules-reference.md) — standing-rule behavior and supported rule shapes.
- [`approval-list-ui-ux.md`](approval-list-ui-ux.md) — current approval-list interaction model.
- [`approval-window-content-reference.md`](approval-window-content-reference.md) — current approval-card and confirmation content.
- [`claude-knowledge-boundary.md`](claude-knowledge-boundary.md) — what MCP clients can know before and after approval.
- [`file-type-support.md`](file-type-support.md) — attachment preview, extraction, and PII-scan support.
- [`pii-detection-keywords.md`](pii-detection-keywords.md) — PII detector categories, patterns, and language-specific keywords.

The primary runtime modules for this area are `src/privacyfence/gate.py`, `approvals.py`, `auto_accept.py`, `privacy_filter.py`, `pii_detector.py`, `text_extraction.py`, and `src/privacyfence/web/`.

## Connector setup

- [`google-cloud-setup.md`](google-cloud-setup.md)
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md)
- [`telegram-setup.md`](telegram-setup.md)

Connector implementation lives under `src/privacyfence/connectors/`; daemon construction and per-principal connector lifecycle are in `daemon_main.py`, `connector_host.py`, and `connector_registry.py`.

## Development and QA

- [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) — coding and test-writing expectations.
- [`testing-policy.md`](testing-policy.md) — what runs automatically and where.
- [`connector-live-check-setup.md`](connector-live-check-setup.md) — self-hosted live-provider runner setup.
- [`qa-environment-setup.md`](qa-environment-setup.md) — dedicated QA accounts and reusable seed data.
- [`connector-qa-testing.md`](connector-qa-testing.md) — extended connector/gate exploratory QA.
- [`release-testing.md`](release-testing.md) — manual release checks that automation cannot reliably judge.

CI and build behavior is defined in `.github/workflows/`, `pyproject.toml`, `tests/`, and `scripts/`.

## Architecture decisions and assets

- [`adr/0001-remove-macos-native-extra.md`](adr/0001-remove-macos-native-extra.md) — current decision that PrivacyFence has no AppKit/PyObjC runtime dependency.
- [`images/screenshots/README.md`](images/screenshots/README.md) — screenshot generation and maintenance.
- `images/` — diagrams and screenshots referenced by documentation.

## Documentation rules

Documentation in this directory is a standing reference, not a changelog. Describe what the current implementation does and the boundaries it currently has. Do not preserve completed implementation plans, migration narratives, phase names, or “before/after” history in standing docs.

The one exception is [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md), which is intentionally a live plan while test automation work remains open. When that work is complete, remove or convert it rather than leaving a completed plan in `docs/`.

When behavior changes, update the nearest standing reference in the same pull request. Prefer stable module, command, route, configuration-key, and workflow names over line numbers or historical pull-request identifiers.
