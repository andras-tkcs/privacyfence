#!/usr/bin/env bash
# Build privacyfence_<version>_<arch>.deb — a `dpkg -i`-able Linux package wrapping a
# self-contained PyInstaller build of the daemon, the Linux equivalent of scripts/build_dmg.sh's
# macOS DMG. See the now-removed docs/linux-local-deb-packaging-plan.md for the full design (why PyInstaller
# instead of a "proper" python3-* dependency package, why /opt + a /usr/bin wrapper, why XDG
# autostart instead of the repo-root --user systemd unit).
#
# Prerequisites (needed only on your build machine, not end-user machines):
#   pip install -e .        # PrivacyFence itself, so VERSION below can read its installed
#                            # metadata (git-tag-derived, see this repo's CLAUDE.md "Releasing")
#   pip install pyinstaller
#   apt-get install -y dpkg-dev lintian   # dpkg-deb ships with dpkg-dev; lintian is separate
#
# Usage:
#   ./scripts/build_deb.sh
#
# Output: dist/privacyfence_<version>_<arch>.deb
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Find Python / PyInstaller — prefer the project venv, then PATH. Identical to build_dmg.sh's own
# lookup.
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

command -v dpkg-deb &>/dev/null || { echo "Required tool not found: dpkg-deb (apt-get install dpkg-dev)" >&2; exit 1; }

# Version comes from the git tag via setuptools_scm now (this repo's CLAUDE.md "Releasing")
# -- read back through the installed package's own metadata, same as PrivacyFenceApp.spec/
# PrivacyFenceApp.linux.spec and src/privacyfence/__init__.py itself. Fails clearly if
# PrivacyFence itself hasn't been `pip install -e .`d yet.
VERSION=$("$PYTHON" -c "from importlib.metadata import version; print(version('privacyfence'))")

# Debian version-string handling (P4.1): setuptools_scm's resolved version is PEP 440
# (e.g. "4.0.0a13", or a dev version like "4.0.1.dev3+gabc1234"), which isn't a valid Debian
# version string as-is -- the bare "a13" pre-release suffix and the "+g<sha>" local segment don't
# sort correctly under `dpkg --compare-versions`. Convert: a/b/rc -> ~a/~b/~rc (Debian's own
# "earlier than the following release" convention, e.g. "4.0.0~a13" sorts before "4.0.0"), and a
# dev build's ".devN+g<sha>" maps to "~devN+g<sha>" (sorts before the next real release this dev
# build is heading towards; the "+g<sha>" tail is carried through unchanged -- it's not itself
# meaningful to dpkg, only there so two dev builds of the same next-version don't collide).
#
# The regex below deliberately mirrors scripts/r2_release.py's _VERSION_RE rather than importing
# it -- same reasoning as that module's own docstring: this script has nothing else in common
# with r2_release.py's job (uploading, not version-string conversion) and the two are cheap to
# keep in sync since PEP 440's shape here isn't expected to change.
DEB_VERSION=$("$PYTHON" - "$VERSION" <<'PYEOF'
import re
import sys

version = sys.argv[1]
m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(\+.*)?$", version)
if not m:
    sys.exit(f"{version!r} doesn't look like a resolved privacyfence version (major.minor.patch[a|b|rc<n>][.devN][+local])")
major, minor, patch, stage, stage_n, dev_n, local = m.groups()
base = f"{major}.{minor}.{patch}"
local = local or ""
if stage:
    print(f"{base}~{stage}{stage_n}{local}")
elif dev_n:
    print(f"{base}~dev{dev_n}{local}")
else:
    print(f"{base}{local}")
PYEOF
)

ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
PKG_NAME="privacyfence"
DEB_NAME="${PKG_NAME}_${DEB_VERSION}_${ARCH}.deb"

echo "=== Building ${PKG_NAME} ${VERSION} (deb version ${DEB_VERSION}, ${ARCH}) ==="

# ── 1. Bake in the Telegram app credentials ───────────────────────────────────
# Identical to build_dmg.sh's own step 2 -- already platform-independent, so reused verbatim
# rather than duplicated (see that script's comment for the full rationale).
CREDS_FILE="src/privacyfence/_telegram_credentials.py"
if [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ]; then
  echo "→ Writing Telegram app credentials…"
  cat > "$CREDS_FILE" <<EOF
API_ID = ${TELEGRAM_API_ID}
API_HASH = "${TELEGRAM_API_HASH}"
EOF
else
  echo "→ TELEGRAM_API_ID/TELEGRAM_API_HASH not set; building without Telegram app credentials."
  rm -f "$CREDS_FILE"
fi

