# Build PrivacyFence-<version>-setup.exe -- the Windows installer.
#
# The now-removed docs/windows-support-plan.md Phase 4 (B4 in the now-removed docs/windows-linux-support-
# plan.md). PowerShell, not bash, since this step only ever runs on a
# Windows build host -- mirrors build_dmg.sh being bash because it only
# ever runs on macOS.
#
# Like the DMG, the installer is the single distributable: it carries both
# halves of PrivacyFence (the daemon .exe and PrivacyFence.mcpb) so the user
# flow is "run the installer, then double-click PrivacyFence.mcpb in Claude
# Desktop" -- no separate downloads.
#
# Prerequisites (needed only on your build machine, not end-user machines):
#   pip install -e ".[dev]"     # PrivacyFence itself + pyinstaller + Pillow
#                                # (icon conversion, Phase 2.3), so VERSION
#                                # below can read its installed metadata
#                                # (git-tag-derived, see this repo's
#                                # CLAUDE.md "Releasing" section)
#   Inno Setup 6                # https://jrsoftware.org/isinfo.php -- iscc.exe
#                                # must be on PATH (winget install
#                                # JRSoftware.InnoSetup, or add its install
#                                # dir to PATH by hand)
#   Git for Windows              # supplies the bash scripts/build_mcpb.sh
#                                # needs; its bash.exe must be on PATH (the
#                                # default Git for Windows install already
#                                # does this)
#   node + npm on PATH           # for scripts/build_mcpb.sh
#   signtool.exe on PATH         # only if $env:SIGN_CERT_PATH is set (Phase 5)
#                                # -- ships with the Windows SDK
#
# Usage:
#   pwsh ./scripts/build_installer.ps1
#
# Optional signing env vars (Phase 5 -- unset means an unsigned build):
#   SIGN_CERT_PATH       Path to an Authenticode .pfx
#   SIGN_CERT_PASSWORD   The .pfx's export password
#   SIGN_TIMESTAMP_URL   RFC 3161 timestamp server (default: DigiCert's)
#
# Output: dist/PrivacyFence-<version>-setup.exe
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Version comes from the git tag via setuptools_scm now (this repo's
# CLAUDE.md "Releasing" section) -- read back through the installed
# package's own metadata, same as PrivacyFenceApp.win.spec and
# src/privacyfence/__init__.py. Fails clearly if PrivacyFence itself hasn't
# been `pip install -e .`d yet, same as build_dmg.sh's identical check.
$Version = python -c "from importlib.metadata import version; print(version('privacyfence'))"
if ($LASTEXITCODE -ne 0) { throw "Could not resolve installed privacyfence version" }
$ProductName = "PrivacyFence"
$AppName = "PrivacyFenceApp"
$SetupName = "${ProductName}-${Version}-setup.exe"

Write-Host "=== Building ${ProductName} ${Version} (Windows) ==="

# -- 1. Convert PNG icon to a multi-resolution .ico (Phase 2.3) --------------
# Neither sips/iconutil (macOS-only, used by build_dmg.sh) nor a Windows-
# native equivalent is available cross-platform in CI, so this uses Pillow
# instead (added to the `dev` extra in pyproject.toml, not a runtime
# dependency -- mirrors how pyinstaller itself is dev-extra-only).
$IconSrc = "src/privacyfence/resources/icon_512.png"
$IcoPath = "build/privacyfence.ico"
if (-not (Test-Path $IcoPath)) {
    Write-Host "-> Converting icon to .ico..."
    New-Item -ItemType Directory -Force -Path "build" | Out-Null
    python -c @"
from PIL import Image
img = Image.open('$IconSrc')
img.save('$IcoPath', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
"@
    if ($LASTEXITCODE -ne 0) { throw "Icon conversion failed" }
}

# -- 2. Bake in the Telegram app credentials ---------------------------------
# Same reasoning and same git-ignored destination module as build_dmg.sh's
# identically-numbered step: api_id/api_hash identify the PrivacyFence app to
# Telegram (not an organization -- see docs/telegram-setup.md), and this repo
# is public, so they're never committed. CI supplies them as
# TELEGRAM_API_ID/TELEGRAM_API_HASH env vars; a local build without them set
# just ships without Telegram support.
$CredsFile = "src/privacyfence/_telegram_credentials.py"
if ($env:TELEGRAM_API_ID -and $env:TELEGRAM_API_HASH) {
    Write-Host "-> Writing Telegram app credentials..."
    @"
API_ID = $($env:TELEGRAM_API_ID)
API_HASH = "$($env:TELEGRAM_API_HASH)"
"@ | Set-Content -Path $CredsFile -Encoding utf8NoBOM
} else {
    Write-Host "-> TELEGRAM_API_ID/TELEGRAM_API_HASH not set; building without Telegram app credentials."
    Remove-Item -Force -ErrorAction SilentlyContinue $CredsFile
}

# -- 3. Build the onedir daemon with PyInstaller -----------------------------
# --clean forces a fresh module analysis every time, same reasoning as
# build_dmg.sh's identical flag: PyInstaller otherwise caches "module not
# found" results in build/PrivacyFenceApp.win/, so a rebuild right after
# _telegram_credentials.py first appears (step 2, above) could otherwise
# silently keep using a stale analysis from before the file existed.
Write-Host "-> Running PyInstaller..."
pyinstaller --noconfirm --clean PrivacyFenceApp.win.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$DistDir = "dist/${AppName}"

# -- 4. Create privacyfence-app.exe alongside PrivacyFenceApp.exe -----------
# The Task Scheduler task (installed by the .iss script below, Phase 3) and
# the mcpb shim's own daemon auto-start (daemon.ts's DEFAULT_APP_PATH) both
# look for this name; the main exe is "PrivacyFenceApp.exe". Windows has no
# cheap equivalent NTFS behaves well with for a symlink from an unprivileged
# installer context (unlike the macOS/Linux builds' plain `ln -s`), so this
# ships as a real copy instead -- same effect (both names resolve on disk).
$MainExe = Join-Path $DistDir "PrivacyFenceApp.exe"
$AliasExe = Join-Path $DistDir "privacyfence-app.exe"
Write-Host "-> Creating privacyfence-app.exe copy..."
Copy-Item -Force $MainExe $AliasExe

# -- 5. Build the Claude Desktop extension (.mcpb) ---------------------------
# scripts/build_mcpb.sh is plain bash + Node/TypeScript -- no macOS-specific
# step in it -- so it runs unchanged here via Git for Windows' bash.exe
# rather than being ported to PowerShell (the now-removed docs/windows-support-plan.md
# Phase 4.1 flagged this as the small decision to make once this script was
# actually being written; bash it is, since Git for Windows is already a
# near-universal Windows dev-machine prerequisite and this avoids a second,
# drifting copy of that script's logic).
Write-Host "-> Building PrivacyFence's Claude Desktop extension..."
bash scripts/build_mcpb.sh
if ($LASTEXITCODE -ne 0) { throw "build_mcpb.sh failed" }
$McpbPath = "dist/${ProductName}-${Version}.mcpb"

# -- 6. Optional code-signing of the daemon executables (Phase 5) -----------
# Sign both PrivacyFenceApp.exe and privacyfence-app.exe -- signing only the
# installer and not these would still show an unrecognized-publisher warning
# if a user runs either directly rather than through the installer.
$TimestampUrl = if ($env:SIGN_TIMESTAMP_URL) { $env:SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
function Invoke-Signing([string]$Path) {
    if (-not $env:SIGN_CERT_PATH) { return }
    Write-Host "-> Signing $Path..."
    & signtool.exe sign /fd sha256 /f "$env:SIGN_CERT_PATH" /p "$env:SIGN_CERT_PASSWORD" `
        /tr $TimestampUrl /td sha256 "$Path"
    if ($LASTEXITCODE -ne 0) { throw "signtool failed on $Path" }
}
Invoke-Signing $MainExe
Invoke-Signing $AliasExe

# -- 7. Build the installer with Inno Setup ----------------------------------
Write-Host "-> Running Inno Setup..."
New-Item -ItemType Directory -Force -Path "dist" | Out-Null
iscc.exe `
    "/DAppVersion=$Version" `
    "/DDistDir=$((Resolve-Path $DistDir).Path)" `
    "/DMcpbPath=$((Resolve-Path $McpbPath).Path)" `
    "/DIconPath=$((Resolve-Path $IcoPath).Path)" `
    "/DOutputDir=$((Resolve-Path 'dist').Path)" `
    "/DSetupBaseName=${ProductName}-${Version}-setup" `
    "installer/privacyfence.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup (iscc.exe) failed" }

$SetupPath = "dist/${SetupName}"

# -- 8. Optional code-signing of the installer itself (Phase 5) -------------
Invoke-Signing $SetupPath

Write-Host ""
Write-Host "Done: $SetupPath"
Write-Host "  Size: $((Get-Item $SetupPath).Length / 1MB) MB"
