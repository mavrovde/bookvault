# PyInstaller spec for the BookVault Linux desktop app (onedir).
#
# Produces dist/BookVault/ -- a self-contained onedir tree EXCEPT for:
#   * Playwright's Chromium, downloaded on first run (bookvault_desktop.app.
#     _ensure_chromium; we DO bundle Playwright's Node driver, collect_all
#     below, which is what makes that first-run install work in a frozen app).
#   * WebKitGTK (libwebkit2gtk-4.1) and its helper processes, which are a
#     documented HOST runtime dependency -- see packaging/linux/build-appimage.sh
#     and the README. pywebview's GTK backend loads WebKit2 via GObject
#     introspection; the typelib + gi bindings ARE bundled here so the frozen
#     app can bind it, but the .so multiprocess helpers stay on the host.
#
# Build (from packaging/linux/, so the shared entry resolves):
#   BOOKVAULT_VERSION=1.2.3 pyinstaller --noconfirm --clean BookVault.spec
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# The bookvault_* packages are installed EDITABLE, so PyInstaller's import
# analysis can't find their sources by name alone -- point it at the source
# roots. (SPECPATH is the dir containing this spec: packaging/linux/.)
_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
pathex = [os.path.join(_ROOT, p) for p in ("core", "web", "desktop")]

# The cross-platform frozen entry point lives at packaging/entry.py, one level
# up. PyInstaller wants a path to the analysis script; give it the shared one.
_ENTRY = os.path.join(SPECPATH, "..", "entry.py")

# Version stamped into the build; the tag-driven CI build passes the release
# version, local builds default to this.
_VERSION = os.environ.get("BOOKVAULT_VERSION", "1.0.0")

datas = []
binaries = []
hiddenimports = []

# Third-party packages that ship data/binaries PyInstaller won't find by import
# analysis alone: Playwright (its bundled Node driver + package json), pywebview
# (its JS shims + the GTK bridge), curl_cffi (native libs), keyring (backends).
for pkg in ("playwright", "webview", "curl_cffi", "keyring"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# GTK / WebKit backend for pywebview. PyInstaller's bundled `gi` hook collects
# the GObject-introspection typelibs (Gtk-3.0, WebKit2-4.1, GdkPixbuf, Soup, ...)
# and emits a runtime hook that sets GI_TYPELIB_PATH -- but only for the
# typelibs it can REACH via the hidden imports, so name them explicitly. (Do NOT
# hardcode GI_TYPELIB_PATH anywhere else; let the runtime hook own it.)
hiddenimports += [
    "gi",
    "gi.repository.Gtk",
    "gi.repository.Gdk",
    "gi.repository.WebKit2",
    "gi.repository.GLib",
    "gi.repository.Gio",
    "gi.repository.GObject",
    "gi.repository.GdkPixbuf",
    "gi.repository.Soup",
    "gi.repository.cairo",
]
d, b, h = collect_all("gi")
datas += d
binaries += b
hiddenimports += h

# bookvault_web ships Jinja templates + static CSS/JS as package data.
datas += collect_data_files("bookvault_web")

# uvicorn resolves its event-loop / protocol / lifespan implementations by
# string import at runtime, so they must be pulled in explicitly.
hiddenimports += collect_submodules("uvicorn")
# The app's own (editable) packages and all their submodules.
for pkg in ("bookvault_core", "bookvault_web", "bookvault_desktop"):
    hiddenimports += collect_submodules(pkg)

a = Analysis(
    [_ENTRY],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BookVault",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app: no terminal window
)

# onedir: COLLECT yields dist/BookVault/ (no BUNDLE on Linux). The AppImage is
# assembled from this tree by packaging/linux/build-appimage.sh.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BookVault",
)
