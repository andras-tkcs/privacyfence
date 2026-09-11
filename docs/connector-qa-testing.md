# Connector QA testing

Use this guide for focused exploratory testing of connector behavior against dedicated QA accounts. Routine correctness is covered by automated unit/integration/live-provider tests; this guide is for new connectors, material connector changes, provider regressions, or investigations that need a real provider/account.

## Prerequisites

- set up the dedicated QA resources from [`qa-environment-setup.md`](qa-environment-setup.md);
- authenticate only dedicated QA accounts;
- run PrivacyFence from the source checkout or package you intend to test;
- keep `tests/fixtures/qa_environment.yaml` available to the QA scripts;
- do not use production/personal data.

## 1. Provider contract check

Run the live fixture checker for the affected connector(s):

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --check <connector>
```

If the provider response shape has legitimately changed, inspect the live response and parser/client code, then re-record only after confirming the change is expected:

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --record <connector>
```

Review the generated diff for identity/content leakage before committing it.

For supported write-capable providers, run the bounded create/read/update/delete lifecycle check:

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --lifecycle <connector>
```

## 2. Tool surface

Exercise the connector's representative read/search/list/get operations and any create/update/delete/send/upload operations it exposes.

For each operation verify:

- the provider call targets the intended QA resource/account;
- returned content/metadata is parsed correctly;
- errors are converted to the connector's expected safe error shape;
- pagination/empty-result behavior is sensible where applicable;
- no credential/token/internal state appears in MCP-visible output.

## 3. Gate behavior

For representative tools verify the gate metadata and resulting behavior match the implementation contract:

- auto-allowed operations execute without an unexpected approval;
- review-gated reads do not release protected result content before approval;
- write/sensitive operations require confirmation unless a matching standing rule permits them;
- Deny/Cancel prevents connector execution or protected release as appropriate;
- PII-sensitive results trigger the configured privacy/confirmation behavior;
- audit entries record the correct connector/tool/decision/principal context.

Do not treat this exploratory run as the primary proof for every gate state; deterministic gate coverage belongs in the automated suite.

## 4. Approval UI

For changes affecting card content or connector metadata, inspect the browser approval surface:

- list row identifies the connector/tool correctly;
- Review opens the expected card;
- card target/details/preview are sufficient to understand the request;
- Deny/Allow outcomes match the connector result;
- eligible always-allow choices create only the intended scoped rule;
- sensitive content is not exposed in notification/list summaries beyond the configured detail level.

See [`approval-list-ui-ux.md`](approval-list-ui-ux.md) and [`approval-window-content-reference.md`](approval-window-content-reference.md).

## 5. Connector authorization

When authentication code changes, test connect/reconnect/revocation with the dedicated QA account. For org mode, confirm authorization is scoped to the signed-in principal and reconnecting causes the principal's connector host to rebuild with the new credentials.

First-time third-party consent screens remain a human check because the provider owns their UI/behavior.

## 6. What to record

Record the tested commit/package, connector/provider account type, operations exercised, and any provider drift or unexpected behavior in the issue/PR where the investigation belongs. Keep chronological run logs out of this standing document.
