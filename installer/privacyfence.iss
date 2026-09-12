; PrivacyFence Windows installer (Inno Setup 6).
;
; The now-removed docs/windows-support-plan.md Phase 4 (B4 in the now-removed docs/windows-linux-support-
; plan.md) -- the Windows analogue of build_dmg.sh's DMG: one distributable
; carrying both the daemon and the Claude Desktop extension (.mcpb), plus
; (unlike the drag-to-Applications DMG) the autostart wiring a real installer
; can do that a disk image can't -- registering/removing the Task Scheduler
; task from Phase 3.
;
; Built by scripts/build_installer.ps1, which passes every {#...} value below
; on the command line (/D...) rather than hardcoding a version or absolute
; paths here -- this file has no VERSION of its own to keep in sync, same
; "derive it from the git tag, don't hand-bump a second copy" reasoning this
; repo's CLAUDE.md gives for pyproject.toml/__init__.py.
;
; Do not run this directly with defaults -- it expects every /D on
; build_installer.ps1's iscc.exe invocation to be supplied.

#ifndef AppVersion
  #error "Pass /DAppVersion=x.y.z (see scripts/build_installer.ps1)"
#endif
#ifndef DistDir
  #error "Pass /DDistDir=<path to dist/PrivacyFenceApp> (see scripts/build_installer.ps1)"
#endif
#ifndef McpbPath
  #error "Pass /DMcpbPath=<path to the built .mcpb> (see scripts/build_installer.ps1)"
#endif
#ifndef IconPath
  #error "Pass /DIconPath=<path to privacyfence.ico> (see scripts/build_installer.ps1)"
#endif
#ifndef OutputDir
  #error "Pass /DOutputDir=<output directory> (see scripts/build_installer.ps1)"
#endif
#ifndef SetupBaseName
  #error "Pass /DSetupBaseName=<setup exe base name, no extension> (see scripts/build_installer.ps1)"
#endif

#define AppName "PrivacyFence"
#define AppExeName "PrivacyFenceApp.exe"
#define AliasExeName "privacyfence-app.exe"
; Where the embedded web settings/approval UI listens by default -- see
; web/server.py's DEFAULT_PORT / default host. Not user-configurable at
; install time (no settings UI toggle exists for this on any platform
; today, same as Phase 3.3's autostart decision).
#define SettingsUrl "http://localhost:8765/settings"
; Matches daemon.ts's Windows DEFAULT_APP_PATH (docs/windows-support-
; plan.md Phase 7 / B6) -- keep these in sync if this changes.
#define InstallDirName "PrivacyFence"
#define TaskName "PrivacyFence"

[Setup]
AppId={{B6E3B6C4-6C2E-4A8B-9C4C-3B6C6E7C6C1B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PrivacyFence
DefaultDirName={autopf}\{#InstallDirName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir={#OutputDir}
OutputBaseFilename={#SetupBaseName}
SetupIconFile={#IconPath}
Compression=lzma2
SolidCompression=yes
; A single-user desktop daemon has no reason to demand an admin elevation
; prompt just to install under Program Files -- lowestprivilege still lets
; per-machine Program Files installs proceed under a standard account's own
; write access where the OS allows it, and falls back to the standard UAC
; prompt otherwise, same tradeoff the DMG's drag-install has no equivalent
; decision for at all.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
; The whole onedir PyInstaller output -- PrivacyFenceApp.exe,
; privacyfence-app.exe (built as a real copy, not a symlink; see
; build_installer.ps1 step 4), and every bundled dependency/data file.
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; The Claude Desktop extension, alongside the daemon -- mirrors the DMG's
; "one distributable carries both halves" (build_dmg.sh's own module
; comment). Kept at its versioned filename so a user who's kept an older
; installer's copy doesn't collide with it.
Source: "{#McpbPath}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Points at the web settings UI in the default browser, not at the daemon
; executable directly -- there's nothing useful to show for double-clicking
; a headless background daemon (same reasoning as the Linux .deb plan's
; P3.2 for its own .desktop entry).
Name: "{group}\{#AppName}"; Filename: "{#SettingsUrl}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; Register the autostart Task Scheduler task at install time (Phase 3.1),
; not from the app itself at runtime, so it's visible/removable through
; normal Windows install/uninstall UI. Logon trigger + limited run level
; (no elevation) -- the direct analogue of the macOS LaunchAgent's
; RunAtLoad and the Linux .deb plan's own XDG autostart entry.
;
; Deliberately NOT /RI/DU: an earlier version of this line added
; "/ri 1 /du 9999:59" trying to get crash-restart behavior (the macOS
; LaunchAgent's KeepAlive/SuccessfulExit=false, the Linux .deb plan's own
; systemd restart policy) out of schtasks.exe's CLI flags. Per Microsoft's
; own schtasks /create documentation, /ri and /du are "not applicable" to
; an ONLOGON schedule (/ri is valid only for MINUTE/HOURLY/DAILY/WEEKLY/
; MONTHLY/ONCE; /du only for MINUTE/HOURLY) -- schtasks.exe rejects the
; combination outright, so this whole /create call was failing on every
; install ("Task Scheduler task 'PrivacyFence' missing after install",
; a confirmed root cause of windows-graphical-session.yml's failures --
; see platform-support.md's "Known open items"), silently, because an
; Inno [Run] entry's own nonzero exit code doesn't abort Setup by
; default. There is no equivalent restart-on-failure knob exposed
; through schtasks.exe's plain flags at all -- Task Scheduler only
; exposes it via a task's own <RestartOnFailure> XML settings
; (schtasks /create /xml), which needs a real Windows host to get the
; file encoding/schema right and isn't implemented here yet; tracked as
; docs/automated-test-strategy-plan.md Phase 13. Until then this task is
; logon-triggered only, same single-shot-at-login behavior a plain
; Startup-folder shortcut would have given -- strictly less than the
; crash-restart parity Phase 3's own decision wanted, but a working
; autostart beats a task that was never actually being created.
;
; /RU "BUILTIN\Users": a second, independent bug in this same line, found
; once the /ri/du fix above let registration itself succeed for the first
; time. Omitting /RU entirely (as this line used to) does NOT make the
; ONLOGON trigger fire for any interactive logon -- per Microsoft's own
; schtasks /create documentation, "By default, the task runs with the
; permissions of the current user" -- so the task was scoped to whichever
; account ran the installer only, never firing for a different account's
; later logon. A real end-to-end run (windows-graphical-session.yml's own
; throwaway-account logon, a different account from the one that ran the
; installer) caught this: task registration succeeded, but the trigger
; never fired within 30s of that account's logon. `/ru "BUILTIN\Users"`
; targets the built-in Users group rather than one specific account, the
; standard technique for "run once per interactive logon, in that user's
; own session, whoever they are" (no password needed or accepted for a
; well-known built-in group, same as `/ru System` needing none) -- the
; actual "whichever account is at the keyboard" scope this task always
; intended, now for real rather than by an incorrect assumption about the
; unqualified default.
Filename: "{sys}\schtasks.exe"; \
    Parameters: "/create /tn ""{#TaskName}"" /tr ""'{app}\{#AliasExeName}'"" /sc onlogon /ru ""BUILTIN\Users"" /rl limited /f"; \
    Flags: runhidden; StatusMsg: "Registering startup task..."
; Start the daemon immediately after install, same as the macOS DMG's
; LaunchAgent starting the app right after a drag-install's first login --
; without this, a user would otherwise have to log out/in before
; PrivacyFence is running at all.
Filename: "{app}\{#AliasExeName}"; Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Must remove the scheduled task -- wired into the uninstaller here, not
; left as a manual step (Phase 3.2). RunOnceId so this only ever runs once
; per uninstall even if Inno retries the uninstall step.
Filename: "{sys}\schtasks.exe"; Parameters: "/delete /tn ""{#TaskName}"" /f"; \
    Flags: runhidden; RunOnceId: "RemovePrivacyFenceTask"

[UninstallDelete]
; Explicitly scope what uninstall does NOT touch (Phase 4.3): per-user data
; -- credentials, settings, the audit log -- lives under
; %USERPROFILE%\.privacyfence\ (paths.py's data_dir(), unchanged on Windows
; since Path.home() resolves correctly there already), created by the app on
; first run. Uninstalling removes the program files (handled automatically
; by Inno Setup for everything under {app}) and the scheduled task
; (UninstallRun, above) only -- there is deliberately no [UninstallDelete]
; entry naming %USERPROFILE%\.privacyfence, unlike the entries a "clean
; uninstall" for a typical app might add.
