# Org Mode: Operational Readiness

This document answers the questions an operator asks *after* deployment is done and org mode is
serving real users — not "how do I install it" (that's
[`org-mode-setup-guide.md`](org-mode-setup-guide.md)) but "what happens when this server dies, when I
upgrade it, or when it restarts at 3am." It exists because §14.3 of the 2026-09-04 technical review
named this gap explicitly: org mode's support level was never stated in one place, and several
operational questions (backup, restore, upgrade/rollback, restart behaviour, availability model) had
no documented answer at all. See item 3.13 of the (now-removed, all phases landed)
`security-remediation-plan.md`.

Every claim below is checkable against the cited source file — this document favors being checkable
over being reassuring, same posture as [`security-and-compliance.md`](security-and-compliance.md).

---

## 1. Support / readiness level

**Org mode is production-supported, but centrally managed and newer than local mode — not a
preview, and not incomplete, with two named exceptions below.**

What "production-supported" means concretely: the code path is real (not a stub or a flag behind a
feature toggle), it has its own unit and integration test suite (nine dedicated modules today,
including `tests/unit/test_org_mode.py`, `tests/unit/test_org_identity.py`,
`tests/unit/web/test_org_session.py`, `tests/unit/web/test_routes_org_approvals.py`, and
`tests/unit/web/test_org_mcp_e2e.py`), and it is documented end to end for a real deployment
([`org-mode-setup-guide.md`](org-mode-setup-guide.md)). It is not a preview build gated behind an
internal flag, and it is not missing core functionality that would make it unsuitable to run.

