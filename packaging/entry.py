"""Frozen-app entry point for the packaged BookVault desktop build.

A packaged app has no meaningful working directory (it may launch with cwd `/`)
and must never write into its own read-only bundle. Two things are set up here,
per-OS, BEFORE importing the app (bookvault_web's cache/session/prefs modules
read their paths from the environment at import time):

1. A per-user **data directory** for the session/cache/state files + downloads.
2. **PLAYWRIGHT_BROWSERS_PATH** pinned to the standard, writable browsers cache.
   This is critical: inside a frozen bundle Playwright otherwise resolves the
   browser to a path *inside* the read-only bundle
   (`.../Resources/playwright/driver/package/.local-browsers/...`) and can
   neither install nor launch Chromium there. Pinning the standard cache fixes
   the first-run install + launch and shares the download with any normal
   Playwright install on the machine.

Only the packaged build uses this entry point; running `bookvault-desktop` from
a source checkout keeps its cwd-relative / default-cache behaviour.
"""
import os
import sys
from pathlib import Path


def _app_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "BookVault"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "BookVault"
    else:  # linux/other: XDG data dir
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "BookVault"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _browsers_dir() -> Path:
    """The standard ms-playwright cache location per OS (writable, persistent,
    shared with a normal Playwright install)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"
    base.mkdir(parents=True, exist_ok=True)
    return base


def main() -> None:
    data = _app_data_dir()
    # setdefault: honour anything the user set, otherwise keep mutable state in
    # the per-user data dir and downloads in the usual place.
    os.environ.setdefault("LITRES_SESSION_FILE", str(data / ".litres_session.json"))
    os.environ.setdefault("LITRES_CACHE_FILE", str(data / ".litres_cache.json"))
    os.environ.setdefault("LITRES_STATE_FILE", str(data / ".litres_state.json"))
    os.environ.setdefault("LITRES_DOWNLOAD_DIR", str(Path.home() / "Downloads" / "litres-library"))
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_browsers_dir()))

    from bookvault_desktop.app import main as run
    run()


if __name__ == "__main__":
    main()
