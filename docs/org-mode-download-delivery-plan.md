# Org-Mode Download Delivery — Implementation Plan

Implementation plan for the bug where `drive_download_file`, `gmail_download_attachment` and
`confluence_download_attachment` write file bytes straight to a `destination_dir` path on whatever
filesystem the PrivacyFence **daemon** is running on. In `mode: local` that's the same Mac the human
and Claude are using, so `~/Downloads` resolves correctly. In `mode: org` (`docs/org-mode-setup-guide.md`)
the daemon runs headless on a shared server the human never has a shell on — `~/Downloads` there
resolves under the `privacyfence` system user's home directory on that server, and the file is gone
as far as the person who asked for it is concerned. All three connectors' own gate preview says so
explicitly today (`connectors/drive.py`'s `new_info`): `"Content returned to Claude": "None — file
bytes are never sent"` — that line is accurate for local mode and actively misleading for org mode,
since the "Saved to" path it sits next to isn't reachable by the human either.

This plan implements the combination of options #1 (return small files' bytes inline in the MCP
tool result) and #3 (stage larger files server-side, per-principal, behind a signed-in-browser
download link) from the discussion that preceded this doc. **Local mode is untouched throughout** —
every phase below is additive and gated on `org_mode.resolve_mode(org_config) == "org"`, so a local
install stays byte-identical.

## The trade-off this plan makes explicit, up front

`drive_client.py`'s `download_file` and its two siblings were written around a deliberate privacy
invariant: file bytes never enter Claude's context at all, only a path and metadata do (see the
`connectors/drive.py` comment above, and `docs/coding-and-testing-guidelines.md` §1.5's "preview
dicts carry metadata only" rule, which this satisfies today for the file's *content* by never
routing it through the model in the first place). **Option #1 (inline base64 in the tool result)
necessarily breaks that invariant for org mode** — the whole point is that the model's own MCP
client is the transport back to the human, so the bytes pass through the model's context to get
there. Option #3 (staged file + authenticated link the human's browser fetches directly) preserves
the original invariant exactly — Claude only ever sees a URL, never file content — at the cost of
needing an authenticated route and being unusable for a non-browser MCP client.

Given that, this plan treats **#3 as the default/primary path** and **#1 as a bounded fallback**,
not two equally-weighted options:

