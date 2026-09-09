# Security, Privacy & Compliance Statement

This document is written for the audiences that typically have to sign off before PrivacyFence
can be installed inside an organization: information security, IT administration, privacy/legal
(GDPR), and AI governance reviewers (EU AI Act). It follows the same structure enterprise vendors
use in their "trust center" pages — deployment model, administrative control, human oversight,
data handling, regulatory positioning, and an FAQ — adapted to what PrivacyFence actually is: a
local, IT-gated control layer, not a hosted service.

If a claim below needs to be verified against the running code rather than taken on trust, that's
noted explicitly — this document favors being checkable over being reassuring.

---

## 1. What PrivacyFence is, in one paragraph

PrivacyFence is software that sits between an AI assistant (Claude, via MCP) and an employee's
connected accounts (Gmail, Drive, Slack, Calendar, Salesforce, Jira/Confluence, Telegram, Tasks,
Contacts). It ships in two deployment shapes, described fully in [§2](#2-deployment-model-local-or-org-run-by-you-either-way):
a packaged macOS application an individual employee installs on their own Mac (**local mode**), and
a headless service IT runs once on a server for the whole organization (**org mode**), which people
reach over HTTPS and sign into as themselves. Either way it is not a cloud service: there is no
PrivacyFence-operated backend, and no PrivacyFence-owned server ever receives, stores, or processes
any of the data it mediates — org mode's server is infrastructure *your organization* stands up and
operates, not one PrivacyFence's author runs. Every read or write the AI attempts is intercepted by
whichever of the two you're running, checked against IT-defined scope and per-user review rules, and
— where the rules require it — held for explicit human approval before it reaches the AI or the
external service.

---

## 2. Deployment model: local or org, run by you either way

PrivacyFence runs in one of two modes, chosen by IT when the daemon (`privacyfence-app`) is
configured — never something an individual employee or the AI can switch — and both are software
your organization runs and controls, not a PrivacyFence-operated service:

| Property | **local mode** (default) | **org mode** (opt-in) |
|---|---|---|
| Who it's for | One employee, one machine | An organization's whole user base, from one shared install |
| Where it runs | On the employee's own Mac, as a local daemon reachable over its own embedded, loopback-only (`localhost`) `/mcp` HTTP endpoint — directly (Claude Code) or via a thin stdio-to-HTTP shim Claude Desktop's `.mcpb` installs (no service credentials, no tool-schema knowledge of its own) | On a server IT provisions and operates (documented for Ubuntu in [`org-mode-setup-guide.md`](org-mode-setup-guide.md)), reachable over HTTPS — directly or, as that guide sets up, behind a reverse proxy (e.g. Caddy) that terminates TLS and forwards plaintext to PrivacyFence's own loopback bind |
| Who authenticates, and how | Nobody signs in as anybody — there is exactly one implicit local principal, and every request to the daemon is authorized by possession of a random secret written to a file on that same machine (see §8) | Each person signs in as themselves via **OIDC against the organization's own identity provider** (Google Workspace, Okta, Entra ID, or any OIDC-compliant IdP, discovered from its `/.well-known/openid-configuration`) — the same login, for a browser or for an MCP client's OAuth 2.1 handshake, resolves to the same `Principal`, so a browser session and an MCP token issued for the same sign-in are provably the same identity |
| Where data is processed | Locally, in-process, on that machine | In-process, on the org-mode server IT operates — still not a PrivacyFence-operated destination |
| Where data is stored | Locally: OS credential storage / local token files, and a local audit log (`logs/audit/*.jsonl`, `*.xlsx`) | On the org-mode server, under a per-principal directory; still a local audit log, on that server, not a PrivacyFence-hosted one |
| Vendor-operated infrastructure | None, either mode. There is no multi-tenant service, no hosted database, and no PrivacyFence API that traffic passes through — org mode's server is infrastructure your organization stands up and operates from PrivacyFence's own source, same as local mode's `.app` |
| Network path for a tool call | `Claude → local, token-authenticated 127.0.0.1 /mcp HTTP endpoint (directly, or via the stdio shim for Desktop) → local daemon → the connector's own cloud API (Google, Slack, Salesforce, Atlassian, Telegram) directly` | `Claude (anywhere) → HTTPS → org-mode server's /mcp endpoint (OAuth 2.1, bearer token issued after the OIDC sign-in above) → connector's own cloud API directly` |
| Telemetry / analytics / phone-home | No usage analytics or crash reporting. One narrow exception: a once-a-day unauthenticated `GET` to `api.github.com` checking for a newer release (`update_checker.py`) — no user, organization, or connector data is included or ever sent, only PrivacyFence's own version string is compared against the response locally. On by default; disable from PrivacyFence Settings's "Check for Updates" > "Enabled" or `update_check.enabled: false` in `settings.yaml` | Same update check, same opt-out |

This is the architectural reason PrivacyFence can make a stronger data-residency claim than a
typical SaaS AI add-on, in either mode: there is no PrivacyFence-vendor server in the request path
to compromise, subpoena, or have a data breach at — org mode's server is your organization's own
infrastructure, under your own operational and physical control, not a third party's. The trust
boundary an auditor needs to evaluate is the employee's endpoint (local mode) or your organization's
own server (org mode), plus the OAuth grants to the underlying SaaS providers (Google, Slack,
Salesforce, Atlassian) and, in org mode, the trust already placed in your own identity provider —
not a new third party in either case.

**What this means for your own review:** you already trust Google/Slack/Salesforce/Atlassian
with this data (your organization is already a customer of theirs), and in org mode you already
trust your own IdP to authenticate your people. PrivacyFence does not add a new data processor to
that chain — it adds a control point, running on infrastructure you already control, that can only
*restrict* what an AI assistant is allowed to do with data that already flows through those
existing, approved services.

**A note on maturity:** org mode is real, implemented, and exercised by its own test suite, but it
is newer and has run in fewer production environments than local mode. Treat the two modes'
relative maturity as part of your own risk assessment — see
[`org-mode-setup-guide.md`](org-mode-setup-guide.md) for its current caveats (e.g. the Linux service
packaging noted there as not yet battle-tested end to end) and
[`docs/security-remediation-plan.md`](security-remediation-plan.md)'s Phase 1 for the org-mode
hardening work still in flight as of this writing.

---

## 3. IT administrative authority

PrivacyFence is deliberately split into two configuration layers so that **the organization**,
not the individual employee, decides what is even possible:

1. **Organization config bundle** (`org_config.json`) — built once by IT (`scripts/build_org_bundle.py`)
   from the organization's own registered OAuth apps (Google Cloud project, Slack app, Salesforce
   Connected App, Atlassian OAuth app), then distributed to users.