What "centrally managed" changes about the risk calculus, versus local mode: org mode moves every
principal's credentials, audit trail, and session state onto **one server your organization
operates** instead of each employee's own machine. That concentrates both the blast radius of a
compromise and the operational responsibility — backup, restore, upgrade discipline, and the
single-daemon availability model this document covers — onto whoever runs that server. Local mode
has none of these because there is nothing centralized to operate; org mode's own maturity note in
[`security-and-compliance.md` §2](security-and-compliance.md#2-deployment-model-local-or-org-run-by-you-either-way)
already flags it as "newer and has run in fewer production environments than local mode" — this
document is where that note's operational consequences are actually spelled out.

Two named gaps, tracked separately rather than left implicit:

- **The Ubuntu systemd service path (`org-mode-setup-guide.md` Step 7) has not had a real
  confirmed end-to-end run against a live server as of this writing** — that guide's own callout at
  the top says so. `tests/integration/test_org_ubuntu_release_smoke.py` (item 3.11, TST-16 of the
  now-removed `security-remediation-plan.md`) has landed and drives `daemon_main.main()` as a real,
  separate OS process — closing the "never exercised as a packaged install" part of this gap — but
  it does so against a synthetic Ed25519-signed `org_config.json`, a loopback mocked IdP, and a
  Host-header-simulated reverse proxy, never a real external IdP, real TLS certificate, or the actual
  Caddy binary, so it does not by itself close the "live server" half of the gap the guide's callout
  names. Nothing about the code path is known to be broken; it simply hasn't been exercised against
  a real deployment outside development and code review the way the macOS `.dmg` path has (TST-15,
  item 3.10, same caveat).
- **`/settings` is not mounted in org mode at all** ([`security-and-compliance.md`
  §4](security-and-compliance.md#4-human-in-the-loop-control)) — org mode's per-principal
  `/approvals`/`/security` surface covers approvals and security settings (step-up enrollment,
  active-session review), but porting the rest of `/settings`'s configuration UI to org mode's
  per-principal session model is documented follow-up work, not something reachable today. This is a
  scope gap, not a maturity gap — nothing about it is expected to change with more production
  mileage; it needs its own implementation.

Every other surface described in this document — OIDC sign-in, per-principal OAuth for connectors,
the OAuth 2.1 authorization server for MCP clients, audit logging (with optional forwarding), and the
download-delivery paths — is implemented, tested, and has no open "not yet battle-tested" caveat
attached to it.

---

## 2. What org mode persists, and where

Everything below lives under `~/.privacyfence/` on the org-mode server (the `privacyfence` system
user's home directory, per [`org-mode-setup-guide.md` Step
2](org-mode-setup-guide.md#2-create-the-dedicated-privacyfence-user)) — `paths.py`'s `data_dir()`.
This table is the map the rest of this document (backup, restore, persisted-state compatibility)
refers back to.

| Path | What it holds | Per-principal? |
|---|---|---|
| `org/org_config.json` | The signed organization config bundle — connector credentials, IdP config, server settings (§5 of the setup guide) | No — one per install |
| `org/org_config_signing_pubkey.txt` | The Ed25519 public key pinned on trust-on-first-use from the first signed bundle this server ever saw (`org_bundle_signing.py`) | No |
| `org/oauth_clients.json` | Registered MCP OAuth clients (RFC 7591 dynamic client registration) — see [§7](#7-restartsession-invalidation-behaviour) for what does and doesn't survive alongside it | No |
| `deployment_id` | A stable, install-wide identifier stamped on every audit entry (`daemon_main.get_or_create_deployment_id()`), so entries from multiple machines/servers can be told apart once aggregated | No |
| `privacyfence.lock` | An `flock`-based instance lock, held only by the running process's own file descriptor | No — not meaningful data; see [§8](#8-single-daemon-availability-model) |
| `users/<principal-id>/config/settings.yaml` | That person's own review/auto-accept preferences, privacy-filter category settings | Yes |
| `users/<principal-id>/credentials/*` | That person's own per-connector OAuth tokens | Yes |
| `users/<principal-id>/logs/audit/*.jsonl`, `*.xlsx`, `.audit_chain.key`, `.audit_chain_state.json` | That person's own audit trail, its HMAC hash-chain key, and the chain's current tip | Yes |
| `users/<principal-id>/downloads/` | Short-lived, encrypted-at-rest staged-download ciphertext (org-mode large-file delivery) | Yes |
| Slack/Telegram directory caches, under each principal's own directory | Weekly-refreshed name-resolution caches — rebuildable from the connector APIs, not authoritative data | Yes |

Nothing here is a PrivacyFence-operated store — it is all local files on infrastructure your
organization already controls, per [`security-and-compliance.md`
§2](security-and-compliance.md#2-deployment-model-local-or-org-run-by-you-either-way). Local mode's
`mcp_token`/`web_token` bootstrap files (§8 of that document) are **never created in org mode at
all** — `web/server.py`'s constructor sets both to `None` whenever `org` is passed, since org mode's
own `OrgSessionStore` and OAuth 2.1 provider replace that entire mechanism with per-principal,
IdP-backed sign-in.

---

## 3. Backup: scope and sensitive-material handling

There is no built-in backup command or API — org mode is ordinary files on an ordinary server, and
backup is a filesystem-level operation you run with your organization's existing backup tooling
(the same posture [§9 of `security-and-compliance.md`](security-and-compliance.md#9-vendor-risk-criteria-no-isms-no-bcp-no-sla--and-why-thats-a-risk-acceptance-decision-not-a-security-gap)
takes toward continuity generally: there's no vendor-operated infrastructure to back up on your
behalf, so this is squarely your organization's own operational responsibility).

**What to include**, for a backup that restores a fully working, identically-behaving install:

- `~/.privacyfence/org/` in full — the signed bundle, the pinned signing public key, and the
  registered OAuth clients file. Without `org_config_signing_pubkey.txt`, a restore would still
  accept the same currently-deployed `org_config.json` (nothing about that file changes), but it
  would trust-on-first-use the key again as if it were a brand-new install — harmless unless an
  attacker can race a restored install with a maliciously re-signed bundle before the legitimate one
  is redeployed.
- `~/.privacyfence/deployment_id` — omitting it means a restored server mints a **new** deployment
  id on next startup (`daemon_main.get_or_create_deployment_id()` creates one if the file is
  absent), which is harmless for local audit-log integrity but means centrally forwarded/aggregated
  entries from before and after the restore appear to come from two different deployments.
- `~/.privacyfence/users/<principal-id>/config/`, `credentials/`, and `logs/audit/` for every
  principal — this is the data an organization actually cares about losing: per-person settings,
  connector tokens, and the audit trail.

**What to deliberately exclude** — sensitive-material handling for the one directory that should
*not* be backed up:

- `~/.privacyfence/users/<principal-id>/downloads/` — already called out in
  [`org-mode-setup-guide.md` §10](org-mode-setup-guide.md#10-day-to-day-admin): this holds only
  short-lived, AES-256-GCM-encrypted staged-download ciphertext whose decryption key is never
  written to this server's disk at all (`docs/org-mode-download-delivery-plan.md`'s "Encryption at
  rest" section). Backing it up doesn't expose plaintext by itself, but it does mean a "deleted"
  staged file's ciphertext can keep existing in a backup snapshot indefinitely after its link
  expired on the live server — excluding it is a cheap way to avoid that, not a response to any
  actual key-exposure risk.
- Weekly directory-name caches (Slack/Telegram) are safe to skip — they contain no message content,
  and they rebuild themselves automatically (see each connector's section in
  [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#connectors--privacy-matrix)) rather than needing
  to be restored.
- `privacyfence.lock` carries no state worth preserving — it's a filesystem handle for `flock`, not
  data; including or excluding it from a backup has no effect either way (see
  [§8](#8-single-daemon-availability-model)).

**Sensitivity of what you *are* backing up:** `credentials/*` holds live OAuth tokens for every
connected user — a stolen backup is exactly as sensitive as a stolen copy of the live server's
`~/.privacyfence/users/` tree, i.e. it grants the same connector access the tokens themselves grant
(each is still scoped to its own connector's OAuth scopes, per
[`security-and-compliance.md` §3](security-and-compliance.md#3-it-administrative-authority)). Apply
the same access control and encryption-at-rest standard to backup storage that you'd apply to the
live server's disk — a backup is not a lower-sensitivity copy of this data, and PrivacyFence has no
mechanism of its own (e.g. a backup-specific encryption pass) to make it one.

**How**, mechanically: standard file-level backup, taken while the daemon is stopped (`sudo
systemctl stop privacyfence`) for a guaranteed-consistent snapshot, or from a filesystem/LVM snapshot
if your infrastructure supports it and you want to avoid the downtime. There is no live/hot backup
mode and no partial-consistency guarantee for a backup taken against a running daemon's files —
`secure_files.py`'s atomic-write scheme (`security-and-compliance.md` §8's "Storage format and
permissions") guarantees any *individual* file a backup captures is never a torn write, but it says
nothing about several files being captured at mutually consistent points in time relative to each
other.

---

## 4. Restore procedure

1. Stop the daemon on the target server (`sudo systemctl stop privacyfence`), if it's running at all.
2. Restore the backed-up tree onto `~/.privacyfence/` for the `privacyfence` system user, preserving
   ownership and the `0700`/`0600` permissions the files already had (`secure_mkdir`/`secure_files.py`
   re-tighten these automatically at next startup if they land looser, per
   [`security-and-compliance.md` §8](security-and-compliance.md#8-security-controls-summary), so a
   restore that loses exact permissions self-heals on the next start rather than failing outright —
   but don't rely on that for anything a plain `tar`/`rsync` with `-p`/`--preserve-permissions-and-owner`
   wouldn't already get right).
3. Start the daemon (`sudo systemctl start privacyfence`) and check `journalctl -u privacyfence -f`
   for the same "Org mode active" startup line the setup guide's Step 7 describes.
4. Every principal must sign in again — a restore is, from the session layer's point of view,
   indistinguishable from an ordinary restart (see [§7](#7-restartsession-invalidation-behaviour));
   nothing about backup/restore changes that.

**Partial restores and what they do to the audit hash chain.** `audit_log.py`'s chain-continuity
check is explicitly written to tolerate a partial restore: if `.audit_chain_state.json` is missing
next to a restored `logs/audit/*.jsonl` directory (e.g. you restored the `.jsonl` files but not the
two dotfiles alongside them, or restored from a source that didn't capture them), the next entry
written starts a **new, valid chain segment from genesis** rather than failing — see that module's
own comment on `GENESIS_HASH`: "the first entry this install ever records (**or the first one after
a chain-state file goes missing, e.g. a restored-from-backup audit directory with no
`.audit_chain_state.json` alongside it**)". This is anticipated, documented behavior, not a bug to
work around — but it does mean `verify_chain()`/`scripts/verify_audit_log.py` will correctly show
two separate unbroken segments rather than one continuous chain across the restore point, which is
the honest state of affairs (the tool that would prove continuity *across* the restore — the old
`.audit_chain.key` plus `.audit_chain_state.json` — is exactly what a partial restore is missing).
Restore all three files (`*.jsonl`, `.audit_chain.key`, `.audit_chain_state.json`) together, from the
same backup snapshot, if unbroken chain continuity across the restore point matters to your
compliance program.

**Restoring onto a different server than the one that failed** (disaster recovery, not routine
maintenance) works the same way, with one addition: point DNS at the new server (or move the
existing DNS record) before or immediately after starting the daemon, since `server.issuer_url`'s
hostname is baked into the signed `org_config.json` and into every registered OAuth redirect URI at
the IdP (§4 of the setup guide) — a restore onto a host reachable at a different hostname needs a
freshly built and re-signed bundle, not just a file copy, exactly as if you were standing up a new
server from scratch.

---

## 5. Upgrade / rollback

**Upgrade** is documented already in [`org-mode-setup-guide.md`
§10](org-mode-setup-guide.md#10-day-to-day-admin): re-run the same `pipx install ... --force` (or
`pipx install privacyfence --force` once published) command used in Step 3, then `sudo systemctl
restart privacyfence`. There is no separate migration step to run by hand — any one-time data shape
change a new version needs applies itself automatically at the next startup (see
[§6](#6-persisted-state-compatibility-across-versions) below) — but the restart itself has the same
consequences as any other restart ([§7](#7-restartsession-invalidation-behaviour)): every active
session and MCP OAuth token is invalidated, and everyone signs in again.

`web_token` (local mode only — see [§2](#2-what-org-mode-persists-and-where): org mode never has one)
is additionally rotated automatically on every version change, per
[`security-and-compliance.md`](security-and-compliance.md#8-security-controls-summary)'s
"Local-mode token semantics" note — this has no org-mode equivalent to rotate, since org mode's
authentication is IdP-backed rather than a persistent local secret in the first place.

**Rollback has no dedicated tooling or documented procedure today, and no forward-compatibility
guarantee protects it.** Reinstalling an older version (`pipx install
"git+https://...@<older-tag>"`, or an older PyPI release once tags exist) is mechanically the same
command as an upgrade, but the one-time config migrations `daemon_main.py` applies on startup
(§6 below) are **one-way** — they exist specifically to move a config *forward* into a newer shape,
and nothing in this codebase reads the newer shape back down into the older one an earlier version
expects. Concretely:

- If the newer version's startup already migrated `settings.yaml` (e.g. folded matching
  `auto_accept_rules` into `auto_accept_grants`, `resource_grants.py`'s
  `migrate_rules_to_grants` — or the analogous `telegram_search_migrated` rename,
  `auto_accept.py`'s `migrate_telegram_search_operation_key`) before you roll back, the older
  version's own code may not understand the migrated shape at all — this has not been tested and
  should not be assumed to work.
- The safe rollback path is: restore `~/.privacyfence/` from a backup taken **before** the version
  you're rolling back from was ever started (so no forward migration has run against it yet — see
  [§4](#4-restore-procedure)), then reinstall the older version. Rolling back the *code* alone,
  without also rolling back the *data* to a pre-migration snapshot, is not a supported or tested
  configuration.
- `org_config.json` itself is not touched by any startup migration as of this writing — rolling back
  the daemon version while keeping the same bundle is expected to keep working, independent of the
  settings.yaml caveat above, unless a future bundle-format change adds one.

Treat "pin to a specific reviewed version, upgrade deliberately" — already this document's sibling
`security-and-compliance.md` §9's recommendation for a different reason (no SLA/patch-turnaround
commitment) — as the practical mitigation for the rollback gap too: the fewer versions you cross in
one upgrade, the smaller the chance any given migration actually matters to you, and the easier it
is to reason about what a rollback would need to undo.

---

## 6. Persisted-state compatibility across versions

Two independent compatibility stories exist, both forward-only as of this writing:

- **`settings.yaml` (per-principal config).** `daemon_main.py` runs a fixed sequence of idempotent,
  marker-guarded migrations against the loaded config on every startup (currently:
  `resource_grants.migrate_rules_to_grants`, then
  `auto_accept.migrate_telegram_search_operation_key`) and persists the result back if anything
  changed, logging a summary either way. Each migration is safe to run against an already-migrated
  file (its own marker — e.g. `MIGRATION_MARKER = "migrated_to_grants_v1"` — makes it a no-op the
  second time), so upgrading through several versions in one jump applies every migration in order
  without needing to stop at each intermediate version first. There is no downgrade path for any of
  these — see [§5](#5-upgrade--rollback)'s rollback caveat.
- **The audit log (`logs/audit/*.jsonl`).** This one *is* explicitly designed for mixed-version
  compatibility, because unlike `settings.yaml` it's meant to be read back — by
  `verify_chain()`, by a forwarded-log consumer, or by hand — long after being written, potentially
  spanning years and several PrivacyFence versions. Every entry carries its own `schema_version`
  (`audit_log.py`'s `CURRENT_SCHEMA_VERSION`, currently `2`); an entry written before that field
  existed at all reconstructs with the dataclass default (`1`) rather than the current constant, so
  old and new entries coexist in the same file and a reader can tell them apart per-line rather than
  needing to know the whole file's "version" up front. `CURRENT_SCHEMA_VERSION`'s own docstring
  carries the version history (currently just the one bump, "2 — SEC-23: + schema_version, event_id,
  deployment_id, security_config_hash, prev_hash, entry_hash") and is the place to look for exactly
  which fields to expect at which version. A downgrade here is a non-issue in practice: an older
  version's audit-log reader that doesn't know about `schema_version` at all simply ignores the
  field, since JSONL parsing is per-line and additive fields don't break an older parser that never
  looks for them — the hash-chain fields work the same way, additive rather than replacing anything
  an older entry shape had.
- **`org_config.json`** carries no migration logic today (see [§5](#5-upgrade--rollback)) — its
  shape has been stable since org mode shipped, and nothing in `daemon_main.py`'s startup path
  rewrites it in place the way `settings.yaml` gets migrated.

None of this is versioned against a formal compatibility matrix (e.g. "v4.x reads v3.x data with no
migration needed") — the guarantee that exists today is narrower and more mechanical: startup
migrations are additive and idempotent going forward, and the audit log's per-entry schema
versioning tolerates old and new entries side by side. Nothing has been tested for what happens
several major versions apart, and nothing promises it will keep working that way indefinitely as the
schema evolves further.

---

## 7. Restart/session-invalidation behaviour

**A daemon restart (crash, upgrade, or a deliberate `systemctl restart`) invalidates every active
session and in-flight interaction, cleanly but without warning.** Nothing about this is a bug —
`web/oauth_provider.py`'s own module docstring states the design choice directly: "authorization
codes, access tokens and refresh tokens are in-memory only... losing them on restart just means
signing in again, not a security gap." Concretely, on every restart:

- **Browser sessions** (`pf_session` cookies, minted by `org_session.py`'s `OrgSessionStore`) are
  gone — held in memory only, per
  [`security-and-compliance.md` §8](security-and-compliance.md#8-security-controls-summary)'s
  session-lifetime note. Anyone with an open `/connect`, `/approvals`, or `/security` tab needs to
  sign in again through `/login`; there is no cross-restart "remember me."
- **MCP OAuth tokens** (access and refresh) issued to Claude or any other MCP client are gone too,
  for the same in-memory-only reason. Claude's *registration* with the authorization server
  (`org/oauth_clients.json`, persisted to disk — [§2](#2-what-org-mode-persists-and-where)) survives,
  so a client doesn't need to redo dynamic client registration, but its next tool call triggers a
  fresh `/authorize` round trip through `/login` and the IdP, exactly like a first-ever connection.
- **A pending review/popup approval mid-decision when the daemon restarts is lost outright.** The
  deferred-approval registry (`approvals.py`'s `PendingApprovalRegistry`) is an in-process object
  with no disk persistence of its own — gate.py's decision loop waits on it for up to
  `hold_window` seconds (30s by default, `DEFAULT_HOLD_WINDOW_SECONDS`) before returning a
  structured "still pending" result for the MCP client to poll on; if the daemon restarts before a
  human answers, that wait, and the popup itself if a human already has it open in a browser tab,
  simply has nothing left to resolve against. The calling tool call fails or times out on the
  client side rather than silently completing or silently being denied — nothing was approved and
  nothing was leaked, but nothing was gracefully drained either. There is no restart-safe or
  graceful-shutdown mode that waits for in-flight approvals to clear first.
- **What is *not* invalidated:** everything under [§2](#2-what-org-mode-persists-and-where)'s
  "Persisted, on disk" column — connector OAuth tokens, `settings.yaml`, the audit log, the signed
  bundle and its pinned key, and the registered-clients file. A restart is a session-layer event
  only; it does not touch any connector credential or configuration.

Operationally: schedule upgrades (which always restart the daemon, per
[§5](#5-upgrade--rollback)) for a low-usage window if avoiding a wave of simultaneous re-sign-ins and
occasional failed-mid-flight tool calls matters to your organization — there's no way to make a
restart transparent to already-connected sessions today.

---

## 8. Single-daemon availability model

**Org mode runs as exactly one process on exactly one server, with no clustering, load balancing, or
failover — this is enforced, not merely the deployment guide's suggestion.**
`daemon_main.py`'s own module docstring states it plainly: "Only one instance is allowed (enforced
via a lock file)" — `_acquire_instance_lock()` takes an exclusive, non-blocking `flock` on
`privacyfence.lock` at startup and exits immediately if another instance already holds it, rather
than queuing or retrying. This isn't specific to org mode (the same lock exists for local mode) —
org mode simply makes the consequence organization-wide rather than single-employee: every
principal's `/mcp`, `/approvals`/`/security`, `/connect`, and `/login` traffic is served by that one
process, so its availability *is* the service's availability.

What follows from that, concretely:

- **No horizontal scaling.** You cannot run two `privacyfence-app` processes behind Caddy for either
  throughput or redundancy — the second one refuses to start (the lock), and even if it somehow did,
  the in-memory session/OAuth-token stores [§7](#7-restartsession-invalidation-behaviour) describes
  aren't shared between processes, so a request load-balanced to the "wrong" instance would see an
  unrecognized session.
- **No automatic failover.** If the server or the process dies, org mode is down for every principal
  until it — or a replacement — comes back up. The only recovery mechanism this repo ships is
  `systemd`'s own `Restart=on-failure` / `RestartSec=5` in the unit file
  [`org-mode-setup-guide.md` Step 7](org-mode-setup-guide.md#7-run-privacyfence-as-a-service) sets
  up, which restarts a crashed *process* on the same host — it does nothing for a dead host, a full
  disk, or any failure that isn't "the process exited." There is no `/health` or `/healthz` endpoint
  for an external load balancer or orchestrator to probe, and none is needed for anything in this
  repo today, since there is never more than one instance to route around.
- **A restart, whether from a crash or a deliberate upgrade, has exactly the session/approval
  consequences [§7](#7-restartsession-invalidation-behaviour) describes** — every principal is
  affected simultaneously, since they all share the one process.
- **This is a single point of failure by design, not by omission** — the same "no BCP, because there
  is no PrivacyFence-operated *service* to keep continuous" reasoning
  [`security-and-compliance.md` §9](security-and-compliance.md#9-vendor-risk-criteria-no-isms-no-bcp-no-sla--and-why-thats-a-risk-acceptance-decision-not-a-security-gap)
  applies to the *vendor's* lack of a BCP applies here to the *server's* own availability: PrivacyFence
  gives you the software and the systemd unit; whether that one process runs on infrastructure with
  its own redundancy (a VM with automatic restart-on-host-failure, a supervisor that pages someone,
  etc.) is entirely your organization's own infrastructure decision, uninvolved with PrivacyFence's
  design. If continuous availability across a host failure matters to your organization, plan for it
  at the infrastructure layer (VM auto-restart, monitoring/alerting on the `privacyfence` systemd
  unit, a documented manual-failover runbook to a standby host with the backup restored per
  [§4](#4-restore-procedure)) — nothing in PrivacyFence itself provides it.

---

## See also

- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) — how to stand this up in the first place.
- [`security-and-compliance.md`](security-and-compliance.md) — the trust-center-style document this
  one supplements; §2's deployment-model table and maturity note, §4's human-in-the-loop model, and
  §8's session-lifetime/storage-format details are all referenced above rather than repeated.
- [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md#connectors--privacy-matrix) — the per-tool
  privacy matrix, including the ungated (`auto`) tier's
  [own section](TECHNICAL_REFERENCE.md#the-auto-tier-across-all-connectors), added per this same
  §14.3 item.
- `security-remediation-plan.md` (now removed — all phases landed) — item 3.13 is what this document
  fulfills; Phase 1's org-mode hardening items are the security fixes this document's operational
  claims build on top of.
