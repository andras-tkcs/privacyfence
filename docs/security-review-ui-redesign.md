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
  (`bridge_main.py:221-234`) — it forwards `(connector, tool, kwargs)` and nothing else. There is
  no PrivacyFence-operated server (`security-and-compliance.md` §2).
- **The gate is synchronous and blocking** (`gate.py:86-246`): a tool call resolves inside one
  `gated_call()` — auto-accept check → native popup → audit write — before Claude ever gets data
  back. This is exactly the "review, not notify" model the concept wants; it doesn't need to be
  invented, only surfaced better.
- **Current popup** (`approval_window.py`) is a native AppKit `NSPanel`, fixed 620pt wide, with a
  plain-text `NSTextView` for the body. It already does: a title, a key/value "summary box"
  (`preview` dict), a scrollable details pane, PII-category tinting/banner, and up to three
  buttons (Deny / Accept / Accept All or Accept-for-5-min). This is a real foundation, not a
  blank slate — but it has no rich layout, no images/PDF rendering, no progressive disclosure.
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
  before persisting it.
- **Audit log** (`audit_log.py`) records every decision with connector/tool/summary/sender/
  decision/latency/pii_detected, one JSONL line per event, weekly Excel export.
- **What is *not* present anywhere in the codebase**: any field carrying the AI's natural-language
  reasoning, the user's original prompt, or a cross-file "why I picked this" rationale. Confirmed
  by grep — `reasoning`/`rationale`/`intent`/`purpose`/`prompt` appear only in client/library
  method names (`slack_client.py`, `gmail_client.py`, OAuth "purpose" strings), never in the gate,
  connector, or bridge layer.

## 3. Feature-by-feature feasibility

| Concept feature | Feasibility | Why |
|---|---|---|
| Data-over-buttons visual hierarchy (WHAT→WHY→RISK→PREVIEW→decision) | **Feasible now** | Pure layout change to `approval_window.py`. No new data needed for WHAT/PREVIEW; RISK partially needs new signals (§below). |
| "Claude wants to answer: '...'" stated purpose | **Derivable via a new mandatory tool parameter — presence guaranteed, truthfulness is not** | MCP tool calls today carry only `(tool name, args)` — see `bridge_main.py:221-234`. But `args` is extensible: adding a required `reason: str` param to every gated `ToolSpec` gives PrivacyFence a real, protocol-enforced channel for this. It must ship labeled as Claude's self-report, never as verified fact. See §4. |
| "Requested because Claude found a reference in board_minutes.docx" | **Same mechanism, same caveat** | A mandatory `reason` param can carry exactly this sentence — Claude is free to write it. What can't be added is any way to confirm it's true; treat it as reviewer context, not evidence. Best independently-verifiable substitute for cross-file rationale is factual session history (§4). |
| Requested-resources checklist | **Feasible now** | Already the `preview` dict's job; just needs a list-shaped rendering instead of key/value rows when a call touches multiple items. |
| Sensitivity badges (🟢 Internal / 🟠 Financial / 🔴 PII) | **Feasible, needs extension** | `pii_detector.py` already returns category labels. Needs: (a) new category groups for "financial" keywords (amounts, "salary", "payroll"), (b) a badge computed for the *popup-gate* (write) path too, where PII scanning is deliberately absent today by design (`gate.py` docstring) — badges there should read from Claude's own drafted content, not treated as an "external PII" gate. See §7 Phase 2. |
| "🟢 Internal / 🟠 Confidential" **classification labels** | **Feasible only where the org has them** | Google Workspace Enterprise has native Drive data-classification labels, readable via `drive.labels.readonly` scope — PrivacyFence does not currently request this scope (`google-cloud-setup.md`). Real for orgs on that tier; must degrade to "no classification available" everywhere else, not a fabricated default. |
| Large (60–70%) preview / "document reader" | **Feasible for text and Google-native docs; not for arbitrary binary files** | `drive.py:437-449` truncates extracted text to 2000 chars for `details_text`; genuinely binary content (real .docx, images) currently renders as a placeholder string (`drive_client.py`'s `"[binary content — N bytes; use drive_download_file to save it]"`), not a preview. True page-faithful preview needs new work per file type — see §7 Phase 3. |
| PDF preview specifically | **Feasible** | macOS `PDFKit` (`PDFView`) is a standard AppKit control; PrivacyFence already fetches `content_bytes` for binary Drive files, just doesn't render them. |
| Arbitrary .docx/.xlsx page-faithful preview | **Not reliably feasible** | No first-party macOS text/layout extraction without Office's own QuickLook generator being installed, which isn't guaranteed on every Mac. Fall back to extracted-text preview (already computed) rather than promising a "first page" render for every format. |
| Gmail-style email layout | **Feasible now** | `gmail.py:334-347` already builds exactly this shape (From/To/Date/Subject preview + plain-text body) — this is a pure layout change, no new data. |
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
conversation (`bridge_main.py`). The fix isn't to give up on a stated-purpose field — it's to be
precise about what adding one to the protocol does and doesn't buy.

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

**Phase 1b — mandatory `reason` parameter (bigger footprint than 1a, still no external calls)**
Larger than a layout tweak: this touches every connector, not just the popup, so it's called out
on its own rather than folded into "layout only."
- Add a required `reason: str` param to every gated (`gate="review"`/`"popup"`) `ToolSpec` across
  all connectors (`connectors/gmail.py`, `drive.py`, `slack.py`, `calendar.py`, `salesforce.py`,
  `jira.py`, `confluence.py`, `telegram.py`, `tasks.py`, `contacts.py`) — each tool's description
  should instruct Claude to state, in one sentence, why it's calling the tool.
- Thread `reason` through each connector's `gated_call(...)` invocation into a new kwarg (e.g.
  `claude_reason`) so `gate.py`/`approval_popup.py` can render it as its own labeled block,
  distinct from `preview`/`details_text` — never merged with verified fields (§4).
