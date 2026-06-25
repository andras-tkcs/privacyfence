#!/usr/bin/env bash
# Build Loopline.dmg — a drag-to-install macOS disk image.
#
# Prerequisites (needed only on your build machine, not end-user machines):
#   pip install pyinstaller
#   brew install create-dmg
#   brew install librsvg   # optional, only if you add SVG assets
#
# Usage:
#   ./scripts/build_dmg.sh [--sign "Developer ID Application: Your Name (TEAMID)"]
#
# Output: dist/Loopline-<version>.dmg
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Find Python / PyInstaller — prefer the project venv, then PATH.
if [ -x ".venv/bin/pyinstaller" ]; then
  PYTHON=".venv/bin/python"
  PYINSTALLER=".venv/bin/pyinstaller"
elif command -v pyinstaller &>/dev/null; then
  PYTHON="$(command -v python3)"
  PYINSTALLER="$(command -v pyinstaller)"
else
  echo "PyInstaller not found — installing into .venv…"
  .venv/bin/pip install --quiet pyinstaller
  PYTHON=".venv/bin/python"
  PYINSTALLER=".venv/bin/pyinstaller"
fi

VERSION=$("$PYTHON" -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['version'])")
APP_NAME="Loopline"
BUNDLE="dist/${APP_NAME}.app"
DMG_NAME="${APP_NAME}-${VERSION}.dmg"
DMG_PATH="dist/${DMG_NAME}"

SIGN_IDENTITY="${SIGN_IDENTITY:-}"
for arg in "$@"; do
  case "$arg" in
    --sign) SIGN_IDENTITY="${2:-}"; shift 2 ;;
  esac
done

echo "=== Building ${APP_NAME} ${VERSION} ==="

# ── 1. Convert PNG icon to ICNS (must happen before PyInstaller) ─────────────
ICON_SRC="src/loopline/resources/icon_512.png"
ICON_DIR="build/loopline_icons.iconset"
ICNS_PATH="build/loopline.icns"

if [ ! -f "$ICNS_PATH" ]; then
  echo "→ Converting icon to .icns…"
  mkdir -p "$ICON_DIR"
  sips -z 16 16     "$ICON_SRC" --out "${ICON_DIR}/icon_16x16.png"     >/dev/null
  sips -z 32 32     "$ICON_SRC" --out "${ICON_DIR}/icon_16x16@2x.png"  >/dev/null
  sips -z 32 32     "$ICON_SRC" --out "${ICON_DIR}/icon_32x32.png"     >/dev/null
  sips -z 64 64     "$ICON_SRC" --out "${ICON_DIR}/icon_32x32@2x.png"  >/dev/null
  sips -z 128 128   "$ICON_SRC" --out "${ICON_DIR}/icon_128x128.png"   >/dev/null
  sips -z 256 256   "$ICON_SRC" --out "${ICON_DIR}/icon_128x128@2x.png" >/dev/null
  sips -z 256 256   "$ICON_SRC" --out "${ICON_DIR}/icon_256x256.png"   >/dev/null
  sips -z 512 512   "$ICON_SRC" --out "${ICON_DIR}/icon_256x256@2x.png" >/dev/null
  cp "$ICON_SRC"                      "${ICON_DIR}/icon_512x512.png"
  iconutil -c icns "$ICON_DIR" -o "$ICNS_PATH"
fi

# ── 2. Build .app bundle ──────────────────────────────────────────────────────
echo "→ Running PyInstaller…"
LOOPLINE_ICNS="$ICNS_PATH" $PYINSTALLER --noconfirm Loopline.spec

# ── 3. Create loopline-app symlink inside the bundle ─────────────────────────
# The LaunchAgent plist and bridge use this name; the main exe is "Loopline".
MACOS_DIR="${BUNDLE}/Contents/MacOS"
if [ ! -e "${MACOS_DIR}/loopline-app" ]; then
  echo "→ Creating loopline-app symlink…"
  ln -s "Loopline" "${MACOS_DIR}/loopline-app"
fi

# Apply the .icns to the .app bundle
if command -v fileicon &>/dev/null; then
  fileicon set "$BUNDLE" "$ICNS_PATH" 2>/dev/null || true
fi

# ── 4. Optional code signing ──────────────────────────────────────────────────
if [ -n "$SIGN_IDENTITY" ]; then
  echo "→ Code-signing with: ${SIGN_IDENTITY}"
  codesign --deep --force --options runtime \
    --sign "$SIGN_IDENTITY" \
    --entitlements scripts/entitlements.plist \
    "$BUNDLE"
fi

# ── 5. Package into DMG ───────────────────────────────────────────────────────
echo "→ Building DMG…"
rm -f "$DMG_PATH"

create-dmg \
  --volname "${APP_NAME}" \
  --volicon "$ICNS_PATH" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 128 \
  --icon "${APP_NAME}.app" 150 185 \
  --hide-extension "${APP_NAME}.app" \
  --app-drop-link 450 185 \
  --no-internet-enable \
  "$DMG_PATH" \
  "dist/${APP_NAME}.app"

# ── 6. Optional notarization ──────────────────────────────────────────────────
# Uncomment and set NOTARIZE_PROFILE (name from `xcrun notarytool store-credentials`)
# if [ -n "$SIGN_IDENTITY" ] && [ -n "${NOTARIZE_PROFILE:-}" ]; then
#   echo "→ Submitting for notarization…"
#   xcrun notarytool submit "$DMG_PATH" \
#     --keychain-profile "$NOTARIZE_PROFILE" \
#     --wait
#   xcrun stapler staple "$DMG_PATH"
# fi

echo ""
echo "✓ Done: ${DMG_PATH}"
echo "  Size: $(du -sh "${DMG_PATH}" | cut -f1)"
