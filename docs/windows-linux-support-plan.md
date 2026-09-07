# Windows & Linux Support Plan

Plan for taking PrivacyFence beyond macOS. Written against the codebase as of `v4.0.0`-era `main`,
after P10 of the (now-removed) `https-connector-refactor-plan.md` deleted the native AppKit menu
bar/dialogs and the Unix-socket IPC transport in favor of a single embedded web UI and `/mcp` over
plain HTTP — the two things that made a non-macOS port a large lift when
[#121](https://github.com/andras-tkcs/privacyfence/issues/121) was first filed. That issue's own
prerequisites (#119, #120) are done; this document supersedes its remaining-work list with what's
actually left, verified directly against `src/` rather than assumed.

The short version: the connector/policy/web core already runs on Linux — `tests.yml` has executed
the full suite on `ubuntu-latest` since P10. What's missing is packaging, autostart, and one real
portability bug, not core logic. Windows needs more: one code fix plus new packaging, autostart, and
signing infrastructure that doesn't exist in any form yet.

## Terminology this plan relies on

PrivacyFence already has two run modes, and the Linux plan below splits along exactly that line —
they're different audiences with different packaging needs, not two flavors of the same install:

- **`local` mode** — the desktop case. One person, one machine, the implicit `local` principal, no
  sign-in. This is what the macOS DMG installs today.
- **`org` mode** — the server case. One shared daemon, reachable over HTTPS, people sign in as
  themselves via the org's OIDC IdP. Documented end-to-end in
  [`org-mode-setup-guide.md`](org-mode-setup-guide.md) (Ubuntu + Caddy + Google identity).

---

## Track A — Linux

Linux splits into two genuinely different deliverables. Don't conflate them — "Linux support" as a
single checkbox hides that one path is basically already shipped and the other hasn't been started.

### A1. Org mode (server) — already exists; needs battle-testing, not building

This is **already a working, documented install path**, not a gap to fill:

- `pip install privacyfence` (via `pipx`, per the guide) already works today — PyPI publishing is
  live (`CLAUDE.md`'s "Releasing" section, OIDC trusted publishing) and needs nothing further built.
- [`org-mode-setup-guide.md`](org-mode-setup-guide.md) walks the entire stack: dedicated system user,
  `pipx` install, Google OIDC registration, org config bundle, Caddy for TLS, and a **system**
  systemd unit (inline in the guide's Step 7 — distinct from the repo-root `--user` unit, see A2)
  running as that dedicated user.
- The blocker that made this impossible through P9 — `run_app()` unconditionally opening the native
  macOS menu bar, crashing the daemon immediately on Linux, in every mode including `org` — is fixed.
  Nothing in `src/privacyfence/` imports a macOS-specific module any more.

What's left is exactly what the guide's own callout says: **a real end-to-end run against a live
Ubuntu server has not been done.** Concretely:

- [ ] **A1.1 — Run the guide for real.** Stand up a fresh Ubuntu server, follow
      `org-mode-setup-guide.md` verbatim (steps 1–10), and fix forward anything that doesn't match —
      most likely candidates: the OIDC round-trip end to end, the org config bundle install step, and
      whether `ProtectSystem=strict`/`ReadWritePaths` in the systemd unit actually leaves the daemon
      able to write everything it needs under `/home/privacyfence`.
      Confirm at least one live connector (Gmail, since Google is the guide's example IdP) works
      through a real MCP client hitting the public `/mcp` URL.
- [ ] **A1.2 — Remove the "not yet battle-tested" callouts** in `org-mode-setup-guide.md`,
      `TECHNICAL_REFERENCE.md`'s "Linux" section, and `privacyfence.service`'s header comment once
      A1.1 passes, and close out the org-mode-shaped part of #121.
- [ ] **A1.3 — CI smoke test (optional, low cost).** A Linux leg that does `pip install .` from the
      built sdist/wheel and starts the daemon in `org` mode against a fake/stubbed IdP just to prove
      the package installs and boots — not a substitute for A1.1's real run, but cheap regression
      insurance once A1.1 has proven the real thing works.

No new packaging format is needed here — PyPI + `pipx` + a systemd unit is the entire distribution
story for this audience, and it already matches how a sysadmin expects to install a service on
Ubuntu.

### A2. Local mode (desktop) — the actual gap; needs a real packaging decision

This is the Linux equivalent of the DMG: someone installing PrivacyFence on their own Linux desktop
to use it themselves, the same way macOS users drag `PrivacyFenceApp.app` to `/Applications`. Today
this path has:

- `pip`/`pipx install privacyfence` — works, but is a developer-tool install experience, not a
  desktop-app one (no autostart wiring, no icon, no uninstall-by-GUI).
- The repo-root `privacyfence.service` — a systemd **`--user`** unit, present and correct in shape,
  but never verified against a real desktop install (same "unverified" status as A1 had before this
  plan's A1.1) and requiring `loginctl enable-linger` or a graphical session to actually autostart at
  login the way the macOS LaunchAgent does.

**Open packaging question — decide before starting, since it changes the shape of the work:**

| Option | What it gets you | Cost |
|---|---|---|
| **(recommended for v1) `pip`/`pipx` + systemd `--user` unit, just documented and verified** | Reuses A1's PyPI story entirely; zero new packaging infra; matches how most Linux power users already install CLI/daemon tools | Low — mostly doc + verification work, same shape as A1.1 |
| **`.deb` (dpkg/apt) package** | A `sudo apt install ./privacyfence.deb` experience closer to the DMG's double-click feel; can wire the systemd unit, a `.desktop` autostart entry, and uninstall via `apt remove` automatically at install time | Medium — new `debian/` packaging metadata, a build step in CI, and it only covers Debian/Ubuntu-family distros (not Fedora/Arch/etc. — an RPM would be a second, separate package for those) |
| **AppImage** | Single portable binary, no install step, works across distros | Doesn't fit this app's model well — a background daemon with autostart-at-login wants to be *installed*, not run ad hoc from a downloaded file; AppImage has no native autostart/systemd integration story |
| **Flatpak/Snap** | Sandboxed, cross-distro, has an app-store-like distribution channel | Heaviest lift (manifest, sandboxing permissions for a tool that needs broad filesystem/network access for OAuth + connectors), and its sandbox model fights a background daemon that manages its own credentials directory more than it helps |

Recommendation: **ship the `pip`/`pipx` + systemd path first** (same low-cost shape as A1), since it
requires no new packaging tooling and reuses everything A1.1 already verifies about the daemon
running correctly under systemd on Linux — only the unit type (`--user` vs `--system`) and target
audience differ. Treat a `.deb` as a **stretch goal**, worth doing once the `pip` path is proven and
if actual desktop-user demand shows up (issue reactions, requests) — it's genuinely more polish, not
more capability, and it doesn't block anyone from using PrivacyFence locally on Linux today.

Checklist:

- [ ] **A2.1 — Verify the `--user` systemd path for real desktop use**: `pipx install privacyfence`
      as a normal (non-service) desktop user, install `privacyfence.service` per its own header
      comment, confirm it actually autostarts at graphical login (test both with and without
      `loginctl enable-linger`, since a `--user` unit's login-time behavior differs from a `--system`
      one) and that the OAuth loopback flow (`oauth_loopback.py`, already cross-platform —
      `webbrowser.open()`) opens the user's browser correctly from a systemd user session.
- [ ] **A2.2 — Add a Linux quickstart to `README.md`** alongside the existing DMG instructions
      (currently the README only documents the macOS/DMG install path — "macOS host" is listed as
      an implementation assumption, but the Linux path isn't documented at all for `local` mode).
- [ ] **A2.3 (stretch, gated on demand) — `.deb` package.** If pursued: `debian/control` +
      `debian/rules` (or `dpkg-deb` invoked from a new `scripts/build_deb.sh`, mirroring
      `scripts/build_dmg.sh`'s shape), packaging the `pip`-installed console script plus the systemd
      unit and a `.desktop` autostart entry, built via a new CI leg parallel to `build.yml`'s macOS
      job. No code-signing equivalent is required (apt doesn't gate on it the way macOS Gatekeeper
      does), which keeps this simpler than the Windows Authenticode story in Track B.
- [ ] **A2.4 — mcpb shim Linux fallback.** `findDaemonCmd()` in `mcpb/shim/src/daemon.ts` has no
      Linux-specific default path (only the macOS `DEFAULT_APP_PATH` and a generic `python3 -m`
      dev fallback). Add a `pipx`-default (`~/.local/bin/privacyfence-app`) check before falling
      back to the dev path, gated on `process.platform`.

---

## Track B — Windows

Unlike Linux, this has essentially nothing shipped yet beyond the fact that the daemon's core logic
is platform-independent Python. Real new work, in five parts:

- [ ] **B1 — Fix the one genuine portability bug.** `daemon_main.py`'s single-instance lock
      (`_acquire_instance_lock`/`_release_instance_lock`) calls `fcntl.flock`, which doesn't exist on
      Windows — this is the one place the daemon would fail to even start, not just fail to package.
      Options: a `sys.platform == "win32"` branch using `msvcrt.locking`, or a small cross-platform
      dependency (`portalocker` is the natural fit — same "don't hand-roll a platform-locking
      primitive" reasoning `pyproject.toml` already applies to WebAuthn/JWT). Recommend `portalocker`
      unless there's a reason to avoid the extra dependency.
- [ ] **B2 — Autostart.** No Windows equivalent of `launchd`/systemd exists yet. Add either a Task
      Scheduler registration (`schtasks`) or a Startup-folder shortcut, installed by whatever B4
      installer ends up being — this should not require the user to hand-edit XML the way the macOS
      plist theoretically allows (in practice the DMG doesn't ask users to edit the plist either, so
      Windows shouldn't regress on that).
- [ ] **B3 — File permissions gap (decide, don't necessarily build).** Several places `chmod`
      credential/token files to `0o600`/`0o700`; on Windows this is a silent no-op, not an error, so
      credentials aren't actually locked down beyond default NTFS user-profile ACLs. For a v1,
      document this as a known, accepted gap (a single-user Windows profile is already
      access-controlled to that user by default) rather than building ACL-tightening
      (`icacls`/`pywin32`) up front — revisit only if a security review flags it as insufficient.
- [ ] **B4 — Packaging + signing.** A Windows PyInstaller spec parallel to `PrivacyFenceApp.spec`
      (no `BUNDLE()`/`.icns` step — a plain `EXE(..., console=False, icon="icon.ico")`), plus an
      installer matching the DMG's "one distributable carries both the app and the `.mcpb`" pattern —
      Inno Setup is the natural choice (scriptable, free, widely used for exactly this shape of app).
      Needs Authenticode code-signing: a different cert/process from Apple notarization entirely, new
      secrets in a Windows leg of `build.yml` (signing cert + password, or an EV signing service —
      EV avoids Windows SmartScreen's "unknown publisher" warning on first run, OV doesn't).
- [ ] **B5 — CI.** A `windows-latest` leg in `build.yml`, building and signing the installer,
      parallel to the existing macOS job. Also worth running `pytest` itself once on `windows-latest`
      after B1 lands — Linux CI already proves the portable core runs correctly on a second POSIX
      platform, but only a real Windows run catches path-separator or `Path`-handling bugs Linux
      can't.
- [ ] **B6 — mcpb shim Windows path.** `findDaemonCmd()` needs a Windows `DEFAULT_APP_PATH` (e.g.
      `%ProgramFiles%\PrivacyFence\privacyfence-app.exe`, matching wherever B4's installer places it)
      and the dev fallback should try `python` before/instead of `python3` on Windows (Windows Python
      installs commonly expose only `python`, not a `python3` alias). `mcpb/manifest.json.tmpl`
      itself needs no changes — it declares only a Node runtime requirement, and the shim is plain
      Node/TypeScript, already cross-platform.

---

## Sequencing

1. **A1 first** (org mode battle-test) — lowest risk, flushes out any remaining "assumed POSIX"
   surprises against code that's already believed to work, and unblocks real Linux server users
   immediately with no new infrastructure.
2. **A2.1–A2.2** (local mode, `pip` path + docs) — same low cost, different audience; can run in
   parallel with or right after A1.
3. **B1–B6** (Windows) — bigger and riskier; benefits from A1/A2 having already re-confirmed the
   "daemon has zero macOS-specific behavior left" claim on Linux before spending signing-cert and
   installer effort on a second platform.
4. **A2.3** (`.deb`) — stretch, revisit only once B1–B6 is done or demand clearly warrants pulling it
   forward.

## Tracking

Rewrite/split [#121](https://github.com/andras-tkcs/privacyfence/issues/121) rather than leave it
describing prerequisites (#119/#120) that already shipped: one issue for Track A (Linux — small,
"verify + document, `.deb` as stretch"), one rewritten issue for Track B (Windows — the B1–B6 list
above, all genuinely new work). Each checklist item above is sized to land as its own PR, per this
repo's usual convention for phased plans (see `docs/security-remediation-plan.md` for the pattern).
