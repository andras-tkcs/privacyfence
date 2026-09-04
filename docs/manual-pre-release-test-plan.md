# Manual pre-release test plan

A durable checklist to run through before cutting any release, on top of the automated CI suite
(`docs/testing-policy.md` §1). None of this runs in CI — it needs real accounts, a real screen,
and a human watching. Budget roughly half a day the first time; faster once the environment
fixtures (`docs/qa-environment-setup.md`) already exist and just need re-confirming.

Do these in order — each step's prerequisites are satisfied by the one before it.

## 0. Before you start

- [ ] Run `.venv/bin/python scripts/pre_release_check.py` from the repo root. It reruns CI's
      automated suite (`pytest`, `npm test`, `npm run typecheck`) locally and checks that
      `pyproject.toml`'s version and `src/privacyfence/__init__.py`'s `__version__` agree — so a
      broken build or a stale version string fails fast, before sinking time into the manual
      sections below. It does not replace any section below; nothing in it needs a real screen or
      real accounts.
- [ ] Confirm you're on Account 1 ("developer/test") per `docs/dev-vs-live-setup.md`, running from
      source via `scripts/dev_start.sh`, not a bundled install — this plan's paths assume that.
- [ ] Confirm `tests/fixtures/qa_environment.yaml` exists and is filled in (copy from
      `tests/fixtures/qa_environment.yaml.example` and work through `docs/qa-environment-setup.md`
      if this is the first release on a fresh environment, or if any fixture named in that file was
      renamed or deleted since the last release).
- [ ] Confirm every connector you're about to test is authenticated from PrivacyFence Settings.

## 1. Fixture recording / refresh check

Goal: confirm the recorded live fixtures under `tests/fixtures/live/<connector>/*.json` — which CI
replays on every PR — still match what each provider's API actually returns right now, not just
what they returned whenever they were last recorded.

- [ ] Run the recorder in check-only mode against every connector (no file writes):
      ```bash
      .venv/bin/python scripts/qa_fixture_recorder.py --check
      ```
      (omit connector names to run all ten: confluence, jira, salesforce, gmail, drive, calendar,
      contacts, tasks, slack, telegram)
- [ ] Read the printed report's fixture-freshness lines (one per connector) — if any connector
      shows well over 90 days since it was last recorded, treat that as due for a refresh even if
      `--check` passed: a passing check only proves today's shape parses, not that it's still
      representative of what CI's fixture replay has been protecting against drifting silently.
- [ ] For any row that fails, or any connector you're refreshing on age grounds, run:
      ```bash
      .venv/bin/python scripts/qa_fixture_recorder.py --record <connector>
      ```
      then diff `tests/fixtures/live/<connector>/*.json` — confirm the diff is a small, meaningful
      shape change with identity fields already redacted to placeholders (`qa-placeholder-*`,
      `example.com`, etc.). If anything in the diff looks like a real email, name, or
      account-specific id, stop — that's a redaction bug, not something to commit around.
- [ ] Commit any updated fixtures as their own small commit (or alongside the client fix that
      caused the shape change) before continuing — a release should not ship with an unexplained
      fixture diff sitting uncommitted.
- [ ] Save the full report (`--report-file`) and keep it for the release PR description.

## 2. QA web smoke test

Goal: confirm the real embedded web approval/settings surface still works end to end in a real
browser — a real click on a real on-screen button actually resolves the card and returns to the
list — which construction-only unit tests (`test_approval_window_html.py`, `tests/unit/web/`) don't
cover. Through P9 this section drove the native macOS popup's modal loop instead
(`qa_popup_smoke.py`); P10 deleted that surface, so this is now the whole of tier 2 — see
`docs/testing-policy.md` §2.2.

- [ ] Run the full, automated smoke suite (needs `playwright`; installs its own headless Chromium if
      none is found):
      ```bash
      .venv/bin/pip install playwright
      .venv/bin/python scripts/qa_web_smoke.py --report-file /tmp/web_smoke_full.md
      ```
      Confirm the final line reads `5/5 scenarios passed` — settings page load/toggle round-trip,
      the approvals list's empty state, a pending row plus Deny-from-row, a card decide returning to
      the list with its toast (the regression this scenario exists for: a script-order bug that only
      a real browser catches), and the service worker registering under the page's real CSP.
