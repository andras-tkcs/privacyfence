# AI Client Compatibility: Claude, ChatGPT, Gemini

Plan and status for using PrivacyFence with AI systems other than Claude, split along the two access
modes documented in [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md) and the
[org mode setup guide](org-mode-setup-guide.md):

- **Local mode** — one macOS user, one daemon, `~/.privacyfence/mcp_token`. The client either talks
  Streamable HTTP directly (Claude Code) or through the stdio shim `PrivacyFence.mcpb` installs
  (Claude Desktop, which has no native way to reach an HTTP MCP endpoint).
- **Org mode** — one shared server (`org-mode-setup-guide.md`), reachable over HTTPS, `/mcp`
  authenticated by PrivacyFence's own OAuth 2.1 authorization server (`web/oauth_provider.py`)
  instead of a static token. This is the "URL" mode: point a client's remote-MCP feature at
  `https://pf.example.com/mcp` and let OAuth do the rest.

**Claude is the checked, shipped target for both.** Everything below asks the same question for
ChatGPT and Gemini: given that `/mcp` is built on the *official, generic* MCP Python SDK
(`StreamableHTTPSessionManager`, `create_auth_routes`, `create_protected_resource_routes` — see
`web/routes_mcp.py`'s module docstring) rather than anything Claude-specific, and the shim
(`mcpb/shim/`) is a generic stdio↔Streamable-HTTP proxy with "no knowledge of ToolSpec, no manifest
fetch... no JSON-RPC framing of its own" (its own module docstring) packaged in a Claude-Desktop-only
format — what does that genericity actually buy with the other two clients, today?

**How to read this doc:** claims about PrivacyFence's own code are verified against this repository.
Claims about ChatGPT's or Gemini's behavior are sourced from vendor documentation as of **2026-09**,
not from a live end-to-end test against those products (no ChatGPT/Gemini account or macOS host was
available in this session) — this is a fast-moving surface (see §6's "OpenAI/Google MCP client
support" gap), so treat every ⚠️ row as "should work per spec/docs, needs a live smoke test before
telling a user it's supported."

---

## Contents

- [Summary](#summary)
- [1. Claude (baseline, already shipped)](#1-claude-baseline-already-shipped)
- [2. ChatGPT](#2-chatgpt)
  - [2.1 Local mode](#21-local-mode)
  - [2.2 Org mode (URL)](#22-org-mode-url)
  - [2.3 Getting listed, to avoid Developer Mode](#23-getting-listed-to-avoid-developer-mode)
- [3. Gemini](#3-gemini)
  - [3.1 Gemini CLI (developer tool — the real analog to Claude Code)](#31-gemini-cli-developer-tool--the-real-analog-to-claude-code)
  - [3.2 Gemini Enterprise (the real analog to org mode's audience)](#32-gemini-enterprise-the-real-analog-to-org-modes-audience)
  - [3.3 Gemini app / Spark (consumer, low priority)](#33-gemini-app--spark-consumer-low-priority)
- [4. Gaps and follow-up work](#4-gaps-and-follow-up-work)
- [5. Verification log](#5-verification-log)

---

## Summary

| | Claude | ChatGPT | Gemini |
|---|---|---|---|
| **Local (stdio)** | ✅ shipped — `PrivacyFence.mcpb` (Claude Desktop); direct `/mcp` (Claude Code) | ⚠️ plausible, unpackaged — ChatGPT desktop's own `mcp_config.json` supports a stdio `command`, but there is no PrivacyFence-built installer for it | ⚠️ unnecessary — Gemini CLI has native Streamable HTTP, so it should skip the shim and hit `/mcp` directly, same as Claude Code |
| **Org mode (URL + OAuth 2.1)** | ✅ shipped — `claude mcp add --transport http`, native DCR | ⚠️ per-user Developer Mode should work unmodified (§2.2) · ⚠️ **admin-published workspace connector can drop the per-user Developer Mode step with no server change** (§2.3, the realistic "listed" path) · public App Directory listing (§2.3) is a poor fit — skip it | Split by surface: **Gemini CLI** ⚠️ should work unmodified (same DCR+PKCE discovery flow as Claude Code) · **Gemini Enterprise** ❌ blocked — no DCR/OAuth-discovery support, needs a pre-registered OAuth client PrivacyFence has no tooling to create · **Gemini app/Spark** ⚠️ unresearched beyond "OAuth-only, CA-signed TLS required" |

No server-side PrivacyFence code changes are implicated by any ⚠️ row above — every one of them is
"point a spec-compliant remote-MCP client at the same URL Claude already uses," including ChatGPT's
admin-published workspace connectors (§2.3). The ❌ row is the one real protocol gap, and it's client
tooling (an admin flow to pre-register an OAuth client), not a mismatch in what PrivacyFence serves.

---

## 1. Claude (baseline, already shipped)

- **Local**: `PrivacyFence.mcpb` — Claude Desktop double-clicks it, the shim discovers
  `~/.privacyfence/mcp_url` / `mcp_token` itself, nothing to configure by hand
  (`TECHNICAL_REFERENCE.md` §"Option A"). Claude Code skips the shim entirely and registers `/mcp`
  directly: `claude mcp add --transport http privacyfence http://localhost:8765/mcp --header
  "Authorization: Bearer $(cat ~/.privacyfence/mcp_token)"` (`TECHNICAL_REFERENCE.md` §"Option B").
- **Org mode**: `claude mcp add --transport http privacyfence https://pf.example.com/mcp` — no
  token to copy; the first tool call drives Claude's own OAuth 2.1 + DCR flow against
  PrivacyFence's authorization server (`org-mode-setup-guide.md` §9).

Both paths are live, documented, and (per `CLAUDE.md`/CI) covered by the test suite. Everything below
is measured against this as the reference implementation of "a spec-compliant MCP client using
`/mcp`."

---

## 2. ChatGPT

### 2.1 Local mode

ChatGPT desktop (macOS/Windows) added its own MCP server support in early 2026, configured via
`~/Library/Application Support/ChatGPT/mcp_config.json` (macOS) with an `mcpServers` map that, like
Claude Desktop's pre-`.mcpb` config, takes either a stdio `command`/`args` pair or a remote
Streamable HTTP `url` — added through Settings → MCP servers → Add server, not by editing JSON by
hand [[1]](#sources).

For local mode, this means:

- **No PrivacyFence-built installer exists for ChatGPT** — `.mcpb` is a Claude-Desktop-specific
  packaging format (a zip with a manifest Claude Desktop knows how to unpack and register); ChatGPT
  has no equivalent one-click extension mechanism, so there's nothing to "install" the way
  `PrivacyFence.mcpb` installs into Claude Desktop.
- **The shim itself is not Claude-specific** — `mcpb/shim/src/index.ts` is a plain
  `StdioServerTransport` ↔ `StreamableHTTPClientTransport` proxy that discovers the daemon's URL/token
  from `~/.privacyfence/mcp_url`/`mcp_token` and launches `privacyfence-app` if it isn't already
  running (`daemon.ts`). In principle, pointing ChatGPT's `command`/`args` at the *built* shim
  (`node <path-to>/dist/shim.js`, the same artifact `scripts/build_mcpb.sh` bundles into
  `PrivacyFence.mcpb`) should work exactly as well for ChatGPT desktop as it does for Claude Desktop —
  the shim has no idea which client spawned it.
- **This is unverified, not shipped.** Nothing in this repo currently builds or documents a
  ChatGPT-facing `mcp_config.json` snippet, and the shim's spawn/env assumptions have only ever been
  exercised under Claude Desktop's process model. Before telling a user "add this JSON to ChatGPT,"
  this needs an actual smoke test (does the shim's relative path resolution, cwd, and PATH lookup for
  `node`/`privacyfence-app` behave the same when ChatGPT spawns it?) — see §4.

### 2.2 Org mode (URL)

ChatGPT's **Developer Mode** (Settings → Apps → Advanced settings, Plus/Pro/Business/Enterprise/Edu
web accounts) is the "point at a URL" analog to Claude's org-mode connector: Settings → Connectors →
Create, remote HTTPS endpoint, Streamable HTTP or SSE, with the connector's auth set to OAuth
[[2]](#sources). The documented handshake is the standard MCP Authorization spec, and it lines up
directly with what PrivacyFence's authorization server already serves for Claude:

| ChatGPT expects [[3]](#sources) | PrivacyFence already serves it via |
|---|---|
| `/.well-known/oauth-protected-resource` discovery | `create_protected_resource_routes` (official MCP SDK), mounted in `web/routes_mcp.py` |
| AS metadata (`/.well-known/oauth-authorization-server`) listing `authorize`/`token`/`register` | `create_auth_routes` against `OrgOAuthProvider` |
| Dynamic Client Registration (RFC 7591) at `/register` | `OrgOAuthProvider.register_client`, persisted to `org_dir()/oauth_clients.json` |
| PKCE with `S256` in `code_challenge_methods_supported` | inherited from the official SDK's AS route implementation — same code path Claude's flow already exercises |

Because none of this is Claude-specific plumbing — it's the general MCP Authorization spec, which
PrivacyFence implemented once against the official SDK rather than hand-rolling anything
Claude-shaped — a ChatGPT Developer Mode connector pointed at `https://pf.example.com/mcp` should
complete the same DCR → `/authorize` → (bounce through PrivacyFence's own `/login` → Google →
back) → token exchange that `org-mode-setup-guide.md` §9 walks through for Claude, with no server
change. This is the highest-confidence ⚠️ in this document, but still unverified end-to-end.

Caveats worth setting expectations on, from current vendor documentation and community reports
[[1]](#sources)[[4]](#sources):

- A connector must be **re-enabled per chat** in Developer Mode — there's no persistent "always on"
  the way Claude Desktop's installed extension is.
- Community reports through 2026 describe custom connectors intermittently disappearing from the
  directory and OAuth completing without the connector actually appearing in chat — treat a first
  failed attempt as "known flaky," not necessarily a PrivacyFence-side problem, before deep-diving.
- ChatGPT layers its **own** consent/approval UI on top of whatever the connector does. Every write
  PrivacyFence already gates behind a human-review popup would then face two separate approval
  surfaces (ChatGPT's tool-call consent, then PrivacyFence's own dialog) — not a compatibility
  blocker, but a UX point worth calling out in any user-facing setup doc for this path.
- Business/Enterprise ChatGPT workspaces may gate "connect any external MCP server" behind a
  workspace-admin setting, independent of anything PrivacyFence controls.

### 2.3 Getting listed, to avoid Developer Mode

Two separate things can remove "each user manually flips on Developer Mode and pastes a URL," and
they are not the same track — worth telling apart before picking one to pursue
[[9]](#sources)[[10]](#sources):

**A. Workspace-admin-published connector (Business/Enterprise/Edu) — the realistic, near-term
path.** On these plans a *workspace admin* (not OpenAI) turns on "Developer Mode / Create custom MCP
connectors" once, in Workspace Settings → Permissions & Roles; adds PrivacyFence's `/mcp` URL there
(endpoint, OAuth, an automated "Scan Tools" pass over the server's tool list); tests it as a draft;
and **publishes it workspace-wide** — after that, ordinary members see and use it with no Developer
Mode toggle of their own. This is exactly org mode's actual deployment shape: the same IT admin who
already runs `org-mode-setup-guide.md` end to end for Claude would run this once, for the whole org,
for ChatGPT too. Nothing about the auth/transport requirements differs from §2.2's OAuth table — it's
the identical OAuth 2.1 + PKCE + DCR handshake, just performed once by an admin instead of once per
user. (Gemini Enterprise's own connector setup, §3.2, is already admin-only in this same shape —
though it's separately blocked there by the missing DCR support.) Two things worth planning around
before recommending this to a user:

- **The "Scan Tools" pass** almost certainly checks the same things §2.2's OAuth table and PrivacyFence's
  own tool annotations already cover — `web/mcp_tools.py` sets `readOnlyHint`/`destructiveHint`/
  `idempotentHint` on every tool, exactly the kind of metadata a safety scan over a tool list would
  look for — but this hasn't been run against a real workspace, so "should pass" stays a ⚠️ like
  everything else in this document until it's actually tried.
- **Published connectors are a frozen snapshot.** ChatGPT freezes the tool list an admin approved at
  publish time; PrivacyFence's tool set changes dynamically as connectors are enabled or disabled
  (`web/routes_mcp.py`'s module docstring: "the tool set depends on which connectors are currently
  built"). Turning on a new connector for the org (say, Confluence) would silently do nothing for its
  ChatGPT users until the workspace admin re-opens and re-publishes the connector. Any setup doc for
  this path needs to say so explicitly — carried into §4 as a follow-up item.

**B. OpenAI's public App Directory (Apps SDK submission) — heavier, and likely the wrong fit.** This
is OpenAI's own reviewed, publicly searchable catalog (Booking.com, Canva, Coursera, Expedia, Figma,
Spotify, and Zillow were its December-2025 launch cohort). Getting listed there needs, on top of the
same OAuth 2.1 + PKCE + DCR + Protected-Resource-Metadata stack §2.2 already covers: verified
developer/business identity, a submitted privacy policy, a support contact, a documented data
retention policy and scope map, written evidence (screenshots or traces) that every write action gets
explicit user confirmation, passing OpenAI's own test suite on both ChatGPT web and mobile, and a
manual OpenAI review before anything goes live [[10]](#sources)[[11]](#sources). Two things make this
track a poor match for PrivacyFence specifically, not just "more process":

- **PrivacyFence would clear the substantive bar easily** — "evidence every write action requires
  explicit user confirmation" is close to a description of its entire approval-dialog model — but the
  App Directory is built for a single vendor's own public-facing product (Canva *is* Canva's app), not
  a self-hosted governance layer standing in front of one company's *own* Gmail/Drive/Slack/
  Salesforce/Jira tenant. There's no single org to "own" a public PrivacyFence listing the way Canva
  owns its own.
- **The App Directory currently excludes the EEA, Switzerland, and the UK** at launch (EU
  availability is "planned," not live) [[12]](#sources) — precisely the regulatory audience
  `README.md`'s "Who is PrivacyFence for?" and `security-and-compliance.md` are written around. A
  public listing would be unusable for exactly the GDPR/EU-AI-Act-motivated deployments PrivacyFence
  is positioned for, at least until that changes.

**Recommendation:** pursue (A), not (B). Track A needs no server-side change and directly matches org
mode's actual admin-led deployment model; track B's cost (business verification, a public listing no
individual customer org has reason to want to own) and current EU exclusion make it a poor use of
effort here, independent of how much smoke-testing it would still need.

---

## 3. Gemini

Unlike Claude and ChatGPT — one company, roughly one client shape per mode — Google splits this
across three separate products with three different MCP postures. Treat them separately; don't
generalize "Gemini supports MCP" across all three.

### 3.1 Gemini CLI (developer tool — the real analog to Claude Code)

Gemini CLI is the closest match to Claude Code's own already-shipped local-mode path, and — unlike
ChatGPT — needs no shim discussion at all for local mode:

- Config lives in `~/.gemini/settings.json` (user-scoped) or `.gemini/settings.json`
  (project-scoped), under an `mcpServers` map where each entry picks its transport via exactly one of
  `command`, `url`, or `httpUrl` [[5]](#sources).
- **Local mode**: since Gemini CLI has *native* Streamable HTTP support (`httpUrl` + a `headers` map),
  it can hit `http://localhost:8765/mcp` directly with `"Authorization": "Bearer $(cat
  ~/.privacyfence/mcp_token)"`, exactly the same shape as Claude Code's `claude mcp add --transport
  http ... --header "Authorization: Bearer ..."`. It has no need for the mcpb shim — that exists only
  because Claude *Desktop* lacks a native way to reach an HTTP MCP endpoint (`README.md`'s own
  architecture note); Gemini CLI was never in that position to begin with.
- **Org mode**: Gemini CLI's remote-MCP OAuth support goes further than a static bearer header — it
  detects a `401`, discovers the AS/PRM metadata, performs Dynamic Client Registration if the server
  offers it, and drives the browser OAuth flow itself, all without OAuth config in `settings.json`
  [[6]](#sources). That is, feature-for-feature, the same discovery → DCR → browser flow
  `org-mode-setup-guide.md` §9 documents for Claude. Pointing a `settings.json` `httpUrl` entry (no
  `headers`) at `https://pf.example.com/mcp` should work unmodified, by the same reasoning as
  ChatGPT's Developer Mode in §2.2 — this is spec-generic authorization-server code, not
  Claude-specific.

### 3.2 Gemini Enterprise (the real analog to org mode's audience)

Gemini Enterprise (Google's business/admin-console product, not the consumer Gemini app) is the
closer match to who actually stands up PrivacyFence's org mode — an IT admin wiring a governed tool
into a company-wide assistant — so it matters more here than the consumer surfaces in §3.3, and it's
the one real gap found in this survey:

- Gemini Enterprise's custom MCP server connector supports **neither Dynamic Client Registration nor
  OAuth discovery** — unlike Claude, ChatGPT, and Gemini CLI, it can't self-register on first
  connect. Instead, an admin creates an OAuth client by hand and pastes its `client_id`/`client_secret`
  into the Google Cloud console; the redirect URI is *fixed by Google*
  (`https://vertexaisearch.cloud.google.com/oauth-redirect`) and can't be changed [[7]](#sources).
- PrivacyFence's `OrgOAuthProvider` persists registered clients to `org_dir()/oauth_clients.json`
  (`web/oauth_provider.py`), but the only way a client currently lands in that file is by completing
  the DCR flow itself (`register_client`, called from the SDK's `/register` route). There is **no
  admin-facing tool today to hand-seed a client** (fixed `client_id`/`client_secret`/`redirect_uris`)
  the way Gemini Enterprise's flow requires.
- This is the one item in this document that is a genuine PrivacyFence-side gap rather than "should
  already work" — see §4 for what closing it would take. Note this claim about Gemini Enterprise's
  requirements is sourced from a third-party integration guide, not fetched directly from Google's
  own documentation (blocked by this session's network egress policy) — reconfirm against
  `docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/` directly before
  building anything against it.

### 3.3 Gemini app / Spark (consumer, low priority)

The consumer Gemini app's "Spark" custom-app connections let an end user (not an org admin) add a
custom MCP app, OAuth-only, requiring the MCP server sit behind a **publicly-trusted CA-signed TLS
certificate** — self-signed certs are explicitly rejected — over Streamable HTTP only
[[8]](#sources). PrivacyFence's org-mode deployment story already satisfies the TLS requirement
as documented (Caddy + Let's Encrypt in front, per `org-mode-setup-guide.md` §6), but:

- This is a **per-user, consumer-app** feature (Spark), not an enterprise governance surface — it's a
  weaker match to PrivacyFence's actual audience (`README.md`'s "Who is PrivacyFence for?") than
  Gemini Enterprise in §3.2.
- Whether Spark supports DCR (self-registration, like ChatGPT/Gemini CLI/Claude) or requires
  pre-registration (like Gemini Enterprise) wasn't established in this pass — flagged as open in §4
  rather than guessed at.
- Given the low audience fit, this is worth revisiting only after §3.2 (Gemini Enterprise) and §2.2
  (ChatGPT) are actually verified end-to-end.

---

## 4. Gaps and follow-up work

In priority order:

1. **Live smoke tests, not just spec-reading.** Every ⚠️ above is "should work because the protocol
   matches" — none of it has been run against a real ChatGPT or Gemini account from this session (no
   such accounts, and no macOS host, were available here). The highest-value next step is one manual
   test per ⚠️ row: ChatGPT Developer Mode against a real org-mode deployment, and a Gemini CLI
   `httpUrl` entry against both local mode and org mode. Log results the way
   `docs/connector-qa-testing.md` and `docs/manual-pre-release-test-plan.md` already log connector QA.
2. **A non-DCR client-registration path**, for Gemini Enterprise (§3.2) and any other client that
   turns out to require pre-registration rather than self-registration. Smallest version: a
   `scripts/` admin command that writes one entry straight into `org_dir()/oauth_clients.json` in the
   shape `OrgOAuthProvider.register_client` would have produced — `client_id`, `client_secret` (or its
   hash, matching however DCR-issued secrets are stored today), and a fixed `redirect_uris` list —
   so an admin can hand Gemini Enterprise's console a `client_id`/`client_secret` pair without going
   through `/register` at all. Confirm Google's own doc for the exact redirect URI and any additional
   required client metadata before implementing.
3. **A documented ChatGPT-desktop / Gemini-CLI local-mode config snippet**, once smoke-tested per
   item 1 — most likely as a new subsection here or in `TECHNICAL_REFERENCE.md`'s installation
   section, alongside the existing Claude Desktop/Claude Code options, rather than a new doc per
   client.
4. **Verify the ChatGPT workspace-admin-published-connector path (§2.3, track A)** against a real
   Business/Enterprise/Edu workspace: confirm the "Scan Tools" pass accepts PrivacyFence's tool list
   and annotations as-is, and confirm the frozen-snapshot behavior actually works the way vendor docs
   describe. If it does, write it up as a companion to `org-mode-setup-guide.md` §9's Claude
   instructions — including an explicit callout that enabling a new connector org-wide means asking
   the ChatGPT workspace admin to re-publish, since PrivacyFence's tool list changes dynamically and
   ChatGPT's copy of it does not.
5. **Do not pursue OpenAI's public App Directory (§2.3, track B)** absent a concrete reason to
   revisit — it's a worse fit for this product than the workspace-admin path, and is currently unusable
   for PrivacyFence's own EEA/UK/Switzerland-heavy target audience regardless. Revisit only if OpenAI
   lifts the EU exclusion *and* someone identifies a customer need the workspace-admin path doesn't
   already cover.
6. **Re-verify this whole document's ⚠️/❌ calls before relying on them** — every vendor-behavior claim
   here is dated 2026-09 and several of the products involved (ChatGPT Developer Mode and its App
   Directory, Gemini Enterprise's custom connectors, Gemini Spark) are recent and still changing;
   don't copy a row out of this table into user-facing docs without re-checking it's still current.

---

## 5. Verification log

**Verified against this repository's code** (not vendor claims):

- `/mcp`'s transport and auth are the official MCP Python SDK's generic Streamable HTTP session
  manager and OAuth 2.1 authorization-server routes, not anything Claude-specific —
  `web/routes_mcp.py`, `web/oauth_provider.py`.
- The `.mcpb` shim (`mcpb/shim/src/index.ts`) is a generic stdio↔Streamable-HTTP proxy with no
  Claude-specific protocol knowledge; `.mcpb` itself is a Claude-Desktop-only *packaging* format
  around that same generic binary.
- `OrgOAuthProvider.register_client` (`web/oauth_provider.py`) is the only code path that currently
  adds an entry to `org_dir()/oauth_clients.json` — there is no separate manual-registration path
  today (relevant to the Gemini Enterprise gap in §3.2/§4).
- Local mode's Claude Code path (`claude mcp add --transport http ... --header "Authorization:
  Bearer ..."`, `TECHNICAL_REFERENCE.md` §"Option B") is the shape §3.1 claims Gemini CLI's `httpUrl`
  + `headers` config matches.

**Sourced from vendor documentation, not verified live** (session had no ChatGPT/Gemini account or
macOS host available):

<a id="sources"></a>

1. ChatGPT desktop MCP config (`mcp_config.json`, stdio vs. Streamable HTTP) — community/aggregator
   documentation of the desktop app's Settings → MCP servers flow, added early 2026; no single
   canonical OpenAI doc page was found for the desktop-specific config file path.
2. [OpenAI Help Center — Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
3. [OpenAI Developers — Building MCP servers for plugins and API integrations](https://developers.openai.com/api/docs/mcp)
4. Community reports of custom-connector flakiness (OAuth completing without the connector appearing
   in chat) — aggregated from third-party guides current as of 2026-09; not an official OpenAI
   status statement.
5. [`google-gemini/gemini-cli` — MCP servers with the Gemini CLI](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md)
6. Gemini CLI's automatic OAuth discovery / DCR behavior for remote MCP servers — per the same
   `google-gemini/gemini-cli` MCP docs [[5]](#sources) and third-party write-ups of the discovery-on-401
   flow current as of 2026-09.
7. Gemini Enterprise custom MCP connector requiring a pre-registered OAuth client with a
   Google-fixed redirect URI (`https://vertexaisearch.cloud.google.com/oauth-redirect`) — from a
   third-party integration guide (SecureAuth's Agent Authority docs); Google's own doc at
   `docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/` was not directly
   reachable from this session (network egress policy) and should be checked directly before this is
   treated as settled.
8. Gemini app "Spark" custom-app MCP requirements (OAuth-only, publicly-trusted CA-signed TLS
   required, Streamable HTTP only) — from third-party coverage of the Spark custom-connector feature
   current as of 2026-09; not fetched from a Google-owned source in this session.
9. Workspace-admin-published custom MCP connectors (Business/Enterprise/Edu) — "Developer Mode /
   Create custom MCP connectors" as a Workspace Settings → Permissions & Roles admin toggle, the
   endpoint/OAuth/"Scan Tools"/draft/publish flow, the "frozen snapshot until an admin re-publishes"
   behavior, and per-app RBAC — aggregated from OpenAI Help Center article summaries ("Admin
   controls, security, and compliance in apps (Enterprise, Edu, and Business)",
   `help.openai.com/en/articles/11509118-...`) and third-party guides; the source pages themselves
   were not directly reachable from this session (network egress policy), so reconfirm directly
   before depending on the exact settings path or terminology.
10. App-submission requirements for OpenAI's public App Directory (business/developer verification,
    privacy policy, support contact, data-retention policy, scope map, written evidence of
    confirmation for write actions, cross-platform test cases, manual review) — from OpenAI's own
    "App submission guidelines" page (`developers.openai.com/apps-sdk/app-submission-guidelines`) and
    "Submitting apps to the ChatGPT app directory" help article, both blocked by this session's
    network egress policy and read only via third-party summaries and search-result snippets; treat
    the specific checklist items as directionally right, not a verbatim quote of OpenAI's own text.
11. [OpenAI — Developers can now submit apps to ChatGPT](https://openai.com/index/developers-can-now-submit-apps-to-chatgpt/)
    (announcement; also blocked from direct fetch in this session, read via search-result summary).
12. App Directory's launch-time exclusion of the EEA, Switzerland, and the UK, and its December-2025
    launch cohort (Booking.com, Canva, Coursera, Expedia, Figma, Spotify, Zillow) — from third-party
    reporting on the Apps SDK/App Directory launch current as of 2026-09; reconfirm against OpenAI's
    own availability documentation before treating the EU exclusion as still current, since this is
    exactly the kind of staged rollout detail that changes without much notice.
