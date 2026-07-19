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
  macOS before this is considered verified, not just written. **Resolved 2026-07-17** — see
  "Verification note — real `pytest` run completed on macOS" above.

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

## Verification note — real `pytest` run completed on macOS (2026-07-17)

Every "verification honesty note" below (Phase 0, 1a, 1b, 2) flagged the same gap: this repo's
full test suite needs real macOS/PyObjC/AppKit and could only be hand-checked against existing
assertions, not executed, in the environment those phases were originally implemented in. That gap
is now closed — `pytest -v --cov=src/privacyfence --cov-report=term-missing` has run for real,
locally, on macOS.

The first run caught two real bugs the hand-checking missed, both now fixed (`cb5febb`):
- `approval_window.py`: `_CONTENT_FLAG_FILL_ALPHA` was accidentally equal to
  `_PII_BANNER_FILL_ALPHA` (both `0.16`), silently defeating the visual distinction Phase 2's
  write-side amber banner is supposed to have from the read-gate's PII red banner —
  `test_flags_and_pii_categories_use_visually_distinct_alphas` caught it.
- `test_gate.py`: a copy-pasted IBAN string in
  `test_review_gate_call_succeeds_without_write_content_flags_kwarg` tripped Phase 2's new
  financial detector on the `gate="review"` path, routing to a real, unmocked
  `show_pii_confirmation_popup` and hanging for 30s on a live `osascript` subprocess call waiting
  for a click nobody was there to give. Not a flaky test — a genuine hang, and a preview of exactly
  the kind of gap these honesty notes existed to flag.

After both fixes: **2447 passed, 0 failed, 93% coverage, ~21s.** Phases 0–2 are now genuinely
pytest-verified end to end, not just internally consistent.

Two more checks from `docs/coding-and-testing-guidelines.md` §2.7 also ran, since this branch
touches `approval_window.py`'s modal-loop plumbing and every connector:
- `scripts/qa_fixture_recorder.py --check` against real accounts for all 10 connectors — 13/13
  checks passed.
- `scripts/qa_popup_smoke.py` — driving real clicks against the real modal popups. Along the way,
  found and fixed a bug in the script itself (pre-existing on `main`, unrelated to this branch):
  `AppHelper.runEventLoop()` (`NSApplicationMain` under the hood) doesn't reliably return control
  to Python once the background thread finishes and calls `stopEventLoop()`, so the report the
  script is supposed to print after all scenarios run never printed. Fixed by printing/writing the
  report and exiting from inside the background thread itself, which does reliably run to
  completion (`cb5febb`).

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

**Phase 1a — layout only, zero new data (lowest risk, ship first) — implemented, see status below**
- Restructure `approval_window.py`'s section order to WHAT → AI VISIBILITY → RISK (existing PII
  banner, relabeled) → PREVIEW → decision. **Done**: `_compute_layout`/`build_panel()` now build
  in that order (summary box, then the new visibility checklist, then the PII banner, then
  details); the "must mirror the real layout" invariant between the two is preserved.
