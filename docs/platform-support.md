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

- **Windows Task Scheduler autostart registration — root cause confirmed and fixed.** The
  `PrivilegesRequired=lowest`/non-elevation theory this bullet previously carried was wrong:
  `installer/privacyfence.iss`'s `schtasks /create` call passed `/ri 1 /du 9999:59`, trying to get
  crash-restart behavior out of plain `schtasks.exe` CLI flags. Both are documented by Microsoft as
  "not applicable" to an `ONLOGON` schedule (`/ri` is valid only for MINUTE/HOURLY/DAILY/WEEKLY/
  MONTHLY/ONCE; `/du` only for MINUTE/HOURLY) — `schtasks.exe` rejected the whole `/create` call
  outright, on every install, silently, since an Inno `[Run]` entry's nonzero exit code doesn't
  abort Setup by default. This explains the installer reporting success while `schtasks /query`
  found nothing registered, on every real run to date. Fixed by dropping the invalid flags;
  re-validated via `workflow_dispatch` on `windows-graphical-session.yml` before merging that fix
  (see that workflow's run history for the result). **This does not restore crash-restart
  behavior** — the task is logon-triggered only now; real restart-on-failure needs the task's own
  `<RestartOnFailure>` XML settings, not exposed through `schtasks.exe`'s plain flags at all, tracked
  as [`automated-test-strategy-plan.md`](automated-test-strategy-plan.md) Phase 13, not yet built.
- **Windows hands-on QA before a signed release ships**: a real installer run on a clean Windows VM
  (confirm SmartScreen/Authenticode presentation), a real OAuth loopback + connector auth through the
  installed app, a simulated crash confirming the Task Scheduler restart-on-failure policy actually
  restarts the daemon (won't pass until Phase 13 above lands), an Add/Remove Programs uninstall
  confirming program files and the scheduled task are gone while `%USERPROFILE%\.privacyfence\` is
  untouched, and installing the bundled `.mcpb` into a real Claude Desktop against the installed
  daemon. None of this is automatable from CI. `TECHNICAL_REFERENCE.md` now has a dedicated
  "Windows" installation section parallel to its "Linux" one, but that documents the mechanism —
  it doesn't substitute for actually running this checklist on real Windows.
- **[privacyfence/privacyfence#121](https://github.com/privacyfence/privacyfence/issues/121)** (the
  Windows-support tracking issue) stays open until a real tagged release ships the signed Windows
  installer and the hands-on QA above has been run against that release build specifically — not an
  earlier dev build. Close it only then, noting in the closing comment what shipped and anything
  deliberately deferred (e.g. arm64 Windows, EV vs. OV code signing).
- **Linux org mode has not had a real end-to-end run against a live Ubuntu server**: a fresh Ubuntu
  host following `org-mode-setup-guide.md` verbatim, a real OIDC round trip against a real identity
  provider, and at least one live connector (Gmail) exercised through a real MCP client hitting the
  public `/mcp` URL. The `org-mode-smoke` CI job exercises the same daemon/MCP/approval/audit
  contract end to end, but against a synthetic, mocked identity provider — a different, narrower
  guarantee than a real deployment run.