# ── 2. Build the PyInstaller onedir bundle ────────────────────────────────────
# --clean for the same reason build_dmg.sh uses it: without it, a rebuild after
# _telegram_credentials.py first appears (step 1, above) can silently keep using a stale module
# analysis from before the file existed.
echo "→ Running PyInstaller…"
"$PYINSTALLER" --noconfirm --clean PrivacyFenceApp.linux.spec

BUNDLE="dist/PrivacyFenceApp"

# ── 3. Create privacyfence-app symlink inside the bundle ─────────────────────
# Same reasoning as build_dmg.sh's step 4 -- the mcpb shim's findDaemonCmd() (mcpb/shim/src/
# daemon.ts) and any autostart entry look for this name specifically.
if [ ! -e "${BUNDLE}/privacyfence-app" ]; then
  echo "→ Creating privacyfence-app symlink…"
  ln -s "PrivacyFenceApp" "${BUNDLE}/privacyfence-app"
fi

# ── 4. Stage the package tree ─────────────────────────────────────────────────
# scripts/build_deb.sh builds the .deb by staging a package tree and calling `dpkg-deb --build`
# directly, rather than going through the full `dh $@`/dpkg-buildpackage pipeline -- see
# debian/rules's own comment for why (no source compilation step exists to run; this script has
# already produced the one artifact debhelper would otherwise be orchestrating a build around).
# debian/install documents the same file mappings applied here by hand, kept in sync manually.
STAGE="build/deb-stage"
rm -rf "$STAGE"
mkdir -p \
  "${STAGE}/DEBIAN" \
  "${STAGE}/opt/privacyfence" \
  "${STAGE}/usr/bin" \
  "${STAGE}/etc/xdg/autostart" \
  "${STAGE}/usr/share/icons/hicolor/512x512/apps" \
  "${STAGE}/usr/share/icons/hicolor/64x64/apps" \
  "${STAGE}/usr/share/icons/hicolor/32x32/apps" \
  "${STAGE}/usr/share/doc/privacyfence"

echo "→ Staging package tree…"
cp -a "${BUNDLE}/." "${STAGE}/opt/privacyfence/"

# PyInstaller's onedir output leaves every bundled shared library group-writable/executable
# (0755, the same mode as the daemon binary itself) -- correct for the DMG's .app bundle (macOS
# doesn't police this), but flagged by lintian as `shared-library-is-executable` on Debian: a
# vendored .so is data, not something meant to be run directly, and convention (and this
# package's own lint gate, P4.3) expects 0644. Only the actual entry points --
# opt/privacyfence/PrivacyFenceApp and the privacyfence-app symlink to it -- need to stay
# executable.
find "${STAGE}/opt/privacyfence" \( -name '*.so' -o -name '*.so.*' \) -type f -exec chmod 0644 {} +

install -m 0755 resources/linux/privacyfence-app-wrapper "${STAGE}/usr/bin/privacyfence-app"
install -m 0644 resources/linux/privacyfence.desktop "${STAGE}/etc/xdg/autostart/privacyfence.desktop"
install -m 0644 src/privacyfence/resources/icon_512.png "${STAGE}/usr/share/icons/hicolor/512x512/apps/privacyfence.png"
install -m 0644 src/privacyfence/resources/icon_64.png "${STAGE}/usr/share/icons/hicolor/64x64/apps/privacyfence.png"
install -m 0644 src/privacyfence/resources/icon_32.png "${STAGE}/usr/share/icons/hicolor/32x32/apps/privacyfence.png"
install -m 0644 LICENSE "${STAGE}/usr/share/doc/privacyfence/LICENSE"
install -m 0644 NOTICE "${STAGE}/usr/share/doc/privacyfence/NOTICE"
install -m 0644 debian/copyright "${STAGE}/usr/share/doc/privacyfence/copyright"

mkdir -p "${STAGE}/usr/share/lintian/overrides"
install -m 0644 debian/privacyfence.lintian-overrides "${STAGE}/usr/share/lintian/overrides/privacyfence"

install -m 0755 debian/postinst "${STAGE}/DEBIAN/postinst"
install -m 0755 debian/prerm "${STAGE}/DEBIAN/prerm"
install -m 0755 debian/postrm "${STAGE}/DEBIAN/postrm"

# /etc/xdg/autostart/privacyfence.desktop is package-owned configuration under /etc -- mark it a
# conffile so dpkg preserves local edits across upgrades instead of silently overwriting them
# (dh_installdeb would do this automatically for anything under /etc in a real dh build; this
# script replicates it by hand for the same reason as everywhere else in this staging step).
echo "/etc/xdg/autostart/privacyfence.desktop" > "${STAGE}/DEBIAN/conffiles"

