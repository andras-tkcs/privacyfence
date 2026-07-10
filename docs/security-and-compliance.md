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

PrivacyFence is a macOS application that sits between an AI assistant (Claude, via MCP) and an
employee's connected accounts (Gmail, Drive, Slack, Calendar, Salesforce, Jira/Confluence,
Telegram, Tasks, Contacts). It is not a cloud service: there is no PrivacyFence-operated backend,
and no PrivacyFence-owned server ever receives, stores, or processes any of the data it mediates.
Every read or write the AI attempts is intercepted locally, checked against IT-defined scope and
per-user review rules, and — where the rules require it — held for explicit human approval before
it reaches the AI or the external service.

---

## 2. Deployment model: local, not SaaS

| Property | PrivacyFence |
|---|---|
| Where it runs | On the employee's own Mac, as a local daemon (`privacyfence-app`) plus an ephemeral MCP bridge process |
| Where data is processed | Locally, in-process, on that machine |
| Where data is stored | Locally: OS credential storage / local token files, and a local audit log (`logs/audit/*.jsonl`, `*.xlsx`) |
| Vendor-operated infrastructure | None. There is no multi-tenant service, no hosted database, and no PrivacyFence API that traffic passes through |
| Network path for a tool call | `Claude → local MCP bridge → local Unix domain socket → local daemon → the connector's own cloud API (Google, Slack, Salesforce, Atlassian, Telegram) directly` |
| Telemetry / analytics / phone-home | None built in — the codebase contains no telemetry, crash-reporting, or usage-analytics client shipping data to the author or any third party |

This is the architectural reason PrivacyFence can make a stronger data-residency claim than a
typical SaaS AI add-on: there is no vendor server in the request path to compromise, subpoena, or
have a data breach at. The trust boundary an auditor needs to evaluate is the employee's own
endpoint and the OAuth grants to the underlying SaaS providers (Google, Slack, Salesforce,
Atlassian) — not a new third party.

**What this means for your own review:** you already trust Google/Slack/Salesforce/Atlassian
with this data (your organization is already a customer of theirs). PrivacyFence does not add a
new data processor to that chain — it adds a local control point that can only *restrict* what an
AI assistant is allowed to do with data that already flows through those existing, approved
services.

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
independent, additive sections, and the bridge only advertises tools for services present in the
installed bundle.

In short: **IT holds the actual access-granting authority.** The employee's role is limited to
signing in (per-connector OAuth) and tuning how cautious *their own* review gate is — never to
expanding *which systems* are reachable in the first place.

**What IT should verify itself, rather than take on trust:** inspect `scripts/build_org_bundle.py`
and confirm which OAuth scopes are requested per connector (documented per-service in
`docs/google-cloud-setup.md`, `docs/slack-setup.md`, `docs/salesforce-setup.md`,
`docs/atlassian-setup.md`); those scopes are the actual ceiling on what any connector can ever
read or write, independent of PrivacyFence's own gating logic.

---

## 4. Human-in-the-loop control

