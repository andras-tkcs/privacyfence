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
; The autostart task-definition template (see [Code]'s RegisterAutostartTask
; below) -- dontcopy means Setup extracts it to {tmp} for [Code] to read at
; install time, but it's never actually installed into {app}.
Source: "privacyfence-task.xml.tmpl"; Flags: dontcopy

[Icons]
; Points at the web settings UI in the default browser, not at the daemon
; executable directly -- there's nothing useful to show for double-clicking
; a headless background daemon (same reasoning as the Linux .deb plan's
; P3.2 for its own .desktop entry).
Name: "{group}\{#AppName}"; Filename: "{#SettingsUrl}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; The autostart Task Scheduler task itself (Phase 3.1) is registered from
; [Code]'s RegisterAutostartTask below, via CurStepChanged(ssPostInstall)
; -- not a [Run] entry -- see that function's own comment and
; privacyfence-task.xml.tmpl's header comment for what actually gets
; registered and why this needs a real Task Scheduler XML task definition
; rather than a plain `schtasks /create` CLI call (short version: two
; independent CLI-flag attempts at this were each found broken by a real
; windows-graphical-session.yml run -- invalid /ri/du flags for an ONLOGON
; schedule, then a trigger that only fired for the installing account
; instead of any interactive logon -- and the capability this task
; actually needs, "run for whichever user just logged on, in their own
; session, restarting on crash," has no equivalent exposed through
; schtasks.exe's plain flags at all).
;
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

[Code]
(* Copies whatever schtasks.exe wrote on stdout/stderr into Setup's own log
   file, one line per log entry.

   Everything about the autostart registration below was otherwise
   invisible: Inno logs its own [Run] entries automatically but says
   nothing at all about an Exec() call made from [Code], and Exec()
   captures neither stream. So a run of consecutive real
   windows-graphical-session.yml runs could establish only that the task
   was missing afterwards, never why -- the install log they dumped
   (tests/integration/test_windows_graphical_session_autostart.py's own
   _install_log_tail) went straight from "Installation process succeeded"
   to "Deinitializing Setup" with no trace of the registration attempt in
   between. An exit code alone would not have closed that gap either: when
   this finally ran, schtasks' own stderr was the thing that named the
   defect outright ("(1,40)::ERROR: unable to switch the encoding", the
   task XML's encoding declaration -- see privacyfence-task.xml.tmpl). *)
procedure LogSchtasksOutput(OutFile: String);
var
  Lines: TArrayOfString;
  I: Integer;
begin
  if not FileExists(OutFile) then
  begin
    Log('RegisterAutostartTask: no schtasks output file at ' + OutFile);
    Exit;
  end;
  if not LoadStringsFromFile(OutFile, Lines) then
  begin
    Log('RegisterAutostartTask: could not read schtasks output file ' + OutFile);
    Exit;
  end;
  for I := 0 to GetArrayLength(Lines) - 1 do
    Log('RegisterAutostartTask: schtasks: ' + Lines[I]);
end;

(* Registers the autostart Task Scheduler task (Phase 3.1) via a real Task
   Scheduler XML task definition (privacyfence-task.xml.tmpl, extracted to
   the temp directory by the [Files] "dontcopy" entry above), not
   schtasks.exe's plain /create flags -- see [Run]'s own comment and that
   template's own header comment for why. Substitutes the real installed
   AliasExeName path for the template's __EXEC_PATH__ placeholder, writes
   the result to a scratch file, then runs `schtasks /create /xml <file>
   /f` against it. Called from CurStepChanged(ssPostInstall) below, i.e.
   after the app's files are already in place (Files copy happens during
   ssInstall, before ssPostInstall) but before this file's own [Run]
   entries execute, so the path it substitutes in always exists by the
   time schtasks reads it.

   schtasks runs through cmd.exe rather than directly for one reason only:
   so its stdout and stderr can be redirected to a file and read back into
   the log (LogSchtasksOutput above).

   The whole body is wrapped in try/except with a Log() at each step. That
   was added while the failure was still unexplained, on the theory that an
   unhandled Pascal Script runtime exception was silently aborting Setup's
   post-install processing; the actual defect turned out to be in the task
   XML itself, and no exception was ever involved. It stays because the
   reasoning behind it holds regardless: an exception here would otherwise
   abort the rest of post-install with the overall install still reporting
   success, and nothing would say so.

   Deliberately using this parenthesis-asterisk comment style rather than
   curly braces: Pascal's curly-brace comments don't nest, and the
   {app}/{tmp}-style Inno constant references this comment needs to talk
   about would otherwise close the comment early at their own closing
   brace -- exactly the "'BEGIN' expected" compile error an earlier
   version of this comment actually hit. *)
function RegisterAutostartTask(): Boolean;
var
  TemplateFile, XmlFile, OutFile, XmlContent, ExecPath, CmdLine: String;
  RawContent: AnsiString;
  ResultCode: Integer;
begin
  Result := False;
  try
    Log('RegisterAutostartTask: starting');
    ExtractTemporaryFile('privacyfence-task.xml.tmpl');
    TemplateFile := ExpandConstant('{tmp}\privacyfence-task.xml.tmpl');
    Log('RegisterAutostartTask: template path = ' + TemplateFile);
    { LoadStringFromFile's own "var S" output parameter is typed
      AnsiString, not String -- passed as RawContent here and converted
      (a plain assignment allows the AnsiString/String conversion that a
      var parameter, like StringChangeEx's own first argument below,
      does not) rather than declared as the var parameter's own type
      throughout, so every other call in this function can use the
      ordinary String type. }
    if not LoadStringFromFile(TemplateFile, RawContent) then
    begin
      Log('RegisterAutostartTask: LoadStringFromFile returned False');
      Exit;
    end;
    Log('RegisterAutostartTask: loaded template, ' + IntToStr(Length(RawContent)) + ' bytes');
    XmlContent := RawContent;
    ExecPath := ExpandConstant('{app}\{#AliasExeName}');
    Log('RegisterAutostartTask: exec path = ' + ExecPath);
    StringChangeEx(XmlContent, '__EXEC_PATH__', ExecPath, False);
    XmlFile := ExpandConstant('{tmp}\privacyfence-task.xml');
    if not SaveStringToFile(XmlFile, XmlContent, False) then
    begin
      Log('RegisterAutostartTask: SaveStringToFile returned False');
      Exit;
    end;
    Log('RegisterAutostartTask: wrote ' + XmlFile + ', ' + IntToStr(Length(XmlContent)) + ' bytes, running schtasks');

    OutFile := ExpandConstant('{tmp}\schtasks-create.out');
    { The doubled outer quotes are cmd.exe's own rule for a /C command line
      whose first token is itself a quoted path: cmd strips the outermost
      pair and runs what is left. }
    CmdLine := '/C ""' + ExpandConstant('{sys}\schtasks.exe') +
      '" /create /tn "{#TaskName}" /xml "' + XmlFile + '" /f > "' + OutFile + '" 2>&1"';
    if not Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode) then
    begin
      Log('RegisterAutostartTask: Exec itself failed to launch cmd.exe');
      Exit;
    end;
    LogSchtasksOutput(OutFile);
    Log('RegisterAutostartTask: schtasks exit code = ' + IntToStr(ResultCode));
    Result := ResultCode = 0;
  except
    Log('RegisterAutostartTask: exception: ' + GetExceptionMessage);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    (* A failed registration deliberately does not abort the install: every
       other part of the install is still usable without autostart, and the
       user can start PrivacyFence from the Start menu meanwhile. It is
       logged as a failure rather than passed over silently so that the
       install log actually says so -- which is also what Phase 7's
       graphical-session test reads back when it finds the task missing. *)
    if not RegisterAutostartTask() then
      Log('RegisterAutostartTask: FAILED; PrivacyFence will not start ' +
          'automatically at logon.');
  end;
end;
