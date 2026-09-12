# Manual pre-release test plan

A durable checklist to run through before cutting any release, on top of the automated CI suite
(`docs/testing-policy.md` §1). [`release-testing.md`](release-testing.md) is the standing reference
for *what* stays manual and why; this document is the concrete, operational version of that —
naming the actual CI jobs/scripts/workflows to check and the exact tag mechanics, which is why it
carries phase references and job names `release-testing.md` deliberately doesn't (`docs/README.md`'s
documentation rules keep that kind of detail out of evergreen standing docs). Once
`automated-test-strategy-plan.md` Phase 12 retires this file, `release-testing.md` remains as the
sole standing reference. `docs/automated-test-strategy-plan.md` Phase 9 rewrote this document:
Phases 0–8 automated nearly everything this plan used to walk a human through by hand (fixture
freshness, the web approval surface's structural behavior, gate-state coverage, cross-platform
system behavior, org mode, and every platform's packaged-artifact lifecycle) — running any of that
by hand again before a release would just be re-proving what a merge gate or a scheduled CI job
already proved. What's left is two short passes: confirm the automation that already ran says
"green," then spend a few minutes on the handful of things `docs/testing-policy.md`'s governing rule
says must stay manual (visual judgment, a real third-party client, an OS-native trust prompt).
Budget minutes, not the half a day this plan used to call for.

## 1. Automated prerequisites

Nothing below is something you run — it's what you confirm is already green on the commit you're
about to tag, before you tag it. If anything here isn't, fix or wait for it first; don't tag over a
known-red or known-stale check.

- [ ] `.github/workflows/tests.yml`'s merge-gate jobs (`test`, `test-python-compat`,
      `platform-windows`, `platform-macos`, `org-mode-smoke`, `static-analysis`) are green on the
      commit you intend to release. If it merged normally, this is already true — merging past a red
      required check isn't possible — but confirm you're tagging that exact commit, not a later
      unreviewed one.
- [ ] `.github/workflows/connector-live-check.yml`'s most recent scheduled run (weekly,
      `docs/testing-policy.md` §0) is green and recent (well under the fixture-freshness window
      `qa_fixture_recorder.py --check`'s own report tracks — see that section for the `<60`/
      `60–90`/`>90` day bands). If it's stale (the runner's been down, the schedule got disabled) or
      you can't tell, run `.venv/bin/python scripts/qa_fixture_recorder.py --check` yourself first.
- [ ] No open, unreviewed `chore/connector-live-fixture-drift` PR (opened automatically by the job
      above on drift). Merge or explicitly defer it before tagging — a release should never ship
      while a known, unreviewed provider-shape drift is sitting open.
- [ ] No open PR touching `gate.py`, `auto_accept.py`, `resource_grants.py`, or the web approval UI
      broadly that hasn't been through the full `connector-qa-testing.md` pass `docs/testing-policy.md`
      §3 still calls for on that kind of change.
- [ ] Nothing else you know of is currently broken in `build.yml`'s packaged-artifact path
      (`test_macos_packaged_smoke.py`, `test_windows_packaged_smoke.py`,
      `test_deb_packaged_lifecycle.py`) or `publish-pypi.yml`'s `wait_for_build` gate. There's
      nothing to run here ahead of time — pushing the tag is what triggers them, and a failure there
      blocks its own platform's upload (and, via `wait_for_build`, PyPI/TestPyPI/R2 too) — but don't
      tag if you have open, unresolved reason to believe one of them is currently red (a recent
      packaging change with no green `build.yml` run against it yet, say).

None of this needs `pre_release_check.py` any more for its own sake — that script only ever reran
`pytest`/`npm test`/`npm run typecheck` locally (plus, until removed, a version-string check that no
longer applies now that `setuptools_scm` derives the version from the git tag, per this repo's
`CLAUDE.md`) — the same suite the merge gate above already ran on this commit. It still exists for
running the suite locally without a PR round-trip (e.g. mid-development); it's not a release-time
step any more.

## 2. Human QA

Do the items below that apply to what actually changed since the last release. Skip a bullet
entirely if its trigger condition didn't happen — don't run it out of habit.

- [ ] **Visual UI sanity** — if this release touched `web_shell.py`, `approval_list_html.py`, CSS,
      or the settings/approvals pages: open `/approvals` and `/settings` in a real desktop browser
      with at least one gated call pending, once in light mode and once in dark mode, and once at a
      phone-width viewport (~375px). Confirm every card, banner, and button row renders with
      readable contrast and the WIDE card's layout collapses to stacked sections with no horizontal
      scrolling — the one thing `docs/testing-policy.md`'s governing rule says can't be judged by an
      automated observer. Run `.venv/bin/python scripts/qa_web_smoke.py` too (`docs/testing-policy.md`
      §2.2) and paste its report under a `## Web smoke check` heading in the release description —
      it catches the deterministic half of this surface (script-order bugs, CSP), leaving you to
      judge only the subjective half by eye.
- [ ] **One real MCP-client compatibility smoke** — connect a real Claude Desktop/Cowork session
      (not the official `mcp` Python client CI already drives against this daemon) to the release
      candidate and make one silent-gated tool call and one popup-gated tool call; confirm the
      popup actually renders with real preview data and the decision lands in the audit log. This is
      the one thing that proves a real third-party MCP client stays compatible, which no automated
      test in this repo attempts (`docs/automated-test-strategy-plan.md`'s "What deliberately remains
      manual").
- [ ] **OS-native UX smoke** — only if this release touched packaging, installer, or autostart code:
      install the freshly built package (DMG/installer/`.deb`) on a real machine and confirm the
      OS's own trust presentation — Gatekeeper on macOS, SmartScreen on Windows, the Linux
      `.desktop` autostart entry actually firing on login — looks right. `build.yml`'s
      packaged-artifact tests (Phase 6) already prove install/uninstall/autostart-registration work
      mechanically; this step is only about the OS-owned prompt UI itself, which CI can't render.
- [ ] **Provider/fixture drift review** — if a `chore/connector-live-fixture-drift` PR landed since
      the last release (whether or not you already merged it above), skim its diff once more for
      anything that reads as a real behavior change worth calling out in the release notes, not just
      a redacted shape tweak.

## 3. Tag and release

Only after sections 1–2 above are clean:

- [ ] Tag `main` at the commit you want to release and push the tag — see this repo's `CLAUDE.md`
      "Releasing" section for the exact commands and tag format (`vX.Y.Z` stable /
      `vX.Y.Z<a|b|rc><n>` pre-release). There's no version-bump commit or release PR to open first:
      pushing the tag is what triggers `.github/workflows/build.yml` to build, sign, test, and
      publish the DMG/Windows installer/`.deb` (and R2-archive all of it), and
      `.github/workflows/publish-pypi.yml` to build and, for a stable tag only, publish the sdist/
      wheel to TestPyPI then PyPI. Do not touch `mcpb/shim/package.json`'s version field.
- [ ] Once `build.yml` has created the GitHub Release, attach any report you generated in section 2
      above (web smoke, or notes from the MCP-client/OS-native smoke) to its description.
- [ ] Note in that same Release description whether this release needed the full
      `connector-qa-testing.md` pass (per section 1's gate/auto-accept check above) and, if so, that
      it was run.
