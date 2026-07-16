# Security-review UI redesign — feasibility & design plan

This document evaluates a redesign concept for PrivacyFence's approval dialogs — reframing them
from "permission popups" (Allow/Deny) into a security-review UI that borrows from code review /
PR patterns (WHAT → WHY → RISK → PREVIEW → decision) — and turns it into a plan that's honest
about what PrivacyFence's architecture can and cannot actually back up.

The concept itself (not reproduced here) proposed: leading with the AI's stated intent, a large
document-reader-style preview, automatic sensitivity badges, an "AI visibility" checklist of
exactly what fields the AI receives, an "Inspect before approve" mode, per-file selection
rationale, richer time-limited approval language, and a long-term vision of PR-style multi-person
governance (Purpose / Files / Risk Analysis / Preview / Comments / Approve).

## Phase 0 — privacy-filter enforcement (implemented, prerequisite for Phase 1a)

Discovered while starting implementation of Phase 1a's "AI will receive" visibility checklist:
`settings.yaml.example`'s `privacy`/`drive_privacy`/`slack_privacy` sections (per-category
allow/redact/block, described in this doc's §3 feasibility table and in
`docs/security-and-compliance.md` §5 as an active enforcement mechanism) were **never actually
read by any code** — editing a category from `allow` to `block` changed nothing. Building the
visibility checklist on top of that would have meant displaying a policy PrivacyFence couldn't
back up, exactly the failure mode this whole redesign exists to avoid.

Per the maintainer's decision, this was implemented as a prerequisite rather than worked around:

- `src/privacyfence/privacy_filter.py` (new): `init_privacy_filter()`/`apply_text()`/
  `apply_list()`/`category_policy()`, mirroring `pii_detector.py`'s module-global init pattern.
  allow passes through; block replaces text with a fixed marker or empties a list; redact reveals
  a text value's length only, and (documented explicitly, since "partial" has no single correct
  shape for a list of structured records) behaves identically to block for list categories. 18
  unit tests in `tests/unit/test_privacy_filter.py` — this module has no AppKit dependency, so
  these actually ran and passed, unlike the rest of this codebase's test suite in this environment.
- Wired into `daemon_main.py`'s startup sequence, and applied to every category documented in
  `settings.yaml.example`, across the three connectors that had a schema for it: Gmail
  (`_get_message`, `_get_thread` — `body`/`thread_history`/`metadata`/`attachments`), Drive
  (`_get_file_content`, `_get_file_metadata`, `_list_files`, `_list_folder`,
  `_sheets_get_values` — `file_content`/`file_metadata`/`file_list`/`folder_structure`), Slack
  (`_get_channel_history`, `_get_thread_replies`, `_search_messages`, `_list_channels` —
  `message_content`/`thread_content`/`user_identity`/`channel_list`). Integration tests added
  per connector, following each file's existing `gated_call_spy` pattern.
- **Deliberately left out of scope**: `gmail_list_message_attachments`, a dedicated auto-approved
  tool whose entire purpose is exposing attachment metadata — applying the `attachments: block`
  default there would make the tool permanently return nothing, a much bigger behavior change
  than "make the documented config real." Flagged for the maintainer rather than decided
  unilaterally.
- `docs/security-and-compliance.md` §5 corrected: it claimed *every* connector had a privacy
  filter; only Gmail/Drive/Slack do (the only three with a documented category schema). Reworded
  to say so precisely rather than leave a now-partially-true claim in a document written for
  auditors.
- **Verification honesty note**: this repo's real test suite requires macOS (PyObjC/AppKit) and
  cannot run in this Linux environment — confirmed by attempting `pip install -e ".[dev]"`, which
  fails at `sw_vers`. `gate.py` (and therefore every connector) imports AppKit transitively, so
  only `privacy_filter.py`'s own isolated unit tests could actually be executed and confirmed
  green here; the per-connector integration tests and the connector edits themselves are
  carefully pattern-matched against each file's existing conventions and cross-checked against
  every existing assertion they run alongside, but not executed. They need a real `pytest` run on
  macOS before this is considered verified, not just written.

With this in place, Phase 1a's visibility checklist (below) can now honestly render
`privacy_filter.category_policy(group, category)` per category instead of a fictional config
value — that implementation is still open, tracked in §7 Phase 1a.

## Revalidation note — main has moved since this plan was written

This branch was rebased onto current `main` (merge commit bringing in PRs #42–#50, including
`457d020` salary/compensation PII patterns, `f24a9f1` connector-scoped auto-accept grants, and the
unattended/scheduled-Cowork-task work). Nothing in that merge invalidates the plan below, but three
things changed enough to update it in place rather than leave stale:

1. **The PII detector already grew a salary/compensation category** (HU/DE/EN patterns,
   `pii_detector.py`) — this was Phase 2's headline example. Phase 2's remaining scope narrows to
   *other* financial signals (currency amounts, budget/revenue keywords) this category doesn't
   cover. See the updated feasibility table and §7 Phase 2.
