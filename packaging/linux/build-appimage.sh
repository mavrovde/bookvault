#!/usr/bin/env bash
# Assemble a BookVault AppImage from the PyInstaller onedir build.
#
# What this does:
#   1. Runs (or reuses) the PyInstaller onedir build -> dist/BookVault/.
#   2. Stages it into an AppDir with a hand-written AppRun, a .desktop file, and
#      a placeholder icon.
#   3. Runs appimagetool to produce BookVault-<version>-x86_64.AppImage.
#
# WebKitGTK is NOT bundled: libwebkit2gtk-4.1 spawns helper executables and
# loads its injected bundle via an absolute path baked into the .so at compile
# time, which cannot survive inside a mounted AppImage. So it stays a documented
# HOST dependency (see the README Linux section). The onedir DOES bundle the gi
# bindings + WebKit2-4.1 typelib so pywebview can bind the host WebKit.
#
# Usage:  packaging/linux/build-appimage.sh [version]   (default: 1.0.0)
#
# CI env you likely want set (the workflow does this):
#   APPIMAGE_EXTRACT_AND_RUN=1   # runners have no working FUSE mount
#   ARCH=x86_64                  # appimagetool refuses to guess
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="${1:-1.0.0}"
ARCH="${ARCH:-x86_64}"

# Prefer the repo venv's pyinstaller; fall back to whatever is on PATH.
PYINSTALLER="$ROOT/.venv/bin/pyinstaller"
[ -x "$PYINSTALLER" ] || PYINSTALLER="pyinstaller"

cd "$HERE"

# ---- 1. PyInstaller onedir -------------------------------------------------
if [ "${SKIP_PYINSTALLER:-0}" != "1" ]; then
  echo ">> PyInstaller onedir build (version $VERSION)"
  rm -rf build dist
  BOOKVAULT_VERSION="$VERSION" "$PYINSTALLER" --noconfirm --clean BookVault.spec
fi

ONEDIR="dist/BookVault"
[ -d "$ONEDIR" ] || { echo "!! build did not produce $ONEDIR"; exit 1; }

# ---- 2. Placeholder icon (generated once, embedded so the repo stays text) --
ICON="$HERE/bookvault.png"
if [ ! -f "$ICON" ]; then
  echo ">> Writing placeholder icon"
  base64 -d > "$ICON" <<'PNG_B64'
iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAIAAADTED8xAAACAElEQVR42u3TQQ0AAAjEsHOCBjRjlDcaaFIFS5bpgrciAQYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABMIAKGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAYAA4ABwABgADAAGAAMAAbAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAGUAEDgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAAGAAMAAYAAwABgADgAHAABgADAAGAAOAAcAAYAAwABgADAAGAAOAAcAAYAAwABgADAAGAAOAAcAAYAAwABgADAAGAAOAAcAAYAAwABgADAAGAAOAAcAAYAAwABgADAAGAAOAAcAAYAAwAFwL6DYOPqaQFFMAAAAASUVORK5CYII=
PNG_B64
fi

# ---- 3. AppDir -------------------------------------------------------------
APPDIR="$HERE/BookVault.AppDir"
echo ">> Assembling $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# The whole onedir payload lives under usr/lib/BookVault; usr/bin symlinks the
# launcher so it's on PATH inside the AppImage.
cp -a "$ONEDIR" "$APPDIR/usr/lib/BookVault"
ln -sf "../lib/BookVault/BookVault" "$APPDIR/usr/bin/BookVault"

# .desktop -- required both at AppDir top level and under share/applications.
cat > "$APPDIR/usr/share/applications/bookvault.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=BookVault
Exec=BookVault
Icon=bookvault
Categories=Utility;
Terminal=false
DESKTOP
cp "$APPDIR/usr/share/applications/bookvault.desktop" "$APPDIR/bookvault.desktop"

# Icon -- both at AppDir top level (matching Icon=bookvault) and in the theme.
cp "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/bookvault.png"
cp "$ICON" "$APPDIR/bookvault.png"

# AppRun -- hand-written launcher. Does NOT export GI_TYPELIB_PATH (PyInstaller's
# runtime hook owns it) and deliberately leaves host WebKitGTK to win.
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib/BookVault:${HERE}/usr/lib:${LD_LIBRARY_PATH}"
# WebKitGTK's DMABUF renderer often fails on VMs, headless-ish sessions, and
# older GPUs, leaving a blank window. Fall back to a safe compositing mode
# unless the user has already chosen one.
: "${WEBKIT_DISABLE_DMABUF_RENDERER:=1}"
export WEBKIT_DISABLE_DMABUF_RENDERER
exec "${HERE}/usr/bin/BookVault" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# ---- 4. appimagetool -------------------------------------------------------
TOOL="$HERE/appimagetool-${ARCH}.AppImage"
if [ ! -x "$TOOL" ]; then
  echo ">> Downloading appimagetool"
  curl -fL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x "$TOOL"
fi

OUT="$HERE/BookVault-${VERSION}-${ARCH}.AppImage"
echo ">> Building $OUT"
rm -f "$OUT"
# ARCH must be exported for appimagetool; APPIMAGE_EXTRACT_AND_RUN avoids FUSE.
ARCH="$ARCH" "$TOOL" "$APPDIR" "$OUT"

echo ">> Done:"
du -sh "$OUT" 2>/dev/null || true
echo "$OUT"