- Do **not** add this to `auto`-gated or `read_only=True` listing tools — no human ever sees those
  calls, so a mandatory reason there only adds token/latency cost with no reviewer benefit; scope
  it strictly to tools that already reach a popup.
- Test impact per `coding-and-testing-guidelines.md` §2.5/§2.6: `tests/helpers.py::build_stub_args`
  needs a default `reason` value for every gated tool's stub args, and the new-connector checklist
  gains a line item ("gated tools carry a `reason` param, rendered as unverified").

**Phase 2 — new local detectors, still zero external calls**
- Extend `pii_detector.py`'s pattern set with a "financial data" category (currency amounts near
  salary/budget/revenue keywords) per its own documented extension model.
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
  `security-and-compliance.md`.
- Native `PDFView` (PDFKit) embed for genuinely binary PDF content already fetched as
  `content_bytes` in `drive_client.py`.
- Optional Drive classification-label lookup (`drive.labels.readonly` scope) for orgs that have
  it configured — must render "no classification available" rather than a default badge when
  absent.
- Three-level progressive disclosure (summary → expanded metadata → full inspect), all within one
  modal session.

**Phase 4 — explicitly out of scope for this plan**
- Multi-person PR-style governance (a second reviewer, comments visible to someone other than the
  approving user, delegated/IT approval workflows). This needs a real design decision from the
  maintainer before any code: see §8.

## 8. The one strategic tension: "PR-style governance" vs. "no server, ever"

The long-term vision — "the user becomes a reviewer... AI should gain access because a human
reviewed and approved the request" — is already true today, locally, per-request. What it can't
become without a real architecture decision is **multi-person** governance: a second reviewer, a
comment thread visible to someone other than the approver, or a delegated/IT-side approval queue.
`security-and-compliance.md` currently makes "no PrivacyFence-operated infrastructure... no
vendor server in the request path" (§2) and "no central admin console with visibility into every
employee's approvals... not currently" (§10 FAQ) into explicit, load-bearing trust claims for
enterprise buyers. Building real multi-reviewer PR-style governance means introducing exactly the
kind of shared/central component that document currently promises doesn't exist.

