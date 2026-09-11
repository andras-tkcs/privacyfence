# Claude / MCP knowledge boundary

PrivacyFence separates what an MCP client can know before approval from protected provider content that is released only after policy and approval checks succeed.

## Before approval

The client can know the tool schema, arguments it supplied, connector/tool identity, and safe error/status information needed to continue the protocol. It must not receive the protected provider result merely because PrivacyFence has already fetched enough data to build an approval preview or run PII detection.

PrivacyFence may fetch/parse provider data internally before a human decision when required to:

- build the approval card;
- identify the target/resource;
- apply privacy/PII policy;
- determine safe metadata for the approval list;
- decide whether a standing rule applies.

That internal visibility does not make the data MCP-visible.

## Approval list and notifications

The approval list intentionally carries a reduced summary. It can identify the connector/tool and safe operation context without becoming a substitute for the full protected result.

Browser notification detail follows the configured `minimal`, `standard`, or `detailed` level. `detailed` may contain the approval summary and therefore can expose gated information on the OS/browser notification surface; use it only when that tradeoff is acceptable.

## Read approvals

For a review-gated read, PrivacyFence holds the protected result until the approval resolves. The full card can show a bounded preview to the human reviewer inside PrivacyFence; the MCP call receives the result only after the applicable decision/policy permits release.

If the decision is Deny/Cancel, the protected result is not returned to the client.

## PII

The PII scanner/privacy filter can inspect content inside PrivacyFence before release. Organization policy can allow, redact, block, or require the configured confirmation behavior for detected categories.

When redaction is required, the MCP client receives the redacted representation rather than the raw protected content.

## Write operations

For a write confirmation, the client already knows the arguments it asked PrivacyFence to execute. Approval controls whether PrivacyFence actually performs the protected external side effect.

Connector/provider credentials, internal tokens, and service authorization material are never part of the MCP result merely because they are needed to execute the write.

## Standing rules

A matching always-allow rule can authorize a future operation without showing another human approval, but it does not expand the tool schema or reveal additional provider data. The rule only changes the gate decision for requests inside its stored scope.

See [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

## Org-mode principal boundary

In org mode, the request principal determines which user-scoped connectors/state/policies apply. One principal must not receive another principal's connector result, approval, service token, staged download, or step-up credential state.

## Errors and diagnostics

Errors returned through MCP should be useful enough for the client to recover without leaking connector credentials, bootstrap/session tokens, local secret paths, or protected provider payloads that were not authorized for release.

Detailed diagnostics belong in the daemon/operator logs with the same secret-minimization requirements, not in a broader MCP-visible error by default.

## Source anchors

The boundary is implemented across:

- `src/privacyfence/gate.py`
- `src/privacyfence/approvals.py`
- `src/privacyfence/privacy_filter.py`
- `src/privacyfence/pii_detector.py`
- `src/privacyfence/card_builder.py`
- `src/privacyfence/web_approval_ui.py`
- connector/tool implementations under `src/privacyfence/connectors/`
- org principal/session/resource-grant modules

Tests should assert both the allowed result and the negative property that protected data is absent on denied/pending paths.
