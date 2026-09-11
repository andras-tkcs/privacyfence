# Windows Support — Implementation Plan

Implementation plan for Track B of [`windows-linux-support-plan.md`](windows-linux-support-plan.md).
Read that doc first for the overview and for why this is smaller than
[#121](https://github.com/andras-tkcs/privacyfence/issues/121) originally scoped it (#121's own
prerequisites — the native-UI/IPC-socket removal — already shipped as part of P10 of the
`https-connector-refactor-plan.md`). This plan turns Track B's six bullet items into sequenced,
PR-sized phases, and — per explicit instruction — ends with closing #121 itself once every phase
before it has actually shipped and been verified, not as a symbolic first step.

Six phases (B1–B6 from the overview doc, expanded) plus a final closure phase:

## Phase 1 — Fix the instance-lock portability bug (B1)

The one real blocker to the daemon even *starting* on Windows: `daemon_main.py`'s
`_acquire_instance_lock`/`_release_instance_lock` call `fcntl.flock`, which has no Windows
implementation (`fcntl` doesn't exist there at all — this is an `ImportError` at module load, not a
runtime failure).

- [ ] **1.1** Add `portalocker>=2.8` to `pyproject.toml`'s `dependencies` (not a platform-conditional
      extra — it's a small, pure-Python-plus-stdlib-ctypes cross-platform file-locking library that
      picks the right primitive (`fcntl` on POSIX, `msvcrt`/`LockFileEx` on Windows) internally, same
      "don't hand-roll a platform primitive" reasoning `pyproject.toml`'s own comments already give
      for `PyJWT`/`webauthn`). Rejected alternative: a manual `if sys.platform == "win32":
      msvcrt.locking(...) else: fcntl.flock(...)` branch in `daemon_main.py` itself — works, but it's
      exactly the kind of "security/correctness-critical, spec-governed-enough to get subtly wrong"
      primitive this repo's own stated policy says to pull in a maintained dependency for instead.
- [ ] **1.2** Replace the `fcntl` import and the two functions' bodies in `daemon_main.py` with
      `portalocker` calls, keeping the **exact same public contract** — `_acquire_instance_lock() ->
      bool` (non-blocking, returns `False` if already locked rather than raising) and
      `_release_instance_lock() -> None` (idempotent, safe to call when nothing is held). This matters
      because `tests/unit/test_daemon_main.py` (~line 1240–1260 and its `monkeypatch.setattr(...,
      "_acquire_instance_lock", ...)` call sites elsewhere in that file) exercises and stubs these
      functions directly by name — a same-shape swap means that whole suite keeps passing unmodified;
      changing the contract would mean rewriting those tests instead of just re-running them.
- [ ] **1.3** Add a `tests/unit/test_daemon_main.py` case (or extend the existing lock tests) that
      runs under both `portalocker.LOCK_EX | portalocker.LOCK_NB`-style flags to confirm behavior is
      unchanged — the existing tests already cover the double-acquire/release/re-acquire sequence, so
      this is mostly confirming the swap didn't change semantics, not writing new coverage from
      scratch.
- [ ] **1.4** Run the full suite (`pytest -v --cov=src/privacyfence`) on Linux CI as-is after this
      change — `portalocker` on POSIX should be a behavior-transparent swap, so this phase should be
      the *only* one that needs zero Windows access to verify (a real Windows run of this specific
      change is still worth doing once CI exists in Phase 5, but isn't a hard gate to merge this
      phase, since portalocker's Windows path is exercised by its own upstream test suite, not
      something this repo needs to re-prove independently).

## Phase 2 — Windows PyInstaller build (part of B4)

- [ ] **2.1** Add `PrivacyFenceApp.win.spec`, structured like the Linux plan's
      `PrivacyFenceApp.linux.spec` (see `linux-local-deb-packaging-plan.md` Phase 1) — same
      `Analysis`/`hidden_imports`/`datas` (shared via the same `scripts/pyinstaller_common.py` module
      proposed there, so there is exactly one place all three platform specs' shared lists live, not
      three copies drifting independently), `EXE(..., console=False, icon="build/privacyfence.ico")`,
      no `BUNDLE()` step (macOS-only). Output: `dist/PrivacyFenceApp/` onedir containing
      `PrivacyFenceApp.exe`.
- [ ] **2.2** `privacyfence-app` "symlink": Windows has no cheap equivalent NTFS behaves well with
      for this from an unprivileged installer context. Ship `privacyfence-app.exe` as a **copy** of
      `PrivacyFenceApp.exe` in the onedir output instead (a build-script `copy`, not a symlink) — same
      effect (both names resolve on disk) without requiring Developer Mode / admin rights for a
      symlink at install time.
