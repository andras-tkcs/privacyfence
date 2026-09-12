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

- **Windows Task Scheduler autostart — two real, independent bugs found and fixed via actual
  `workflow_dispatch` runs, not by inspection alone.** The `PrivilegesRequired=lowest`/non-elevation
  theory this bullet previously carried was wrong on both counts it tried to explain:
  1. **Registration itself was failing.** `installer/privacyfence.iss`'s `schtasks /create` call
     passed `/ri 1 /du 9999:59`, trying to get crash-restart behavior out of plain `schtasks.exe`
     CLI flags. Both are documented by Microsoft as "not applicable" to an `ONLOGON` schedule (`/ri`
     is valid only for MINUTE/HOURLY/DAILY/WEEKLY/MONTHLY/ONCE; `/du` only for MINUTE/HOURLY) —
     `schtasks.exe` rejected the whole `/create` call outright, on every install, silently, since an
     Inno `[Run]` entry's nonzero exit code doesn't abort Setup by default. Fixed by dropping the
     invalid flags. **This does not restore crash-restart behavior** — the task is logon-triggered
     only; real restart-on-failure needs the task's own `<RestartOnFailure>` XML settings, not
     exposed through `schtasks.exe`'s plain flags at all, tracked as
     [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) Phase 13, not yet built.
  2. **Once registration was fixed and re-validated, the trigger itself turned out to be scoped to
     the wrong account.** The installer's `schtasks /create` call omitted `/RU` entirely, on the
     assumption (also baked into `test_windows_graphical_session_autostart.py`'s own docstring) that
     Microsoft's unqualified default for an `ONLOGON` trigger already meant "fires for any
     interactive logon." It doesn't: per Microsoft's own documentation, omitting `/RU` scopes the
     task to whichever account ran `schtasks /create` — i.e. the installing user only. The re-run
     against a real Windows runner caught this directly: task registration succeeded, but a
     different (throwaway) account's logon never fired the trigger within 30s. Fixed by adding
     `/ru "BUILTIN\Users"` — the built-in group rather than one specific account, the standard
     technique for "fire on any interactive logon, in that user's own session."
  Fix 1 alone was re-run via `workflow_dispatch` and is what surfaced fix 2's bug; fix 2's own
  `workflow_dispatch` re-run is what should confirm both together before this note calls the
  mechanism proven — check `windows-graphical-session.yml`'s own run history for the actual result
  rather than trusting this note alone.
  None of this needed a dedicated bullet on its own here for the manual-QA/issue-closure part of it:
  that content now lives in [`release-testing.md`](release-testing.md)'s human-checks list
  (Windows-specific bullets — a real installer run on a clean Windows VM, OAuth loopback, the
  crash-restart check above, and a clean Add/Remove Programs uninstall) and as a standing comment on
  [privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121) itself
  recording that it stays open until a real tagged release ships the signed installer and that QA
  has run against it — not duplicated here as well.
- **Linux org mode has not had a real end-to-end run against a live Ubuntu server**: a fresh Ubuntu
  host following `org-mode-setup-guide.md` verbatim, a real OIDC round trip against a real identity
  provider, and at least one live connector (Gmail) exercised through a real MCP client hitting the
  public `/mcp` URL. The `org-mode-smoke` CI job exercises the same daemon/MCP/approval/audit
  contract end to end, but against a synthetic, mocked identity provider — a different, narrower
  guarantee than a real deployment run.
