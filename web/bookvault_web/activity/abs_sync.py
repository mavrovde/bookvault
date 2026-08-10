"""The Audiobookshelf-shaped on-disk library sync (SYNCING).

Distinct from `mirror`, which lays books out flat in the user's save folder:
this builds the Author/Title tree with `metadata.json` that Audiobookshelf
expects, under `LITRES_LIBRARY_DIR`, and is off unless that is set.
"""
from __future__ import annotations

import logging

from bookvault_core import session
from bookvault_core.client import LitresClient
from bookvault_core.library_fs import library_root_from_env
from bookvault_core.library_sync import sync_library

from . import state
from .state import IDLE, SYNCING

logger = logging.getLogger(__name__)


def start_sync(
    client: LitresClient,
    *,
    audio_only: bool = True,
    preferred_ext: str | None = None,
    preferred_file_type: str | None = None,
    art_ids: set | None = None,
) -> bool:
    """Sync purchased titles into LITRES_LIBRARY_DIR (SYNCING).

    Returns False if library dir is unset or another activity is running.
    """
    root = library_root_from_env()
    if root is None:
        logger.info("start_sync ignored -- LITRES_LIBRARY_DIR is not set")
        return False
    if not state._begin(SYNCING, message="Syncing library to disk…"):
        return False
    logger.info(
        "Starting on-disk library sync into %s (audio_only=%s)",
        root,
        audio_only,
    )
    session.submit(
        _run_sync,
        client,
        root,
        audio_only,
        preferred_ext,
        preferred_file_type,
        art_ids,
    )
    return True

def _run_sync(
    client: LitresClient,
    root,
    audio_only: bool,
    preferred_ext: str | None,
    preferred_file_type: str | None,
    art_ids: set | None,
) -> None:
    try:
        def on_progress(title, done, total):
            state._update(
                current_title=title or None,
                done=done,
                total=total if total else None,
                message=f"Syncing “{title}”…" if title else "Finishing library sync…",
            )

        summary = sync_library(
            client,
            root,
            audio_only=audio_only,
            preferred_ext=preferred_ext,
            preferred_file_type=preferred_file_type,
            should_cancel=state._cancel_event.is_set,
            on_progress=on_progress,
            art_ids=art_ids,
        )
        cancelled = bool(summary.get("cancelled")) or state._cancel_event.is_set()
        done = summary.get("done", 0)
        skipped = summary.get("skipped", 0)
        failed = summary.get("failed", 0)
        if cancelled:
            message = (
                f"Stopped library sync — saved {done}, skipped {skipped}, "
                f"failed {failed}."
            )
        else:
            message = (
                f"Library sync finished — saved {done}, skipped {skipped}, "
                f"failed {failed}."
            )
        with state._lock:
            state._state.update(
                state=IDLE,
                result="cancelled" if cancelled else "done",
                message=message,
                current_title=None,
                current_downloaded=None,
                current_total=None,
                log=list(summary.get("log") or []),
                results=list(summary.get("log") or []),
                done=done,
                total=summary.get("total"),
            )
        logger.info(
            "Library sync %s: done=%s skipped=%s failed=%s root=%s",
            "cancelled" if cancelled else "finished",
            done,
            skipped,
            failed,
            root,
        )
    except Exception as exc:
        logger.exception("Library sync crashed")
        state._update(
            state=IDLE,
            result="error",
            error=state._friendly_error(exc),
            current_title=None,
            current_downloaded=None,
            current_total=None,
            message="",
        )
