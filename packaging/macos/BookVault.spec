# PyInstaller spec for the BookVault macOS desktop app.
#
# Produces dist/BookVault.app -- a self-contained bundle EXCEPT for Playwright's
# Chromium, which is downloaded on first run (see bookvault_desktop.app.
# _ensure_chromium). We DO bundle Playwright's Node driver (collect_all below),
# which is what makes that first-run install work in a frozen app.
#
# Build:  ../../.venv/bin/pyinstaller --noconfirm --clean BookVault.spec
# (run from packaging/macos/ so `entry.py` resolves).
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# The bookvault_* packages are installed EDITABLE, so PyInstaller's import
# analysis can't find their sources by name alone -- point it at the source
# roots. (SPECPATH is the dir containing this spec: packaging/macos/.)
_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
pathex = [os.path.join(_ROOT, p) for p in ("core", "web", "desktop")]

# The frozen entry point is the cross-platform launcher shared with the Windows
# and Linux builds: packaging/entry.py (one level up from this spec). It pins a
# per-user data dir and, crucially, PLAYWRIGHT_BROWSERS_PATH to the standard
# writable cache (a frozen app otherwise resolves Chromium inside its read-only
# bundle and can't launch it).
_ENTRY = os.path.join(SPECPATH, "..", "entry.py")

# Version stamped into the bundle; the tag-driven CI build passes the release
# version, local builds default to this.
_VERSION = os.environ.get("BOOKVAULT_VERSION", "1.0.1")

datas = []
binaries = []
hiddenimports = []

# Third-party packages that ship data/binaries PyInstaller won't find by import
# analysis alone: Playwright (its bundled Node driver + package json), pywebview
# (its JS shims + the pyobjc bridge), curl_cffi (native libs), keyring (backends).
for pkg in ("playwright", "webview", "curl_cffi", "keyring"):
    d, b, h = collect_all(pkg)
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BookVault",
)

app = BUNDLE(
    coll,
    name="BookVault.app",
    icon=None,
    bundle_identifier="de.mavrov.bookvault",
    info_plist={
        "CFBundleName": "BookVault",
        "CFBundleDisplayName": "BookVault",
        "CFBundleShortVersionString": _VERSION,
        "CFBundleVersion": _VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # Single-window app; no need for a Dock-less agent.
        "LSApplicationCategoryType": "public.app-category.utilities",
    },
)
