# Security and compliance

This document describes the security controls implemented by PrivacyFence. It is a technical control reference, not a certification statement.

## Security model

PrivacyFence mediates MCP access to connected third-party services. The daemon, policy engine, approval UI, connector credentials, organization configuration, and audit log are part of the trusted computing base.

The primary goals are:

- do not release protected provider data before the configured policy permits it;
- require explicit human approval for operations configured to require review/confirmation;
- keep connector credentials and user-scoped state out of MCP-visible content;
- keep principals isolated in org mode;
- fail closed on invalid security configuration;
- preserve enough audit evidence to reconstruct policy/approval decisions.

## Deployment model

PrivacyFence runs in one of two deployment modes, chosen by IT when the daemon is configured — not
something an individual user or the AI can switch. In **local mode** (the default), one instance
runs on one employee's own machine, with a single implicit principal authorized by a random secret
written to local state; there is no sign-in. In **org mode** (opt-in), IT operates a shared
instance, typically on Ubuntu (see [`org-mode-setup-guide.md`](org-mode-setup-guide.md)), and each
person authenticates via OIDC against the organization's own identity provider — a browser session
and an MCP client's OAuth 2.1 token issued for the same sign-in resolve to the same principal.

Neither mode runs on PrivacyFence-operated infrastructure: local mode runs entirely on the
employee's device, and org mode runs on a server the organization itself provisions and controls.
There is no multi-tenant service and no PrivacyFence API that connector traffic passes through —
every tool call reaches the underlying provider (Google, Slack, Salesforce, Atlassian, Telegram)
directly from that machine or server.

## Authentication boundaries

### Local web UI

The local browser UI is not authenticated by a reusable token in the URL. The daemon uses a one-time bootstrap exchange to establish an HttpOnly session cookie. Browser requests are then authenticated from that session.

Mutating requests require the authenticated session, same-origin checks, and CSRF validation. Session/bootstrap secrets are not intended for logging or propagation into connector data.

### Local MCP

The local `/mcp` endpoint uses the generated bearer token stored in the user's PrivacyFence state so local MCP clients/shims can authenticate independently from the browser session.

### Org mode

Org mode authenticates human users through the configured OIDC provider and applies PrivacyFence's org authorization/session model to MCP and web traffic. Principal identity is carried explicitly through request handling and user-scoped storage/connector resolution.

Where configured, WebAuthn step-up is used for sensitive org-mode approval actions. Credential enrollment and lookup are scoped to the authenticated principal.

## Authorization and principal isolation

Local mode has one principal for the daemon instance. Org mode supports multiple principals and maintains user-scoped state under principal-aware paths.

`ConnectorRegistry` creates/caches connector hosts per principal. Service authorization callbacks evict the affected principal's cached connector host so subsequent calls use the updated credentials.

Org approval routes filter/authorize by principal rather than exposing the local-mode all-pending-approvals view across users.

## Approval and policy enforcement

Tool calls pass through the common gate before connector execution where required by policy. A user decision is bound to the pending request; stale/already-resolved approvals are not reusable as fresh authorization.