- [ ] **2.3** Icon conversion: unlike Linux (Phase 1 of the `.deb` plan needs no icon conversion —
      raw PNGs are enough), Windows `EXE()` needs a real multi-resolution `.ico`. Neither `sips`/
      `iconutil` (macOS-only, used by `build_dmg.sh`) nor a Windows-native equivalent is available
      cross-platform in CI. Add a small, build-time-only conversion step using `Pillow` (add to the
      `dev` extra in `pyproject.toml`, not a runtime dependency — mirrors how `pyinstaller` itself is
      `dev`-extra-only): `Image.open("icon_512.png").save("privacyfence.ico", sizes=[(16,16),
      (32,32),(48,48),(256,256)])`, called from the new build script (Phase 4).
- [ ] **2.4** Reuse the Telegram-credentials-baking step from `build_dmg.sh` (step 2) unchanged, same
      as the Linux plan's P1.3 — already platform-independent, just needs to run before PyInstaller in
      the new Windows build script too.

## Phase 3 — Autostart (B2)

**Decision: Task Scheduler, not a Startup-folder shortcut**, for closer parity with the macOS
LaunchAgent's crash-restart behavior (`com.privacyfence.app.plist`'s `KeepAlive`/
`SuccessfulExit=false`) and the Linux `.deb` plan's own systemd-unit restart policy — a plain Startup
shortcut launches once at login with no restart-on-crash story at all, which would make Windows the
one platform of the three with strictly weaker autostart behavior.

- [ ] **3.1** Register a Task Scheduler task at install time (from the installer, Phase 4 — not from
      the app itself at runtime, so it's visible/removable through normal Windows install/uninstall
      UI): logon trigger (`schtasks /create /tn "PrivacyFence" /tr
      "%ProgramFiles%\PrivacyFence\privacyfence-app.exe" /sc onlogon /rl limited`), with restart-on-
      failure configured via the task's own `<RestartOnFailure>` XML settings (Task Scheduler supports
      this natively — up to N restarts with a configurable interval, the direct Windows analogue of
      `KeepAlive`).
- [ ] **3.2** Uninstall must remove the scheduled task (`schtasks /delete /tn "PrivacyFence" /f`) —
      wire this into the Inno Setup uninstaller script (Phase 4), not left as a manual step.
- [ ] **3.3** No settings-UI toggle for "launch at login" exists on any platform today (confirmed —
      `grep` across `src/privacyfence/` for autostart-related settings turns up nothing; it's
      install-time-only on macOS too, via the plist). Keep Windows consistent with that — no new
      settings surface needed here, matching the other two platforms' current behavior.

## Phase 4 — Packaging and installer (B4)

