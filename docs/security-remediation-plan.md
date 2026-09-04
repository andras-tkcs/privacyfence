# Security & Quality Remediation Plan

Implementation plan for the findings in the 2026-09-04 technical code review
(reviewed commit `32fe3a2266389c41cc2eb48beb14540af31f40fd`, `v4.0.0a12`). This
document turns that review's finding list and its own §15 roadmap into
PR-sized, sequenced work. It does not re-derive the findings — see the review
for evidence, exploit paths and reasoning. Every ID from the review's
Appendix A is accounted for in [Coverage matrix](#coverage-matrix) at the
bottom; nothing is dropped silently.

Branch names follow `docs/coding-and-testing-guidelines.md` / `CLAUDE.md`
convention (`<type>/<kebab-case-description>`). Each row is sized to land as
one PR with its own tests, per the review's own "each fix ships with its
test in the same change" rule for P0, extended here to every phase.

## How this differs from the review's own §15 roadmap

The source roadmap (3 buckets: P0 / P1 / P2) is sound and this plan keeps its
priority order. It's split further here into 4 phases so each phase is a
coherent, independently-shippable body of work rather than a grab-bag, and a
few items are re-sequenced or re-scoped. Flagging these explicitly since
they're judgment calls, not agreement/disagreement with a finding:

1. **SEC-05 (sign the org bundle) is split into two steps.** Full signing
   (build-time signature, pinned public key, verify-at-every-start) is a
   real subproject — key management, `build_org_bundle.py` changes, a
   verification path that must itself fail closed. The review offers an
   explicit "minimum viable fallback" (log a bundle hash at every startup so
   tampering is *detectable*). Doing the fast interim step in Phase 1 and the
   full signing scheme as its own tracked item (Phase 1b) gets the
   detectability win immediately without blocking the rest of Phase 1 on a
   crypto-and-key-distribution design. **Flagging this rather than deciding
   it silently** — if you'd rather go straight to full signing and skip the
   interim hash-log step, say so and Phase 1's PR1.1 becomes the signing
   work directly.
2. **DOC-01 (rewrite `security-and-compliance.md`) ships in Phase 0 as the
   review requires, but described as a two-pass rewrite.** Pass 1 (Phase 0)
   corrects the factual architecture errors (A–C, E, G in the review's list)
   and states plainly, as a known limitation, that `web_token`/`mcp_token`
   are persisted and reused (D) rather than describing the post-SEC-06
   state that doesn't exist yet. Pass 2 (end of Phase 1) updates that one
   section once SEC-06's bootstrap-flow redesign lands. Rewriting it twice
   is cheaper than either shipping a document that's inaccurate about a
   fix-in-flight, or blocking a release-blocker doc fix on a multi-day
   credential redesign.
3. **SEC-12 and SEC-13 (absolute token/session lifetimes) are combined into
   one PR** — same shape of fix (add an issuance timestamp + absolute-expiry
   check) in two adjacent modules, and the review itself cross-references
   them together.
4. **TST-01a/b/c (the abuse tests) are not separate PRs** — the review
   already specifies they land in the *same* change as SEC-01/02/03 per its
   P0 table; this plan just keeps that pairing explicit per phase item
   below rather than listing tests as a separate row.

If none of the above needs adjusting, Phase 0 can start immediately.

---

## Phase 0 — Release blockers

Ship this week. Nothing here is deployment-mode-specific; all of it applies
to every install. Each PR lands the fix and its adversarial test together.

| # | Branch | Finding(s) | Scope | Effort |
|---|---|---|---|---|
| 0.1 | `fix/sec-01-approval-window-url-scheme-allowlist` | SEC-01, TST-01a | Lift `_is_safe_url`/`_ALLOWED_URL_SCHEMES` from `email_markdown.py` into a shared helper; apply in `markdown_to_html._inline`; drop unsafe `href`s, keep link text. New `tests/unit/abuse/test_abuse_markdown_rendering.py` parameterised over the payload list in SEC-01 (raw, percent-encoded, entity-encoded, mixed-case scheme), plus the full-chain assertion `markdown_to_html(html_to_markdown(x))`. | ~70 lines |
| 0.2 | `fix/sec-02-auto-accept-identity-parsing` | SEC-02, TST-01b | `email.utils.parseaddr()` + case-insensitive `==` in `_rule_i_am_sender`, `_rule_i_am_sole_recipient`, `_rule_i_am_owner`/`_rule_created_by_me`. One parameterised test iterating every `_rule_*` method with lookalike/spoofed-display-name/lookalike-domain identities, asserting none match. | ~50 lines |
| 0.3 | `fix/sec-03-audit-export-formula-injection` | SEC-03, TST-01c | Add `_excel_literal()` in `audit_log.py`, apply to `summary`, `sender`, `pii_match_details`, `claude_reason` before `ws.append`. Parameterised test over `=`, `+`, `-`, `@`, tab-prefixed payloads in each of the four columns, asserting literal string round-trips through the loaded workbook. | ~35 lines |
| 0.4 | `fix/sec-04-org-config-fail-closed` | SEC-04 | Replace the two-state (`{}`/parsed) config load in `daemon_main.py`/`org_mode.py` with three states: absent → local; valid → as configured; unreadable/malformed/non-object/incomplete-IdP-or-server → raise `ConfigurationError` and refuse to start. Tests for all 7 cases the review lists (absent, valid-no-mode, explicit-org, malformed JSON, unreadable, non-object, org-mode-incomplete-IdP-or-server). | ~40 lines |
| 0.5 | `docs/doc-01-security-compliance-rewrite-pass1` | DOC-01 | Rewrite `docs/security-and-compliance.md` items A, B, C, E, G from the current architecture (org-mode HTTPS/OIDC support, web-only approval UI, org-mode auth description, actual storage format/permissions). Item D (token semantics) documented as a known limitation with a forward reference to the SEC-06 fix, not claimed as already fixed. | 1 day |

**Phase 0 exit criteria:** the four release-acceptance-criteria bullets under
"No externally influenced string reaches an HTML `href` or a spreadsheet
cell without neutralisation," "No identity rule matches a spoofed or
lookalike value," and "Malformed organization configuration fails startup"
are met; `security-and-compliance.md` no longer contains the errors flagged
DOC-01 A/B/C/E/G.

---

## Phase 1 — Organization-mode trust & credential hardening

Gates treating organization mode as enterprise-production-ready. Can start
in parallel with late Phase 0 items once 0.4 (org config fail-closed) is
merged, since several of these build on that code path.

| # | Branch | Finding(s) | Scope | Effort |
|---|---|---|---|---|
| 1.1 | `fix/sec-05-org-bundle-hash-logging` | SEC-05 (interim) | Record a hash of the installed `org_config.json` in the audit log at every daemon startup, so tampering is detectable even before it's preventable. See note above — swap for full signing if preferred. | ~1 day |
| 1.1b | `feature/sec-05-org-bundle-signing` | SEC-05 (full) | Sign the bundle at build time in `scripts/build_org_bundle.py`; verify against a pinned public key in `settings_controller.py` at install **and** at every daemon start in org mode; refuse to start on verification failure. | ~1 week |
| 1.2 | `fix/sec-06-browser-token-bootstrap-flow` | SEC-06 | Replace persistent URL-carried token with: short-lived single-use bootstrap code → `?bootstrap=<code>` → validate/invalidate → mint independent random session ID → redirect without query credentials → cookie with idle + absolute expiry. Stop logging the token in the "Web approval UI active" line. Rotate the existing `web_token` on upgrade. | ~2-3 days |
| 1.3 | `fix/sec-07-privacy-filter-fail-closed` | SEC-07 | Schema-validate the privacy-filter settings file at startup; reject invalid `default_policy` values and malformed groups instead of falling back to `allow`; keep documented backward-compat only for a genuinely absent group on older installs; prefer `block` as the org-managed fail-safe default. | ~1 day |
| 1.4 | `fix/sec-09-secure-file-writes` | SEC-09 | Shared `secure_mkdir(path, 0o700)` / `atomic_write_text` / `atomic_write_json` helper (temp file `O_CREAT|O_EXCL,0o600` in the same dir → write → fsync → `os.replace`). Apply at all 11 token-writer call sites, `oauth_clients.json`, `settings.yaml`, `org_config.json`, and to `data_dir()`/`org_dir()`/`user_dir()` creation. Raise the swallowed `chmod` failure to `warning`. Startup check that warns (org mode: fails) on overly broad permissions. | ~2 days |
| 1.5 | `fix/sec-11-oidc-discovery-trust-validation` | SEC-11 | Verify discovery document's `issuer` equals the configured issuer; require HTTPS on org-mode issuer/token/JWKS endpoints (dev-only override); validate metadata shape before constructing `IdpConfig`. | ~1 day |
| 1.6 | `fix/sec-12-sec-13-absolute-token-session-lifetimes` | SEC-12, SEC-13 | Add issuance timestamp + absolute-expiry check to refresh tokens (`oauth_provider.py`) and browser sessions (`org_session.py`); keep rotation and the sliding idle timeout as secondary checks; expire on whichever threshold hits first. | ~1 day |
| 1.7 | `fix/sec-10-safe-error-taxonomy` | SEC-10 | Typed exception → public-message mapping at the MCP boundary in `routes_mcp.py`; sanitizing log formatter that redacts token-shaped strings for the detailed diagnostic that still goes to local logs. Tests inject exceptions carrying fake secrets and assert none reach the MCP result. | ~1 day |
| 1.8 | `fix/sec-15-per-principal-approval-cap` | SEC-15 | Count live pending approvals per `principal_id` in `approvals.py`, keep the global figure as secondary backstop. | ~0.5 day |
| 1.9 | `tests/tst-02-mcp-tools-coverage` | TST-02 | New `tests/unit/web/test_mcp_tools.py`: tool schema assertions, `begin_unattended_session` refused when disabled, `propose_auto_accept_rule_change` denied inside an unattended session, `list_auto_accept_rules` disclosure is audited. | ~150 lines |
| 1.10 | `docs/sec-20-security-md` | SEC-20 | Add root `SECURITY.md`: supported versions, private reporting address, report contents expected, acknowledgement/triage expectations, disclosure process, GitHub private vulnerability reporting status. | ~0.5 day |
| 1.11 | `docs/doc-01-security-compliance-rewrite-pass2` | DOC-01 (follow-up) | Update the token-semantics section of `security-and-compliance.md` to describe the post-SEC-06 bootstrap flow. | ~1 hour |

**Phase 1 exit criteria:** the review's "Security" release-acceptance
checklist items on org-config authenticity, secrets never in logs, closed
privacy-policy failure, absolute token/session lifetimes, no raw exceptions
to the MCP client, and atomic `0600`-in-`0700` credential files are all met.

---

## Phase 2 — Test and quality gates

Independent of Phase 1; can run in parallel. This is what makes Phase 0/1's
fixes durable and catches the *next* instance of the same defect class.

| # | Branch | Finding(s) | Scope | Effort |
|---|---|---|---|---|
| 2.1 | `chore/tst-03-coverage-ratchet` | TST-03 | Turn on `--cov-branch`; record current line+branch coverage as the initial `--cov-fail-under` floor (overall, plus a higher floor for the security-critical module list in the review's TST-03 table); publish the coverage report as a CI artifact. | ~1 day |
| 2.2 | `chore/tst-05-python-version-matrix` | TST-05 | CI matrix `["3.11", "3.12", "3.13"]`; full suite on 3.13, reduced core suite on 3.11/3.12 if CI cost matters. | ~0.5 day |
| 2.3 | `chore/tst-07-static-analysis-gate` | TST-07 | Add `ruff` as a blocking CI step immediately; `mypy`/`pyright` incrementally (non-blocking → blocking per module); `bandit` or `semgrep` for security-focused rules. | ~2 days (ruff) + ongoing |
| 2.4 | `chore/sec-19-supply-chain-gates` | SEC-19 | Dependency audit (`pip-audit`/OSV-Scanner, `npm audit --omit=dev`) with a defined severity policy; SHA-pin GitHub Actions with Dependabot/Renovate updating them; add `.github/dependabot.yml`; produce an SBOM (CycloneDX/SPDX) per release; add a locked dependency set with hashes. | ~2-3 days |
| 2.5 | `tests/tst-06-browser-smoke-suite` | TST-06 | Small always-on Playwright suite: bootstrap+login, Allow/Deny, CSRF rejection, approval-list refresh, WebAuthn UI error handling with mock authenticator, no-inline-script CSP check (once SEC-08 lands), token absent from URL post-bootstrap, PDF preview actually renders. | ~2-3 days |
| 2.6 | `tests/tst-04-session-auth-coverage` | TST-04 | `tests/unit/web/test_session_auth.py` covering `authenticated()`'s `?token=` path, `check_origin()`'s missing-Origin behaviour, constant-time comparison, `set_session_cookie` flag correctness in both modes. | ~80 lines |

**Phase 2 exit criteria:** the review's "Build and test" release-acceptance
checklist is fully met.

---

## Phase 3 — Hardening, cleanup and assurance

Lower individual severity, but real — and this is where the accumulated
assurance-drift (§14) gets closed out so it doesn't recur.

| # | Branch | Finding(s) | Scope |
|---|---|---|---|
| 3.1 | `fix/sec-08-csp-hardening` | SEC-08 | **First**, manually verify the PDF preview against a real browser (currently likely broken — no test catches this). Then migrate `script-src`/`style-src` off `unsafe-inline` to nonces; set `object-src` explicitly once the PDF path is resolved; correct the now-inaccurate CSP source comment; fix `_SecurityHeadersMiddleware`'s header-extend-vs-replace behaviour. |
| 3.2 | `fix/sec-14-attachment-parsing-dos` | SEC-14 | Check `ZipInfo.file_size` against a cap before `zf.read()`; bounded-stream read; swap `xml.etree.ElementTree` for `defusedxml.ElementTree`. |
| 3.3 | `fix/sec-16-dcr-resource-controls` | SEC-16 | Total client cap, registration-size validation, stale-client pruning where MCP compatibility allows, count-bound `_pending` inside a TTL window, reverse-proxy rate-limiting guidance in the org setup guide. |
| 3.4 | `fix/sec-17-ipv6-host-parsing` | SEC-17 | Standards-aware Host header parsing (not manual colon-split); tests for bare `localhost`, IPv4±port, IPv6 literal ±port, malformed Host, the public org issuer host. |
| 3.5 | `fix/sec-18-security-headers` | SEC-18 | Emit `Strict-Transport-Security` when `mode=org`; add `Permissions-Policy`, `Cross-Origin-Opener-Policy` where compatible; verify `Cache-Control: no-store` on every sensitive page via test, not just the routes that already set it. |
| 3.6 | `feature/sec-23-audit-tamper-evidence` | SEC-23 | Append-integrity (HMAC or hash chaining) on the JSONL audit log; centralized forwarding for org mode (syslog/OTLP/SIEM); explicit versioned event schema; stable event IDs; deployment identifier and security-config version/hash per decision. Largest item in this phase — may warrant its own sub-plan. |
| 3.7 | `feature/sec-22-app-level-authz-policy` | SEC-22 | Optional PrivacyFence-level allowlist (`allowed_domains`/`required_groups`) layered on top of IdP auth in org mode. |
| 3.8 | `docs/post-p10-comment-and-doc-sweep` | §14.2, DOC-02, DOC-03, DOC-04 | Repo-wide sweep for the terms the review lists (`menu bar`, `native popup`, `AppKit`, `ipc_server.py`, `bridge`, `per-launch`, etc.); fix `gate.py`'s "Set by `ipc_server.py`" comments to point at `web/mcp_dispatch.py`; fix `org_session.py`'s misleading docstring (review flags this as the single highest-risk sentence to get wrong first); correct the CSP comment (folds into 3.1); README menu-bar instructions; `build_org_bundle.py` vs org-setup-guide Google client-type mismatch (DOC-03); correct the testing-policy module-mapping claim (DOC-04, TST-14). |
| 3.9 | `chore/orphan-and-dormant-code-cleanup` | §13 (ORP-01..06) | Delete `SlackDirectoryUnavailable`; remove `macos-native` extra (document the old dependency set in an ADR first); deprecation-warn then remove `rule_suggestion_priority`; fix the stale `tests/integration/test_bridge_daemon_contract.py` reference in `pyproject.toml`; decide on migrating vs. documenting the retired "bridge proposal" audit-vocabulary strings (review recommends documenting, not migrating, since it's stored data). |
| 3.10 | `tests/tst-15-macos-packaged-smoke` | TST-15 | Release-workflow smoke test against the packaged `.dmg`/app bundle: install, start daemon, connect via MCP shim, open approval UI, one synthetic Allow/Deny round trip. |
| 3.11 | `tests/tst-16-ubuntu-org-smoke` | TST-16 | Release-workflow smoke test for the Ubuntu org-mode service: strict startup, reverse-proxy/Host handling, route mounting, per-principal session creation, MCP OAuth discovery, clean shutdown/restart, using a synthetic org config and mocked IdP boundary. |
| 3.12 | `tests/tst-08-through-13-remaining-test-depth` | TST-08, TST-09, TST-10, TST-11, TST-12, TST-13 | Record fixtures for the highest-risk read path per connector + CI guard on fixture presence (TST-08); one end-to-end deferred-approval-round-trip integration test (TST-09); explicit cross-principal step-up-binding tests in `routes_security.py` (TST-10); replace fixed `sleep`s with `Event.wait` + per-test timeout markers on genuinely timing-dependent tests (TST-11); `hypothesis` round-trip property tests on the four parsers, especially the `html_to_text` → `markdown_to_html` chain (TST-12); extend the parameterised-invariant pattern to the other systemic checks the review names — `reason` param present on every gated tool, `pii_scan_text` passed by every `review`-gated tool, all 11 token sites using the same secure-write helper (TST-13). |
| 3.13 | `docs/org-mode-operational-readiness` | §14.3 | Document org-mode's support/readiness level explicitly (production-supported-but-centrally-managed vs. preview vs. incomplete); backup scope and sensitive-material handling; restore procedure; upgrade/rollback; persisted-state compatibility across versions; restart/session-invalidation behaviour; single-daemon availability model; give the 41-tool ungated (`auto`) tier its own documented section in the privacy matrix, per §14.3. |

---

## Suggested execution order

```
Phase 0 ──────────────────────────────────────▶ ship this week
   │
   ├─▶ Phase 1 (org-mode hardening)  ─────────▶ before calling org mode
   │                                             enterprise-production-ready
   └─▶ Phase 2 (test/quality gates)  ─────────▶ parallel with Phase 1,
                                                  independent team/session

Phase 3 (hardening + cleanup) ─────────────────▶ after 0-2, ongoing;
                                                   3.1's PDF-preview check
                                                   and 3.8's org_session.py
                                                   docstring fix are the two
                                                   highest-value items to
                                                   pull forward if time is
                                                   short, since one is a
                                                   likely live functional
                                                   bug and the other is
                                                   flagged by the review as
                                                   the single most
                                                   misleading sentence in
                                                   the repo for an external
                                                   reviewer to trip over.
```

---

## Coverage matrix

Every finding ID from the review's Appendix A, mapped to where it's handled.
Used to confirm nothing was dropped when converting the review into this
plan.

| ID | Phase item | ID | Phase item | ID | Phase item |
|---|---|---|---|---|---|
| SEC-01 | 0.1 | SEC-09 | 1.4 | SEC-17 | 3.4 |
| SEC-02 | 0.2 | SEC-10 | 1.7 | SEC-18 | 3.5 |
| SEC-03 | 0.3 | SEC-11 | 1.5 | SEC-19 | 2.4 |
| SEC-04 | 0.4 | SEC-12 | 1.6 | SEC-20 | 1.10 |
| SEC-05 | 1.1 / 1.1b | SEC-13 | 1.6 | SEC-21 | 3.8 (documented, no code change — see note below) |
| SEC-06 | 1.2 | SEC-14 | 3.2 | SEC-22 | 3.7 |
| SEC-07 | 1.3 | SEC-15 | 1.8 | SEC-23 | 3.6 |
| SEC-08 | 3.1 | SEC-16 | 3.3 | | |
| TST-01 | 0.1/0.2/0.3 | TST-07 | 2.3 | TST-13 | 3.12 |
| TST-02 | 1.9 | TST-08 | 3.12 | TST-14 | 3.8 |
| TST-03 | 2.1 | TST-09 | 3.12 | TST-15 | 3.10 |
| TST-04 | 2.6 | TST-10 | 3.12 | TST-16 | 3.11 |
| TST-05 | 2.2 | TST-11 | 3.12 | | |
| TST-06 | 2.5 | TST-12 | 3.12 | | |
| DOC-01 | 0.5 / 1.11 | DOC-03 | 3.8 | ORP-01..04, ORP-06 | 3.9 |
| DOC-02 | 3.8 | DOC-04 | 3.8 | ORP-05 | 3.9 |

**SEC-21** (shared OAuth client secrets distributed to every endpoint) is
Info-severity and the review's own recommendation is to *document* the
blast radius and rotation procedure, not change the code — folded into the
3.8 documentation sweep rather than given its own branch.

---

## Notes on scope not in the source review

Nothing added here — this plan is deliberately scoped to exactly the
findings in the review. If new issues turn up while implementing (e.g. a
Phase 0 fix reveals a related bug), raise those separately rather than
silently expanding this plan's scope.