2. **Per-user settings** — each employee authenticates the connectors they need and configures
   their own review/auto-accept preferences within the space IT has allowed.

The load-bearing control is layer 1: **a connector is only offered to a user at all if IT included
its section in the bundle.** If IT does not want Salesforce or Slack data reachable by AI in a
given team, they simply omit that block when building the bundle — the connector never appears as
an option, regardless of what the employee or the AI requests. There is no user-side override and
no way for Claude to request a connector into existence. This is enforced in
[`scripts/build_org_bundle.py`](../scripts/build_org_bundle.py): each service's credentials are
independent, additive sections, and `/mcp` only advertises tools for services present in the
installed bundle.

In short: **IT holds the actual access-granting authority.** The employee's role is limited to
signing in (per-connector OAuth) and tuning how cautious *their own* review gate is — never to
expanding *which systems* are reachable in the first place.

**What IT should verify itself, rather than take on trust:** inspect `scripts/build_org_bundle.py`
and confirm which OAuth scopes are requested per connector (documented per-service in
`docs/google-cloud-setup.md`, `docs/slack-setup.md`, `docs/salesforce-setup.md`,
`docs/atlassian-setup.md`); those scopes are the actual ceiling on what any connector can ever
read or write, independent of PrivacyFence's own gating logic.

