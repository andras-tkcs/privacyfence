# Platform support

PrivacyFence local mode is packaged for macOS, Windows, and Debian/Ubuntu Linux. Org mode is a Linux/server deployment path.

## Support matrix

| Platform | Distribution | Startup model | Release automation |
|---|---|---|---|
| macOS | signed/notarized DMG containing the PyInstaller app bundle and MCPB | packaged app/LaunchAgent path | `.github/workflows/build.yml` on `macos-latest` |
| Windows | Inno Setup installer containing the PyInstaller executable and MCPB | Task Scheduler entry created by the installer | `.github/workflows/build.yml` on `windows-latest` |
| Debian/Ubuntu local mode | self-contained `.deb` built from the PyInstaller onedir output | XDG autostart desktop entry | `.github/workflows/build.yml` on `ubuntu-latest` |
| Linux Python install | wheel/sdist with `privacyfence-app` console script | operator-managed process or `privacyfence.service` | PyPI publishing workflow |
| Linux org mode | Python/system service behind the configured reverse proxy and identity provider | operator-managed service | release smoke coverage in the build/test suite |

## Shared runtime architecture

All desktop platforms run the same Python daemon and embedded web UI. MCP clients connect to the daemon's `/mcp` Streamable HTTP endpoint. Claude Desktop uses the bundled Node/TypeScript stdio shim in `mcpb/shim/` to discover and proxy to that endpoint.

The daemon uses `portalocker` for the single-instance lock, so the locking abstraction is shared across POSIX and Windows. Platform-specific behavior is concentrated in packaging, process discovery/startup, filesystem locations, browser launching, and installer integration.

## macOS

The macOS app is defined by `PrivacyFenceApp.spec`. Release builds are produced by `scripts/build_dmg.sh` and the macOS job in `.github/workflows/build.yml`.

The packaged application keeps user state outside the application bundle. The release workflow signs and notarizes the app/DMG when the required signing credentials are configured.

## Windows

The Windows executable is defined by `PrivacyFenceApp.win.spec`. `scripts/build_installer.ps1` builds the application and invokes `installer/privacyfence.iss` to produce the installer.

The installer:

- installs PrivacyFence under Program Files;
- installs the bundled MCPB/shim assets;
- creates a Start Menu entry for the settings UI;
- creates a Task Scheduler entry for user-session startup;
- starts PrivacyFence after installation;
- removes the scheduled task on uninstall;
- preserves the user's PrivacyFence state directory on uninstall.

Optional signing is configured through `SIGN_CERT_PATH`, `SIGN_CERT_PASSWORD`, and optionally `SIGN_TIMESTAMP_URL`.