Every tool call — from either direction — passes through one of three gates before it executes,
defined in the [Review model](TECHNICAL_REFERENCE.md#review-model) section of the Technical Reference:

- **`auto`** — allowed to proceed automatically, but still recorded in the audit log as
  `auto_accepted`. Reserved for narrow, pre-defined low-risk conditions (e.g., "I am the sender,"
  "the file is one I created this session") — see [Auto-accept rules](TECHNICAL_REFERENCE.md#auto-accept-rules).
- **`review`** — the AI-bound read is held; the human sees a minimal preview and must explicitly
  **Accept** or **Deny** before any content reaches the AI.
- **`popup`** — the AI-initiated write/action (send an email, post to Slack, edit a Jira issue,
  etc.) is held in a native macOS popup showing the full action before it goes out, with the same
  **Accept**/**Deny** choice.

No tool call bypasses this gate silently. Even the `auto` gate is a logged, IT-and-user-configured
exception — never a default absence of control. Sensitive actions (writes, and any read of full
message/document bodies) default to `review` or `popup`; only low-sensitivity metadata listing
operations (e.g., "list my calendars") default to `auto`.

**PII detection gate:** on the `review` (read) direction only, PrivacyFence runs a local,
regex-based scan (Hungarian, English, German) over the content shown in every `review` dialog,
before the human decides — and before any auto-accept rule is checked. A match overrides a
matching rule (a `review` call is content-blind to the rule, so PII in an otherwise-trusted
sender/folder still routes to a human) and tints the dialog, forcing one additional explicit
confirmation on top of Accept — see [PII detection gate](TECHNICAL_REFERENCE.md#pii-detection-gate) in the
Technical Reference. It is a best-effort heuristic layered on top of human review, not a substitute for it,
and it never logs or stores the matched text — only category labels, in the audit entry for that
decision. It does not run on the `popup` (write) direction: that content is Claude's own generated
output for an action already described in chat, not external personal data newly reaching Claude.

**Note for reviewers evaluating the MCP-level permission model:** since v0.4.9 the bridge
advertises every tool to Claude as `readOnlyHint = true`, including writes. This is documented and
intentional (see [Why every tool is advertised as read-only](TECHNICAL_REFERENCE.md#why-every-tool-is-advertised-as-read-only))
— it removes a redundant, non-configurable client-side prompt, because PrivacyFence's own gate,
not the MCP client's tool annotations, is the actual enforcement point. Authorization decisions
are made by the daemon against the tool's real `read_only`/gate metadata before any external
request is made; the annotation Claude sees is cosmetic UI hinting, not a security control, and
should not be read as PrivacyFence treating writes as safe.

---

## 5. Data handling

- **Data minimization by default:** the default policy for undefined categories is `block` (see
  `src/privacyfence/resources/settings.yaml.example`). Each connector's privacy filter narrows what the review UI
  even shows before a human approves it — filtering is a floor under human review, not a
  substitute for it.
- **No aggregation, no secondary use:** PrivacyFence does not copy data to any store beyond the
  local audit log entry needed to record the decision. It does not build profiles, does not train
  models, and has no mechanism to transmit mediated content anywhere other than back to the
  Claude session that requested it (once approved).
- **Credentials:** OAuth tokens and connector credentials are stored locally
  (`credentials/`, local token files) and never transmitted to any PrivacyFence-operated
  destination — there isn't one.
- **Audit trail:** every decision (approved, denied, or auto-accepted) is appended to a local
  JSON-lines file per week, auto-exported to a formatted Excel workbook. This log is local to the
  employee's machine by default — PrivacyFence does not currently ship a mechanism to centrally
  collect these logs for IT. Organizations that require centralized audit collection for their own
  compliance program should plan for that separately (e.g., MDM-based log collection) rather than
  assume it happens automatically.

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
| Least privilege | Per-connector, per-operation gating (`auto`/`review`/`popup`); auto-accept rules can be scoped down to a single folder, spreadsheet tab, channel, or task list |
| PII detection gate | Local regex heuristic (Hungarian/English/German) over `review` (read) dialog content only; a match requires an extra explicit confirmation before Accept takes effect. Toggleable per user (menu bar / `pii_detection.enabled`) |
| Transport between processes | Local Unix domain socket only (`~/.privacyfence/privacyfence.sock`); the bridge carries no credentials and only relays |
| Process isolation | Bridge (untrusted-facing, talks to Claude) and daemon (holds credentials) are separate processes; only the daemon can reach external APIs |
| Secrets at rest | Local OS-level storage / local files under `credentials/`; never committed to source control (`.gitignore`'d), never transmitted off-device |
| Auditability | Every decision logged with outcome (accepted/denied/auto_accepted), locally, in a human-readable format (JSONL + Excel) |
| Code signing / notarization | **Not yet notarized** as of the current release — Gatekeeper requires a manual `xattr` step on first install (see [Technical Reference](TECHNICAL_REFERENCE.md#installation)). Treat this as an open item for endpoint security review, not a settled control. |
| Third-party dependencies | Standard OAuth/SDK libraries per connector (google-auth, slack_sdk, telethon, atlassian-python-api); no PrivacyFence-operated backend dependency |

---

## 9. Vulnerability reporting

Report suspected security issues to **privacyfence@tkcs.name** rather than filing a public GitHub
issue. Include reproduction steps and, where relevant, which connector and gate configuration was
involved.

---

## 10. Quick-reference FAQ for reviewers

| Question | Answer |
|---|---|
| Is this a SaaS product? | No. It's local software; there is no vendor-operated backend at all. |
| Does our data leave our own approved cloud providers? | No new destination is added — data flows only between the employee's device and the same Google/Slack/Salesforce/Atlassian/Telegram accounts your organization already uses. |
| Can an employee connect a service IT didn't approve? | No — a connector only exists as an option if IT included it in the organization config bundle. |
| Can the AI read or write data without a human seeing it first? | Only for narrowly-scoped, IT/user-configured `auto` rules, which are still logged; sensitive reads and all writes require explicit approval (`review`/`popup`). |
| Is there a central admin console with visibility into every employee's approvals? | Not currently — audit logs are local per device. Plan for separate centralized log collection if your compliance program requires it. |
| Who is the data controller/processor under GDPR? | Your organization remains the controller; PrivacyFence does not add a new processor since it operates entirely within your own infrastructure boundary. |
| Does PrivacyFence make AI Act risk-tier determinations for us? | No. It's a deployer-side control (human oversight, access restriction, audit trail) — the risk classification of your AI use case is your organization's own determination. |
| Is the app notarized by Apple? | Not yet as of the current release — see §8. |
