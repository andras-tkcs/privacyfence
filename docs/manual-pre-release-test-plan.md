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
- [ ] Confirm every connector you're about to test is authenticated from the PrivacyFence menu bar.

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

## 2. QA popup smoke test

Goal: visually confirm the real approval-window modal loop still works end to end — a real click
on a real on-screen button actually resolves the popup — which construction-only unit tests
(`tests/unit/test_approval_window.py`) don't cover.

You don't need to run all 90 scenarios before every release (that's specifically for
`approval_window.py` modal-loop changes, per `docs/testing-policy.md` §2.2) — running one narrow
and one wide representative scenario is enough to confirm the mechanism itself still works:

- [ ] Run one review-gate (read) example, watching the window as it appears (raise
      `--pause-seconds` so you actually have time to look):
      ```bash
      .venv/bin/python scripts/qa_popup_smoke.py --scenario "gmail_get_thread" --pause-seconds 3
      ```
      Confirm: the popup actually appears on screen, shows the From/Subject/thread preview, and
      the script reports `passed` after "Allow once" is clicked programmatically.
- [ ] Run one popup-gate (write) example with the temp-accept caption:
      ```bash
      .venv/bin/python scripts/qa_popup_smoke.py --scenario "drive_sheets_write_range" --pause-seconds 3
      ```
      Confirm: the popup appears, shows the temp-accept disclosure caption above the buttons, and
      the script reports `passed`.
- [ ] Run the menu-bar scenario (a separate mechanism — a real click on the real status-bar icon,
      then a real click into the "Manage Auto-accept Rules…" window):
      ```bash
      .venv/bin/python scripts/qa_popup_smoke.py --scenario "Menu bar" --pause-seconds 3
      ```
      Confirm: the status-bar menu opens and the rules window appears.
- [ ] If `approval_window.py`'s modal-loop plumbing changed since the last release (not just popup
      *content*, which PR review already covers), run the full suite instead of the three
      spot-checks above:
      ```bash
      .venv/bin/python scripts/qa_popup_smoke.py --report-file /tmp/popup_smoke_full.md
      ```
      and confirm the final line reads `90/90 scenarios passed` (89 tool-approval scenarios plus
      the one menu-bar scenario).
- [ ] Requires macOS and Accessibility permission granted to your terminal/IDE — grant it once from
      System Settings → Privacy & Security → Accessibility if you haven't already.
- [ ] Repeat one of the two scenarios above with your Mac in dark mode (System Settings →
      Appearance → Dark), and again in light mode if you started in dark — confirm every card,
      the risk/PII banners, and the button row all render with readable contrast in both, since
      this isn't covered by any automated test.

## 3. "QA prompt" manual test — representative live connector pass

Goal: exercise real approval popups end to end (silent auto-accept / native popup / Deny / Always
allow) against real accounts through an actual Claude Cowork/Desktop session — the one thing that
proves the gate, the popup UI, and the audit log agree with each other, not just with themselves.

This plan does not replace running the full prompt in
[`connector-qa-testing.md`](connector-qa-testing.md) — do that in full before any release that
touched `gate.py`, `auto_accept.py`, `resource_grants.py`, or the menu bar's auto-accept UI broadly
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
      > **Manage Auto-accept Rules… → Gmail → Filters** so it doesn't linger.
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
- [ ] From the menu bar: install/update the organization config bundle, authenticate at least one
      connector, and confirm `PrivacyFence.mcpb` installs into Claude Desktop from the mounted DMG.
- [ ] Confirm `~/.privacyfence/` (not the repo folder) is where config/credentials/logs land for
      this bundled install — spot-check `~/.privacyfence/config/settings.yaml` exists.
- [ ] Exercise one silent-gate call and one popup-gate call from a real Claude Desktop conversation
      against this bundled install, confirming both the popup and the audit log
      (`~/.privacyfence/audit/<current-week>.jsonl`) behave the same as they did from source in
      section 3 above.
- [ ] Quit the bundled daemon (or at minimum don't leave it running alongside the source-mode
      daemon on the same account — the IPC socket path collides) once this check is done.

## 5. Version bump and release

Only after sections 1-4 all pass:

- [ ] Bump `pyproject.toml`'s `project.version` and `src/privacyfence/__init__.py`'s `__version__`
      together, in their own commit (`Bump to vX.Y.Z` or `Bump to vX.Y.Z: <short summary>`) — see
      this repo's `CLAUDE.md`. Do not touch `bridge/package.json`'s version field.
- [ ] Attach the saved fixture-recorder report (section 1) and popup-smoke report (section 2) to
      the release PR description, the same convention as a normal PR's "## Local QA check" /
      "## Popup smoke check" headings (`docs/testing-policy.md` §2.1/§2.2).
- [ ] Note in the release PR which of section 3's live-prompt runs you did (shortened two-connector
      version vs. the full `connector-qa-testing.md` prompt) and why.