**Bundle integrity.** Because `org_config.json` carries real credentials (and, in org mode, this
daemon's own IdP/authorization-server trust configuration), a silent replacement of it is as
dangerous as a compromise of IT's own build process. Every daemon startup logs the bundle's
sha256 to both the application log and the audit trail, so a tampered file is detectable by
comparing hashes even on an install that hasn't adopted the rest of this. Signing is additionally
available: `scripts/build_org_bundle.py --generate-signing-key`/`--sign-key` sign the bundle with
an Ed25519 key IT keeps; the first signed bundle any install ever sees has its key trusted and
pinned on that basis (trust-on-first-use, the same model SSH host keys use — see
`src/privacyfence/org_bundle_signing.py`), and every bundle after that — installed via PrivacyFence
Settings or dropped onto disk by hand — must verify against that pinned key or is rejected outright,
including a downgrade to an unsigned bundle. Org mode requires a signed bundle; local mode's
adoption of signing is optional.

The Calendar connector's optional room-lookup feature is a concrete example of that ceiling being
kept as narrow as possible: `calendar_list_rooms` needs Google Workspace's admin-level directory
scope to discover rooms at all, but that scope never touches the OAuth client every employee
authorizes day to day. Instead, IT runs a separate script (`scripts/sync_room_directory.py`)
against a *second*, admin-scoped Google Cloud project to produce a one-time (or periodically
refreshed) data snapshot — plain room names/emails/buildings/capacities — which is merged into the
same `org_config.json` bundle everyone already installs. The everyday Calendar client's own token
never carries directory-read access; a leaked or over-shared employee token simply cannot enumerate
your Workspace's room resources.

---

## 4. Human-in-the-loop control

Every tool call — from either direction — passes through one of three gates before it executes,
defined in the [Review model](TECHNICAL_REFERENCE.md#review-model) section of the Technical Reference:

- **`auto`** — allowed to proceed automatically, but still recorded in the audit log as
  `auto_accepted`. Reserved for narrow, pre-defined low-risk conditions (e.g., "I am the sender,"
  "the file is one I created this session") — see [Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules).
- **`review`** — the AI-bound read is held; the human sees a minimal preview and must explicitly
  **Allow once** or **Deny** before any content reaches the AI.
- **`popup`** — the AI-initiated write/action (send an email, post to Slack, edit a Jira issue,
  etc.) is held in a popup showing the full action before it goes out, with the same **Allow
  once**/**Deny** choice.

Both `review` and `popup` are popups PrivacyFence shows itself, on its own embedded local web
approval page — there is no native OS dialog and no separate Claude-side approval step for either
one. This has been true since the "P10" refactor retired PrivacyFence's earlier native macOS menu
bar/dialog UI in favor of the web surface described in §4's later notes and §8; the web page is now
the only approval surface, on every platform the daemon runs on, in both local and org mode.

No tool call bypasses this gate silently. Even the `auto` gate is a logged, IT-and-user-configured
exception — never a default absence of control. Sensitive actions (writes, and any read of full
message/document bodies) default to `review` or `popup`; only low-sensitivity metadata listing
operations (e.g., "list my calendars") default to `auto`.

**PII detection gate:** on the `review` (read) direction, PrivacyFence runs a local,
regex-based scan (Hungarian, English, German) over the content shown in every `review` dialog,
before the human decides — and before any auto-accept rule is checked. A match overrides a
matching rule (a `review` call is content-blind to the rule, so PII in an otherwise-trusted
sender/folder still routes to a human) and tints the dialog, forcing one additional explicit
confirmation on top of Allow once — see [PII detection gate](TECHNICAL_REFERENCE.md#pii-detection-gate) in the
Technical Reference. It is a best-effort heuristic layered on top of human review, not a substitute for it,
and by default it never logs or stores the matched text — only category labels, in the audit entry
for that decision. An opt-in setting (`pii_detection.audit_match_details`, off by default, meant
for a bounded refinement-trial window rather than everyday use) additionally records the matched
text for an *approved* decision only — redacted for a category whose match is itself the sensitive
value (IBAN, credit card, national ID/tax numbers, IP address, currency figures) — and never for a
denied one; see the Technical Reference section above for the full contract. It does not otherwise
run on the `popup` (write) direction: that content is normally
Claude's own generated output for an action already described in chat, not external personal data
newly reaching Claude. `drive_upload_file` is the one deliberate exception — its payload can be an
arbitrary local file Claude never actually read, so it gets the same real scan and forced
confirmation, extracting text from plain text, HTML, PDF, DOCX, PPTX, and XLSX content (no OCR on
images).

**Note for reviewers evaluating the MCP-level permission model:** `/mcp` advertises every tool
to Claude as `readOnlyHint = true`, including writes. This is documented and
intentional (see [Why every tool is advertised as read-only](TECHNICAL_REFERENCE.md#why-every-tool-is-advertised-as-read-only))
— it removes a redundant, non-configurable client-side prompt, because PrivacyFence's own gate,
not the MCP client's tool annotations, is the actual enforcement point. Authorization decisions
are made by the daemon against the tool's real `read_only`/gate metadata before any external
request is made; the annotation Claude sees is cosmetic UI hinting, not a security control, and
should not be read as PrivacyFence treating writes as safe.

**Note for reviewers evaluating the web settings surface (`/settings`, on by default in local mode,
`web.settings.enabled` — see [Web surfaces](TECHNICAL_REFERENCE.md#web-surfaces-approvals-settings)
in the Technical Reference):** it is reachable only within an authenticated `pf_session` (§8's
"Local-mode token semantics" note — a short-lived, single-use bootstrap link exchanged for an
independent, expiring session, not a persistent secret carried in the URL), and every mutating
request additionally carries a CSRF double-submit token (the session id itself) and an `Origin`
check. Within that authenticated session, the set of actions a request can
invoke is an **explicit allowlist** — an unrecognized action name is rejected before any lookup
happens at all, and each allowed action's arguments are validated against its real parameter types
(a malformed argument is a 400, not passed through). Nothing reachable from this surface — or from
`/approvals` — ever shells out to a native OS picker, `open`, or any other subprocess on the host.
Quitting the daemon from a browser is behind its own explicit in-page confirmation and a
config-level kill switch (`web.settings.allow_quit`). This posture — allowlist, CSRF/Origin, no host
subprocess — was deliberately built for local mode's `/settings`. **`/settings` itself is not
currently mounted in org mode at all** (org mode's `/approvals` and `/security`, described in §2, are
a separate, principal-aware route set built for that mode instead); porting settings management to
org mode's per-principal session model is documented follow-up work, not something reachable today.

**Auto-accept configuration itself is human-gated, not just the tool calls it governs.** Claude can
read the current `auto_accept_rules`/`auto_accept_grants` config (`privacyfence_list_auto_accept_rules`)
and propose adding, updating, or removing an entry
(`privacyfence_propose_auto_accept_rule_change`) — see
[Reading and proposing auto-accept changes over MCP](TECHNICAL_REFERENCE.md#reading-and-proposing-auto-accept-changes-over-mcp).
Every proposed change still blocks on the same confirmation step, on the same web approval page, the
"Always allow" button already uses; there is no code path from `/mcp` to `settings.yaml` that skips a
human decision, including when an identical entry already exists. This keeps the gate itself — not
just what passes through it — under the same human-in-the-loop control described above.

**Scheduled/unattended tasks — an explicit, opt-in exception to "always ask a human."** A scheduled
Claude Cowork Routine can run with nobody at the keyboard, so a `review`/`popup` call with no
matching auto-accept rule has no human available to answer a popup. An administrator can opt into
**unattended-session mode** (off by default, set only in the organization config bundle — never a
per-user or per-session setting) so that, for a connection explicitly marked as running such a task,
an unmatched call is **denied immediately** rather than left as an unanswered popup. This never
changes what auto-accepts — only what happens when nothing would have; the denial is logged
distinctly (`denied_unattended`) from a human's own `rejected` decision. See
[Scheduled / unattended Cowork tasks](TECHNICAL_REFERENCE.md#scheduled--unattended-cowork-tasks) for
the full mechanism. Reviewers evaluating human-oversight guarantees (§4, Article 14 in §7) should
treat this as the one deliberate, IT-gated carve-out from "every sensitive call waits for a human" —
narrower calls still deny rather than silently proceed.

---

## 5. Data handling

- **Data minimization by default:** for the six connectors with a documented category schema —
  Gmail, Google Drive (including Sheets), Slack, Google Contacts, Google Tasks, and Confluence (see
  `src/privacyfence/resources/settings.yaml.example`'s `privacy`/`drive_privacy`/`slack_privacy`/
  `contacts_privacy`/`tasks_privacy`/`confluence_privacy` sections, enforced by
  `src/privacyfence/privacy_filter.py`) — the default policy for undefined categories is `block`,
  and each configured category narrows what reaches a human's review popup or, for several of
  these connectors' auto-approved list/search tools, what reaches Claude directly with no popup at
  all: filtering is a floor under human review, not a substitute for it. The remaining connectors
  (Salesforce, Jira, Telegram) have no category-based privacy filter of their own; what they
  disclose is governed by the review/popup gate (§4) and by what each connector's code
  structurally includes or omits (e.g. attachment content is never carried in a read, by design —
  see `coding-and-testing-guidelines.md` §1.3), not by a configurable category policy. Calendar has
  one narrower, single-setting exception: `calendar.free_busy_full_event_details` can force
  `calendar_get_free_busy` to withhold full event titles from colleagues' calendars regardless of
  access — a standalone boolean, not part of the category-schema system above. See
  [`claude-knowledge-boundary.md`](claude-knowledge-boundary.md) for exactly which fields each
  category governs, tool by tool, and which auto-approved tools bypass a category that looks like
  it should cover them.
- **No aggregation, no secondary use:** PrivacyFence does not copy data to any store beyond the
  local audit log entry needed to record the decision. It does not build profiles, does not train
  models, and has no mechanism to transmit mediated content anywhere other than back to the
  Claude session that requested it (once approved).
- **Credentials:** OAuth tokens and connector credentials are stored locally
  (`credentials/`, local token files) and never transmitted to any PrivacyFence-operated
  destination — there isn't one.
- **Audit trail:** every decision (approved, denied, or auto-accepted) is appended to a local
  JSON-lines file per week, auto-exported to a formatted Excel workbook. In **local mode** this log
  is local to the employee's own machine — PrivacyFence does not ship a mechanism to centrally
  collect these logs for IT, so an organization that requires that for its own compliance program
  should plan for it separately (e.g., MDM-based log collection) rather than assume it happens
  automatically. In **org mode** the log is already on one server IT controls, covering every
  principal's actions from that install — but it is still a local file on that server, not forwarded
  anywhere (e.g. to a SIEM) and not append-integrity-protected against tampering by whoever has write
  access to that server; centralized forwarding and tamper-evidence are tracked as SEC-23 in
  [`security-remediation-plan.md`](security-remediation-plan.md), not implemented yet.

---

## 6. GDPR positioning

PrivacyFence is software the organization runs on its own (or its employees') endpoints, using
the organization's own OAuth grants to the organization's own existing SaaS providers. Read this
section as a starting point for your own DPIA/legal assessment, not as a substitute for it —
PrivacyFence's author is not your data processor.

- **Controller:** your organization, as it already is for the underlying Google/Slack/Salesforce/
  Atlassian/Telegram data.
- **Processor chain:** unchanged from today. PrivacyFence does not insert a new processor into the
  chain between your organization and those providers, because no PrivacyFence-operated
  infrastructure ever receives the data (see §2). There is no PrivacyFence Data Processing
  Agreement to sign for the same reason a local text editor doesn't need one — the software runs
  entirely within your own controller boundary.
- **Sub-processors:** none. PrivacyFence has no sub-processor list because it has no processing
  operation of its own outside the local device.
- **Purpose limitation / Article 5:** the auto-accept rules and default-`block` policy exist
  specifically to let the organization encode purpose limitation as machine-enforced policy (e.g.,
  "only auto-approve reads of mail the account itself sent") rather than relying on the AI's
  self-restraint.
- **Data subject rights:** because PrivacyFence stores no data on its own infrastructure, access/
  erasure/portability requests are served exactly as they are today, against Google/Slack/
  Salesforce/Atlassian/Telegram directly. The local audit log is the one PrivacyFence-specific
  record and is subject to whatever retention policy the organization sets locally (it is a
  file on disk, not a managed data store).
- **International transfers:** no PrivacyFence-controlled cross-border transfer exists, because
  there is no PrivacyFence-controlled processing location.

---

## 7. EU AI Act positioning

PrivacyFence is not itself the AI system under the Act — Claude (or whichever assistant is
connected) is. PrivacyFence's role is best read as a **deployer-side risk-mitigation and human
oversight measure**, sitting in front of the AI system rather than being one:

- **Human oversight (Article 14):** PrivacyFence is, structurally, a human-in-the-loop enforcement
  layer — see §4. It gives the deployer a concrete, auditable mechanism ("this specific read/write
  was approved by this human, at this time, and here is the record") rather than a policy
  statement that oversight exists.
- **Technical documentation / traceability (Article 12):** the audit log (§5) provides a
  per-action record of what the AI requested, what gate applied, and what a human (or a
  pre-approved rule) decided — useful raw material for an organization's own AI system logging
  obligations, though it documents PrivacyFence's mediation, not the AI model's internal
  reasoning.
- **Data governance (Article 10):** the connector-level access control (§3) and category-level
  privacy filters (§5) let an organization restrict, in advance, which categories of personal or
  sensitive data an AI system is permitted to access at all — independent of what the AI model
  itself would otherwise be capable of requesting.
- **What PrivacyFence does not do:** it does not classify AI systems, perform conformity
  assessments, generate an FRIA/DPIA on your behalf, or make any claim about the risk tier of the
  connected AI assistant. Whether your particular use of Claude (or another assistant) with these
  connectors constitutes a "high-risk" use case under the Act depends on your use case, not on
  PrivacyFence — that determination, and the resulting obligations, remain the deploying
  organization's responsibility.

---

## 8. Security controls summary

| Control | Implementation |
|---|---|
| Authentication to connected services | OAuth2 (or Telethon/MTProto for Telegram), per user, per connector — no shared service accounts |
| Authentication to PrivacyFence itself | **Local mode:** possession of a random bearer token written to a local file is the whole authorization model — see the "Local-mode token semantics" note below for exactly what that does and doesn't provide. **Org mode:** each person signs in via OIDC against the organization's own IdP (§2); the resulting server-side session is an unguessable, HttpOnly/Secure/SameSite=Strict cookie with a 30-minute sliding idle timeout and a 24-hour absolute cap from sign-in regardless of activity, held in memory only (a session does not survive a daemon restart — signing in again is the accepted cost, see `web/org_session.py`); an MCP client's OAuth refresh token carries the same absolute-lifetime cap (30 days from its original issuance, unaffected by rotation) alongside its own rotate-on-use behavior (see `web/oauth_provider.py`) |
| Least privilege | Per-connector, per-operation gating (`auto`/`review`/`popup`); auto-accept rules can be scoped down to a single folder, spreadsheet tab, channel, or task list |
| PII detection gate | Local regex heuristic (Hungarian/English/German) over `review` (read) dialog content only; a match requires an extra explicit confirmation before Allow once takes effect. Toggleable per user (PrivacyFence Settings / `pii_detection.enabled`) |
| Transport to Claude | **Local mode:** loopback-bound (`localhost`) `/mcp` Streamable HTTP endpoint, authenticated by a shared bearer token (`~/.privacyfence/mcp_token`) required on every request; Claude Desktop's stdio shim carries no credentials of its own and only relays it. **Org mode:** `/mcp` over HTTPS, authenticated by a real OAuth 2.1 authorization server (dynamic client registration, PKCE, tokens bound to the OIDC-verified principal) instead of one shared secret |
| Web approval/settings surface | **Local mode** (opt-in for `/settings`, always-on for `/approvals`): loopback-bound (`localhost`) embedded HTTP server; every request requires an authenticated `pf_session` cookie — minted by exchanging a short-lived, single-use bootstrap code, never a persistent secret carried in the URL (see "Local-mode token semantics" below) — CSRF double-submit + `Origin` check on every mutation, an explicit action allowlist (not `getattr`) behind `/settings`, and no code path reachable from an HTTP request ever runs a subprocess on the host. **Org mode:** a separate, principal-scoped `/approvals`/`/security` surface (§2) authenticated by the OIDC-backed session above, not a local secret; a write decision additionally requires a fresh WebAuthn step-up when the organization has turned that on. `/settings` is not mounted in org mode at all yet (see §4) |
| Process isolation | The Desktop-only shim (untrusted-facing, no credentials, no tool-schema knowledge) and the daemon (holds credentials) are separate processes; only the daemon can reach external APIs |
| Secrets at rest | Local OS-level storage / local files under `credentials/`, org-mode credentials under a per-principal directory on the org-mode server; never committed to source control (`.gitignore`'d), never transmitted off-device. See "Storage format and permissions" below for exactly what protects these files today, and what doesn't yet |
| Auditability | Every decision logged with outcome (accepted/denied/auto_accepted), locally, in a human-readable format (JSONL + Excel) |
| Code signing / notarization | The macOS `.app` release is code-signed with a Developer ID Application certificate and notarized by Apple; Gatekeeper accepts it with no manual steps (see [Technical Reference](TECHNICAL_REFERENCE.md#installation)). Org mode is deployed from source/PyPI onto a server IT controls, so Gatekeeper/notarization doesn't apply there — the equivalent control is your organization's own provenance for the server it stands up (e.g. installing a pinned, reviewed release rather than an unreviewed checkout, per §9) |
| Third-party dependencies | Standard OAuth/SDK libraries per connector (google-auth, slack_sdk, telethon, atlassian-python-api); no PrivacyFence-operated backend dependency |

**Local-mode token semantics — the post-SEC-06 bootstrap flow.** Two persistent secrets, each
generated once and written 0600 to a file under `~/.privacyfence/`, still anchor local mode's whole
authorization model — but since SEC-06 (Phase 1 item 1.2 in
[`security-remediation-plan.md`](security-remediation-plan.md)) they no longer play the role an
earlier version of this document described, and neither is ever carried in a URL or written to the
log file:

- **`mcp_token`** is unaffected by SEC-06 and remains the whole authorization model for `/mcp`:
  possession of it, presented as an `Authorization: Bearer` header on every request (Claude Desktop's
  stdio-to-HTTP shim relays it but holds no credentials of its own), is what lets Claude reach the
  daemon at all. It is generated once and reused unchanged across restarts, same as before.
- **`web_token`** no longer authenticates a browser directly and is never sent to one. Its only
  remaining job is authorizing `POST /api/bootstrap` — via an `Authorization: Bearer` header, never a
  query string — to mint a fresh bootstrap code on demand, without restarting the daemon.

What a browser actually sees is a **bootstrap code**: a random, single-use value, valid for 10
minutes, that the daemon mints and logs at every startup as a full link
(`http://localhost:8765/approvals?bootstrap=<code>`, and similarly for `/settings`) — the only thing
this flow ever puts in a URL or a log line. Opening that link consumes the code immediately (a
replay, a second click, or a link that has simply expired all fail the same way, with no
distinguishing signal), and on success mints an independent, random session id
(`web/session_auth.py`'s `LocalSessionStore`), set as an `HttpOnly`/`SameSite=Strict` `pf_session`
cookie — the same value the CSRF double-submit check (§4) compares against. That session has a
30-minute sliding idle timeout, renewed on every authenticated request, and a 24-hour absolute cap
from creation regardless of activity; it lives in memory only and does not survive a daemon restart.
Once a session lapses, getting back in means either restarting the daemon (which logs a fresh
bootstrap link) or, without restarting, `POST /api/bootstrap` with the persistent `web_token` as a
bearer header to mint a new code on demand.

`web_token` is additionally rotated automatically whenever the installed version changes — including
the very first startup after upgrading to this fix, which dead-ends any `?token=` link, shell-history
entry, or log line an older, pre-SEC-06 build had already produced, since that value no longer means
anything to the new code. Net effect versus the design this section used to describe: nothing
long-lived is ever carried in a URL or written to the log file, a leaked bootstrap link is a single,
time-boxed attempt rather than a standing credential, and a session established from it has a real
ceiling instead of lasting forever. What hasn't changed: reaching either secret still requires
filesystem or log access to that specific machine, and neither secret by itself grants access to any
connected service (each connector still requires its own separate OAuth grant).

**Storage format and permissions.** Every credential/token/config file is written through a shared
helper (`secure_files.py`, SEC-09 in [`security-remediation-plan.md`](security-remediation-plan.md))
that writes to a fresh
`O_CREAT|O_EXCL`-created temp file in the same directory — already at `0600` from the instant it
exists, never created with the process's default umask even briefly — then `fsync`s and
atomically `os.replace`s it into place. A reader can only ever see the old complete file or the new
complete file, never a partial write from a crash or a concurrent daemon instance. The directories
these files live in (`~/.privacyfence` and its subdirectories — `data_dir()`/`org_dir()`/
`user_dir()`) are created, and re-tightened on every resolution if they already existed at looser
permissions (e.g. from a pre-SEC-09 install), to `0700` the same way. A failure to apply either the
file or directory permissions — e.g. an unusual filesystem — is logged at `warning`, not silently
swallowed at `debug` the way it was before this fix. Daemon startup additionally audits
`data_dir()`/`org_dir()`/`user_dir()`'s actual on-disk permissions: local mode logs a warning and
keeps starting if any of them grants group/other access, organization mode refuses to start.

---

## 9. Vendor risk criteria: no ISMS, no BCP, no SLA — and why that's a risk-acceptance decision, not a security gap

Standard vendor-risk questionnaires typically ask for a certified information security
management framework (e.g. ISO 27001), a Business Continuity Plan, and a contractual risk-response
process or SLA. **PrivacyFence has none of these**, and reviewers should not expect to find them —
but the reason is structural, not an oversight, and it doesn't change the technical risk assessment
in §§2–8.

| Criterion | Status | Why, and what it does (and doesn't) mean |
|---|---|---|
| Certified information security framework (ISO 27001, SOC 2, etc.) | Not established | These certifications attest to controls around *operated infrastructure* — the thing being certified is an organization's data centers, access management, incident processes, etc. PrivacyFence has none of that to certify: there is no PrivacyFence-operated infrastructure at all (§2). A certification program doesn't map onto software that runs entirely on the employee's own machine. |
| Business Continuity Plan | None | A BCP protects continuity of a *service*. There is no PrivacyFence-operated service whose outage could disrupt your organization — if the maintainer became unreachable tomorrow, already-installed copies keep running locally exactly as before; nothing in PrivacyFence's core function depends on ongoing vendor availability beyond the pre-existing OAuth relationships you already have with Google/Slack/Salesforce/Atlassian, plus the optional, disable-able daily update check (§2). Continuity risk here is really *source availability* risk, and it is mitigated by the code being open source: your organization can audit, fork, or internally maintain a pinned version independent of the original maintainer. |
| Risk response process / SLA | None | There is no support contract. Suspected security issues go to **privacyfence@tkcs.name** or a public GitHub issue (§10 below) and are handled best-effort, not against a committed response time. Organizations that need a guaranteed patch turnaround should treat that as a real gap to plan around (e.g., pin to an internally reviewed release rather than auto-updating, and assign an internal owner able to patch or roll back if a report doesn't land in time) — not assume it away. |

**Why this doesn't add Information Security Risk in itself:** these three gaps describe the
*absence of vendor governance overhead*, not a technical vulnerability. The technical risk profile
that actually determines exposure — no vendor infrastructure in the data path, no new data
processor, no vendor-held credentials, a local-only audit trail, human-in-the-loop enforcement on
every sensitive call, code-signed and notarized macOS releases — is unaffected by any of the three
(§§2–8).
A vendor with a full ISMS and an SLA but a hosted backend in the request path is a *different*, and
in some respects larger, attack surface than a tool with none of that governance but nothing to
attack because there's no vendor infrastructure to reach.

**Recommended path for reviewers:** because PrivacyFence cannot supply an ISMS certification, BCP,
or contractual SLA, approve it through your organization's formal risk-acceptance / exception
process rather than a standard vendor-security sign-off — using the architectural mitigations in
§§2–8 as the supporting rationale, and the compensating controls below as the conditions attached
to that acceptance:

- Install from code-signed, notarized releases (§8) rather than building from an unreviewed
  checkout; pin to a specific reviewed version rather than auto-updating into unreviewed changes.
- Assign an internal owner responsible for periodic review of new releases and for patching/rolling
  back if a vulnerability is reported and no fix lands quickly.
- Treat the local audit log (§5) — not a vendor SLA — as your incident-visibility mechanism, and
  decide separately whether it needs centralized collection for your compliance program.
- Re-run this section's assessment (or your own source review, since the code is open) periodically,
  rather than treating this document as a one-time attestation.

---

## 10. Vulnerability reporting

Report suspected security issues to **privacyfence@tkcs.name** rather than filing a public GitHub
issue. Include reproduction steps and, where relevant, which connector and gate configuration was
involved.

---

## 11. Quick-reference FAQ for reviewers

| Question | Answer |
|---|---|
| Is this a SaaS product? | No, in either deployment mode (§2). It's software your organization runs — either an individual employee's local install, or an org-mode server your own IT operates — there is no PrivacyFence-vendor-operated backend at all. |
| Does our data leave our own approved cloud providers? | Organization/connector data does not — it flows only between the device running PrivacyFence (the employee's own machine, or your org-mode server) and the same Google/Slack/Salesforce/Atlassian/Telegram accounts your organization already uses. The one exception is the update checker's own daily version check against `api.github.com` (see §2's Telemetry row) — no organization or connector data is included in it, and it can be disabled. |
| Can an employee connect a service IT didn't approve? | No — a connector only exists as an option if IT included it in the organization config bundle, in either deployment mode. |
| Can the AI read or write data without a human seeing it first? | Only for narrowly-scoped, IT/user-configured `auto` rules, which are still logged; sensitive reads and all writes require explicit approval (`review`/`popup`). The one further exception is opt-in unattended-session mode for scheduled tasks (§4), which denies rather than silently approves an unmatched call. |
| What's the difference between local and org mode, and which should we use? | Local mode (default) is one employee, one machine, no sign-in beyond a local secret file (§8) — simplest to reason about, but with no per-person audit trail across a team and no central control once installed. Org mode (§2) is IT-run, multi-user, and ties every action to a real identity via your own IdP — the right choice once more than a handful of people need this and you want centralized deployment, but it's the newer of the two modes (see §2's maturity note) and doesn't yet cover every surface local mode does (e.g. `/settings`, per §4). |
| Is there a central admin console with visibility into every employee's approvals? | In local mode, no — audit logs are local per device; plan for separate centralized log collection if your compliance program requires it. Org mode consolidates the daemon and its audit log onto one server IT controls, but still has no dedicated admin-console UI over that log today — the log itself (§5) is the current mechanism. |
| Who is the data controller/processor under GDPR? | Your organization remains the controller in either mode; PrivacyFence does not add a new processor since it operates entirely within your own infrastructure boundary, whether that boundary is an employee's laptop or a server your IT operates. |
| Does PrivacyFence make AI Act risk-tier determinations for us? | No. It's a deployer-side control (human oversight, access restriction, audit trail) — the risk classification of your AI use case is your organization's own determination. |
| Is the app notarized by Apple? | The packaged macOS `.app` used for local-mode installs is — see §8. Org mode is deployed from source/PyPI onto your own server, where Gatekeeper/notarization doesn't apply; use your own software-provenance controls there instead (§8, §9). |
| Does PrivacyFence have a certified ISMS, a Business Continuity Plan, or a contractual SLA? | No — see §9. Because there's no vendor-operated infrastructure to certify or keep continuous in either deployment mode, these don't map onto self-hosted software the way they would a hosted vendor. Approve through your organization's risk-acceptance process, using §§2–8's architecture as the supporting rationale, not a standard vendor-security sign-off. |
