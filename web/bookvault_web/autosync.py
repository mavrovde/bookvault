"""Optional background scheduler for on-disk library autosync.

Enabled only when LITRES_LIBRARY_DIR is set and LITRES_AUTOSYNC=1.
Never touches LitresClient directly -- only claims the activity machine via
activity.start_sync so work stays on the Playwright worker thread.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from bookvault_core.library_fs import library_root_from_env
from bookvault_web import activity

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def autosync_enabled() -> bool:
    if library_root_from_env() is None:
        return False
    return os.environ.get("LITRES_AUTOSYNC", "0").lower() in ("1", "true", "yes", "on")


def audio_only_from_env() -> bool:
    return os.environ.get("LITRES_AUTOSYNC_AUDIO_ONLY", "1").lower() not in ("0", "false", "no", "off")


def interval_seconds() -> float:
    try:
        return max(30.0, float(os.environ.get("LITRES_AUTOSYNC_INTERVAL", "600")))
    except ValueError:
        return 600.0


def on_start_enabled() -> bool:
    return os.environ.get("LITRES_AUTOSYNC_ON_START", "1").lower() not in ("0", "false", "no", "off")


def try_start_sync(get_client: Callable, get_prefs: Callable) -> bool:
    """Start a sync if enabled, logged in, and idle. Returns whether started."""
    if not autosync_enabled():
        return False
    client = get_client()
    if client is None:
        return False
    snap = activity.snapshot()
    if snap.get("state") != activity.IDLE:
        return False
    prefs = get_prefs() or {}
    return activity.start_sync(
        client,
        audio_only=audio_only_from_env(),
        preferred_ext=prefs.get("ebook_format"),
        preferred_file_type=prefs.get("audiobook_format"),
    )


def _loop(get_client: Callable, get_prefs: Callable) -> None:
    interval = interval_seconds()
    # Optional first tick soon after boot (session restore may still be settling).
    if on_start_enabled():
        for _ in range(30):  # up to ~30s
            if _stop.is_set():
                return
            if try_start_sync(get_client, get_prefs):
                logger.info("Autosync started on boot")
                break
            if get_client() is not None:
                # Logged in but busy or start_sync returned False for another reason.
                break
            time.sleep(1.0)

    while not _stop.wait(interval):
        try:
            if try_start_sync(get_client, get_prefs):
                logger.info("Autosync tick started a library sync")
        except Exception:
            logger.exception("Autosync tick failed")


def start_background_scheduler(get_client: Callable, get_prefs: Callable) -> None:
    """Start the daemon timer thread (no-op if autosync disabled or already running)."""
    global _thread
    if not autosync_enabled():
        logger.info("Autosync disabled (set LITRES_LIBRARY_DIR and LITRES_AUTOSYNC=1 to enable)")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop,
        args=(get_client, get_prefs),
        name="bookvault-autosync",
        daemon=True,
    )
    _thread.start()
    logger.info(
        "Autosync scheduler started (interval=%ss, library=%s)",
        interval_seconds(),
        library_root_from_env(),
    )


def stop_background_scheduler() -> None:
    global _thread
    _stop.set()
    t = _thread
    _thread = None
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
