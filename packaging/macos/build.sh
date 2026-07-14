#!/usr/bin/env bash
# Build the BookVault macOS desktop app (.app) and wrap it in a drag-to-install
# .dmg. Unsigned dev build -- Gatekeeper will warn on first open (right-click ->
# Open, or `xattr -dr com.apple.quarantine BookVault.app`). Chromium is fetched
# on first run, so the .app itself stays small (~80-120 MB).
#
# Usage:  packaging/macos/build.sh [version]   (default version: 1.0.0)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="${1:-1.0.0}"

# Prefer the repo venv's pyinstaller; fall back to whatever is on PATH.
PYINSTALLER="$ROOT/.venv/bin/pyinstaller"
[ -x "$PYINSTALLER" ] || PYINSTALLER="pyinstaller"

cd "$HERE"
rm -rf build dist

echo ">> PyInstaller build (version $VERSION)"
BOOKVAULT_VERSION="$VERSION" "$PYINSTALLER" --noconfirm --clean BookVault.spec

APP="dist/BookVault.app"
[ -d "$APP" ] || { echo "!! build did not produce $APP"; exit 1; }

echo ">> Assembling .dmg"
DMG="dist/BookVault-${VERSION}.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # drag-to-install target
rm -f "$DMG"
hdiutil create -volname "BookVault ${VERSION}" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo ">> Done:"
du -sh "$APP" "$DMG" 2>/dev/null || true
echo "$DMG"
