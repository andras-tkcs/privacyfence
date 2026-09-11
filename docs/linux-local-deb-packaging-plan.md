# Linux Local-Mode `.deb` Packaging — Implementation Plan

Implementation plan for Track A2.3 of [`windows-linux-support-plan.md`](windows-linux-support-plan.md)
— a `.deb` package for `local`-mode (desktop) Linux installs, the Linux equivalent of the macOS DMG.
Read that doc first for why this is scoped separately from org-mode Linux support (already shipped)
and for the packaging-format comparison that landed on `.deb` over AppImage/Flatpak/Snap.

This plan assumes Track A2.1/A2.2 (verify `pip`/`pipx` + the repo-root `--user` systemd unit for a
real desktop install, document it in the README) are **done first** — this `.deb` is a distribution
wrapper around that already-verified install path, not a separate thing to prove correct from
scratch. Building packaging around an unverified install path just moves the same unknowns one layer
deeper.

## Key decision: how the package carries its dependencies

PrivacyFence's dependency list (`pyproject.toml`) includes packages unlikely to exist as Debian
archive packages at compatible versions (`slack-sdk`, `telethon`, `atlassian-python-api`,
`simple-salesforce`, `mcp`, `webauthn`, pinned version ranges on several others). A "proper" Debian
package built against `python3-*` system packages isn't realistic here without maintaining a private
APT repo of every dependency — far more infrastructure than this project needs for v1.

**Decision: build the `.deb` around a PyInstaller onedir bundle**, the same tool this repo already
uses for the macOS `.app` (`PrivacyFenceApp.spec`) and will use for Windows
(`windows-support-plan.md`). This means:

- One build tool across all three platforms — no new build technology to learn or maintain.
- The `.deb` ships a fully self-contained `privacyfence-app` (Python interpreter + all deps
  bundled), so it has **no `python3-*` dependency requirements** beyond glibc — `dpkg -i` installs it
  standalone, same experience as the DMG.
- Trade-off, stated plainly: this is a "Debian package that happens to install a bundled binary,"
  not a package that integrates with the system Python — normal and expected for apps distributed
  this way (this is how e.g. `signal-desktop`, `1password`, and most Electron/PyInstaller-style apps
  ship `.deb`s), not a shortcut specific to this project.

Alternative considered and rejected: `dh-virtualenv` (bundles a real venv into the package, closer to
"native" Debian tooling). Rejected because it still needs every dependency to build from source or a
wheel at package-build time with no obvious win over PyInstaller here, and it's one more piece of
Debian-specific tooling (`dh_virtualenv`) to install and keep working in CI versus reusing the
PyInstaller pipeline already proven for macOS.

## Phase 0 — Prerequisite (blocks everything else)

- [ ] **P0.1** Track A2.1/A2.2 from `windows-linux-support-plan.md` complete: `pipx install
      privacyfence` verified as a real desktop autostart path (`--user` systemd unit, OAuth loopback
      browser flow) and documented in `README.md`. Do not start Phase 1 before this lands — it's the
      thing being packaged, and the `.deb`'s own build carries none of the OAuth/systemd-behavior
      risk itself; that risk lives entirely in the app, which A2.1 already covers.

      **Status:** A2.2 (README quickstart) is done — see "Install from the `.deb`" in `README.md`,
      plus a `.deb`-specific `.` section in `TECHNICAL_REFERENCE.md`'s Installation section. A2.1's
      *real desktop* autostart verification (a real or VM graphical login, `loginctl enable-linger`
      both ways, the OAuth loopback browser flow from a systemd user session) has not happened --
      no such environment was available while doing Phases 1-7 below. Phases 1-7 were implemented
      and verified anyway (PyInstaller build, `debian/` packaging, and the full install/run/
      remove/purge lifecycle all genuinely exercised — see P7.1's note), on the judgment that the
      `.deb`'s own packaging correctness doesn't depend on A2.1's outcome (it wraps the same
      onedir bundle either way) even though this plan's own ordering recommends against starting
      early. A2.1's graphical-login verification remains open — do it before treating either the
      `--user` systemd unit or the `.deb`'s autostart entry (P7.2, also still open) as proven.