- Render `privacy.categories`/`drive_privacy.categories`/`slack_privacy.categories` state as the
  "AI will receive" checklist. **Done**: `gate.py`'s `gated_call()` gained a `visibility: dict[str,
  str] | None` kwarg (read-gate calls only — a write already shows exactly what it's sending, see
  `approval_popup.show_popup`'s docstring), threaded through `show_read_popup` →
  `show_native_approval` → a new `_visibility_lines`/`_visibility_height`/
  `_build_visibility_overlay` trio in `approval_window.py` (same box+overlay pattern as the
  existing summary box, single-column since a checklist has no natural second column). Wired at
  the call sites for every review-gated read tool in Gmail (`_get_message`, `_get_thread`), Drive
  (`_get_file_content`, `_sheets_get_values`), and Slack (`_get_channel_history`,
  `_get_thread_replies`, `_search_messages`) — the same three connectors and tools Phase 0 covers,
  using `privacy_filter.category_policy()` directly rather than re-deriving policy state.
- Rename buttons/labels per the concept ("Allow once" / "Allow for 5 min" / relabel Accept All).
  **Done 2026-07-17** (previously deferred): `_build_button` calls in `approval_window.py` now use
  "Allow once" / "Deny" / "Always allow" / "Allow for 5 min"; `buttonClicked_`'s title-based
  dispatch updated to match, with the underlying result vocabulary
  (`"accept"`/`"accept_all"`/`"accept_temp"`/`"deny"`) left unchanged since `gate.py`/
  `audit_log.py`/every test keys on those internally. Propagated to every place that names a
  button: `test_approval_window.py`'s button-set/dispatch assertions, `qa_popup_smoke.py`'s click
  targets, `connector-qa-testing.md`'s human-operator instructions, the audit log's Excel export
  labels, and the relevant prose comments/docstrings in `gate.py`, `auto_accept.py`,
  `approval_popup.py`, `pii_detector.py`, `settings.yaml.example`, `TECHNICAL_REFERENCE.md`,
  `security-and-compliance.md`, and `qa-environment-setup.md`. `atlassian-setup.md`'s "Accept" is
  Atlassian's own OAuth consent screen, not this button — left alone.
- Default keyboard focus to a non-destructive affordance rather than Accept. **Done, adapted**:
  rather than a separate "Inspect" button (the popup already shows full content inline, with no
  expand step to attach one to), `_build_button` no longer sets `"\r"` as Accept's key equivalent,
  and the details pane's `NSTextView` is now the panel's initial first responder — Enter can no
  longer approve a request nobody's looked at, and default focus visibly lands on the content.
  Deny keeps Escape.
- Add reading-time estimate from `details_text` length. **Done**: `_estimate_reading_seconds`/
  `_reading_time_label` (~200 wpm, floored at 1 second), shown in the "Preview (~N sec/min read)"
  label above the details pane.
- Gmail-specific layout for `gmail_get_message`/`gmail_get_thread`. **Not done** — deferred to
  Phase 3 (WKWebView), per this doc's own original reasoning: a bespoke Gmail-style NSView layout
  in raw AppKit is exactly the kind of per-surface layout work the WKWebView migration exists to
  avoid hand-building.

**Phase 1a implementation status**: the data-flow and layout-reorder pieces (visibility checklist,
section order, reading time, focus/Enter change, button relabeling) are implemented; the
Gmail-specific layout remains deliberately deferred to Phase 3's job. **Verification honesty note,
same as Phase 0**: this repo's test
suite requires macOS (PyObjC/AppKit) and could not be executed in this environment — every change
here was checked by hand against `tests/unit/test_approval_window.py`'s existing assertions
(including working out, line by line, that the `_compute_layout`/`build_panel()` height math still
matches) and new tests were added following the same patterns, but none of it has run through a
real `pytest` yet. That is the one remaining gate before this is trustworthy, not just plausible.
**Resolved 2026-07-17** — see "Verification note — real `pytest` run completed on macOS" above.

**Phase 1b — mandatory `reason` parameter, on every tool including auto-gated (largest-footprint
item in this whole plan — decided scope, see §10) — implemented, see status below**

The maintainer's decision (§10 Q1) is to capture `reason` universally, not just on tools a human
reviews, so it's available for later audit-log pattern analysis even where there's no popup to
show it in.

**Design refinement made during implementation, worth recording**: the plan as originally written
called for threading `claude_reason` explicitly through every one of the ~95 `gated_call(...)`/
`_auto_audit(...)` call sites. Implementing it surfaced a better option already idiomatic to this
codebase: `gate.py` already carries `is_unattended()`'s state via a `contextvars.ContextVar` set
once per dispatched request in `ipc_server.py` (`unattended_scope`), not threaded through every
call site individually. `reason` now uses the identical pattern (`reason_scope`/`current_reason()`
in `gate.py`) — set once, centrally, in `ipc_server.py`'s `_call_connector`, read internally by
`gated_call()` and every connector's `_auto_audit()`. This turned out to be **not just lower-risk
but more correct**: `_call_connector`'s existing request-deduplication (see its own docstring)
hashes `(connector, tool, args)` to coalesce a client-timeout retry with the original in-flight
call — if `reason` (naturally-varying, freshly-regenerated text) stayed inside `args`, a genuine
retry would get a different dedupe key every time and silently defeat that coalescing, reproducing
the exact double-popup bug the mechanism exists to prevent. `reason` is popped out of `args` before
the dedupe key is computed and before it reaches `connector.call()` — which also means **no
connector method signature needed to change at all**, only the `ToolSpec` declarations (so the MCP
schema still enforces "required") and a handful of central files.

- **Done**: a required `reason: str` `ToolParam` on all 95 tools across all ten connectors
  (verified count: `ToolSpec(` occurrences == `ToolParam("reason"` occurrences in every file,
  95/95), description instructing Claude to state in one sentence why it's calling the tool.
- **Done**: `gate.py`'s `reason_scope`/`current_reason()` (mirrors `unattended_scope`/
  `is_unattended()` exactly); `ipc_server.py`'s `_call_connector` pops `reason` from `args` before
  the dedupe-key computation and wraps `connector.call(...)` in `reason_scope(reason)`.
- **Done**: `gated_call()` reads `current_reason()` internally (no new required kwarg on its own
  signature) and forwards it to `_audit()` (both the gated and, via each connector's own
  `_auto_audit`, the auto-accepted path write to the same `AuditEntry.claude_reason` field) and to
  both `show_read_popup` and `show_popup` — unlike `visibility` (§7 Phase 1a), `reason` applies to
  writes too, since "why am I doing this" isn't read-specific.
- **Done**: `approval_window.py` renders a "Claude says (unverified)" block (own label, own
  secondary-colored, unbolded text — deliberately not styled like the verified WHAT/AI-VISIBILITY/
  RISK sections above it), positioned between RISK and PREVIEW per the design mockup (§6).
- **Done**: `audit_log.py`'s `AuditEntry.claude_reason` field, plus a "Claude's Reason (unverified)"
  column in the Excel export — genuinely unit-tested and passing in this environment (see
  verification note below), unlike everything touching the AppKit/gate chain.
- **Done**: `tests/helpers.py::build_stub_args` updated — it now deliberately *excludes* `reason`
  from the args it builds, since it models what a connector method actually receives (post
  `ipc_server.py` stripping), and no method signature accepts `reason`.
- **Done 2026-07-17** (previously deferred): the three bridge meta-tools (`privacyfence_check_policy`,
  `privacyfence_begin_unattended_session`, `privacyfence_end_unattended_session`) now each declare a
  required `reason: str` parameter directly in their handler's function signature — a distinct,
  smaller extension from the contextvar mechanism above (there's no connector-layer `_auto_audit`
  for these to read it from), touching `bridge_main.py`'s handlers and tool descriptions,
  `ipc_client.py`'s three method signatures, `ipc.py`'s protocol docstring, and `ipc_server.py`'s
  `_check_policy`/`_begin_unattended_session`/`_end_unattended_session` plus their audit-write
  helpers. `reason` is required at the MCP tool-schema layer (so Claude must always supply it,
  matching every other tool) but optional (default `""`) at the wire-protocol layer between
  `ipc_client.py` and `ipc_server.py`, so an old bridge talking to a new daemon (or vice versa)
  degrades to an empty `claude_reason` instead of breaking — consistent with this doc's note (§7
  intro) that bridge and daemon ship and update independently. Recorded on the resulting
  `"policy_check"`/`"unattended_session_started"`/`"unattended_session_ended"` audit entries; the
  automatic session-end-on-disconnect path has no reason to attribute and stays `""`.
- Trade-off accepted deliberately, per §10 Q1: a small token/latency cost on high-frequency,
  low-risk calls (e.g. `list_messages`) that no human ever reviews, in exchange for uniform
  coverage in the audit log.

**Verification honesty note**: this piece split cleanly along a line worth naming explicitly.
`privacy_filter.py`, `audit_log.py`, `tests/helpers.py`, and `connector.py` have no AppKit
dependency and their test suites **actually ran and passed** in this environment (89 tests total
across `test_audit_log.py`, `test_privacy_filter.py`, `test_pii_detector.py`) — including a real
`openpyxl` Excel export round-trip confirming the new "Claude's Reason" column. The 95 `ToolSpec`
edits were generated by a small script (bracket-depth-aware, handling empty/single-line/multi-line
`params=[...]` and description strings containing nested quotes/brackets), dry-run and diffed
before being applied, with the resulting count cross-checked (`ToolSpec(` count == new-param count,
95/95, in every file) and every file re-verified with `py_compile`. `gate.py`, `ipc_server.py`, and
every connector still require macOS/AppKit to actually import or run — that verification gap is
unchanged from Phase 0/1a and still needs a real `pytest` run before this is trustworthy end to
end, not just internally consistent. **Resolved 2026-07-17** — see "Verification note — real
`pytest` run completed on macOS" above.

**Phase 2 — new local detectors, still zero external calls — implemented, see status below**

- **Done**: `pii_detector.py` gained a "Financial figures (currency amounts)" category, distinct
  from the salary/compensation category that had already shipped before this plan's first draft.
  Anchored on a currency symbol (`$`/`€`/`£`) or ISO code (USD/EUR/GBP/HUF/CHF/Ft) adjacent to a
  number — never a bare number alone, matching the module's own stated discipline for avoiding
  the near-universal false positives email/phone patterns would cause. 10 new tests, **actually
  run and passing** (this module has no AppKit dependency) — including confirming a spelled-out
  currency word ("3000 Euro") does *not* match the ISO-code-anchored pattern, and that a bare
  number/date/section-reference doesn't either.
- **Done**: a separate, deliberately weaker `write_content_flags` signal for the popup (write)
  gate, computed in `gated_call()` from Claude's own drafted content via the same
  `detect_pii_categories()` entry point. Explicitly **not** routed through the existing
  `pii_categories`/`_confirm_pii_or_deny` machinery (`gate.py`'s module docstring is clear that
  distinction — read-gate-only PII confirmation — is deliberate) and never folded into
  `AuditEntry.pii_detected`, whose established meaning is specifically about the read-gate scan.
  Rendered in `approval_window.py` as its own amber-tinted, non-alarming banner (`_CONTENT_FLAG_AMBER`,
  distinct alpha from the PII banner's red) with no confirmation gate and no full-window wash —
  informational only, e.g. "This message appears to contain: IBAN (bank account number)" before
  Claude sends it. Forwarded to both `show_read_popup`-adjacent code paths as a genuinely new
  `write_content_flags` kwarg on `show_popup`.
- **Done**: request fingerprinting. `AuditLogger.recent_matches(connector, tool, summary, week=...)`
  (new method, `audit_log.py`) counts prior approved-like decisions for the same
  `(connector, tool, summary)` in one week's log — a practical proxy for "the same request" given
  `AuditEntry` carries neither an `operation_key` nor the full `preview` dict. Computed once in
  `gated_call()` (`seen_count`), forwarded to **both** `show_read_popup` and `show_popup` (applies
  to writes as much as reads, like `claude_reason`), rendered as a small "Seen N times this week"
  caption right under the title — silent when zero, so a first-time request adds no noise. 9 new
  `recent_matches` tests, **actually run and passing**.
- While extending `gate.py`'s popup-call threading for this phase, found and fixed a latent bug
  from Phase 1b: five `fake_show_popup` test doubles in `test_gate.py` were never updated when
  `claude_reason` was added to that call site, meaning those tests would have failed the moment a
  real `pytest` ran on macOS. Caught only because adding `seen_count` required touching the same
  call sites again — a concrete illustration of why the verification note below matters.

**Verification honesty note**: `pii_detector.py` and `audit_log.py` have no AppKit dependency —
their test suites (108 tests total across every runnable file so far) actually ran and passed in
this environment. `gate.py`, `approval_popup.py`, and `approval_window.py`'s wiring (the
`write_content_flags`/`seen_count` threading, the new amber banner, the fingerprint caption) could
not be executed here for the same reason as every prior phase's AppKit-touching work — checked by
hand against every existing test assertion and the layout-height "must mirror" invariant, with a
real bug (the `fake_show_popup` gap above) already found this way once. Still needs a real
`pytest` run on macOS before this is trustworthy end to end. **Resolved 2026-07-17** — a real run
found one more real bug beyond the `fake_show_popup` gap (a duplicated banner-alpha constant); see
"Verification note — real `pytest` run completed on macOS" above.

**Phase 3 — real preview rendering (bigger UI investment) — partially implemented, see status below**
- Move `approval_window.py`'s body from a plain `NSTextView` to an embedded local `WKWebView`
  (PyObjC exposes WebKit) rendering a local HTML template — this is the practical way to get
  badges, a Gmail-style header, progressive disclosure (`<details>`), and a wider/taller window
  without hand-building each new layout in raw AppKit constraints. Keep it `file://`/`data:`-only,
  no network — this preserves the "no telemetry, no network calls out of the popup" property from
  `security-and-compliance.md`. Render via `loadHTMLString(_:baseURL:)` with a single
  self-contained string (inlined `<style>`, no separate resource files) rather than
  `loadFileURL(...)` — WKWebView's `file://` loading model is stricter than the legacy `WebView`
  class and this sidesteps it entirely, at no cost since the template is generated in-process
  anyway. **Done 2026-07-17**: `approval_window.py`'s details/body pane (`_build_details_view`) is
  now a `WKWebView` rendering `_details_html()`'s output — a pure function (same "must mirror"
  contract `_compute_layout()` has) that HTML-escapes `details_text` (already stripped of any real
  markup upstream by `html_to_text.py`, so this is defense in depth, not the primary safeguard)
  into a self-contained document: system-font `<style>` block, `prefers-color-scheme` light/dark
  support, `white-space: pre-wrap` to preserve the old NSTextView's line-break/wrap behavior.
  Loaded via `loadHTMLString_baseURL_(html, None)` — nil base URL, so there is nothing for it to
  even attempt to load out to. `WKWebViewConfiguration.preferences().setJavaScriptEnabled_(False)`
  explicitly, not just left unused — nothing this pane renders today needs script, matching §5.5's
  "keep it local and synchronous." **Gmail-style header done 2026-07-17**: `gate.py`'s
  `gated_call()` gained a `content_kind: str = "generic"` param ("generic" | "email"), read-gate
  only (same scoping rationale as `visibility`) and forwarded to `show_read_popup` →
  `show_native_approval` → `ApprovalWindowController.content_kind`. `_details_html()` prepends a
  new `_email_header_html()`-built From/To/Subject/Date block (own pure function, `_html_escape`'d
  per field, styled like a real email — §6's mockup) when `content_kind == "email"`. Set only at
  `connectors/gmail.py`'s `_get_message` call site, whose `preview` dict already has exactly the
  From/To/Date/Subject shape the header reads — **deliberately an explicit connector-set hint, not
  guessed from preview's shape**, so a future connector reusing similar label names can't
  accidentally get styled as an email. `gmail_get_thread` does **not** opt in: a thread is several
  messages each with its own sender, which doesn't fit one single-message header (it already
  renders per-message "From:"/"Date:" lines inline in `details_text`) — left as its own,
  differently-shaped follow-up rather than forcing a mismatched header onto it. Per-file-type
  *badges* (as opposed to the email header specifically) remain **not done** — this pass only
  builds the one per-surface rendering §6 actually specifies a mockup for.
- Native `PDFView` (PDFKit) embed for genuinely binary PDF content already fetched as
  `content_bytes` in `drive_client.py`. **Done 2026-07-17**: `gate.py`'s `gated_call()` gained a
  `pdf_bytes: bytes = b""` param (read-gate only, same scoping as `visibility`/`content_kind`),
  forwarded through `show_read_popup` → `show_native_approval` →
  `ApprovalWindowController.pdf_bytes`. `_build_details_view` renders a `Quartz.PDFView` (backed by
  `PDFDocument.alloc().initWithData_()`) instead of the usual `WKWebView` when non-empty, falling
  back to the WKWebView if the bytes don't parse as a real document (`PDFDocument` returns `nil`).
  `pyobjc-framework-Quartz` added to `pyproject.toml` for `PDFView`/`PDFDocument` (there's no
  separate `pyobjc-framework-PDFKit` package — Quartz exposes these classes).

  Only ever set at `connectors/drive.py`'s `_get_file_content`, and only when **all** of: real
  `application/pdf` mime type, `not content.truncated` (a partial PDF stream from
  `get_file_content`'s `max_bytes` cap almost always fails to parse as a valid document anyway —
  full PDFs commonly exceed the current 100KB default, an honest, documented limitation, not a
  bug), and — the load-bearing condition — `category_policy("drive_privacy", "file_content") ==
  "allow"`. That last check matters more than it looks: `raw_text`/`text` (the placeholder string
  for binary content) already only flows through unredacted under that exact same condition, and
  `filtered_data.content` (what Claude actually receives for this call) is always just that
  placeholder text, never the real bytes. Rendering the actual PDF to the *reviewer* without that
  check would mean the human sees something strictly richer than what "AI will receive" already
  discloses Claude gets — exactly the kind of gap this whole redesign exists to close, not open a
  new instance of. Skipping the check would have been the easy, wrong version of this feature.
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
    **not** trip notarization review. **Done 2026-07-17**: added to `entitlements.plist`, with a
    comment noting it's only load-bearing if the popup ever runs JavaScript at all -- which, per
    the item above, it explicitly doesn't (`setJavaScriptEnabled_(False)`).
  - WKWebView's own XPC helper processes (`com.apple.WebKit.WebContent`, etc.) are
    system-provided and Apple-sandboxed independently of the host app — no extra entitlements
    needed to use them from a non-sandboxed host.
  - Build footprint: add `pyobjc-framework-WebKit` alongside the existing
    `pyobjc-framework-Cocoa>=10.0` in `pyproject.toml`. **Done 2026-07-17.** `PrivacyFenceApp.spec`
    doesn't list `AppKit`/`Foundation`/`objc` in `hidden_imports` at all today — PyInstaller already
    discovers PyObjC bridge modules from the plain `import AppKit` in `approval_window.py`; a static
    `from WebKit import WKWebView` should be discovered the same way. **Left `PrivacyFenceApp.spec`
    unchanged** on that basis — `approval_window.py`'s `from WebKit import WKWebView,
    WKWebViewConfiguration` is exactly this same kind of static top-level import, and
    `approval_popup.py` (the only importer of `approval_window.py`) is only ever bundled into the
    daemon `Analysis` (`PrivacyFenceApp.spec`), never the bridge (`PrivacyFenceBridge.spec`) —
    confirmed by grep, matching where `AppKit` itself is already daemon-only. **`pyobjc-framework-
    Quartz` added the same way** once `PDFView`/`PDFDocument` (below) needed it — there's no
    separate `pyobjc-framework-PDFKit` package; Quartz is where PyObjC exposes those classes.
  - **What's still genuinely unverified, and not WebKit-specific**: notarization is currently
    commented out in `build_dmg.sh` §8 and code-signing itself is optional/unexercised in CI
    today (`SIGN_IDENTITY` secret and the cert-import step in `.github/workflows/build.yml` are
    both optional/commented out) — so *no* signing path in this repo has been run end-to-end yet,
    independent of WebKit. The actual gate before shipping Phase 3 is running one real signed
    build (and, once notarization is turned on, one real notarization submission) with the
    WebKit dependency included, on the existing `macos-latest` CI runner — not a design decision,
    an empirical check.
- Three-level progressive disclosure (summary → expanded metadata → full inspect), all within one
  modal session. **Not done** — deferred, and worth recording *why* rather than just leaving it
  unchecked: this codebase's own hard invariant (`approval_popup.py`'s module docstring: "full
  content is always shown before the decision, so the human always sees what they're approving
  before they can click") rules out the literal reading of "progressive disclosure" as *hiding*
  the primary content behind a click by default — that would be a regression, not a feature. The
  feasibility table's own assessment of "Inspect before approve" (§3, row: "Same modal session, an
  expand/collapse of the existing scrollable pane, or a resized `NSPanel`. No protocol change.")
  points at the only honest reading available with today's data model: an *area* expansion (make
  the already-fully-visible body pane bigger on demand), not an *information* one, since there's no
  currently-held-back "expanded metadata" layer to reveal -- everything the window has access to
  (`preview`, `details_text`, `visibility`, `pii_categories`, `claude_reason`, `seen_count`) is
  already rendered, unconditionally, today. Implementing a real second/third disclosure level
  meaningfully would need either a new data source (e.g. raw per-field metadata beyond what
  `preview` already flattens) or an area-only expand/collapse toggle — both left as follow-up work
  rather than building something that looks like three levels but doesn't hide or reveal anything.

**Phase 3 implementation status**: the WKWebView migration (details/body pane only), the Gmail-style
From/To/Subject/Date header, the native `PDFView` embed (gated on the same privacy-policy condition
as the text it replaces), and the signing-investigation action items (`entitlements.plist`,
`pyproject.toml`) are all implemented; per-file-type badges beyond the email header and PDF case,
and progressive disclosure, remain deliberately deferred -- see the individual items above for why
each one is a distinct follow-up rather than a small addition to this pass. **Verification honesty
note**: the new `_details_html()`/`_email_header_html()`/`WKWebView` wiring is unit-tested the same
way every other AppKit-touching piece of this window is (`test_approval_window.py`'s
`TestDetailsPane`/`TestEmailStyleHeader`, construction-only, asserting the exact HTML string handed
to `loadHTMLString_baseURL_` rather than reading the WKWebView's own asynchronously-loaded content
back out; `connectors/test_gmail_connector.py` confirms `content_kind="email"` is set only at
`_get_message`'s call site, never `_get_thread`'s) and the full suite passes on a real macOS run.
Fixing this content_kind's threading through `gate.py` also surfaced the exact same latent-bug
pattern Phase 2 found once already (§7 Phase 2's note on the `fake_show_popup` gap): 16
`fake_show_read_popup` test doubles in `test_gate.py` had a fixed positional signature that didn't
anticipate a 9th argument, so they would have raised `TypeError` the moment a real `pytest` ran on
macOS -- caught immediately by actually running the suite locally, not by inspection, which is
exactly the point this recurring class of gap keeps making. Adding `pdf_bytes` as a 10th positional
argument right afterward hit the identical gap a second time in the same session -- fixed the same
way, same 16 call sites, confirming this class of test double (fixed positional signature, no
`**kwargs` catch-all) is worth watching for specifically on any future param addition to this call
chain, not just a one-off.

`_build_details_pdf_view`/`PDFDocument`/`PDFView` and `connectors/drive.py`'s gating logic are
covered by `test_approval_window.py`'s `TestPdfViewEmbed` (a real, parseable minimal PDF plus a
garbage-bytes fallback case, both via a real `PDFDocument.alloc().initWithData_()` call -- not
mocked) and `test_drive_connector.py`'s `TestPdfViewEmbed` (every combination of mime type,
`truncated`, and `category_policy` that gates whether `pdf_bytes` is set at all). What the suite
*cannot* verify: whether the page/PDF actually renders correctly on screen, since WKWebView's and
PDFView's real render are out of unit-test reach the same way AppKit's real modal loop already was
— `scripts/qa_popup_smoke.py` needs a real interactive run (Accessibility granted, a real
WindowServer session) to confirm that, same gate every prior phase's popup change has had. (It
doesn't yet have a PDF-rendering scenario of its own -- worth adding before relying on it for that
specific path.)
The real signed-build empirical check (§10 Q3) is still outstanding and unrelated to this
verification gap — it needs the maintainer's own Developer ID signing certificate, not something
achievable from this environment.

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
  **Done**: every `ToolSpec.params` — gated and auto/`read_only` alike — has a new
  `ToolParam(name="reason", annotation="str", required=True, description="One sentence: why are
  you calling this tool right now?")`, picked up unchanged by `bridge_main.py`'s existing dynamic
  registration (`_build_tool_fn`, generic over `spec.params`).
- **Superseded by a better design, see §7 Phase 1b**: the original plan here was an explicit
  `claude_reason: str` kwarg threaded through every `gated_call(...)`/`_auto_audit(...)` call site.
  Implemented instead as `gate.py`'s `reason_scope`/`current_reason()` — a `contextvars.ContextVar`
  set once, centrally, in `ipc_server.py`'s `_call_connector` (mirroring the existing
  `unattended_scope`/`is_unattended()` pattern), read internally by `gated_call()` and every
  connector's `_auto_audit()`. No connector method signature changed; `gated_call()`'s own
  parameter list didn't gain a new required kwarg either. This also fixed a real correctness gap
  the original plan would have introduced: `ipc_server.py`'s request-dedup hashes `(connector,
  tool, args)`, and leaving naturally-varying `reason` text inside `args` would have silently
  defeated deduplication for genuine client-timeout retries — see §7 Phase 1b for the full
  explanation.
- **Done**: `gate.py`'s `gated_call()` forwards `current_reason()` to `show_read_popup` **and**
  `show_popup` (the same pass-through pattern `pii_categories`/`visibility` use) and into
  `_audit()`, which gained a `claude_reason` parameter — the single audit entry point both the
  gated and (indirectly, via each connector's own `_auto_audit`) auto paths write through.
- **Done** (§7 Phase 1a): `gate.py` / the three connectors with a category schema thread the
  resolved `privacy_filter.category_policy()` state into a `visibility: dict[str, str]` kwarg,
  alongside the existing `preview`/`details_text`, so the popup renders the "AI will receive"
  checklist without re-deriving policy state itself.
- `pii_detector.py`: additive category patterns only (broader financial keywords beyond the
  salary/compensation category already shipped) — no interface change, matches its existing
  `_PATTERNS` extension model. **Not yet done** — Phase 2, still open.
- **Done**: `audit_log.py`'s `AuditEntry.claude_reason: str = ""`, populated on every entry (gated,
  auto-accepted, or otherwise) via `_audit()`/`_auto_audit()`, plus a "Claude's Reason (unverified)"
  column in the Excel export. The request-fingerprint lookup helper (`recent_matches(operation_key,
  preview) -> int`) is **not yet done** — still Phase 2.
- **Done**: `approval_window.py`'s Phase 1a layout reorder plus a new "Claude says (unverified)"
  block. Phase 3's WKWebView migration (and the `com.apple.security.cs.allow-jit`/
  `pyobjc-framework-WebKit` additions it needs) remain **not yet done** — still gated on the real
  signed build check from §7 Phase 3 / §10 Q3.

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
