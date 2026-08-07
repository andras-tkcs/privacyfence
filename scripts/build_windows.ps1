# Build a Windows onedir distribution of the PrivacyFence daemon (issue #121).
#
# Windows analog of build_dmg.sh, but deliberately narrower in scope for now
# -- see docs/windows-port-status.md's "Explicitly out of scope" section.
# This script only produces the unsigned PyInstaller onedir build; it does
# NOT (yet):
#   - code-sign the output (the Authenticode equivalent of build_dmg.sh's
#     `codesign`/`notarytool` steps -- needs a real Windows machine and the
#     project owner's own signing certificate)
#   - package it into an installer (MSI/Inno Setup/NSIS -- the DMG-equivalent
#     step)
#
# Must run on a real Windows machine -- PyInstaller always targets the OS
# it's invoked on, this can't cross-compile a Windows build from macOS/
# Linux/CI running any other OS.
#
# Prerequisites:
#   Python 3.11+ on PATH, with the project installed editable + the `dev`
#   extra: pip install -e ".[dev]"
#   (Optional) PRIVACYFENCE_ICO environment variable pointing at a real
#   .ico file -- see PrivacyFenceApp.windows.spec's own comment on why the
#   PNG fallback isn't a real icon.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#
# Output: dist\PrivacyFenceApp\PrivacyFenceApp.exe (plus its own _internal\
# support directory alongside it -- the whole dist\PrivacyFenceApp\ folder
# is the distributable unit, same as any PyInstaller onedir build).

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Single source of truth per CLAUDE.md's version-bump policy -- read the
# same way PrivacyFenceApp.windows.spec itself does, not duplicated here.
$Version = python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb')); print(d['project']['version'])"

Write-Host "=== Building PrivacyFenceApp $Version (Windows, unsigned) ==="

if (-not $env:PRIVACYFENCE_ICO) {
    Write-Warning "PRIVACYFENCE_ICO not set -- falling back to the PNG icon, which PyInstaller will reject on Windows. Set it to a real .ico path (e.g. converted from src\privacyfence\resources\icon_512.png via Pillow) before building a real distributable."
}

pyinstaller --noconfirm --clean PrivacyFenceApp.windows.spec

Write-Host ""
Write-Host "Done: dist\PrivacyFenceApp\PrivacyFenceApp.exe"
Write-Host "This build is UNSIGNED and not packaged into an installer -- see"
Write-Host "docs\windows-port-status.md before distributing it to anyone else."