# debian/changelog isn't hand-maintained per release (this repo's CLAUDE.md "Releasing" section:
# there's no hand-bumped version file at all, on the same reasoning) -- generate a single-entry
# Debian changelog from the resolved version instead, matching debian/source/format's "3.0
# (native)" (a native package's doc dir carries changelog.gz, not changelog.Debian.gz).
CHANGELOG_DATE="$(date -Ru)"
{
  echo "${PKG_NAME} (${DEB_VERSION}) unstable; urgency=medium"
  echo
  echo "  * Release ${VERSION}. See GitHub Releases for details:"
  echo "    https://github.com/privacyfence/privacyfence/releases/tag/v${VERSION}"
  echo
  echo " -- Andras Takacs <info@privacyfence.eu>  ${CHANGELOG_DATE}"
} | gzip -9 -n > "${STAGE}/usr/share/doc/privacyfence/changelog.gz"

# ── 5. Render DEBIAN/control ──────────────────────────────────────────────────
# debian/control (committed) is a *source package* control file (a Source: stanza plus a
# Package: stanza) -- policy-conformant and lintian-checkable on its own, per debian/rules's own
# note, but not directly usable as a binary .deb's DEBIAN/control. Render the binary control file
# from its Package: stanza here: drop comment lines (not valid in a binary control file, only in
# the source-package one dpkg-source parses) and dh substvar placeholders (nothing computes
# ${misc:Depends} outside a real dh build -- an empty/absent Depends is exactly the "no python3-*
# dependency requirements" property the key decision in the now-removed docs/linux-local-deb-packaging-plan.md is
# built around), then fill in this build's Architecture/Version/Installed-Size.
INSTALLED_SIZE_KB=$(find "$STAGE" -mindepth 1 -maxdepth 1 ! -name DEBIAN -exec du -sk {} + | awk '{sum+=$1} END {print sum+0}')

"$PYTHON" - "$ARCH" "$DEB_VERSION" "$INSTALLED_SIZE_KB" "${STAGE}/DEBIAN/control" <<'PYEOF'
import sys

arch, ver, installed_size, out_path = sys.argv[1:5]
text = open("debian/control").read()
stanzas = text.strip().split("\n\n")
source_stanza, pkg_stanza = stanzas[0], stanzas[-1]

out = []
for line in pkg_stanza.splitlines():
    stripped = line.strip()
    if stripped.startswith("#"):
        continue
    if line.startswith("Architecture:"):
        out.append(f"Architecture: {arch}")
        continue
    if "${" in line:
        # Drop dh substvar placeholders (e.g. "Depends: ${misc:Depends}") -- see this script's
        # own comment above for why.
        continue
    out.append(line)

# Maintainer/Section/Priority live in the Source: stanza (conventional single-binary-package
# placement); a real dh build inherits fields like these onto the binary control file via
# dpkg-gencontrol when the Package: stanza doesn't override them -- replicate that here since this
# script bypasses dh entirely.
for field in ("Maintainer", "Section", "Priority"):
    if any(line.startswith(f"{field}:") for line in out):
        continue
    inherited = next((line for line in source_stanza.splitlines() if line.startswith(f"{field}:")), None)
    if inherited:
        out.append(inherited)

# Package: must be the first field; Version/Installed-Size go right after it.
insert_at = 1 if out and out[0].startswith("Package:") else 0
out[insert_at:insert_at] = [f"Version: {ver}", f"Installed-Size: {installed_size}"]

with open(out_path, "w") as f:
    f.write("\n".join(out).strip() + "\n")
PYEOF

# ── 6. Build the .deb ──────────────────────────────────────────────────────────
echo "→ Running dpkg-deb…"
mkdir -p dist
rm -f "dist/${DEB_NAME}"
dpkg-deb --build --root-owner-group "$STAGE" "dist/${DEB_NAME}"

# ── 7. Lint ─────────────────────────────────────────────────────────────────────
# Non-fatal warnings are logged but don't fail the build; any error-severity finding does --
# catches packaging-policy mistakes (bad permissions, FHS violations, a malformed changelog)
# before they ship, the same role PrivacyFenceApp.spec's own structure already plays for macOS
# bundling mistakes (P4.3).
if command -v lintian &>/dev/null; then
  echo "→ Running lintian…"
  set +e
  LINTIAN_OUTPUT="$(lintian --fail-on error "dist/${DEB_NAME}" 2>&1)"
  LINTIAN_STATUS=$?
  set -e
  echo "$LINTIAN_OUTPUT"
  if [ "$LINTIAN_STATUS" -ne 0 ]; then
    echo "lintian found error-severity issues — failing the build." >&2
    exit 1
  fi
else
  echo "→ lintian not found — skipping lint (install with: apt-get install -y lintian)"
fi

echo ""
echo "✓ Done: dist/${DEB_NAME}"
echo "  Size: $(du -sh "dist/${DEB_NAME}" | cut -f1)"
