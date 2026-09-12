# Release testing

This document contains the release checks that still require human judgment. Automated correctness belongs in CI and is documented in [`testing-policy.md`](testing-policy.md). Remaining automation work belongs only in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).

## Before release

Confirm the release commit is on a reviewed branch/PR and all required CI checks are green. Also confirm the relevant platform build workflow succeeds for the artifact being published.

For a release that touches connectors or provider behavior, verify the scheduled live-connector workflow is healthy and review any open fixture-drift pull request before publishing.

## Human checks

Run only checks that automation cannot judge reliably:

- inspect the approval list, approval cards, settings pages, notifications, responsive layout, and light/dark presentation when UI code changed;
- complete a real first-time OAuth/consent flow when authentication setup changed or provider consent behavior is in question — on Windows specifically when a Windows-affecting change lands, confirm the OAuth loopback flow opens the default browser and completes a real connector auth through the installed app;
- exercise one real MCP client against the packaged application when MCP discovery, the shim, packaging, or daemon startup changed — on Windows specifically, install the `.mcpb` into a real Claude Desktop on the same machine and confirm the shim finds and launches the installed daemon end to end;
- verify OS-native installation/security presentation when installer, signing, notarization, SmartScreen/Gatekeeper/UAC, autostart, or login-session behavior changed — before a signed Windows release ships specifically: a real installer run on a clean Windows VM confirming SmartScreen/Authenticode presentation isn't an outright block, a simulated crash confirming whether the Task Scheduler restart-on-failure policy restarts the daemon (expected not to until `automated-test-strategy-plan.md` Phase 13 lands — see `platform-support.md`'s "Known open items"), and an Add/Remove Programs uninstall confirming the program files and scheduled task are gone while `%USERPROFILE%\.privacyfence\` is untouched;
- perform focused exploratory connector QA using [`connector-qa-testing.md`](connector-qa-testing.md) for a new connector, a major connector rewrite, or an unexplained provider regression.

A signed Windows release specifically must not ship without the Windows-specific bullet above having actually been run against that release build — this is the human QA pass [privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121) is gated on closing until.

Do not repeat the full automated unit/integration/browser/provider matrix manually just because a release is being cut.

## Platform artifacts

Use [`platform-support.md`](platform-support.md) to identify the packaging path and startup mechanism for each platform. Test the exact artifact intended for release, not a source checkout standing in for the package.

Preserve user state across normal upgrade/uninstall flows according to the platform packaging contract. Do not delete a real user's `~/.privacyfence` (or equivalent home-scoped state) as part of routine release testing.

## Evidence

A release review should be able to answer:

1. Which commit/tag produced the artifact?
2. Which automated checks ran and passed?
3. Which human-only checks were relevant to the changes?
4. Were any provider drift, signing, packaging, or login/autostart issues observed?

Keep that evidence in the release/PR discussion rather than turning this document into a chronological release log.
