# Org mode setup guide

Org mode runs PrivacyFence as a centralized Linux service for multiple authenticated users. The daemon serves the MCP endpoint and browser approval/settings surfaces, while identity comes from the organization's configured OIDC provider.

**Unverified on a real install.** This guide is exercised in CI against a real daemon subprocess and a mocked IdP (`org-mode-smoke`, see `testing-policy.md`), but a real end-to-end run of the steps below — a live Ubuntu server, a real OIDC identity provider, a real connector authorized through it — has not been done yet (see `platform-support.md`'s "Known open items"). Treat this guide as ready to try, not yet a battle-tested path — same status as `privacyfence.service`'s own header comment for the single-user desktop variant of this install.

## Deployment model

A typical deployment contains:

1. the PrivacyFence Python environment/service;
2. a signed/validated organization configuration bundle;
3. an HTTPS reverse proxy in front of the daemon;
4. an OIDC identity provider configuration;
5. per-user connector authorization state managed by PrivacyFence.

Org mode is not the desktop `.deb` autostart path. Use the Python/system-service deployment model for a centralized server.

## Prerequisites

Use a supported Python version (`>=3.11`) and install PrivacyFence in an isolated environment suitable for a long-running service. Configure the reverse proxy and DNS/TLS before exposing the service to users.

Keep the service account's PrivacyFence state directory writable only by the service identity. Protect the organization configuration/trust material as security-sensitive deployment configuration.

## Organization configuration

Place the organization configuration where the daemon expects it and configure its trust/signature validation according to the repository's org configuration tooling. Startup rejects missing/invalid required trust/configuration instead of silently using a weaker configuration.

The organization configuration defines the deployment's identity/provider settings and privacy/security policy. Use explicit policy values; invalid policy values fail startup/config validation.

## OIDC identity provider

Configure the organization's OIDC issuer/client settings and redirect URIs for the public HTTPS origin used by PrivacyFence.

The reverse proxy must preserve the host/origin assumptions used by the application so redirect/origin validation sees the intended public origin.

PrivacyFence validates ID tokens against the provider's OIDC/JWKS information and binds the authenticated identity to a `Principal` used throughout request handling.

## Reverse proxy

Terminate HTTPS at the supported reverse proxy and forward traffic to the PrivacyFence daemon on the configured internal bind address/port.

Do not expose an unprotected internal listener directly to the Internet. Configure forwarded host/proto behavior consistently with the deployment's public origin and test sign-in/redirect behavior through the same hostname users will use.

## Starting the daemon

Run `privacyfence-app` under the chosen service manager with the org-mode configuration/environment required by the deployment. The repository's `privacyfence.service` is the systemd-oriented service template/reference for Linux installs.

Use a single active daemon per state directory. PrivacyFence takes a `portalocker`-backed single-instance lock and should not have two processes concurrently serving the same state.

## Per-user connectors

Org mode does not share one provider credential set across all users by default. Connector authorization state is principal-scoped.

`ConnectorRegistry` lazily creates a `ConnectorHost` for the authenticated principal and evicts idle entries. After a user connects/reconnects a service, the affected principal's cached connector host is evicted so the next request rebuilds it with the updated credentials.

Use the web connector/settings routes to authorize services for the signed-in user.

## Approvals

Org-mode approval routes are principal-aware: a signed-in user can act only on approvals authorized for that principal. Sensitive write approvals can require WebAuthn step-up when configured.

The UI behavior itself is the same embedded browser approval surface documented in [`approval-list-ui-ux.md`](approval-list-ui-ux.md).

## Downloads

Centralized deployments cannot write directly to a user's local filesystem. Org-mode file delivery therefore uses inline content or encrypted short-lived staged links as documented in [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Operations

Before production use, define backup/restore, upgrades/rollback, monitoring, audit retention/forwarding, and service restart procedures. See [`org-mode-operational-readiness.md`](org-mode-operational-readiness.md).

## Validation

Validate the deployment through the public HTTPS origin:

- unauthenticated requests are rejected/redirected appropriately;
- OIDC sign-in establishes the intended principal;
- users cannot see or decide another principal's approvals;
- connector authorization is stored under the signed-in principal;
- MCP requests apply the signed-in/authorized principal's policy and connectors;
- audit entries contain the correct principal;
- restart preserves intended persistent state.

Automated org-mode coverage is described in [`testing-policy.md`](testing-policy.md); remaining automation gaps are tracked in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).