- Small files (below a configurable cap) get delivered inline (#1) *only* because forcing every
  small attachment through a browser click is worse UX for something like a 40KB PDF, and the
  exposure (file content the human already approved via the existing gate, now also visible to the
  model) is bounded by size.
- Everything else — and everything at all, for an org that wants the stricter original posture back
  — goes through the staged link (#3).
- The choice and its size cap are both configurable per-org (§3 below) so an org that wants the
  original "never touches Claude" guarantee unconditionally can set the inline cap to `0`.

This is a security posture change from what `docs/security-and-compliance.md` documents today and
must be called out there, not just in code comments — see Phase 4.

---

## Phase 1 — Staging infrastructure (no connector behavior change yet)

Branch: `feature/org-download-staging`. Lands dead code (nothing calls it yet) so it can be reviewed
and merged on its own without touching any connector's live behavior.

1. **`src/privacyfence/paths.py`**: add `downloads_dir(principal: Principal | None = None) -> Path`
   returning `user_dir(principal) / "downloads"`, created on demand — same pattern as `org_dir()`.
   Reuses the existing per-principal root, so this needs no new directory-safety logic beyond what
   `user_dir()` already enforces (`_is_safe_principal_id`).

2. **`src/privacyfence/download_staging.py`** (new module): an in-memory registry mirroring
   `approvals.py`'s `PendingApprovalRegistry` shape (same TTL-sweep pattern, same "ephemeral state
   lost on daemon restart is acceptable" posture — an interrupted download just means the human
   re-runs the tool call).
   - `StagedDownload` dataclass: `token: str`, `principal_id: str`, `path: Path`, `name: str`,
     `size_bytes: int`, `mime_type: str`, `created_at: float`, `expires_at: float`,
     `claimed_at: float | None`.
   - `DownloadStagingStore`:
     - `stage(principal: Principal, data: bytes, name: str, mime_type: str, *, ttl_seconds: float) -> StagedDownload` —
       token via `secrets.token_urlsafe(32)` (unguessable, unlike `uuid4` used for approval IDs
       elsewhere — this token is a bearer credential for file content, so it gets the stronger
       generator). Writes under `paths.downloads_dir(principal) / f"{token}-{safe_name}"`, reusing
       the existing `os.path.basename(...) or "file"` sanitization pattern
       (`docs/coding-and-testing-guidelines.md` §1.6) for `safe_name`.
     - `claim(token: str, principal_id: str) -> tuple[StagedDownload, bytes] | None` — returns
       `None` on missing/expired/wrong-principal token (the wrong-principal check is
       defense-in-depth; the route's own auth is the primary guard — see Phase 2). Reads the file,
       deletes it from disk, and removes the registry entry on a successful claim (files are
       single-use; nothing meant to be downloaded once should still be sitting on the server after
       it was). Marks `claimed_at` rather than deleting immediately if you'd rather keep a short
       grace window for a client retry after a network blip — start with immediate delete-on-claim
       (simpler, matches "one-time link" framing in the tool description) and revisit only if QA
       against `qa-environment-setup.md` surfaces retry issues.
     - A `_sweep_expired_locked()` matching `approvals.py`'s `_expire_stale_locked()`, called
       opportunistically on `stage`/`claim` (no separate timer thread) — deletes both the registry
       entry and the orphaned file for anything past `expires_at` that was never claimed.
     - Module-level singleton (`_INSTANCE` + `get_download_staging_store()`) like `audit_log.py`'s
       and `auto_accept.py`'s own singletons — **must** get a reset added to `tests/conftest.py`'s
       autouse fixture (coding-and-testing-guidelines §2.3) or state leaks across tests.

3. **`src/privacyfence/web/routes_downloads.py`** (new): one route, `GET /downloads/{token}`,
   mounted only when `mode == "org"` (mirrors the `web/server.py` comment about mounting
   `routes_connect.py`'s routes conditionally). Auth: reuse `org_session.authenticated(request,
   sessions)` exactly as `routes_connect.py` does — no new auth mechanism.
   - Resolve principal from the `pf_org_session` cookie; 401/redirect-to-`/login` if absent, same as
     every other org-mode browser route.
   - `store.claim(token, principal.id)`; `404` if `None` (covers missing, expired, and
     wrong-principal in one branch — deliberately not distinguishing "expired" from "not yours" in
     the response, so the endpoint doesn't leak which case applies to an attacker guessing tokens).
   - On success: stream the bytes back with `Content-Disposition: attachment; filename="<name>"`
     and the stored `mime_type`, and write one audit-log entry (`kind="download_claimed"` or similar
     — see Phase 3) since this is the moment file content actually left the server.
   - **CSRF/cross-site GET note**: this is a state-changing GET (it deletes the file on success), the
     same shape of risk `org_session.check_origin` exists to cover for other org-mode routes. Add an
     `check_origin(request)` check here too, tolerant of it failing open only the way
     `routes_connect.py` already tolerates it (check that file's own handling before copying the
     pattern) — token unguessability (`secrets.token_urlsafe(32)`, never logged, only ever
     transmitted once inside a tool result) is the primary defense either way, `check_origin` is
     belt-and-suspenders.

4. **Config knobs** in `org_mode.py`, following `StepUpConfig.from_org_config`'s existing pattern
   for an optional org-config section with defaults:
   - `DownloadDeliveryConfig.inline_max_bytes` (default `1_000_000` — deliberately smaller than the
     existing `_ATTACHMENT_PREFETCH_MAX_BYTES` / `_UPLOAD_PREVIEW_MAX_BYTES` 5MB constants already in
     the three connectors, since *this* cap gates what reaches the model's context, not just a
     PII-scan prefetch).
   - `DownloadDeliveryConfig.link_ttl_seconds` (default `900` — 15 minutes, long enough to switch to
     a browser tab and click, short enough that a leaked link in a log or transcript is stale fast).
   - Setting `inline_max_bytes: 0` disables inline delivery entirely for that org (every download
     goes through the staged link) — this is the knob an org picks to keep the original "never
     touches Claude" guarantee unconditionally, per the trade-off section above.
   - `scripts/build_org_bundle.py` gets two matching optional flags
     (`--downloads-inline-max-bytes`, `--downloads-link-ttl-seconds`); omitting them keeps today's
     bundles valid with no re-signing forced, same as `StepUpConfig`'s own optionality.

5. **Tests** (`tests/unit/test_download_staging.py`, `tests/unit/web/test_routes_downloads.py`):
   - Stage → claim round-trip returns the right bytes/name/mime type; file is gone from disk after.
   - Expired token → `claim` returns `None`; orphaned file swept.
   - Wrong-principal claim → `None`, even before expiry.
   - Route: no cookie → unauthenticated response; valid cookie + valid token → 200 with correct
     headers and the file deleted afterward; valid cookie + someone else's token → 404; valid cookie
     + already-claimed token → 404.
   - Route only mounted in org mode — assert it's absent from the local-mode app (mirrors how
     `test_server_org_mode.py` presumably already asserts other org-only routes' presence/absence;
     check that file for the exact assertion style before writing a new one).

Nothing in Phase 1 is reachable from any tool yet — safe to merge standalone.

---

## Phase 2 — Wire the three connectors

One PR per connector keeps each change small and independently revertable; all three follow the
identical shape, so write Drive first and the other two are close ports.

### PR 2.1 — Drive (`feature/org-download-delivery-drive`)

- `daemon_main.py::build_connectors` already sets `connector.my_email = email` post-construction
  for Drive; add `connector.download_mode = resolve_mode(org_config)` and, when org,
  `connector.download_config = DownloadDeliveryConfig.from_org_config(org_config)` and
  `connector.download_base_url = server_config.issuer_url` (needed to build a fully-qualified
  `https://pf.example.com/downloads/<token>` URL — a bare `/downloads/<token>` string means nothing
  outside a browser already on that origin, and the tool result is plain text handed to a model).
- `connectors/drive.py::_download_file`: after the existing `gated_call` (unchanged — the human
  still approves the same preview before any bytes move), branch on `self.download_mode`:
  - `"local"`: unchanged — call `self._drive.download_file(file_id, destination_dir)` exactly as
    today.
  - `"org"`: fetch bytes via the client's existing internal byte-fetch path (`drive_client.py`
    already has `_download`/`get_file_content` used for the PII-scan prefetch above this call —
    reuse the same call rather than adding a second HTTP round trip) instead of ever calling
    `open(dest_path, "wb")`. Then:
    - `size_bytes <= self.download_config.inline_max_bytes` (and `inline_max_bytes > 0`): return
      `{"delivery": "inline", "name": name, "mime_type": mime_type, "size_bytes": size_bytes,
      "content_base64": base64.b64encode(data).decode("ascii")}`.
    - otherwise: `staged = get_download_staging_store().stage(principal, data, name, mime_type,
      ttl_seconds=self.download_config.link_ttl_seconds)`; return `{"delivery": "link", "name":
      name, "size_bytes": size_bytes, "download_url": f"{self.download_base_url}/downloads/
      {staged.token}", "expires_at": _iso(staged.expires_at)}`.
  - `destination_dir` stays a tool parameter (existing MCP clients/tests don't break), but becomes
    **ignored, with a note in the tool description**, when `download_mode == "org"` — it was never
    meaningful there in the first place; nothing in org mode should keep asking the model to guess a
    path that doesn't correspond to anything.
- Update the `drive_download_file` tool description (`connectors/drive.py`, `~line 316`) to state
  the org-mode behavior plainly: small files come back to you directly so you can hand them to the
  human; larger files come back as a one-time link that opens in the human's own signed-in browser
  tab — `destination_dir` is only honored in local-mode installs.
- **Gate preview honesty** (`gate.py`'s `new_info`, coding-and-testing-guidelines §1.5): this is the
  one place the existing wording is now sometimes false and must become mode/delivery-conditional:
  - local: unchanged — `"Content returned to Claude": "None — file bytes are never sent"`.
  - org + inline: `"Content returned to Claude": "Yes — file bytes are included in the tool result
    (file is <size>, under this org's <cap> inline-delivery limit)"`.
  - org + link: `"Content returned to Claude": "None — a one-time link is generated for you to open
    in your own browser"`.
  This is disclosure of *whether* content flows to the model, not the content itself — still
  satisfies "preview dicts carry metadata only."
- Tests (`tests/unit/connectors/test_drive_connector.py`): add a `TestOrgModeDownloadDelivery` class
  (or similar) with cases for inline (small file → `content_base64` present, nothing staged, nothing
  written to `destination_dir`), staged (large file → `download_url` present, no `content_base64`,
  staging store has exactly one entry for the right principal), and a **regression pin** proving
  local mode's return shape and disk write are byte-identical to before this change (coding-and-testing-guidelines
  §2.2's "regression tests carry a docstring explaining the original bug" convention — this test's
  docstring should say what it's pinning and why).

### PR 2.2 — Gmail (`feature/org-download-delivery-gmail`)

Same shape against `gmail_client.py::download_attachment` / `connectors/gmail.py::_download_attachment`.
Gmail already has its own `_ATTACHMENT_PREFETCH_MAX_BYTES`-gated byte-prefetch for the PII scan —
reuse those bytes for inline delivery when they were already fetched and are within
`inline_max_bytes`, rather than fetching twice.

### PR 2.3 — Confluence (`feature/org-download-delivery-confluence`)

Same shape against `confluence_client.py::download_attachment` / `connectors/confluence.py::_download_attachment`.
Note from that file's own comments: Confluence's attachment download has no partial/range fetch, so
there's no analogous "reuse the prefetch" shortcut — a full fetch happens either way; this PR only
changes what happens to the bytes afterward (write-to-disk vs. inline-or-staged).

---

## Phase 3 — Audit trail

`docs/coding-and-testing-guidelines.md` §1.5: every tool call leaves an audit trail either via
`gated_call` (already true here — unchanged) or an explicit auto-audit. What's new is that *which
delivery path was used* is itself a fact worth auditing, since "did file content actually reach the
model" is a materially different privacy event from "did it stay server-side" — right now that fact
would only be recoverable by cross-referencing tool-call args, which isn't what the audit log is for.

- Add a `delivery` field (`"local_disk" | "inline_base64" | "staged_link"`) to the audit entry
  `gated_call` already writes for these three tools (`audit_log.py::AuditEntry` — check whether this
  fits as a new optional field or belongs in the entry's existing free-form details; match whichever
  convention the rest of `AuditEntry` already uses for tool-specific extras).
- Add a second, un-gated audit entry at the moment `routes_downloads.py` actually serves a staged
  file (Phase 1, item 3) — this is the point file bytes leave the server for a staged-link download,
  and today nothing records that it happened at all, let alone when, versus when the tool call that
  generated the link ran. Follow `gate.py::_audit`'s existing "wrap in try/except, never block the
  primary operation" pattern (§1.4) for this one, same as everywhere else non-critical logging
  happens.
- No new audit entry is needed for the inline-delivery moment itself — that's already covered by the
  existing `gated_call` entry, since (unlike the staged case) content delivery and tool-call
  completion are the same event.

---

## Phase 4 — Docs

- **`docs/security-and-compliance.md`**: the table/rows describing "file bytes are never sent to
  Claude" need a mode-conditional rewrite — state plainly that this holds unconditionally in local
  mode and in org mode's staged-link path, but not in org mode's inline path below
  `inline_max_bytes`, and that an org can restore the unconditional guarantee by setting
  `inline_max_bytes: 0`. This is exactly the kind of deliberate-trade-off documentation
  `security-remediation-plan.md`'s own Phase 0 (DOC-01) treats as a release-blocker-grade fix, not
  an afterthought — don't let this land as a code comment only.
- **`docs/org-mode-setup-guide.md`**: short new subsection (under "10. Day-to-day admin" or its own
  numbered section) explaining the two delivery paths from an admin's point of view, and the two new
  `build_org_bundle.py` flags from Phase 1.
- **`docs/file-type-support.md`**: check whether it documents the existing 5MB prefetch caps; if so,
  add the new `inline_max_bytes` cap alongside them so the size-limit story stays in one place.

---

## Sequencing summary

| PR | Branch | Depends on | Ships |
|----|--------|-----------|-------|
| 1 | `feature/org-download-staging` | — | Staging store, `/downloads/{token}` route, config knobs, tests. Dead code — no connector calls it yet. |
| 2.1 | `feature/org-download-delivery-drive` | 1 | Drive wired to inline/staged delivery in org mode + tests + tool description + gate preview wording. |
| 2.2 | `feature/org-download-delivery-gmail` | 1 | Same for Gmail. |
| 2.3 | `feature/org-download-delivery-confluence` | 1 | Same for Confluence. |
| 3 | (folded into 2.1–2.3, or its own `feature/org-download-audit-trail` if the `AuditEntry` shape change is non-trivial) | 2.1–2.3 | Delivery-path audit fields + staged-download-served audit entry. |
| 4 | `chore/org-download-docs` | 2.1–2.3 | `security-and-compliance.md`, `org-mode-setup-guide.md`, `file-type-support.md` updates. |

Each PR should independently satisfy `docs/coding-and-testing-guidelines.md` §2.7's definition of
done (100% test pass, every gated tool call still audited, no content in a preview dict, new
`*ClientError`/`RuntimeError` boundary respected where touched, new singleton reset registered in
`tests/conftest.py`).

## Open questions to confirm before starting Phase 2

1. **Default `inline_max_bytes`.** This plan proposes 1MB as a conservative starting point (smaller
   than the existing 5MB prefetch caps). Confirm that's the right default rather than something
   org-size-dependent, or 0 (staged-link-only by default, opt-in to inline).
2. **Claim semantics.** Immediate delete-on-first-successful-claim (proposed above) vs. a short
   grace window allowing one retry after a network blip — worth deciding before writing the route's
   tests, since the test assertions differ.
3. **Whether Phase 3's `AuditEntry` change is additive-only** (safe to fold into 2.1–2.3) or touches
   enough of `audit_log.py`'s existing shape (Excel export columns, etc.) to warrant its own
   reviewed PR — worth a quick look at `audit_log.py` before committing to the folded-in option in
   the table above.
