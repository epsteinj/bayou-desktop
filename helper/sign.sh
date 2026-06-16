#!/usr/bin/env bash
# Notarize + staple a built+signed .pkg/.dmg/.app. Fill in your identity first.
# Prereq: a notarytool profile (see SIGNING.md):
#   xcrun notarytool store-credentials bayou-notary --apple-id … --team-id … --password …
set -euo pipefail
ART="${1:?usage: sign.sh <bayou-helper.pkg|.dmg|.app.zip>}"
PROFILE="${BAYOU_NOTARY_PROFILE:-bayou-notary}"
echo "▸ submitting $ART to Apple notary (waits for result)…"
xcrun notarytool submit "$ART" --keychain-profile "$PROFILE" --wait
echo "▸ stapling ticket…"
xcrun stapler staple "$ART"
xcrun stapler validate "$ART" && echo "✓ notarized + stapled: $ART"
