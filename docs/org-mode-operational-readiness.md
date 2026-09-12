# Org mode operational readiness

This document covers the standing operational requirements for a centralized org-mode deployment.

## Availability model

PrivacyFence uses a single active daemon per state directory. The daemon takes a `portalocker`-backed file lock; the underlying implementation selects the appropriate operating-system locking primitive.

Do not run multiple active daemon processes against the same writable state directory. PrivacyFence is not a clustered shared-state service and does not provide built-in multi-node leader election or shared-session replication.

If higher availability is required, design it at the deployment level with explicit state/credential ownership and tested failover procedures rather than pointing multiple instances at the same state.

## Persistent state

Back up the persistent configuration and user-scoped state that your deployment depends on, including as applicable:

- organization configuration/trust material;
- connector authorization/token state;
- per-user state under the PrivacyFence org/user directories;
- audit data when local retention is required;
- WebAuthn/step-up enrollment state;
- deployment-specific service/reverse-proxy configuration maintained outside the package.

Do not treat temporary staged downloads, caches, process locks, or generated runtime discovery files as primary backup data unless the implementation explicitly documents them as durable state.

## Backup security

Backups contain credentials and identity-linked state and must be protected accordingly. Restrict read access, encrypt backups according to the organization's policy, and control retention/destruction.

A backup that exposes connector tokens is equivalent to exposing the live connector credentials until those grants are revoked/rotated.

## Restore

Test restore on a non-production copy. A restore should preserve expected ownership/permissions and should be performed while the daemon is stopped so no process writes into the state being replaced.

After restore:

1. start a single daemon instance;
2. validate org configuration/trust;
3. sign in through the normal public origin;
4. verify representative connector authorization;
5. run an approval and confirm audit principal/decision data;
6. verify any external audit forwarding/monitoring resumes.

## Upgrade

Use normal package/environment replacement while preserving the external state directory. Stop the service cleanly, back up state, deploy the new version, and start the service under the same intended service identity/configuration.

Run the public-origin sign-in/MCP/approval smoke after upgrade. If the release changes configuration shape, storage, identity, or connector authorization behavior, follow the release notes/code migration instructions for that version rather than improvising a partial downgrade.

## Rollback

A code rollback is safe only when the older version understands the state/configuration produced by the newer version. Keep a pre-upgrade backup and treat state migrations/configuration changes as part of the rollback decision.

Do not restore only selected token/database/config files unless the implementation explicitly supports that combination.

## Restart behavior

A service restart invalidates in-memory state such as currently pending approvals, connector-host caches, and active browser/SSE connections. Durable configuration/credentials/audit state remains on disk according to its own storage rules.

Clients and browsers should reconnect to the restarted daemon. Requests that depended on an in-memory pending approval should be retried as a new request rather than assuming the old in-memory approval still exists.

## Connector cache and principal capacity

Org mode lazily caches connector hosts per principal. `ConnectorRegistry` applies a maximum principal count and idle eviction. Capacity exhaustion fails closed rather than allowing unbounded connector memory growth.

Monitor memory/use patterns for the expected number of simultaneously active users and tune deployment sizing accordingly.

## Download staging

Encrypted staged downloads are temporary delivery state. A token past its configured TTL is never claimable, but the underlying ciphertext file is swept opportunistically — only on the next `stage()`/`claim()` for that principal, not on a background timer — so an unclaimed expired file can persist on disk until then. Don't rely on staged downloads for persistence or backup either way. See [`org-mode-download-delivery.md`](org-mode-download-delivery.md).

## Audit operations

Define who can read audit data, how long it is retained, whether it is forwarded externally, how integrity verification is performed, and what monitoring alerts on forwarding failure or suspicious decisions.

Audit forwarding is not a substitute for securing local state and credentials.

## Monitoring

At minimum monitor:

- service/process availability;
- reverse-proxy/TLS health;
- sign-in failures and OIDC/JWKS reachability;
- connector authorization failures;
- disk capacity for state/audit/staging;
- repeated startup/instance-lock failures;
- audit-forwarding failures where forwarding is enabled.

Avoid logging bearer tokens, browser bootstrap secrets, connector tokens, or provider content that the application does not otherwise intend to expose.

## Disaster recovery

Document the recovery point objective, recovery time objective, backup schedule, backup location, credential rotation procedure, and who is authorized to restore the service.

A recovery exercise should prove the deployment can restore configuration/state, authenticate a user, access an authorized connector, complete an approval, and produce/forward a valid audit record.

## Automated evidence

`tests/integration/test_org_ubuntu_release_smoke.py` (TST-16) is the automated evidence for org mode's core claims above, not just a general pointer to "broader unit/security tests": it provisions and exercises org mode from scratch, entirely in Linux CI, against `daemon_main.main()` end to end (a real separate OS process, real socket, real reverse-proxy Host handling) rather than any in-process shortcut. Concretely, it proves:

- **strict fail-closed startup** on a malformed/unsigned/incomplete `org_config.json` bundle (the "Config integrity/signing" section above);
- **an unauthenticated request is rejected** and **an authenticated MCP request resolves to the correct, IdP-verified principal**, including when an `authz` allowlist (SEC-22) is configured -- an admitted principal signs in, one outside every allowed domain is denied with no session issued;
- **an approval is exercised end to end and audited under the correct principal**: a real MCP-triggered approval can only be decided by the principal it belongs to (a different principal's decide attempt is rejected), and the resulting audit entry lands in that principal's own per-principal audit log directory, not a shared or wrong one -- the disaster-recovery exercise's "complete an approval" and "produce ... a valid audit record" claims, automated;
- **persisted state -- a confirmed auto-accept rule and its audit trail -- survives a clean stop/restart** (`SIGTERM`, the same signal `systemctl stop` sends), the same continuity a real `systemctl restart privacyfence` needs.

This is CI-enforced, not just locally runnable: `.github/workflows/tests.yml`'s `org-mode-smoke` job runs it on every PR (promoted from a release-tag-only job per `automated-test-strategy-plan.md` Phase 8 item 2), and `.github/workflows/build.yml`'s `build-deb` job runs it again at release time. What it deliberately does not cover -- a real systemd unit, a real Caddy reverse proxy, a real external IdP, connector-specific behavior once authenticated (that's the rest of this repo's connector test suites) -- is unchanged manual/operational surface; see the module's own docstring for the full boundary. See [`testing-policy.md`](testing-policy.md) for how this fits the seven-layer taxonomy. Remaining system/release automation gaps are tracked only in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).