The `platform-windows` job in `.github/workflows/tests.yml` runs the full core Python suite on `windows-latest` on every PR, alongside the normal Ubuntu suite. Windows packaging itself (the installer build, silent install/autostart/uninstall) is exercised only by the release build workflow (`build.yml`'s `build-windows` job, tag/`workflow_dispatch`-triggered), not per PR — see "Known open items" below for its current live status.

## Debian/Ubuntu local mode

The local desktop package is defined by `PrivacyFenceApp.linux.spec`, `scripts/build_deb.sh`, `debian/`, and `resources/linux/privacyfence.desktop`.

The `.deb` installs the self-contained application under `/opt/privacyfence`, exposes `/usr/bin/privacyfence-app`, installs application icons, and installs an XDG autostart desktop entry under `/etc/xdg/autostart/`.

The XDG desktop autostart path is separate from the repository's `privacyfence.service`, which is the Python/system-service template rather than the desktop `.deb` startup mechanism.

Package removal does not delete per-user PrivacyFence state from the user's home directory.

## Architecture and CPU constraints

PyInstaller builds are native to the runner architecture. The current Debian release job produces the architecture supported by its Ubuntu runner rather than cross-compiling another CPU target.

## Verification boundaries

The repository distinguishes build automation from target-environment validation. Packaging workflows prove that release artifacts can be built and exercise their automated smoke tests; OS-native presentation and login-session behavior still require the relevant platform environment where automation does not cover it.

Remaining test-automation work is tracked only in [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md).

## Known open items

- **Windows autostart — now actually works, verified end to end by real `workflow_dispatch` runs,
  after a chain of independent bugs of which the last was in the daemon rather than the installer.
  Two things remain: the `LogonTrigger`'s own firing, which a hosted runner cannot produce and the
  Windows human checks cover instead, and crash-restart, which the shipped setting does not provide
  (both below).** This
  mechanism went through several real, independently-found-and-fixed bugs before
  landing where it is now — see `installer/privacyfence-task.xml.tmpl`'s own header comment and
  `installer/privacyfence.iss`'s `[Code]` section for the full detail — and the early ones are worth
  naming here only because this bullet itself carried wrong theories about them at the time:
  `PrivilegesRequired=lowest`/non-elevation was never the cause; nor, in the end, was the `/ri`/`/du`
  and `/RU`-scoping pair of `schtasks /create` CLI-flag bugs this bullet previously described as the
  fix — those flags were superseded entirely once the mechanism moved to a real Task Scheduler XML
  task definition (`schtasks /create /xml`), which is what actually ships today.
  **The real, final blocker in that XML approach** was an `encoding="UTF-8"` declaration in the XML
  prolog: `schtasks.exe` hands the file to MSXML as a Unicode stream already, so a declaration
  claiming UTF-8 contradicted the stream the parser was already on and MSXML rejected the whole
  registration outright (`ERROR: The task XML is malformed. (1,40)::ERROR: unable to switch the
  encoding`) — on every install, silently, until `[Code]` was changed to actually capture and log
  `schtasks`'s own output. Fixed by dropping the encoding declaration entirely. Alongside it, the
  task definition also regained three elements an earlier simplification pass had dropped and that
  turned out to be load-bearing once registration itself started succeeding: `version="1.2"` on the
  root `<Task>` element (the schema version `<RestartOnFailure>` and `<MultipleInstancesPolicy>`
  actually need), `id` on `<Principal>`, and the matching `Context` on `<Actions>` — without that
  id/Context pair, the registered `GroupId` principal is never actually bound to anything that runs.
  **The defect that actually kept Windows autostart from ever working was not in the task at all —
  it was in the daemon, and only a test that let Task Scheduler do the launching could see it.** The
  Windows build is a windowed executable (`PrivacyFenceApp.win.spec`'s `console=False`, since a
  console window flashing up at every sign-in would be a bug of its own), and a windowed process
  started with no console has no standard handles, so CPython sets `sys.stdout`/`sys.stderr` to
  `None`. uvicorn's default log formatter calls `sys.stdout.isatty()` while `uvicorn.Config(...)` is
  being built, so the daemon raised `AttributeError: 'NoneType' object has no attribute 'isatty'`,
  logged `Fatal error`, and exited 1 — before binding its port. Task Scheduler was starting the
  daemon correctly and the daemon was killing itself; the same would have happened to the installer's
  own "launch PrivacyFence now" step and to double-clicking the executable. Nothing caught it because
  every automated start of this app until now — the packaged smoke tests included — ran it from a
  shell with stdout redirected to a file, which is a perfectly valid stream.
  `privacyfence/std_streams.py` now repairs the frozen process's streams before anything else runs
  (the same move as `_daemon_entry.py`'s `SSL_CERT_FILE` fix-up), with
  `tests/unit/test_daemon_std_streams.py` as a per-PR regression test built around the real failing
  call.
  **One more real defect came out of asserting the definition Task Scheduler stored rather than the
  one this repo ships**: `<DisallowStartIfOnBatteries>` and `<StopIfGoingOnBatteries>` both default
  to `true` and the template had never mentioned either, so on a laptop the shipped task would not
  start PrivacyFence at sign-in while on battery, and would stop it the moment the machine was
  unplugged — a privacy gate quietly not running, with the MCP client simply finding no daemon. Both
  are now explicitly `false`, and the contract asserts them with no default fallback.
  **Crash-restart is *not* implemented, contrary to what this note and
  [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) Phase 13 both previously
  claimed.** The task carries `<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>`,
  added as the Windows analogue of the macOS LaunchAgent's `KeepAlive`/`SuccessfulExit=false` and the
  Linux `.deb`'s systemd `Restart=on-failure`. It is not one, and the first test ever to kill a
  Scheduler-started daemon measured that directly: nothing came back, and Task Scheduler's own
  operational log says why — it logged the dead action as a *success*:

  ```
  Event ID 201: Task Scheduler successfully completed task "\PrivacyFence", instance "{63cf2afb-…}",
                action "…\privacyfence-app.exe" with return code 2147942401.
  ```

  `2147942401` is `0x80070001`, the action's own non-zero exit surfaced as an HRESULT. The setting
  answers a task that fails to *run*, not an action that ran and then died, so it never engages.
  `windows-graphical-session.yml` now pins that measured behavior with a deliberately negative
  assertion, so the setting cannot be re-added and re-declared a fix without measuring it again.
  **Real crash-restart needs a different mechanism, and picking one is an open product decision.**
  The Task Scheduler idiom is a repeating trigger (a `<TimeTrigger>` with a past `StartBoundary` and
  an indefinite `<Repetition>`, so it is live without waiting for a sign-in), which relaunches the
  daemon on the next tick after it dies. Two sub-decisions come with it and both have real costs: the
  interval trades restart latency against a no-op process spawn per signed-in user per tick (the
  daemon already exits immediately when its single-instance lock is held, but every tick still pays a
  PyInstaller cold start, and on a laptop that is battery), and `MultipleInstancesPolicy` has to stay
  `Parallel` — `IgnoreNew` would suppress the spawn but also stop a second user's sign-in from ever
  getting a daemon. Whoever takes it should also quiet the "another instance is already running"
  path, which currently logs at ERROR and exits 1 on every tick.
  **One gap remains, and it is in what CI can observe, not in the installer: the `LogonTrigger`'s own
  firing.** The test used to claim it drove that, via PowerShell's `Start-Process -Credential`
  (`CreateProcessWithLogonW`) as a stand-in for signing in, and was red on every run because of it:
  `schtasks /query /v` reported the task `Enabled`/`Ready`, scoped to the right group, pointing at the
  right exe, and simply never fired (`Last Result: 267011` / `SCHED_S_TASK_HAS_NOT_RUN`).
  `CreateProcessWithLogonW` creates a logon session but not the Terminal Services *session* logon a
  `LogonTrigger` subscribes to, so the trigger was never evaluated — a limitation of the substitution,
  not a defect in the shipped task definition, and one no task-XML or `[Code]` change could fix.
  Of the three ways out this note used to list unchosen, the first is now taken: the automated
  assertions are narrowed to what a hosted runner can actually prove, and the trigger's own firing is
  covered by the Windows human checks in [`release-testing.md`](release-testing.md) on a machine with
  a real sign-in. RDP loopback would create a genuine session logon but needs an RDP client that can
  run without a desktop of its own, which a hosted runner does not have; retiring the workflow would
  have given up the Scheduler-driven coverage below as well. What CI now proves, every run: the
  definition **Task Scheduler itself stored** (`schtasks /query /xml`, not this repo's template)
  matches the autostart contract element by element; Task Scheduler itself starts the daemon, into the
  real signed-in account's own profile with no injected environment, running as that account, serving
  the full daemon/MCP/approval/audit round trip and ending on "Quit PrivacyFence"; and the
  crash-restart above. The one substitution left is asking Task Scheduler to run the task on demand
  instead of the trigger asking it — everything after that decision (resolving the `Builtin\Users`
  principal to a signed-in member, its `LeastPrivilege` token, its profile, the action launch) is the
  same code path. A group principal runs as a member who is *signed in*, so the signed-in account is
  the only one a hosted runner can have it run for — an attempt with a throwaway account returned
  `ERROR: Access is denied.` The cheap half of the same coverage also runs on every PR, on any OS:
  `tests/unit/test_windows_autostart_task_template.py` holds the shipped template to the same
  contract (`tests/windows_task_contract.py`), so a regression in it no longer waits for a scheduled
  Windows-only workflow to notice. Check `windows-graphical-session.yml`'s own run history for the
  current result rather than trusting this note alone.
  **A related wrinkle worth knowing, not currently a defect**: `installer/privacyfence.iss` is
  `PrivilegesRequired=lowest`, so a silent install resolves `{autopf}` to `{userpf}` —
  `%LOCALAPPDATA%\Programs\PrivacyFence`, inside the installing account's own profile, which no other
  account can read. The task's `Builtin\Users` group principal therefore only composes with a
  per-machine install; on the single-user desktop this product targets, installing and signing-in
  accounts are the same one and nothing is wrong. The CI test installs to a machine-wide directory
  for exactly this reason.
  None of this needed a dedicated bullet on its own here for the manual-QA/issue-closure part of it:
  that content now lives in [`release-testing.md`](release-testing.md)'s human-checks list
  (Windows-specific bullets — a real installer run on a clean Windows VM, OAuth loopback, the
  sign-out/sign-in check that covers the `LogonTrigger` above, and a clean Add/Remove Programs
  uninstall) and as a standing comment on
  [privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121) itself
  recording that it stays open until a real tagged release ships the signed installer and that QA
  has run against it — not duplicated here as well.
- **Linux org mode has not had a real end-to-end run against a live Ubuntu server**: a fresh Ubuntu
  host following `org-mode-setup-guide.md` verbatim, a real OIDC round trip against a real identity
  provider, and at least one live connector (Gmail) exercised through a real MCP client hitting the
  public `/mcp` URL. The `org-mode-smoke` CI job exercises the same daemon/MCP/approval/audit
  contract end to end, but against a synthetic, mocked identity provider — a different, narrower
  guarantee than a real deployment run.
