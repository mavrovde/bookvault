"""Frozen-app entry point for the packaged macOS BookVault build.

A packaged .app has no meaningful working directory (it may be launched with cwd
`/`), and it must never write into its own read-only bundle. So before importing
anything from bookvault_web -- whose cache/session/prefs modules read their file
paths from the environment *at import time* -- we point those at a per-user data
directory under ~/Library/Application Support/BookVault. Then we hand off to the
normal desktop launcher unchanged.

Only the packaged build uses this entry point; running `bookvault-desktop` from
a source checkout keeps its cwd-relative defaults.
"""
import os
from pathlib import Path


def _data_dir() -> Path:
    base = Path.home() / "Library" / "Application Support" / "BookVault"
    base.mkdir(parents=True, exist_ok=True)
    return base


def main() -> None:
    data = _data_dir()
    # setdefault: respect anything the user set in the environment, otherwise
    # keep all mutable state in the per-user data dir (and downloads in the
    # usual place). These must be set BEFORE importing the app.
    os.environ.setdefault("LITRES_SESSION_FILE", str(data / ".litres_session.json"))
    os.environ.setdefault("LITRES_CACHE_FILE", str(data / ".litres_cache.json"))
    os.environ.setdefault("LITRES_STATE_FILE", str(data / ".litres_state.json"))
    os.environ.setdefault("LITRES_DOWNLOAD_DIR", str(Path.home() / "Downloads" / "litres-library"))

    from bookvault_desktop.app import main as run
    run()


if __name__ == "__main__":
    main()
