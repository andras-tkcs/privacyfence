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

`tests/unit/test_gate.py` is the primary, deterministic proof for gate-state coverage —
`automated-test-strategy-plan.md` Phase 5 cross-checked it against the full gate/policy matrix
(auto→allowed, review→Allow/Deny, review+PII→Proceed/Cancel, popup/write→Allow/Deny, "Always
allow"→proposed rule, matching/non-matching rule or resource grant, unattended allowed/forbidden)
and confirmed it's already exhaustive. This tier is **no longer required as routine release proof
for gate-state coverage itself** — don't re-verify the generic auto/review/popup/PII/unattended
state machine here.

What this tier is still required for: **connector-specific tool-to-gate-metadata mapping** — that a
given tool is actually wired to the gate/metadata its own implementation intends (e.g. a review-gated
read really does carry `pii_scan_text`, a write really does offer the sandbox-folder suggestion it's
supposed to) against a live provider response, not a synthetic one. `test_systemic_gate_invariants.py`
(TST-13) already proves the source-level wiring for the invariants it scans for; this tier is the one
that catches a live response shape defeating that wiring in practice. For representative tools verify:

- the tool's gate (auto/review/popup) and metadata match its documented contract against real
  provider data — this, not the generic Allow/Deny/rule-creation state machine, is this tier's job;
- Deny/Cancel prevents connector execution or protected release, against the real provider;
- PII-sensitive live results trigger the configured privacy/confirmation behavior;
- audit entries record the correct connector/tool/decision/principal context.

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
