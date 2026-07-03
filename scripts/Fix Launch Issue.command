#!/usr/bin/env bash
# Double-click this if macOS says "PrivacyFenceApp is damaged and can't be
# opened" — that's Gatekeeper blocking an unsigned app downloaded from the
# internet, not real corruption. This clears the quarantine flag so it'll
# launch normally.
set -euo pipefail

APP="/Applications/PrivacyFenceApp.app"

if [ ! -e "$APP" ]; then
  echo "Couldn't find $APP — drag PrivacyFenceApp.app to Applications first, then run this again."
  read -r -p "Press Return to close…"
  exit 1
fi

xattr -cr "$APP"
echo "Done. PrivacyFenceApp should open normally now."
read -r -p "Press Return to close…"