- [ ] Open the real daemon's `/approvals` and `/settings` pages in a real desktop browser (not the
      headless one the script above drives) with at least one gated call pending, once in light mode
      and once in dark mode (your OS/browser's own appearance toggle) — confirm every card, the
      risk/PII banners, and the button row all render with readable contrast in both, since that's a
      visual judgment no automated test makes.
- [ ] Open the same two pages at a phone-width viewport (browser dev tools' device toolbar, ~375px,
      or a real phone on the same network) — confirm the WIDE card's two-column layout collapses to
      stacked sections with no horizontal scrolling (docs/https-connector-refactor-plan.md §7.3).

## 3. "QA prompt" manual test — representative live connector pass

Goal: exercise real approval popups end to end (silent auto-accept / popup / Deny / Always
allow) against real accounts through an actual Claude Cowork/Desktop session — the one thing that
proves the gate, the popup UI, and the audit log agree with each other, not just with themselves.

This plan does not replace running the full prompt in
[`connector-qa-testing.md`](connector-qa-testing.md) — do that in full before any release that
touched `gate.py`, `auto_accept.py`, `resource_grants.py`, or the web approval UI broadly
(per `docs/testing-policy.md` §3). For a release that didn't touch that logic, the shortened
version below (two connectors, one of each approval path) is the minimum bar — don't skip it
entirely.

- [ ] Confirm `privacyfence-app` is running from source (`scripts/dev_start.sh`), the
      `privacyfence` MCP server is attached to a Cowork/Desktop conversation, and that
      conversation's working context is this repo — ask Claude to read `config/settings.yaml` to
      confirm filesystem access before proceeding.
- [ ] **Gmail — one of each path.** Paste into the conversation:
      > Call `gmail_list_messages` (expect: silent, no prompt). Then pick any recent message and
      > call `gmail_get_message` on it — I'll click **Allow once**. Then pick a different short
      > message and call `gmail_get_message` on it again — this time tell me first, and wait for
      > me to say go, because I'm going to click **Deny**; confirm you get an error, not data. Then
      > call `gmail_add_label` on the first message with a fresh test label — tell me first,
      > because I'm going to click **Always allow**; confirm it proposes a `label_name_allowlist`
      > rule scoped to that exact label, not a broader one. Afterward, remove that rule again from
      > PrivacyFence Settings' **Auto-accept Rules → Gmail** page so it doesn't linger.
- [ ] **Slack — the auto-accept contrast.** Paste into the conversation:
      > Call `slack_get_channel_history` on `<approved channel from qa_environment.yaml>` — expect
      > silent, no prompt, since it's on the allowlist. Then call it again on a channel that isn't
      > approved — expect a review popup this time; I'll click **Allow once**. Then call
      > `slack_send_message` to my own self-DM with test text tagged `[release-smoke-<date>]` —
      > this is popup-gated regardless of any rule; I'll click **Allow once**.
- [ ] For each step above, confirm: the gate you expected (silent / popup) actually matched what
      happened, the popup rendered with real preview data (not blank/placeholder fields), and — if
      the PII detection gate fires unexpectedly on any real-account content mid-run — that's
      expected on real data (not a bug, per `connector-qa-testing.md`'s PII ground rule); only flag
      it if a **write** popup renders tinted, which should never happen.
- [ ] Note anything that didn't match expectations (wrong gate, missing preview field, popup didn't
      appear, error instead of data) as a release blocker, not something to quietly work around.

## 4. Dev vs. live mode switching

Goal: confirm the release candidate actually installs and runs cleanly as an end user would get
it, not just from source.

- [ ] Build the DMG: `bash scripts/build_dmg.sh`.
- [ ] On your live macOS account (`docs/dev-vs-live-setup.md`'s Account 2), install the freshly
      built DMG as a hand-test of the release candidate before it's published: `xattr -cr`, install
      the LaunchAgent plist, load it.
- [ ] From PrivacyFence Settings: install/update the organization config bundle, authenticate at least one
      connector, and confirm `PrivacyFence.mcpb` installs into Claude Desktop from the mounted DMG
      with no config file edited and no token copied — the shim discovers `~/.privacyfence/mcp_url`
      and `mcp_token` itself (D11, `docs/https-connector-refactor-plan.md` §12).
- [ ] Confirm `~/.privacyfence/` (not the repo folder) is where config/credentials/logs land for
      this bundled install — spot-check `~/.privacyfence/config/settings.yaml` exists.
- [ ] Exercise one silent-gate call and one popup-gate call from a real Claude Desktop conversation
      against this bundled install, confirming both the popup and the audit log
      (`~/.privacyfence/audit/<current-week>.jsonl`) behave the same as they did from source in
      section 3 above.
- [ ] Quit the bundled daemon (or at minimum don't leave it running alongside the source-mode
      daemon on the same account — the `/mcp` port collides) once this check is done.

## 5. Tag and release

Only after sections 1-4 all pass:

- [ ] Tag `main` at the commit you want to release and push the tag — see this repo's `CLAUDE.md`
      "Releasing" section for the exact commands and tag format (`vX.Y.Z` stable /
      `vX.Y.Z<a|b|rc><n>` pre-release). There's no version-bump commit or release PR to open first:
      pushing the tag is what triggers `.github/workflows/build.yml` to build, sign, and publish the
      DMG. Do not touch `mcpb/shim/package.json`'s version field.
- [ ] Once `build.yml` has created the GitHub Release, attach the saved fixture-recorder report
      (section 1) and popup-smoke report (section 2) to its description, the same convention as a
      normal PR's "## Local QA check" / "## Popup smoke check" headings (`docs/testing-policy.md`
      §2.1/§2.2).
- [ ] Note in that same Release description which of section 3's live-prompt runs you did (shortened
      two-connector version vs. the full `connector-qa-testing.md` prompt) and why.
