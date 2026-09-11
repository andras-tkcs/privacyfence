# Approval card content reference

This document describes the information PrivacyFence renders on approval cards and confirmations. The HTML builders in `src/privacyfence/approval_window_html.py`, `dialog_window_html.py`, and the connector/card-building code are authoritative.

## Common card structure

A normal approval card identifies:

- connector/service;
- operation/tool;
- why the operation needs approval;
- the resource/target involved when known;
- a bounded preview/details section appropriate to the operation;
- PII warnings/details when the privacy scanner flags content;
- decision controls.

Cards are rendered as HTML and served through the embedded web approval route. The browser route supplies the session/CSRF decision bridge; the content builder does not depend on a native UI host.

## Read/retrieval operations

Read-gated cards show enough metadata and bounded preview content for the user to understand what will be released to the MCP client. PrivacyFence should not disclose the protected result to the client before the review decision.

Examples of useful card fields include resource name/title, account/calendar/channel/project context, sender/author metadata where appropriate, requested query/range, and a bounded content preview.

Tool-specific behavior and extraction limits are documented in [`file-type-support.md`](file-type-support.md) and the connector implementations.

## Write operations

Write confirmations describe the action PrivacyFence is about to perform and its destination/target. Depending on the connector/tool this can include recipients, channel/project, record/object, file/folder, event/task details, message/content preview, or other operation-specific parameters.

A write is not executed before the confirmation gate succeeds unless an applicable standing rule/policy explicitly permits it.

## PII display

When the PII detector flags content relevant to a gated release, the approval surface identifies the detected category information and highlights/warns the user according to the current card builder/privacy-filter behavior.

A PII confirmation is a separate authorization decision from merely displaying the card. Cancel/deny must not release the flagged protected content.

## Always-allow choices

Where a tool can propose a standing rule, the card can offer an always-allow choice tied to the rule shape supported for that operation. The rule must be derived from the reviewed request; it is not a generic global bypass.

See [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

## Data minimization

Approval content should contain what the user needs to decide the request, not unrelated provider data. Preview/extraction code is bounded and tool-specific. Credentials, access tokens, session secrets, bootstrap values, and internal authorization material must never be rendered as approval content.

## Source-of-truth files

When changing card content, update this reference together with the implementation/tests. Key files include:

- `src/privacyfence/card_builder.py`
- `src/privacyfence/approval_window_html.py`
- `src/privacyfence/dialog_window_html.py`
- `src/privacyfence/approval_icons.py`
- `src/privacyfence/gate.py`
- connector tool definitions under `src/privacyfence/connectors/`
- corresponding unit/browser tests under `tests/`
