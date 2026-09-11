# PrivacyFence technical reference

PrivacyFence is a local or organization-hosted MCP privacy gateway. It sits between an MCP client and third-party data providers, applies policy and approval gates, and records auditable decisions.

## Runtime architecture

A PrivacyFence daemon owns connector clients, policy evaluation, approval state, the audit log, and the embedded HTTP application.

The embedded web application serves:

- the approval list and approval cards;
- settings and connector authorization surfaces;
- state/event streams used by the web UI;
- the `/mcp` Streamable HTTP endpoint;
- org-mode identity/authorization routes when org mode is enabled;
- short-lived staged downloads when org-mode delivery requires them.

Claude Code and other HTTP-capable MCP clients can connect to `/mcp` directly. Claude Desktop uses the bundled Node/TypeScript shim in `mcpb/shim/`, which reads the daemon discovery/auth files and proxies stdio MCP traffic to the daemon's HTTP endpoint.

There is no native AppKit approval/settings runtime. The browser-based embedded UI is the approval/settings surface on every supported platform.

## Modes

### Local mode

Local mode represents one user/principal for the life of the daemon. Connector credentials and policy are resolved for that local user. The web server binds locally and uses a bootstrap/session mechanism for the human approval/settings UI plus bearer-token protection for MCP.

### Org mode

Org mode is a centralized Linux/server deployment. Human identity is established through the configured OIDC identity provider, MCP authorization is handled by PrivacyFence's org authorization flow, and connectors are scoped per principal.

`ConnectorRegistry` lazily builds and caches one connector host per authenticated principal, with bounded capacity and idle eviction. User-scoped state paths are resolved inside the active principal scope.

Org mode is normally deployed behind the configured HTTPS reverse proxy. See [`org-mode-setup-guide.md`](org-mode-setup-guide.md) and [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## Local state and discovery

`src/privacyfence/paths.py` is the source of truth for application paths.

A source/unbundled development run keeps its normal configuration, credentials, and logs in the repository-local development locations. A bundled release keeps user state under the user's PrivacyFence home directory.

MCP discovery/auth files under the user's PrivacyFence home include the daemon MCP URL and token so the Desktop shim can find the running daemon independently of the checkout/install location.

The daemon enforces a single-instance lock with `portalocker`.

## MCP endpoint

The daemon exposes the MCP protocol over Streamable HTTP at `/mcp` using the official MCP Python SDK. Local mode protects MCP with the generated bearer token. Org mode uses its own authorization/identity path and principal-aware request handling.

The MCP tool registry is built from the configured connectors. Tool calls are routed through the PrivacyFence gate before connector execution where policy requires review or confirmation.

## Approval model

A gated request becomes a `PendingApproval` managed by `PendingApprovalRegistry`.

The approval list at `/approvals` can contain multiple pending requests. Each row exposes **Deny** and **Review**; there is deliberately no one-click Allow on the list. Review opens the full approval card, where the user can inspect the operation and make the decision.

Pending approvals update in the browser through the state/event stream. Decisions are idempotent: an approval that is no longer pending cannot be approved again as a fresh request.

Approval content is built by `approval_window_html.py` and confirmation content by `dialog_window_html.py`; the web routes inject the browser decision bridge and enforce the current session/CSRF/CSP controls. See [`approval-window-content-reference.md`](approval-window-content-reference.md).

## Gate behavior

Connector tools declare their gate behavior through the shared connector/tool machinery. The important policy outcomes are:

- **auto** — the operation can run without a human decision under the current policy;
- **review** — PrivacyFence shows the read/retrieval operation before releasing protected data;
- **popup/write confirmation** — PrivacyFence requires an explicit decision before a write or other sensitive action;
- **PII confirmation** — detected sensitive content can require an additional confirmation before release/delivery.

Always-allow rules can bypass a matching future approval only within the rule shape and scope the user approved. See [`always-allow-rules-reference.md`](always-allow-rules-reference.md).

## Privacy filtering and PII

Privacy filtering runs before protected content is released to the MCP client. Organization policy can allow, redact, or block configured PII categories. Invalid configured policy values fail closed at startup/config validation rather than silently becoming permissive.

The PII detector and privacy filter are implemented in `pii_detector.py` and `privacy_filter.py`. Tool-specific preview/extraction limits are documented in [`file-type-support.md`](file-type-support.md).

## Audit logging

PrivacyFence records tool/gate decisions to the audit log, including the connector/tool, decision, request metadata, and principal information where applicable. Security-sensitive audit integrity/forwarding behavior is implemented in the audit modules and described in [`security-and-compliance.md`](security-and-compliance.md).

Treat the audit log as security-relevant state: protect its directory, include it in operational backup decisions where required, and do not expose it through connector content paths.

## Web authentication and CSRF

Local browser sessions are established from the one-time bootstrap exchange and then represented by the HttpOnly session cookie. Mutating browser requests require same-origin/session checks plus the CSRF value carried by the page/request flow.

CSP nonces are generated and applied to the inline scripts/styles required by the rendered application. Security headers are applied by the web-server middleware.

Org-mode routes use the org session/identity machinery and apply principal-aware authorization rather than the local single-user session model.

## Browser notifications

The shared web shell maintains the state stream, pending-approval count, and optional browser notifications. Notification detail is controlled by the web notification configuration:

- `minimal` exposes only a pending-count style notification;
- `standard` can include safe operation metadata such as connector/direction;
- `detailed` may include the approval summary and therefore can expose gated content in the OS/browser notification surface.

Notification permission is requested only after a user interaction/decision path, not automatically on initial page load.

## Connector lifecycle

Local mode builds one `ConnectorHost` at daemon startup. Org mode uses `ConnectorRegistry` to build connector sets lazily per principal and evict idle hosts.

Connector setup documentation:

- [`google-cloud-setup.md`](google-cloud-setup.md)
- [`slack-setup.md`](slack-setup.md)
- [`salesforce-setup.md`](salesforce-setup.md)
- [`atlassian-setup.md`](atlassian-setup.md)
- [`telegram-setup.md`](telegram-setup.md)

The definitive tool surface lives in `src/privacyfence/connectors/` and the connector registry/daemon construction code.

## File and download handling

PrivacyFence extracts/normalizes supported attachment types for preview and PII inspection using the bounded extraction paths documented in [`file-type-support.md`](file-type-support.md).

Local-mode downloads can be written on the user's machine. Org-mode downloads are delivered inline or through encrypted short-lived staged downloads as documented in [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Configuration

`config/settings.yaml` (and the packaged example under resources) defines local/web/connector behavior. Org deployments additionally use the signed/validated organization configuration bundle and org-specific identity/settings.

Configuration that affects security boundaries is validated strictly; invalid values should stop startup rather than silently widen access.

## Installation and packaging

Current packaging paths are documented in [`platform-support.md`](platform-support.md):

- macOS signed/notarized DMG;
- Windows Inno Setup installer;
- Debian/Ubuntu self-contained `.deb` for local desktop mode;
- Python package/system-service path for Linux/server deployments.

## Testing

[`testing-policy.md`](testing-policy.md) describes the checks that currently run. [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) is the only plan document and tracks automation gaps that still exist.

The source files, tests, workflow definitions, build scripts, and configuration examples are authoritative if this reference drifts.