2. **A new unattended/scheduled-session mode exists** (`gate.py`'s `unattended_scope`/
   `is_unattended`, three new bridge meta-tools, new `denied_unattended`/`policy_check`/
   `unattended_session_started`/`unattended_session_ended` audit decision types — see
   `docs/TECHNICAL_REFERENCE.md`'s "Scheduled / unattended Cowork tasks" section). This doesn't
   change any conclusion here — it fails closed and stays entirely local, so §8's resolution
   stands — but it strengthens the case for Phase 1b's mandatory `reason`: for calls denied
   unattended, there is *never* a popup, so a captured `reason` is the only human-legible record
   of what Claude was attempting. Folded into §7 Phase 1b and §9 below.
3. **`approval_window.py` was refactored** to split pure view construction (`build_panel()`) from
   activation (`runApproval_()`), specifically so the view hierarchy can be asserted on in tests
   without a real interactive session (`tests/unit/test_approval_window.py`, new). This lowers
   Phase 1a's risk: layout changes can follow the same tested pattern rather than needing manual
   click-through verification for every change. Nothing in Phase 3's WKWebView plan is affected —
   none of the merged PRs touch `entitlements.plist`, `PrivacyFenceApp.spec`, `build_dmg.sh`, or
   build dependencies, so the §7 Phase 3 / §10 Q3 signing investigation still holds as written.

Line-number references throughout this doc have been refreshed against the merged tree. See §11
for the implementation-readiness verdict.

## 1. Verdict

**Feasible, and directionally right — with one central correction.** The concept's instinct
(make the data the focus, not the buttons; borrow PR-review discipline) fits what PrivacyFence
already is: a synchronous, blocking, human-in-the-loop gate (`gate.py`) that already refuses to
release data without a decision. Several of the proposed features are near-free because
PrivacyFence already computes the underlying data — the popup just isn't showing it. Others
require the UI to claim things PrivacyFence cannot actually verify, and one (multi-person PR-style
governance) contradicts the "no server, fully local" architecture that is currently a headline
trust claim (`docs/security-and-compliance.md` §2, §8). Section 3 below is a feature-by-feature
feasibility matrix; section 8 is the one strategic tension worth resolving before building
anything.

## 2. What PrivacyFence already has (grounding facts)

- **Architecture**: `Claude → stdio MCP bridge (bridge_main.py) → local Unix socket →
  daemon (gate.py + connectors) → external API`. The bridge is a thin FastMCP relay
  (`bridge_main.py`'s `_build_tool_fn`, around line 199-232) — for connector tools it forwards
  `(connector, tool, kwargs)` and nothing else. There is no PrivacyFence-operated server
  (`security-and-compliance.md` §2). Three additional **bridge meta-tools** now exist
  (`_register_meta_tools`, `bridge_main.py:267-328`) — `privacyfence_check_policy`,
  `privacyfence_begin_unattended_session`, `privacyfence_end_unattended_session` — not backed by a
  connector, for planning around scheduled/unattended runs (see below and
  `docs/TECHNICAL_REFERENCE.md`'s "Scheduled / unattended Cowork tasks" section).
- **The gate is synchronous and blocking** (`gate.py`'s `gated_call`, starting line 127): a tool
  call resolves inside one call — auto-accept check → native popup → audit write — before Claude
  ever gets data back. This is exactly the "review, not notify" model the concept wants; it
  doesn't need to be invented, only surfaced better. New since this plan's first draft: a
  connection can be marked **unattended** (`gate.py`'s `unattended_scope`/`is_unattended`,
  scheduled/triggered Cowork runs only, off by default, admin opt-in via
  `unattended_sessions.enabled`) — while marked, any call a rule doesn't already cover is denied
  immediately (`_deny_unattended`, `gate.py:301`) rather than opening a popup nobody will answer.
  This doesn't touch anything a human actually reviews interactively, so it doesn't change any
  conclusion in this plan, but see the Revalidation note above for how it strengthens Phase 1b.
- **Current popup** (`approval_window.py`) is a native AppKit `NSPanel`, fixed 620pt wide, with a
  plain-text `NSTextView` for the body. It already does: a title, a key/value "summary box"
  (`preview` dict), a scrollable details pane, PII-category tinting/banner, and up to three
  buttons (Deny / Accept / Accept All or Accept-for-5-min). This is a real foundation, not a
  blank slate — but it has no rich layout, no images/PDF rendering, no progressive disclosure.
  Its construction is now split into `build_panel()` (pure view-hierarchy construction, line 295)
  and `runApproval_()` (activation/modal-block, line 427) specifically so tests can assert on the
  resulting view hierarchy without a real interactive session (`tests/unit/test_approval_window.py`)
  — new test infrastructure this plan's Phase 1a/3 work should extend, not work around.
- **Data minimization is already enforced upstream of the popup**: `gate.py`'s docstring states
  `filtered_data` vs `raw_data` are computed per connector and the popup's `preview` dict is
  metadata-only by convention (`coding-and-testing-guidelines.md` §1.5). `settings.yaml.example`
  shows the actual mechanism: per-category `allow`/`redact`/`block` policy
  (`privacy.categories`, `drive_privacy.categories`, `slack_privacy.categories`).
- **PII detection** (`pii_detector.py`) is a local regex heuristic (IBAN, credit card w/ Luhn,
  national IDs, HU/DE/EN personal-data phrases) that runs on `review`-gate content only, returns
  category labels (never matched text), and forces a second confirmation when it fires.
- **Time-limited approval already exists**: `gate.py`'s `TEMP_ACCEPT_ELIGIBLE_OPERATIONS` /
  `register_temp_accept` implements exactly "Accept for 5 minutes," in-memory only, per
  `(operation, file)` key.
- **Auto-accept rules** (`auto_accept.py`) already implement "Always allow this workflow":
  `suggest_rule()` proposes a rule from the item's own attributes (sender domain, folder, "I am
  the organizer"), and `show_rule_confirmation_popup` requires an explicit second confirmation
  before persisting it. New since this plan's first draft: **auto-accept grants**
  (`resource_grants.py`, `resource_names.py`) let a specific resource (a Drive folder, Tasks list,
  Slack channel, ...) be trusted once, with a small set of capability booleans, instead of
  repeating the same ID across every operation key — they compile down to the same rule shape
  `auto_accept_rules` always used, so `AutoAcceptEvaluator` has no separate code path. Relevant to
  this plan: resource **names are already resolved and cached** (`resource_name_cache.json`,
  falling back to "(resolving…)"/"(connect X to see its name)") for display in the menu bar's
  grant-management UI — the same resolved-name machinery the "Resources requested" section (§6)
  and request-fingerprint feature (§7 Phase 2) should reuse rather than re-deriving display names
  from raw IDs.
- **Audit log** (`audit_log.py`) records every decision with connector/tool/summary/sender/
  decision/latency/pii_detected, one JSONL line per event, weekly Excel export. `AuditEntry.decision`
  now has four more values beyond `approved`/`rejected`/`auto_accepted`/`accepted_via_accept_all`/
  `error`: `accepted_via_temp_session`, `denied_unattended`, `policy_check`,
  `unattended_session_started`/`_ended` — all relevant surface area for where a mandatory `reason`
  (§4, §7 Phase 1b) should also be recorded.
- **What is *not* present anywhere in the codebase**: any field carrying the AI's natural-language
  reasoning, the user's original prompt, or a cross-file "why I picked this" rationale. Confirmed
  by grep — `reasoning`/`rationale`/`intent`/`purpose`/`prompt` appear only in client/library
  method names (`slack_client.py`, `gmail_client.py`, OAuth "purpose" strings), never in the gate,
  connector, or bridge layer. One near-miss worth flagging: `privacyfence_check_policy`'s response
  schema (new bridge meta-tool) has its own `"reason": "<str>"` field — that's PrivacyFence
  *explaining its verdict* to Claude (e.g. "matched rule X"), the opposite direction from what
  this plan's `reason` param would carry (Claude explaining itself to PrivacyFence). Naming this
  plan's field `claude_reason` throughout (already done in §9) avoids the collision.

## 3. Feature-by-feature feasibility

| Concept feature | Feasibility | Why |
|---|---|---|
| Data-over-buttons visual hierarchy (WHAT→WHY→RISK→PREVIEW→decision) | **Feasible now** | Pure layout change to `approval_window.py`. No new data needed for WHAT/PREVIEW; RISK partially needs new signals (§below). |
| "Claude wants to answer: '...'" stated purpose | **Derivable via a new mandatory tool parameter — presence guaranteed, truthfulness is not** | MCP tool calls today carry only `(tool name, args)` — see `bridge_main.py:221-234`. But `args` is extensible: adding a required `reason: str` param to every gated `ToolSpec` gives PrivacyFence a real, protocol-enforced channel for this. It must ship labeled as Claude's self-report, never as verified fact. See §4. |
| "Requested because Claude found a reference in board_minutes.docx" | **Same mechanism, same caveat** | A mandatory `reason` param can carry exactly this sentence — Claude is free to write it. What can't be added is any way to confirm it's true; treat it as reviewer context, not evidence. Best independently-verifiable substitute for cross-file rationale is factual session history (§4). |
| Requested-resources checklist | **Feasible now** | Already the `preview` dict's job; just needs a list-shaped rendering instead of key/value rows when a call touches multiple items. |
| Sensitivity badges (🟢 Internal / 🟠 Financial / 🔴 PII) | **Feasible, partially shipped already** | `pii_detector.py` already returns category labels, and now includes a dedicated "Salary/compensation information" category (HU/DE/EN) added since this plan's first draft. Remaining: (a) a broader "financial" category for amounts/budget/revenue that salary-specific patterns don't catch, (b) a badge computed for the *popup-gate* (write) path too, where PII scanning is deliberately absent today by design (`gate.py` docstring) — badges there should read from Claude's own drafted content, not treated as an "external PII" gate. See §7 Phase 2. |
| "🟢 Internal / 🟠 Confidential" **classification labels** | **Feasible only where the org has them — deferred, see §10 Q2** | Google Workspace Enterprise has native Drive data-classification labels, readable via `drive.labels.readonly` scope — PrivacyFence does not currently request this scope (`google-cloud-setup.md`). Real for orgs on that tier, but the maintainer has deferred it: narrow benefit relative to the new scope/consent surface. Sensitivity badges ship from local detectors only (Phase 2) until revisited. |
| Large (60–70%) preview / "document reader" | **Feasible for text and Google-native docs; not for arbitrary binary files** | `drive.py`'s `_get_file_content` (line 539) truncates extracted text to 2000 chars for `details_text`; genuinely binary content (real .docx, images) currently renders as a placeholder string (`"[binary content — N bytes; use drive_download_file to save it]"`), not a preview. True page-faithful preview needs new work per file type — see §7 Phase 3. |
| PDF preview specifically | **Feasible** | macOS `PDFKit` (`PDFView`) is a standard AppKit control; PrivacyFence already fetches `content_bytes` for binary Drive files (`drive.py:546`), just doesn't render them. |
| Arbitrary .docx/.xlsx page-faithful preview | **Not reliably feasible** | No first-party macOS text/layout extraction without Office's own QuickLook generator being installed, which isn't guaranteed on every Mac. Fall back to extracted-text preview (already computed) rather than promising a "first page" render for every format. |
| Gmail-style email layout | **Feasible now** | `gmail.py`'s `_get_message` (around line 250) already builds exactly this shape (From/To/Date/Subject preview + plain-text body) — this is a pure layout change, no new data. |
| "AI visibility" checklist (exactly which fields reach the AI) | **Feasible now, and the strongest feature in the concept** | This is *already computed*, not new: `privacy.categories` / `drive_privacy.categories` / `slack_privacy.categories` in `settings.yaml.example` decide `allow`/`redact`/`block` per category before the popup is ever built, and `gate.py`'s `filtered_data` is the literal payload Claude receives. Rendering this policy state as a checklist is surfacing ground truth, not inventing a promise. |
| "Inspect before approve" mode | **Feasible now** | Same modal session, an expand/collapse of the existing scrollable pane, or a resized `NSPanel`. No protocol change. |
| Split view (prompt vs. document) | **Not feasible as literally stated** | There is no "the AI's prompt" available to place side by side (same root cause as row 2). Could split PREVIEW vs. DETAILS instead, or PREVIEW vs. self-reported reasoning (labelled as such). |
| Reading-time estimate | **Feasible now** | Trivial function of `details_text` length; no new data. |
| Request fingerprint / "seen before" | **Feasible now** | `audit_log.py` already has every prior decision keyed by connector/tool/summary/sender. A stable hash of `(operation_key, preview)` plus a lookup against recent audit entries gives "you approved this exact request 3 times this week" for free. |
| "Allow once / Allow for 5 min / Always ask" language | **Feasible now — mostly a naming fix** | Maps directly onto existing Accept / Accept-for-5-min / (absence of Accept All) — see §2. |
| Default focus on "Inspect," not "Allow" | **Feasible now** | One line in `approval_window.py`'s button setup / first-responder assignment. |
| Risk explanation ("this document contains salary information, confirm Claude really needs it") | **Feasible, needs new detector categories** | Extends `pii_detector.py`'s pattern set (it's designed for exactly this — see its module docstring) rather than a new subsystem. |
| PR-style framing (Purpose / Files / Risk / Preview / Comments / Approve), single reviewer | **Feasible as an interaction metaphor** | Nothing about visual structure or button-order requires new infrastructure — it's the existing gate wearing a different layout. |
| PR-style **multi-person** governance (a real second reviewer, comments visible to someone else, delegated/IT approval) | **Conflicts with current architecture** | Requires a channel for a second human to see and act on a request — i.e., some server or shared store. PrivacyFence's current trust story is explicitly "no PrivacyFence-operated infrastructure, no server in the request path" (`security-and-compliance.md` §2, §8, FAQ). Building this either breaks that claim or has to be pitched as a deliberately separate, opt-in product surface. See §8. |

## 4. The central correction: mandatory closes the presence gap, not the trust gap

The concept's mental-model shift — "do I allow this?" → "do I understand what the AI is about to
see?" — is the right instinct, but "Claude wants to answer: ..." implies PrivacyFence can read
Claude's intent directly. It can't, structurally: the MCP bridge sees a function call, not a
conversation (`bridge_main.py`'s `_build_tool_fn`). The fix isn't to give up on a stated-purpose
field — it's to be precise about what adding one to the protocol does and doesn't buy.

**It is fully feasible to make this mandatory, not optional.** `ToolParam.required` already
exists (`connector.py`), and MCP tool-call schemas enforce required fields at the protocol level:
a call missing a required argument doesn't reach PrivacyFence at all, the same way Claude can't
today omit `message_id` from `gmail_get_message`. Adding a required `reason: str` parameter to
every gated tool's `ToolSpec`, with a tool description instructing Claude to explain why it's
calling the tool, guarantees the field is never empty. This is a real upgrade over an optional
field, which would predictably be omitted on the calls where a reviewer most wants it.

**What mandatory does not do is make the content trustworthy.** Two separate properties are easy
to conflate:

- *Presence* — is a reason string always there? A required schema field settles this
  completely, deterministically, at the protocol level.
- *Fidelity* — does that string reflect why Claude actually issued the call? Nothing about a
  required field touches this. Claude generates the reason text concurrently with deciding to
  make the call; there is no mechanism — introspective or architectural — to check it against the
  model's actual reasoning process, let alone against the user's real prompt, which PrivacyFence
  never sees either way. The realistic failure mode isn't an empty field, it's a low-information
  boilerplate one ("needed to complete the user's request" on every call), which defeats the
  "slow thinking" goal about as effectively as no field at all. And in the specific scenario this
  gate exists to catch — a request shaped by injected/manipulated content rather than genuine user
  intent — a plausible, well-formed fabricated reason is exactly the failure mode to expect, not
  an edge case that mandatory field would flush out.

So: ship it mandatory (Phase 1, not Phase 2 — see §7), because presence is worth having
unconditionally. But render it as a distinct, clearly-labeled **"Claude says (unverified)"**
block, never merged into or styled like the verified WHAT/AI-VISIBILITY sections. Its actual value
to a reviewer is as a *cross-check* — "the stated reason doesn't match the file being requested"
is a real, catchable signal — not as the evidentiary basis for approval. The dialog's strongest,
most defensible headline stays **"here's exactly what Claude will receive if you approve"** (the
AI-visibility checklist from `filtered_data`, which PrivacyFence can actually prove), promoted to
the dialog's second-most-prominent section, right under WHAT. "Claude says" sits below it, visibly
a different kind of information.

## 5. Refined design principles

1. **Lead with what's provable, not what's inferred.** WHAT (resources) and AI VISIBILITY
   (exact fields Claude receives) are ground truth PrivacyFence already computes — put them
   first. Self-reported WHY (if the `reason` parameter is adopted) goes in its own visually
   distinct "Claude says" block, never merged with verified fields.
2. **RISK is a function of local detectors, stated as such.** Sensitivity badges come from
   `pii_detector.py` category matches and (where available) real Drive classification labels —
   not from guessing intent. No badge should claim something PrivacyFence didn't actually scan
   for.
3. **PREVIEW shows what can honestly be shown per file type**, not a uniform "document reader"
   promise: full text for text-convertible content, native PDF rendering for real PDFs,
   Gmail-style structured layout for email, and a plain "binary content, N bytes, use download
   tool to inspect the actual file" fallback — never a fabricated first-page preview.
4. **Decision affordances stay last, exactly as proposed** — buttons pinned below the reviewed
   content, "Inspect" (not "Allow") as default focus, existing Accept/Accept-for-5-min/Accept-All
   relabeled per the concept's clearer language.
5. **Keep it local and synchronous.** Every enhancement in this plan renders from data the daemon
   already has in-process, inside the same blocking modal session `gate.py` already runs. Nothing
   here should require the bridge, the daemon, or the popup to phone out anywhere.

## 6. Refined layout (per surface)

**Generic document request**
```
PrivacyFence · Read Request
──────────────────────────────────────────────
Resources requested                 [checklist, existing preview dict]
  ☑ Quarterly M&A Strategy.docx  (Owner: John Smith · 2.3 MB · Modified yesterday)
──────────────────────────────────────────────
AI will receive                     [from privacy.categories / drive_privacy.categories]
  ✓ File name & metadata   ✓ Document text
  ✗ Sharing/permissions history   ✗ Revision history
──────────────────────────────────────────────
Sensitivity                          [pii_detector.py + financial-keyword extension]
  🟠 Contains financial figures   🔴 Possible personal data: IBAN
──────────────────────────────────────────────
Claude says (unverified)            [mandatory `reason` param, always present — see §4]
  "Needed to summarize Q3 pricing changes."
──────────────────────────────────────────────
Preview                              [text / PDFKit / fallback per §5.3]
  ...
──────────────────────────────────────────────
[ Deny ]                    [ Accept for 5 min ]  [ Accept ]
```

**Email (Gmail-style)** — mostly what `gmail.py` already computes, just laid out as a message:
```
From: Legal Department        To: John Doe
Subject: Updated acquisition terms         Received: Today
AI will receive: ✓ Subject ✓ Sender ✓ Body   ✗ Prior thread ✗ Hidden recipients
Sensitivity: 🟠 Financial terms
──────────────────────────────────────────────
[body, styled like a real email]
──────────────────────────────────────────────
[ Deny ]                                    [ Accept ]
```

## 7. Phased roadmap

**Phase 1a — layout only, zero new data (lowest risk, ship first)**
- Restructure `approval_window.py`'s section order to WHAT → AI VISIBILITY → RISK (existing PII
  banner, relabeled) → PREVIEW → decision.
- Render `privacy.categories`/`drive_privacy.categories`/`slack_privacy.categories` state as the
  "AI will receive" checklist — the policy object already exists at the point `gated_call` is
  invoked; it just needs to be threaded into `preview` or a new `visibility` kwarg.
- Rename buttons/labels per the concept ("Allow once" / "Allow for 5 min" / relabel Accept All).
- Default keyboard focus to a non-destructive "Inspect/expand" affordance rather than Accept.
- Add reading-time estimate from `details_text` length.
- Gmail-specific layout for `gmail_get_message`/`gmail_get_thread`.

**Phase 1b — mandatory `reason` parameter, on every tool including auto-gated (largest-footprint
item in this whole plan — decided scope, see §10)**
The maintainer's decision (§10 Q1) is to capture `reason` universally, not just on tools a human
reviews, so it's available for later audit-log pattern analysis even where there's no popup to
show it in. That's a materially bigger change than a layout tweak or even a gated-tools-only
version would have been — it touches literally every tool declaration and every tool call site in
every connector, plus the audit schema:
- Add a required `reason: str` param to **every** `ToolSpec` in **every** connector
  (`connectors/gmail.py`, `drive.py`, `slack.py`, `calendar.py`, `salesforce.py`, `jira.py`,
  `confluence.py`, `telegram.py`, `tasks.py`, `contacts.py`) — gated and auto/`read_only` tools
  alike. Each tool's description should instruct Claude to state, in one sentence, why it's
  calling the tool right now.
- Gated tools (`gate="review"`/`"popup"`): thread `reason` through `gated_call(...)` as a new
  `claude_reason` kwarg so `gate.py`/`approval_popup.py` can render it in the popup as its own
  labeled block, distinct from `preview`/`details_text` — never merged with verified fields (§4).
- Auto-accepted / `read_only=True` tools: there is no popup to render it in — every connector
  currently has its own `_auto_audit(tool, tool_name, summary, sender, created_at)` helper (one
  copy per connector, same shape: `gmail.py:783`, `drive.py:1136`, `slack.py:296`,
  `calendar.py:656`, `salesforce.py:318`, `confluence.py:320`, `contacts.py:356`, `jira.py:378`,
  `telegram.py:216`, `tasks.py:304` — `tasks.py`'s and `telegram.py`'s already differ slightly in
  signature from the rest, worth normalizing while touching all ten anyway). Each needs a
  `reason: str` parameter, recorded straight to the audit log — not reviewed in real time, but
  available for later pattern analysis (e.g. spotting when Claude gives the same boilerplate
  reason across hundreds of auto-accepted calls).
- **The three bridge meta-tools are in scope too, not just connector `ToolSpec`s**:
  `privacyfence_check_policy`, `privacyfence_begin_unattended_session`, and
  `privacyfence_end_unattended_session` (`bridge_main.py:267-328`) are hand-registered outside the
  connector-manifest mechanism, but they already write their own audit entries (`policy_check`,
  `unattended_session_started`, `unattended_session_ended` — see `audit_log.py`'s `AuditEntry`
  docstring). `begin_unattended_session` in particular is worth a `reason` even more than most
  auto-accepted calls: it changes gate posture for the rest of the connection (every uncovered
  call fails fast from that point on), and it's exactly the kind of decision a later audit review
  would want explained ("why did Claude switch this connection into unattended mode").
- `audit_log.py`: add `claude_reason: str = ""` to `AuditEntry`, and pass it through `_audit()` in
  `gate.py:338` (currently `_audit(...)` doesn't take one — add it there too) so the gated, auto,
  and meta-tool paths all write to the same field.
- Test impact per `coding-and-testing-guidelines.md` §2.5/§2.6: `tests/helpers.py::build_stub_args`
  needs a default `reason` value for every tool's stub args (not just gated ones), and
  `assert_all_tools_leave_an_audit_trail` / the new-connector checklist gain a line item ("every
  tool — gated or auto — carries a `reason` param and records it in the audit entry").
- Trade-off accepted deliberately: this adds a small token/latency cost to high-frequency,
  low-risk calls (e.g. `list_messages`) that no human ever reviews, in exchange for uniform
  coverage in the audit log. Worth revisiting if that overhead turns out to matter in practice.

**Phase 2 — new local detectors, still zero external calls**
- Extend `pii_detector.py`'s pattern set with a broader "financial data" category (currency
  amounts near budget/revenue/invoice keywords), per its own documented extension model. Narrower
  in scope than originally planned: a dedicated "Salary/compensation information" category
  (HU/DE/EN) shipped since this plan's first draft — that part of Phase 2 is already done.
- Compute badges for the popup (write) gate from Claude's *own drafted content* — explicitly not
  routed through the PII-detection "is this external data" framing (`gate.py` docstring is clear
  that distinction is deliberate), but still worth flagging e.g. "this draft email contains what
  looks like a bank account number" before Claude sends it.
- Request fingerprinting from `audit_log.py` history ("approved 3 times this week").

**Phase 3 — real preview rendering (bigger UI investment)**
- Move `approval_window.py`'s body from a plain `NSTextView` to an embedded local `WKWebView`
  (PyObjC exposes WebKit) rendering a local HTML template — this is the practical way to get
  badges, a Gmail-style header, progressive disclosure (`<details>`), and a wider/taller window
  without hand-building each new layout in raw AppKit constraints. Keep it `file://`/`data:`-only,
  no network — this preserves the "no telemetry, no network calls out of the popup" property from
  `security-and-compliance.md`. Render via `loadHTMLString(_:baseURL:)` with a single
  self-contained string (inlined `<style>`, no separate resource files) rather than
  `loadFileURL(...)` — WKWebView's `file://` loading model is stricter than the legacy `WebView`
  class and this sidesteps it entirely, at no cost since the template is generated in-process
  anyway.
- Native `PDFView` (PDFKit) embed for genuinely binary PDF content already fetched as
  `content_bytes` in `drive_client.py`.
- Drive classification-label lookup (`drive.labels.readonly` scope): **deferred** per the
  maintainer's decision (§10 Q2) — narrow benefit (Workspace Enterprise-tier only) relative to the
  added consent-screen surface and scope request. Revisit if a specific org asks for it; until
  then the sensitivity section is driven entirely by the local PII/financial detectors (Phase 2),
  with no "Classification: Internal/Confidential" row at all rather than a fabricated one.
- **Signing/notarization investigation (§10 Q3) — resolved, no blocker found**: checked against
  the actual build config (`scripts/entitlements.plist`, `PrivacyFenceApp.spec`,
  `scripts/build_dmg.sh`, `.github/workflows/build.yml`):
  - The app is **not sandboxed** (`entitlements.plist` has no `com.apple.security.app-sandbox`
    key) — it's Developer-ID/DMG distribution, not Mac App Store, which removes most of the
    App-Sandbox-specific WKWebView restrictions that show up in generic guidance online.
  - Signing already uses Hardened Runtime (`codesign --options runtime`, `build_dmg.sh:117-120`).
    The one entitlement WKWebView needs under Hardened Runtime is
    `com.apple.security.cs.allow-jit`, so JavaScriptCore can JIT-compile JS — a standard,
    Apple-documented entitlement (same one Safari/Xcode/every Electron app ships with) that does
    **not** trip notarization review. Add it to `entitlements.plist` proactively; it's only
    load-bearing if the popup ever runs JavaScript at all (the `<details>`/`<summary>`-based
    progressive disclosure this plan calls for needs none).
  - WKWebView's own XPC helper processes (`com.apple.WebKit.WebContent`, etc.) are
    system-provided and Apple-sandboxed independently of the host app — no extra entitlements
    needed to use them from a non-sandboxed host.
  - Build footprint: add `pyobjc-framework-WebKit` alongside the existing
    `pyobjc-framework-Cocoa>=10.0` in `pyproject.toml`. `PrivacyFenceApp.spec` doesn't list
    `AppKit`/`Foundation`/`objc` in `hidden_imports` at all today — PyInstaller already discovers
    PyObjC bridge modules from the plain `import AppKit` in `approval_window.py`; a static `from
    WebKit import WKWebView` should be discovered the same way.
  - **What's still genuinely unverified, and not WebKit-specific**: notarization is currently
    commented out in `build_dmg.sh` §8 and code-signing itself is optional/unexercised in CI
    today (`SIGN_IDENTITY` secret and the cert-import step in `.github/workflows/build.yml` are
    both optional/commented out) — so *no* signing path in this repo has been run end-to-end yet,
    independent of WebKit. The actual gate before shipping Phase 3 is running one real signed
    build (and, once notarization is turned on, one real notarization submission) with the
    WebKit dependency included, on the existing `macos-latest` CI runner — not a design decision,
    an empirical check.
- Three-level progressive disclosure (summary → expanded metadata → full inspect), all within one
  modal session.

**Phase 4 — decided: not being scoped**
Per the maintainer's decision (§10 Q4), the single-reviewer "PR grammar, no PR infrastructure"
version (Phases 1–3) is the intended ceiling for this product. Real multi-person PR-style
governance (a second reviewer, a comment thread visible to someone other than the approver, a
delegated/IT approval queue) is not being scoped — see §8 for why.

## 8. The one strategic tension: "PR-style governance" vs. "no server, ever" — resolved

The long-term vision — "the user becomes a reviewer... AI should gain access because a human
reviewed and approved the request" — is already true today, locally, per-request. What it can't
become without a real architecture decision is **multi-person** governance: a second reviewer, a
comment thread visible to someone other than the approver, or a delegated/IT-side approval queue.
`security-and-compliance.md` currently makes "no PrivacyFence-operated infrastructure... no
vendor server in the request path" (§2) and "no central admin console with visibility into every
employee's approvals... not currently" (§10 FAQ) into explicit, load-bearing trust claims for
enterprise buyers. Building real multi-reviewer PR-style governance means introducing exactly the
kind of shared/central component that document currently promises doesn't exist.

**Decided (§10 Q4): the single-reviewer version is the intended ceiling.** The *visual grammar* of
PR review (Purpose / Files / Risk / Preview / decision, reviewer mental model, structured
sections) ships, applied to the existing single-user, single-device, synchronous approval — that's
everything in Phases 1–3. Genuine second-reviewer/delegated-approval infrastructure is explicitly
not being pursued, which keeps `security-and-compliance.md`'s current trust claims intact without
needing to re-litigate them. If that ever changes, it should be re-opened as its own ADR/product
discussion — with the GDPR/data-residency claims in `security-and-compliance.md` re-evaluated for
whatever new infrastructure it would require — rather than revisited inside this redesign.

## 9. Concrete data-model additions this plan needs

- `connector.py`: no mechanism change needed — `ToolParam.required` already defaults to `True`.
  **Every** `ToolSpec.params` — gated and auto/`read_only` alike, per the decided scope (§10 Q1) —
  gets a new `ToolParam(name="reason", annotation="str", required=True, description="One
  sentence: why are you calling this tool right now?")`, which `bridge_main.py`'s existing dynamic
  registration (`_build_tool_fn`) picks up unchanged — it already builds the function signature
  from `spec.params` generically.
- Every connector: the `reason` value arrives as a normal kwarg alongside the tool's other args.
  Gated tools pass it into `gated_call(...)` as a new required `claude_reason: str` kwarg, kept
  separate from `args`/`preview`/`details_text` so it can never be silently folded into content
  that's rendered as verified. Auto/`read_only` tools pass it into their connector's `_auto_audit`
  helper instead (ten near-duplicate implementations, one per connector — see §7 Phase 1b for the
  file/line list).
- `gate.py`: `gated_call()` gains the `claude_reason: str` parameter, forwards it to
  `show_read_popup`/`show_popup` (the same pass-through pattern `pii_categories` already uses),
  and passes it into `_audit()` — which also needs a new parameter, since it's the single audit
  entry point both the gated and (indirectly, via each connector's own `_auto_audit`) auto paths
  ultimately mirror the shape of.
- `gate.py` / connectors: thread the resolved `privacy.categories`-style policy (already computed
  per connector before `gated_call`) into a new `visibility: dict[str, bool]` kwarg, alongside
  the existing `preview`/`details_text`, so the popup can render the "AI will receive" checklist
  without re-deriving policy state itself.
- `pii_detector.py`: additive category patterns only (broader financial keywords beyond the
  salary/compensation category already shipped) — no interface change, matches its existing
  `_PATTERNS` extension model.
- `audit_log.py`: add `claude_reason: str = ""` to `AuditEntry` (populated on every entry, gated
  or auto-accepted, per the decided scope) and a lookup helper (`recent_matches(operation_key,
  preview) -> int`) for the request-fingerprint feature.
- `approval_window.py`: layout rewrite (Phase 1a pure-AppKit reorder plus a new "Claude says"
  block; Phase 3 WKWebView migration — see resolved signing/notarization investigation in §7
  Phase 3 and §10 Q3). Also add `com.apple.security.cs.allow-jit` to `scripts/entitlements.plist`
  and `pyobjc-framework-WebKit` to `pyproject.toml`'s dependencies.

## 10. Open questions for the maintainer — decisions recorded

1. **Decided**: `reason` ships mandatory (§4) on **every** tool, gated and auto-accepted alike —
   not scoped to just the tools a human reviews. Accepted trade-off: larger implementation
   footprint (touches all ten connectors' `ToolSpec`s, `_auto_audit` helpers, and the audit
   schema — see §7 Phase 1b) and a small token/latency cost on high-frequency auto calls, in
   exchange for `reason` being available for pattern analysis across 100% of the audit log rather
   than only the popup-reviewed subset.
2. **Decided**: Drive classification-label support is **deferred** (§7 Phase 3) — narrow benefit
   relative to the new OAuth scope and consent-screen surface it requires. Revisit only if a
   specific org asks for it.
3. **Investigated, no blocker found — see §7 Phase 3 for the full write-up.** Checked against the
   actual `entitlements.plist`/`PrivacyFenceApp.spec`/`build_dmg.sh`/CI config rather than generic
   guidance: the app is unsandboxed Developer-ID/DMG distribution (not App Store), which removes
   most of the WKWebView friction that shows up in sandboxed-app discussions. The one entitlement
   WKWebView needs under the app's existing Hardened Runtime signing
   (`com.apple.security.cs.allow-jit`, for JavaScriptCore JIT) is standard, Apple-documented, and
   does not trip notarization — the same entitlement Safari/Xcode/Electron apps ship with. What
   remains genuinely unverified is not WebKit-specific: no signing path in this repo has been
   exercised end-to-end yet (notarization is commented out in `build_dmg.sh`, signing is optional
   in CI) — that's a pre-existing gap, not something WKWebView introduces. Action items: add
   `com.apple.security.cs.allow-jit` to `entitlements.plist`, add `pyobjc-framework-WebKit` to
   `pyproject.toml`, and run one real signed build with it on the existing `macos-latest` CI
   runner before shipping Phase 3.
4. **Decided**: multi-reviewer governance is not being scoped. The single-reviewer "PR grammar, no
   PR infrastructure" version (Phases 1–3) is the intended ceiling — see §8.

## 11. Implementation readiness — verdict

**Ready to start.** This branch is merged up to date with `main` (through PR #50 as of this
revalidation); the merge was clean (no conflicts — this doc's only prior footprint was a new file)
and nothing it brought in blocks or invalidates the plan. Concretely:

- **No open design blockers remain.** All four original open questions (§10) are resolved or
  investigated to a concrete action-item level; nothing is still "waiting on a decision."
- **Two pieces of Phase 2 work are already done upstream** (salary/compensation PII category) —
  start there smaller than originally scoped, the doc above reflects the narrowed remainder.
- **Phase 1a's risk went down, not up**, since the branch point: `approval_window.py`'s
  `build_panel()`/`runApproval_()` split and `tests/unit/test_approval_window.py` give the layout
  work a tested seam that didn't exist when this plan was first drafted. New layout sections
  should be asserted on through that same harness rather than verified by manual click-through.
- **Phase 1b's justification got stronger, not weaker**: the new unattended-session fail-fast path
  means some fraction of real calls will now go through `denied_unattended` with no popup ever
  shown — `claude_reason` is the only artifact a later reviewer gets for those, which is a
  concrete answer to "why capture this if it's never displayed live."
- **Phase 3's signing investigation is unaffected**: none of the merged PRs touched
  `entitlements.plist`, `PrivacyFenceApp.spec`, `build_dmg.sh`, or `pyproject.toml`'s build
  dependencies, so §7 Phase 3 / §10 Q3's findings and action items (add
  `com.apple.security.cs.allow-jit`, add `pyobjc-framework-WebKit`, run one real signed build)
  still stand exactly as written.
- **No merge-introduced scope creep requires re-litigating §8.** The new unattended-session mode
  is a fail-closed, purely local mechanism — it doesn't introduce a server, a second reviewer, or
  any shared state, so it doesn't reopen the multi-reviewer-governance question that was
  deliberately closed off.

Recommended order unchanged: **1a → 1b → 2 → 3**, each independently shippable and testable before
the next starts. Phase 1a can begin immediately with no prerequisites. Phase 1b is the largest
single unit of work (touches all ten connectors plus the three meta-tools) but has no unresolved
design question blocking it. Phase 3 has exactly one prerequisite — the real signed build check
from §7/§10 Q3 — before committing to the WKWebView rewrite specifically.