- [ ] **4.1** Add `scripts/build_installer.ps1` (PowerShell, since this step only ever runs on a
      Windows build host — mirrors `build_dmg.sh` being bash because it only ever runs on macOS):
      resolve `VERSION` via `importlib.metadata.version("privacyfence")` same as the other two build
      scripts, run PyInstaller against `PrivacyFenceApp.win.spec` (Phase 2), build the `.mcpb` via
      `scripts/build_mcpb.sh` (already cross-platform — plain Node/TypeScript, needs only `bash`
      available, e.g. via CI's Git Bash on `windows-latest`, or port the handful of shell steps it
      does to PowerShell if that proves friction some — flag this as a small decision to make once
      Phase 5's CI leg is actually being written, not before), then invoke Inno Setup (`iscc.exe`)
      against a new `installer/privacyfence.iss`.
- [ ] **4.2** `installer/privacyfence.iss`: installs the onedir output to
      `%ProgramFiles%\PrivacyFence\`, the `.mcpb` alongside it (mirrors the DMG's "one distributable
      carries both halves" — see `build_dmg.sh`'s own module comment), Start Menu shortcut(s) pointing
      at `http://127.0.0.1:8765/settings` in the default browser rather than at the daemon executable
      directly (same reasoning as the Linux plan's P3.2 — there's nothing useful to show for
      double-clicking the daemon binary itself), a standard Windows uninstaller entry
      (Add/Remove Programs), and wires the Task Scheduler registration/removal from Phase 3 into its
      install/uninstall steps.
- [ ] **4.3** Explicitly scope what the installer does **not** touch, mirroring the Linux plan's
      P2.2: per-user data lives under `%USERPROFILE%\.privacyfence\` (`paths.py`'s `data_dir()`,
      unchanged on Windows — `Path.home()` resolves correctly there already), created by the app on
      first run, never touched by the installer or uninstaller. Uninstalling removes the program files
      and the scheduled task only.
- [ ] **4.4** File-permissions gap (B3 from the overview doc): `chmod(0o600/0o700)` calls on
      credential/token files are silent no-ops on Windows rather than errors, so credentials rely on
      default NTFS user-profile ACLs rather than an explicit lock-down. **Decision carried over from
      the overview doc: document as a known, accepted v1 gap** (a single-user Windows profile is
      already ACL-restricted to that user by default) rather than adding `icacls`/`pywin32`-based
      ACL-tightening — revisit only if a security review flags it as insufficient. Add one sentence
      to `TECHNICAL_REFERENCE.md`'s eventual Windows section (Phase 7) saying so explicitly, so it
      reads as a decision, not an oversight.

## Phase 5 — Code signing (part of B4)

- [ ] **5.1** Decide OV vs. EV Authenticode certificate before setting up CI signing — this is a real
      cost/tradeoff, not a default to assume silently: OV (organization-validated) is cheaper and
      simpler to obtain but new installer builds still trigger a SmartScreen "unrecognized publisher"
      warning until the binary accumulates enough install reputation; EV (extended-validation) avoids
      that warning immediately but requires a hardware token or a cloud HSM-backed signing service
      (e.g. Azure Trusted Signing, SignPath, DigiCert KeyLocker) rather than a portable `.pfx` file —
      changes what CI's signing step looks like. **Flagging this rather than picking one** — say which
      you want before Phase 5 starts; default assumption if not specified: start with OV via a
      portable `.pfx` (closer to the macOS `.p12` flow `build.yml` already has, so the CI secrets
      pattern — base64-encoded cert + password — carries over directly), revisit EV later if
      SmartScreen friction turns out to matter for real users.
- [ ] **5.2** Sign both the daemon `.exe` and the installer `.exe` itself (`signtool.exe sign /fd
      sha256 /a /tr <timestamp-server> /td sha256 ...`) — signing only the installer and not the
      inner binary still shows an unrecognized-publisher warning if a user runs `PrivacyFenceApp.exe`
      directly rather than through the installer.
- [ ] **5.3** Add the Windows signing secrets to `.github/workflows/build.yml`'s new Windows job
      (Phase 6) following the same pattern as the existing macOS certificate-import step: base64
      cert + password (or the HSM-service credentials, if 5.1 lands on EV) as GitHub secrets, decoded
      and used only within that job.

## Phase 6 — CI (B5)

- [ ] **6.1** Add a `windows-latest` job to `.github/workflows/build.yml`, triggered identically to
      the macOS job (`push: tags: ['v*']` + `workflow_dispatch`), running Phase 4's
      `build_installer.ps1` and Phase 5's signing, uploading the signed installer `.exe` to the same
      GitHub Release the macOS (and, once shipped, Linux `.deb`) jobs attach to — one release, three
      platform assets.
- [x] **6.2** Added a `windows-latest` run of `pytest -v --cov=src/privacyfence
      --cov-report=term-missing` (`tests.yml`'s `test-windows` job) right after Phase 1 landed, as a
      real-Windows confirmation that nothing path-separator- or `Path`-handling-related breaks that
      Linux CI's existing full-suite run (already proven platform-independent, per
      `windows-linux-support-plan.md`) can't catch. Gated to `workflow_dispatch` only at first — and,
      it turned out, never actually dispatched-and-passed for real in that state (no recorded
      `workflow_dispatch` run of this job exists prior to 6.3 below actually exercising it). The
      decision this item left open — keep it permanent, or drop back to relying on `build.yml`'s
      release-time Windows job alone — is now made: `docs/automated-test-strategy-plan.md` Phase 2.1
      promoted it to a permanent per-PR leg (small ongoing CI cost, catches regressions before
      release rather than only at tag-build time), renamed `platform-windows` to match. `Phase 2.3`
      has since added `tests/platform/` (runs here automatically as part of the suite below), and
      `Phase 2.4` decided this job keeps running the full core suite rather than being narrowed down
      to just that targeted subset — `docs/automated-test-strategy-plan.md` Phase 2's own status
      note records the reasoning: this job's own first real run (6.3 below) is the evidence that a
      subset defined ahead of time would have missed real bugs the full suite caught.
- [x] **6.3** First real run of 6.2's promoted job (on the PR that landed 2.1 itself) failed 55
      tests + 1 error — this is what actually running the full suite on Windows for the first time
      was always going to surface, not a regression from that PR's own (CI-only) diff. Triaged and
      closed out per-cause rather than blanket-skipped:
      - **Fixed** (real, narrow, cross-platform-safe bugs, not Windows-only workarounds):
        `settings_controller.py`'s org-install-date formatting used `strftime("%b %-d, %Y")` —
        `%-d` is a glibc/macOS strftime extension the Windows CRT rejects
        (`ValueError: Invalid format string`); rewritten to build the no-leading-zero day without a
        platform-specific directive. `drive.py`'s upload preview/PII-scan guessed a local file's
        MIME type via `mimetypes.guess_type()`, whose result for `.docx`/`.pptx`/`.xlsx`/`.zip` can
        depend on that machine's Windows registry rather than being the one fixed answer this
        connector always treats them as — `text_extraction.guess_mime_type()` now answers those four
        extensions from a fixed table first. `test_shim_mcp_contract.py`'s `built_shim_entry` fixture
        passed bare `"npm"` to `subprocess.run()`; Windows' `npm` is `npm.cmd`, and unlike
        `shutil.which()` (which the fixture already called one line above), `CreateProcess` never
        consults `PATHEXT` itself — resolved to `shutil.which("npm")`'s actual path instead.
        `test_verify_audit_log.py`'s `~`-expansion test only set `$HOME`, which controls
        `os.path.expanduser()` on POSIX but not reliably on Windows (`$USERPROFILE`) — now sets both.
      - **Skipped on Windows, not fixed** (the POSIX file-permission model itself — ~40 tests across
        `test_secure_files.py`, `test_paths.py`, every `*_client.py`'s `TestSaveToken`, `test_server.py`,
        `test_webauthn_stepup.py`, and the `test_daemon_main.py` org-mode-startup tests that trip
        `secure_files.audit_directory_permissions()` for the same reason): this is exactly B3's
        already-documented "known, accepted gap" (`windows-linux-support-plan.md`, the ACL-tightening
        note) — `chmod`/`stat().st_mode` verification has nothing meaningful to assert on Windows
        without the ACL-tightening work that note already deferred, so these tests
        `@pytest.mark.skipif(sys.platform == "win32", ...)` rather than pretending to prove something
        Windows doesn't have. One related but distinct case, `TestInstanceLock::
        test_lock_file_records_holder_pid`, is skipped for a different reason: portalocker's Windows
        backend (Phase 1 above) takes a *mandatory* lock, so this test's own same-process re-open of
        the lock file to read it back raises `PermissionError` there — the instance-lock feature
        itself is unaffected, only this test's verification method doesn't port.
      - **New finding, tracked, not fixed here** (7 tests, `test_daemon_main.py`'s `TestResolvePath`
        (both cases) and `TestBuildConnectorsTelegram::test_built_when_creds_and_session_present`,
        plus one preview-text test each in `test_confluence_connector.py`/`test_gmail_connector.py`
        and two in `test_drive_connector.py`): code that builds a path by combining a POSIX-style
        string (a literal like `"credentials/telegram.session"`, or a `destination_dir`/config value
        of `"/tmp"`) with `os.path.join()`/`os.path.isabs()` gets Windows-`ntpath` behavior applied to
        a string that was never meant to be OS-specific — at best a cosmetically mixed separator
        (`/tmp\report.pdf`), at worst (`daemon_main._resolve_path("/etc/hosts")` returning
        `D:/etc/hosts` instead of the path unchanged) a silently *different on-disk location* than
        intended, because Python 3.13 narrowed `ntpath.isabs()` to require a drive letter, so a
        rootless POSIX-style path that used to read as absolute on Windows no longer does. This
        touches `_resolve_path()` (used to locate every credential/token file for the local
        principal) and each connector's `resolve_*_destination()` helpers, so it's a real
        security-adjacent design question — does this codebase treat these path strings as always
        POSIX-style internally, or does it need to reject/normalize a POSIX-style absolute path on
        Windows instead of silently reinterpreting it? — deliberately left for whoever picks this up
        to design deliberately rather than patched reflexively under CI-red pressure. The seven tests
        above are skipped on Windows in the meantime with a reason pointing back to this paragraph.

## Phase 7 — mcpb shim Windows support (B6)

- [ ] **7.1** In `mcpb/shim/src/daemon.ts`, make `DEFAULT_APP_PATH` platform-conditional:
      `process.platform === "win32" ? "C:\\Program Files\\PrivacyFence\\privacyfence-app.exe" :
      "/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app"` (extend further once the
      Linux `.deb` plan's `/usr/bin/privacyfence-app` path is also wired in here, per that plan's
      A2.4 — all three platform defaults can land in the same small change once both this phase and
      that one exist).
- [ ] **7.2** The dev fallback (`["python3", "-m", "privacyfence.daemon_main"]`) should try `python`
      before/instead of `python3` when `process.platform === "win32"` — Windows Python installs
      commonly expose only `python`, not a `python3` alias (the reverse of most POSIX distros).
- [ ] **7.3** Add `mcpb/shim/test/daemon.test.ts` cases covering the Windows branch of
      `findDaemonCmd()` (the existing tests already exercise `defaultAppPath` as an injectable
      override per `FindDaemonCmdOptions` — this is a small addition, not new test infrastructure).
- [ ] **7.4** Confirm `mcpb/manifest.json.tmpl` needs no changes — it declares only a Node runtime
      requirement (`compatibility.runtimes.node`), and the shim itself is already plain, dependency-
      free TypeScript with no platform-specific code outside `daemon.ts` — worth a manual install-and-
      run check on Windows once Phase 8's QA pass happens, not a code change to plan for here.

## Phase 8 — Manual QA pass (real Windows machine or VM)

Everything above is either automatable in CI or a code change; this phase is the human-in-the-loop
verification that ties it together, parallel to the Linux plan's Phase 7:

- [ ] **8.1** Install via the signed installer (Phase 4/5) on a clean Windows VM. Confirm no
      SmartScreen block prevents installation (or, if using an OV cert per 5.1's default, confirm the
      warning is the expected "unrecognized publisher, run anyway" rather than an outright block).
- [ ] **8.2** Log out/in (or reboot), confirm the daemon autostarts via the Task Scheduler task
      (Phase 3) — check `%USERPROFILE%\.privacyfence\mcp_url` exists and names a port something is
      listening on, same signal the mcpb shim's own `socketConnectable()` already checks.
- [ ] **8.3** Confirm the OAuth loopback flow (`oauth_loopback.py`'s `webbrowser.open()`, already
      cross-platform code — no change expected, just needs a real confirmation) opens the default
      browser correctly and completes a real connector auth (Gmail is the natural pick, matching the
      org-mode Linux guide's own choice of example connector).
- [ ] **8.4** Kill the daemon process directly (simulate a crash) and confirm the Task Scheduler
      restart-on-failure policy (Phase 3.1) actually restarts it — this is the one behavior that's
      hard to verify any other way than watching it happen on a real machine.
- [ ] **8.5** Uninstall via Add/Remove Programs, confirm: program files gone, scheduled task gone,
      `%USERPROFILE%\.privacyfence\` (credentials, settings, audit log) **untouched** (per 4.3).
- [ ] **8.6** Install the `.mcpb` into a real Claude Desktop on the same machine, confirm the shim
      (Phase 7) finds and launches the daemon correctly end to end — a live MCP tool call through to
      one of the connectors set up in 8.3.

## Phase 9 — Docs

- [ ] **9.1** `README.md`: add a Windows install path (download the signed installer, run it)
      alongside the macOS DMG and (once it exists) the Linux `.deb`/`pip` paths. Remove "macOS host"
      from the "Current implementation assumptions" list once this and the Linux work have both
      shipped — that line becomes actively wrong the moment either lands.
- [ ] **9.2** `TECHNICAL_REFERENCE.md`: add a "Windows" installation section parallel to the existing
      "Linux" one, covering the installer, the Task Scheduler autostart, and the file-permissions
      caveat from 4.4.

## Phase 10 — Close the issue

- [ ] **10.1** Only after Phases 1–9 have actually shipped in a released version (a real tag, a real
      GitHub Release carrying the signed Windows installer, not just merged PRs) and Phase 8's manual
      QA pass has been run against that release build specifically (not an earlier dev build) — post
      a closing summary comment on
      [#121](https://github.com/andras-tkcs/privacyfence/issues/121) naming the release tag/version
      that ships Windows support and briefly noting what changed since the issue was filed (the
      native-UI/IPC prerequisites it originally listed already shipped separately at P10; this work
      closes the remaining packaging/autostart/signing/CI gap it called out), then close the issue
      with `state_reason: completed`.
- [ ] **10.2** If anything from Phases 1–9 is deliberately deferred rather than shipped (e.g. Phase
      4's EV-vs-OV cert decision lands on "ship OV now, EV later" per 5.1, or arm64 Windows support is
      out of scope entirely, unlike the Linux plan's arm64 follow-up), say so explicitly in the same
      closing comment rather than closing #121 silently over known-incomplete scope — open a fresh,
      narrower follow-up issue for anything intentionally deferred so it doesn't get lost.

---

Do not skip ahead to Phase 10 while any earlier phase is only "code merged, not yet released and
verified" — the instruction this plan was written against is explicit that closing the issue is the
*last* step, gated on the real thing existing and working, not a checkbox to tick alongside the
code changes.