## Phase 1 — PyInstaller Linux build

- [x] **P1.1** Add `PrivacyFenceApp.linux.spec`, adapted from `PrivacyFenceApp.spec`: same `Analysis`/
      `PYZ`/`EXE`/`COLLECT` structure and `datas`/`hidden_imports` lists (kept in sync between the two
      specs — factor the shared list into a small importable Python module, e.g.
      `scripts/pyinstaller_common.py`, rather than hand-copying it twice and letting them drift), but
      **no `BUNDLE()` step** (that's macOS-only `.app` bundling) and no `.icns`/entitlements/codesign
      arguments. Output is `dist/PrivacyFenceApp/` (onedir) containing `PrivacyFenceApp` (the daemon
      binary) plus its bundled libs.
- [x] **P1.2** Add the `privacyfence-app` symlink inside the onedir output (same reasoning as
      `build_dmg.sh`'s step 4 — the mcpb shim's `findDaemonCmd()` and any autostart entry look for
      this name specifically).
- [x] **P1.3** Reuse `build_dmg.sh`'s Telegram-credentials-baking step (step 2) unchanged — same
      `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` env vars, same `_telegram_credentials.py` mechanism,
      already platform-independent.
- [x] **P1.4** Icon: PyInstaller's Linux `EXE()` doesn't need an `.ico`/`.icns` (no embedded exe
      icon on Linux) — the existing `icon_512.png`/`icon_64.png`/`icon_32.png` in
      `src/privacyfence/resources/` are used as-is for the `.desktop` entry (P3) and app-menu icon,
      no conversion step needed here (contrast with Windows, which does need a generated `.ico` —
      see `windows-support-plan.md`).

## Phase 2 — Debian packaging metadata

- [x] **P2.1** Add a `debian/` directory (new top-level, mirrors how `scripts/` and `mcpb/` sit
      alongside the rest of the build tooling):
      - `debian/control` — `Package: privacyfence`, `Architecture: amd64` (and `arm64` — see P4.2),
        `Depends:` left minimal/empty (the PyInstaller bundle is self-contained; no `python3-*` deps
        to declare per the key decision above), `Maintainer:`, `Description:` (short/long, mirrors
        `pyproject.toml`'s `description`).
      - `debian/changelog` — Debian-format changelog; since this repo already has no hand-maintained
        version file (`CLAUDE.md`'s "Releasing" section — version comes from git tags via
        `setuptools_scm`), generate this file's single entry at build time from the resolved version
        rather than hand-maintaining Debian changelog entries per release (see P4.1's version-string
        handling).
      - `debian/copyright` — machine-readable copyright per Debian policy, referencing `LICENSE` and
        `NOTICE` (Apache-2.0) already in the repo root.
      - `debian/rules`, `debian/compat` (or `debian/debian/source/format` with `debhelper-compat` in
        `control`, the modern equivalent) — minimal `dh $@` boilerplate; the actual build (P1) already
        happened before `dpkg-deb` is invoked (P4), so `debian/rules`'s `override_dh_auto_build`/
        `override_dh_auto_install` mostly just copies the already-built PyInstaller output into place
        rather than re-invoking the Python build.
      - `debian/install` (or explicit `cp` in `debian/rules`) — maps the PyInstaller onedir output to
        `/opt/privacyfence/` (Debian policy §9.1.2: third-party packages that don't integrate with
        the system package management for their internals belong in `/opt`, not `/usr/lib` — this
        matches how e.g. Chrome's and Slack's own `.deb`s lay themselves out), plus:
        - a thin wrapper script at `/usr/bin/privacyfence-app` (`exec
          /opt/privacyfence/PrivacyFenceApp "$@"`) so it's on `PATH` the same way `pipx`'s
          `~/.local/bin/privacyfence-app` already is for A2.1 — this is what the mcpb shim's `which()`
          fallback (`daemon.ts`'s `findDaemonCmd`, per A2.4) finds.
        - the icon files (P1.4) to `/opt/privacyfence/resources/` and/or
          `/usr/share/icons/hicolor/512x512/apps/privacyfence.png` (standard icon-theme location, for
          the `.desktop` entry to reference by name rather than absolute path).
      - `debian/postinst` / `debian/prerm` / `debian/postrm` — see Phase 3 for what these actually do
        (autostart wiring); keep them minimal and idempotent (postinst safe to re-run on upgrade,
        prerm/postrm never touch anything under a user's `$HOME` — see P2.2).
- [x] **P2.2 — Explicitly scope what the package does *not* touch.** Per-user data
      (`~/.privacyfence/`, `config/settings.yaml`, `credentials/`) is created by the app itself on
      first run (`paths.py`), not by the package. `apt remove privacyfence` must leave that data
      alone (it's outside anything `dpkg` tracks, so this is automatic — no maintainer-script code
      needed, just don't add any that reaches into `$HOME`). `apt purge` conventionally also cleans
      up *package-owned* config (e.g. `/etc/privacyfence/` if one existed) — there isn't any
      system-wide config here to purge, so `postrm purge` has nothing extra to do either. State this
      explicitly in a code comment in `debian/postrm` so a future edit doesn't casually add
      `$HOME`-reaching cleanup that would silently delete a user's credentials/audit log on an
      unrelated `apt purge`.

## Phase 3 — Autostart wiring (the actual "runs at login" behavior)

**Decision: XDG autostart `.desktop` entry, not a systemd `--user` unit, for the packaged autostart
default.** Track A2.1 already covers `privacyfence.service` (`--user` systemd unit) as the
power-user/documented path for anyone doing a bare `pip`/`pipx` install — keep that working and
documented independently. For the `.deb`'s own default behavior, an XDG autostart entry
(`/etc/xdg/autostart/privacyfence.desktop`, `Exec=/usr/bin/privacyfence-app`) is the better fit
specifically *because* it's package-installed for potentially any user on the machine:

- It works the same way across GNOME/KDE/XFCE/etc. without per-desktop-environment special-casing.
- It doesn't require `loginctl enable-linger` or a per-user `systemctl --user enable` step the
  package can't run on the user's behalf from a root-run `postinst` — a `.desktop` drop-in under
  `/etc/xdg/autostart/` is picked up automatically by any XDG-compliant session at graphical login,
  no per-user enablement step at all, which is the actual DMG-parity behavior wanted here (macOS's
  LaunchAgent plist likewise just needs to exist in the right place, no separate "enable" step for a
  user-scoped one once `launchctl load` has run at install time equivalent — see P3.1).

Checklist:

- [x] **P3.1** Write `resources/linux/privacyfence.desktop` (packaged into
      `/etc/xdg/autostart/privacyfence.desktop` by `debian/install`): `Type=Application`,
      `Exec=/usr/bin/privacyfence-app`, `Icon=privacyfence`, `X-GNOME-Autostart-enabled=true`,
      `NoDisplay=true` (it's a background daemon, not something that should also show as a launchable
      app in the applications menu — mirrors the macOS bundle's `LSUIElement: True`, "headless
      background daemon — no Dock icon").
- [x] **P3.2** Also ship a normal (non-autostart) `.desktop` entry for the Applications menu — mirrors
      the mac app being drag-installed into `/Applications` and discoverable there — actually, decide
      whether this is wanted at all: the daemon has no windows to open when launched directly (per
      `daemon_main.py`'s docstring, all human interaction is through the web `/approvals`/`/settings`
      surfaces reached via a browser, not by double-clicking the app). Recommend **skip this** — a
      visible-but-does-nothing-when-clicked menu entry is worse than no entry; document "open
      `http://127.0.0.1:8765/settings` in your browser" instead (README, per P6).
- [x] **P3.3** `debian/postinst`: on install (not upgrade — check `$1 = configure` and whether a
      previous version existed, standard Debian maintainer-script pattern), nothing needs to actively
      *start* the daemon — the autostart entry only fires at the next graphical login, which is
      correct DMG-parity behavior (the DMG doesn't launch the app immediately after a drag-install
      either). Do not add a `postinst` step that force-starts the daemon as root — it must run as the
      installing user, which `postinst` (root) can't correctly determine or safely do.

## Phase 4 — Build script and versioning

- [x] **P4.1** Add `scripts/build_deb.sh`, mirroring `build_dmg.sh`'s shape and prerequisites
      (`pip install -e ".[dev]"` already done, `pyinstaller` available, plus `dpkg-deb` and
      `lintian` — both standard on any Debian/Ubuntu build host, install via `apt-get install -y
      dpkg-dev lintian` on CI). Steps: run PyInstaller against `PrivacyFenceApp.linux.spec` (P1),
      stage `debian/` + the onedir output into a `build/deb-stage/` tree, run `dpkg-deb --build
      --root-owner-group`, output `dist/privacyfence_<version>_amd64.deb`.
      **Version string handling**: `setuptools_scm`'s resolved version (`CLAUDE.md`'s "Releasing")
      uses PEP 440 (`4.0.0a13`, or a dev version like `4.0.1.dev3+gabc1234`), which isn't valid as a
      Debian version string as-is (`+g<sha>` and the bare `a13` pre-release suffix don't sort
      correctly under `dpkg --compare-versions`). Convert at build time: `a`/`b`/`rc` → `~a`/`~b`/`~rc`
      (Debian's own convention for "earlier than the following release," e.g. `4.0.0~a13`), and a dev
      build's `+g<sha>` local segment maps to a Debian `+g<sha>` build-metadata suffix (still sorts
      correctly appended after the numeric/pre-release part, just not itself meaningful to `dpkg` —
      fine, since dev builds are never a package end users install from a release anyway, only from
      CI validation runs per P5).
- [ ] **P4.2 — Architecture.** `amd64` covers the common case. `arm64` (e.g. Raspberry Pi desktops,
      ARM laptops) is a straightforward second build (PyInstaller cross-arch builds require running
      the build *on* that architecture, not cross-compiling — so this means a second CI runner arch,
      not a build-script code change) — scope as a follow-up once `amd64` ships and there's a
      concrete request for it, not a Phase 4 blocker.
- [x] **P4.3** Run `lintian` against the built `.deb` in the build script itself (non-fatal warnings
      logged, but fail the build on any `error`-severity finding) — catches packaging-policy mistakes
      (missing changelog, bad permissions, FHS violations) before they ship, the same role
      `PrivacyFenceApp.spec`'s own structure already plays for catching macOS bundling mistakes early.

## Phase 5 — CI

- [x] **P5.1** Add a Linux leg to `.github/workflows/build.yml` (`runs-on: ubuntu-latest`, alongside
      the existing `macos-latest` job — not `tests.yml`, which stays test-only), triggered the same
      way (`push: tags: ['v*']` + `workflow_dispatch`), running `scripts/build_deb.sh` and uploading
      `dist/privacyfence_<version>_amd64.deb` as a release asset on the same GitHub Release
      `build.yml`'s macOS job already creates (so a tag push produces one Release carrying the DMG,
      the `.mcpb`, and now the `.deb` together).

      The job itself has been exercised locally (`scripts/build_deb.sh` end to end, including the
      lintian gate — see P4.3), but not yet through an actual GitHub Actions run (no tag has been
      pushed against this change) — confirm the workflow syntax and runner behavior for real on the
      next tag push before trusting it unattended.
- [x] **P5.2** No code-signing equivalent is required here (unlike the macOS Developer ID / Windows
      Authenticode stories) — `apt`/`dpkg` don't gate untrusted-publisher installs the way Gatekeeper
      or SmartScreen do. If an APT repository is ever stood up (P6 explicitly recommends against this
      for v1), package signing (`dpkg-sig`/a repo-level `Release` file GPG signature) would become
      relevant then, not before.

## Phase 6 — Docs and distribution

- [x] **P6.1** `README.md`: add a `.deb` install path (`sudo dpkg -i privacyfence_<version>_amd64.deb`
      — or `sudo apt install ./privacyfence_<version>_amd64.deb` to also resolve any future declared
      `Depends:` automatically) alongside the `pip`/`pipx` path A2.2 already added and the macOS DMG
      instructions. Direct-download-and-`dpkg -i` from the GitHub Release, same distribution model as
      the DMG — **no hosted APT repository for v1** (a PPA/custom APT repo is real ongoing
      infrastructure — GPG key rotation, repo hosting, `apt update` freshness — disproportionate to
      current demand; revisit only if adoption clearly warrants it).
- [x] **P6.2** `TECHNICAL_REFERENCE.md`'s "Linux" section: split it the way this plan splits Linux —
      note the `.deb` as the `local`-mode desktop path, distinct from the `org`-mode section that
      already documents the `pip`/systemd-system-unit story.

## Phase 7 — Verification

- [x] **P7.1** Clean-container install/uninstall lifecycle test (`docker run --rm -it
      ubuntu:24.04`, or similar): `dpkg -i`, confirm `/usr/bin/privacyfence-app` runs, confirm
      `/etc/xdg/autostart/privacyfence.desktop` is present and well-formed (`desktop-file-validate`),
      `dpkg -r` (remove) leaves no dangling files outside `/opt/privacyfence` and
      `/etc/xdg/autostart/`, `dpkg -P` (purge) likewise, neither touches a simulated `$HOME`.

      **Verified**, with one substitution: the build environment used to implement this plan could
      reach neither Docker Hub nor any other image registry (outbound network policy), so this ran
      directly on that environment's own Ubuntu 24.04 base instead of a fresh `docker run` container
      -- same `dpkg`/lintian/`desktop-file-validate` tooling, same real `dpkg -i`/`-r`/`-P` lifecycle
      against a simulated `$HOME`, just not a throwaway container. Re-run in a real container (or a
      real machine) before relying on this as the final sign-off; nothing here suggests it would
      behave differently, but it hasn't been proven inside one.
- [ ] **P7.2** Real desktop-session test (not just a container — autostart needs an actual graphical
      login to verify): install on a real or VM Ubuntu/Debian desktop, log out/in, confirm the daemon
      is running post-login (`curl 127.0.0.1:8765/settings` or checking the `mcp_url` file), confirm
      the OAuth loopback browser flow opens correctly from that session.
- [ ] **P7.3** Upgrade-in-place: install version N, do something that creates real state
      (`~/.privacyfence/config/settings.yaml`, a connected connector's token file), install version
      N+1 over it (`dpkg -i` the new `.deb`), confirm that state survived untouched (expected — it
      lives outside anything the package manages, per P2.2 — but worth proving once rather than
      asserting).

      **Partially checked:** re-running `dpkg -i` with the *same* built `.deb` over an already-
      configured install left `~/.privacyfence/config/settings.yaml` untouched, which exercises the
      same "package reinstall/upgrade must not touch $HOME" path P2.2 relies on. What's not yet
      proven is a real N -> N+1 version bump (this session only had one resolvable
      `setuptools_scm` version to build from, since no new tag was pushed) -- low-risk given how
      that state is scoped (outside anything the package manages at all, per P2.2), but still worth
      the real two-version run before calling this fully closed.

---

Once Phase 7 passes, close out the `.deb`-specific item in whichever tracking issue
`windows-linux-support-plan.md`'s "Tracking" section ends up filing for Linux, and update that plan
doc's Track A2.3 row from "stretch, gated on demand" to done.