That's not a reason to drop the framing — it's a reason to be precise about which version ships:

- **Ships now, no architecture change**: the *visual grammar* of PR review (Purpose / Files /
  Risk / Preview / decision, reviewer mental model, structured sections) applied to the existing
  single-user, single-device, synchronous approval. This is everything in Phases 1–3.
- **A real product decision, not a code change**: whether PrivacyFence ever adds a genuine
  second-reviewer/delegated-approval mode, and if so, whether that's an opt-in enterprise feature
  built on new infrastructure (with all the GDPR/data-residency claims in
  `security-and-compliance.md` re-evaluated for it), or a deliberately separate product. Recommend
  raising this as its own ADR/discussion before Phase 4 is ever scoped, rather than folding it
  into this UI redesign.

## 9. Concrete data-model additions this plan needs

- `connector.py`: no mechanism change needed — `ToolParam.required` already defaults to `True`.
  Every gated tool's `ToolSpec.params` gets a new `ToolParam(name="reason", annotation="str",
  required=True, description="One sentence: why are you calling this tool right now?")`, which
  `bridge_main.py`'s existing dynamic registration (`_build_tool_fn`) picks up unchanged — it
  already builds the function signature from `spec.params` generically.
- Every connector: the `reason` value arrives as a normal kwarg alongside the tool's other args;
  each connector passes it into `gated_call(...)` as a new required kwarg (e.g. `claude_reason:
  str`), kept separate from `args`/`preview`/`details_text` so it can never be silently folded
  into content that's rendered as verified.
- `gate.py`: `gated_call()` gains the `claude_reason: str` parameter and forwards it to
  `show_read_popup`/`show_popup` unchanged, the same pass-through pattern `pii_categories` already
  uses.
- `gate.py` / connectors: thread the resolved `privacy.categories`-style policy (already computed
  per connector before `gated_call`) into a new `visibility: dict[str, bool]` kwarg, alongside
  the existing `preview`/`details_text`, so the popup can render the "AI will receive" checklist
  without re-deriving policy state itself.
- `pii_detector.py`: additive category patterns only (financial keywords) — no interface change,
  matches its existing `_PATTERNS` extension model.
- `audit_log.py`: add a lookup helper (`recent_matches(operation_key, preview) -> int`) for the
  request-fingerprint feature; no schema change to `AuditEntry` needed since existing fields
  already carry what's needed. Consider also recording `claude_reason` in `AuditEntry` itself —
  it's cheap to store and lets a later audit review spot patterns of generic/boilerplate reasons
  across many calls, which is itself a useful signal at the fleet level even though it isn't
  verifiable per-call.
- `approval_window.py`: layout rewrite (Phase 1a pure-AppKit reorder plus a new "Claude says"
  block; Phase 3 WKWebView migration).

## 10. Open questions for the maintainer

1. Confirmed direction: `reason` ships **mandatory**, not optional, enforced at the MCP schema
   level (§4). Remaining question is scope — mandatory on every gated (`review`/`popup`) tool
   only, as recommended in §7 Phase 1b, or should it also cover `auto`-gated tools that a human
   never sees (recommendation: no — no reviewer benefit, pure overhead)?
2. Is Drive classification-label support (needs a new OAuth scope, Enterprise-tier-only) worth
   the added consent-screen surface for the subset of orgs that have it?
3. Does the WKWebView migration in Phase 3 conflict with any code-signing/notarization plans
   (`security-and-compliance.md` §8 already flags notarization as an open item)?
4. Should Phase 4 (multi-reviewer governance) be scoped at all, or is the single-reviewer "PR
   grammar, no PR infrastructure" version the intended ceiling for this product?