Always-allow rules are explicit scoped policy objects, not global bypasses. Rule matching is documented in [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

Policy denials and unattended-mode restrictions fail before protected connector results are released.

## PII and content privacy

Provider content can be inspected for PII before release. Organization policy can allow, redact, or block configured categories. Invalid policy values are rejected instead of falling back to permissive behavior.

Preview and scan paths are bounded to avoid unbounded processing of provider-controlled content. Structured file parsing uses dedicated extraction code and hardened XML parsing where applicable. See [`file-type-support.md`](file-type-support.md) and [`pii-detection-keywords.md`](pii-detection-keywords.md).

## Credential and secret handling

Connector OAuth/session credentials are stored in PrivacyFence state, not returned through MCP tools. File creation/update paths that contain credentials use the repository's secure file helpers and restrictive permissions where the operating system supports them.

The self-hosted live-provider test runner keeps its real QA connector credentials outside GitHub-hosted runners and outside committed repository content. See [`connector-live-check-setup.md`](connector-live-check-setup.md).

Each connector's OAuth client secret is shared across every user of a given deployment rather than issued per-user: it authenticates the PrivacyFence installation to the provider, not an individual end user. A leaked client secret should be rotated with the provider directly; PrivacyFence itself has no per-secret rotation schedule or automated rotation mechanism.

## Organization configuration trust

Org-mode configuration is validated before use, including the configured trust/signature model for organization bundles. Startup should fail when required trust/configuration fields are absent or invalid rather than silently switching to a weaker mode.

Operators should protect the org configuration/trust material as deployment configuration and control who can replace it.

## HTTP security controls

The embedded web application applies security headers and CSP. Inline script/style elements required by the generated UI use per-response/content nonces rather than broad unsafe-inline allowances.

Browser sessions use HttpOnly cookies and same-origin/CSRF checks for state-changing operations. Sensitive bootstrap/bearer material is not intended to be carried in persistent browser URLs.

Deploy org mode behind the configured HTTPS reverse proxy and preserve the Host/origin assumptions documented by the setup guide.

Org mode's OAuth dynamic client registration (DCR) endpoint bounds its own resource usage: it caps the total number of registrations it will hold at once, validates registration metadata against a size limit, and prunes stale/expired registrations on an age-and-count basis rather than retaining them indefinitely.

## Download staging

Org-mode files that cannot be returned inline can be staged as encrypted temporary content and served from an opaque short-lived download token. Staged-link lifetime and inline-size thresholds are configurable and validated.

See [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Audit integrity and forwarding

PrivacyFence records gate/approval activity in its audit log, including principal information in org mode. Audit integrity controls and verification code detect tampering in the recorded chain where configured by the implementation.

Org deployments can use the implemented forwarding/export path for external retention/monitoring. Forwarding does not replace local operational decisions about retention, backup, and access control.

Treat audit data as sensitive: it can reveal which services/tools/resources were used even when protected content itself was not released.

## Dependencies and supply chain

Runtime/test/build dependencies are declared in `pyproject.toml`, with release/dependency audit workflows under `.github/workflows/` and lock/update tooling under `requirements/` and `scripts/`.

CI includes dependency auditing and static analysis in addition to the normal test suite. Ruff is blocking in the test workflow; mypy and Bandit are informational there unless workflow configuration changes.

Each tagged release build generates a CycloneDX software bill of materials (SBOM) alongside the packaged artifacts.

Release artifacts use the platform signing/notarization paths described in [`platform-support.md`](platform-support.md).

## Operational security

Back up only the state your deployment needs and protect backups equivalently to the live credentials/configuration they contain. Restore procedures must preserve file ownership/permissions and should be tested on a non-production copy.

PrivacyFence uses a single-instance file lock via `portalocker`; one state directory should not be actively served by multiple daemon processes at once.

For centralized deployments, availability depends on the operator's service/reverse-proxy design. PrivacyFence itself is not a clustered shared-state service.

See [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## Testing evidence

Current automated security evidence includes unit/integration tests, browser/CSP tests, coverage-floor enforcement, static analysis, Python compatibility checks, scheduled live-provider checks, and release/platform smoke coverage described in [`testing-policy.md`](testing-policy.md).

Remaining automation gaps are tracked only in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).

## Vendor risk criteria

PrivacyFence has no certified information security management framework (e.g. ISO 27001), no
Business Continuity Plan, and no contractual risk-response process or SLA. This is a structural
consequence of the deployment model above, not an oversight:

- **Certified information security framework:** none — there is nothing to certify, since there is
  no PrivacyFence-operated infrastructure (see Deployment model above). Such certifications attest
  to controls around *operated* infrastructure, which doesn't exist here.
- **Business continuity plan:** none — there is no PrivacyFence-operated service whose outage could
  disrupt a deployment. If the maintainer became unreachable, already-installed copies keep running
  exactly as before; the code being open source lets an organization audit, fork, or maintain a
  pinned version independently of the original maintainer.
- **Risk response process / SLA: None.** Reports are handled best-effort, not against a committed
  response time — see [`SECURITY.md`](../SECURITY.md) for how to report and what to expect.

None of this changes the technical risk profile described elsewhere in this document — no vendor
infrastructure in the data path, no new data processor, human-in-the-loop enforcement on sensitive
calls, and a local audit trail. Organizations evaluating PrivacyFence against a standard
vendor-risk questionnaire should treat the absence above as a risk-acceptance decision, not a
security gap: approve it through a risk-acceptance/exception process rather than a standard
vendor-security sign-off, pin deployments to a specific reviewed release rather than auto-updating,
and assign an internal owner to track new releases and patch or roll back if a report doesn't land
in time.

## Vulnerability reporting

Report suspected vulnerabilities to **info@privacyfence.eu**, or use GitHub's private vulnerability
reporting from this repository's Security tab, rather than a public issue. See
[`SECURITY.md`](../SECURITY.md) for the full disclosure process, what to include in a report, and
scope.

## Compliance positioning

PrivacyFence provides technical controls that can support an organization's privacy/security program, including approval gates, PII filtering, principal isolation, secure credential handling, audit logging, and controlled deployment configuration.

Whether a deployment satisfies a particular regulatory, contractual, or certification requirement depends on the organization's configuration, infrastructure, policies, identity provider, retention practices, operational procedures, and independent compliance assessment. The repository documentation should not be read as claiming certification by itself.
