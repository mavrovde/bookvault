"""Tests for bookvault_web/activity.py -- the single backend state machine.

Covers all three activities (PREPARING the zip, the CHECKING size sweep,
and REFRESHING the library list), the mutual-exclusion guard between them,
cooperative cancellation, and the shared book/size helpers. Activities run
on session.py's real background executor (submitted via session.submit), so
these tests wait for the machine to return to IDLE rather than calling the
worker bodies directly -- that also exercises the real threading path.
"""
from __future__ import annotations

import pathlib
import threading
import time
import zipfile

import pytest
from bookvault_core import cache
from bookvault_core.client import DownloadCancelled
from bookvault_core.library_fs import (
    MIRROR_INDEX,
    read_mirror_index,
    record_in_mirror_index,
)
from bookvault_web import activity

from tests.fakes import FakeLitresClient

TEXT_FILES = [{"id": 100, "extension": "epub", "is_additional": False, "size": 1_000_000}]  # 1.0 MB
BIG_FILES = [{"id": 200, "extension": "epub", "is_additional": False, "size": 2_400_000}]  # 2.4 MB


def _book(id, title, files=None):
    return {"id": id, "title": title}, files or []


def _make_client(*books_and_files):
    library = []
    files_by_id = {}
    for art, files in books_and_files:
        library.append(art)
        files_by_id[art["id"]] = files
    return FakeLitresClient(library=library, files_by_id=files_by_id)


def wait_until_idle(timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if activity.snapshot()["state"] == activity.IDLE:
            return activity.snapshot()
        time.sleep(0.005)
    raise AssertionError(f"activity did not settle within {timeout}s: {activity.snapshot()}")


def _record_get_files(client):
    """Wrap client.get_files so tests can assert *which* books (and in what
    order) actually triggered a live file fetch."""
    calls = []
    original = client.get_files

    def recording(art_id, should_cancel=None):
        calls.append(art_id)
        return original(art_id)

    client.get_files = recording
    return calls


# ==========================================================================
# PREPARING -- building the zip (formerly download_job)
# ==========================================================================


def test_prepare_downloads_everything_when_no_selection():
    client = _make_client(_book(1, "Book One", TEXT_FILES), _book(2, "Book Two", TEXT_FILES))
    assert activity.prepare(client) is True
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 2
    assert sorted(client.download_calls) == [1, 2]


def test_prepare_downloads_only_the_selected_ids():
    client = _make_client(
        _book(1, "Book One", TEXT_FILES),
        _book(2, "Book Two", TEXT_FILES),
        _book(3, "Book Three", TEXT_FILES),
    )
    activity.prepare(client, art_ids={1, 3})
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 2
    assert sorted(client.download_calls) == [1, 3]


def test_prepare_with_empty_selection_downloads_nothing():
    """An explicitly empty selection must not be treated the same as "no
    filter" (which would silently prepare the whole library instead)."""
    client = _make_client(_book(1, "Book One", TEXT_FILES), _book(2, "Book Two", TEXT_FILES))
    activity.prepare(client, art_ids=set())
    result = wait_until_idle()
    assert result["done"] == 0
    assert client.download_calls == []


def test_prepare_returns_false_if_already_running():
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    assert activity.prepare(client) is True
    assert activity.prepare(client) is False  # second call is a no-op
    wait_until_idle()


def test_book_with_no_downloadable_file_is_skipped_not_fatal():
    client = _make_client(_book(1, "Has files", TEXT_FILES), _book(2, "No files at all", []))
    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 1
    skipped = [e for e in result["log"] if e["status"] == "skipped"]
    assert [e["title"] for e in skipped] == ["No files at all"]


def test_one_books_download_failure_does_not_abort_the_rest():
    client = _make_client(_book(1, "Will fail", TEXT_FILES), _book(2, "Will succeed", TEXT_FILES))
    client.fail_downloads = {1}
    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 1
    errors = [e for e in result["log"] if e["status"] == "error"]
    assert [e["title"] for e in errors] == ["Will fail"]
    assert "Will succeed" in [e["title"] for e in result["log"] if e["status"] == "done"]


def test_prepare_job_level_failure_marks_result_error():
    client = FakeLitresClient()

    def broken_iter_library(limit=100):
        raise RuntimeError("session expired")
        yield  # pragma: no cover -- makes this a generator

    client.iter_library = broken_iter_library
    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "error"
    assert "session expired" in result["error"]


def test_cancel_stops_the_prepare_queue_before_the_next_book():
    client = _make_client(
        _book(1, "First", TEXT_FILES),
        _book(2, "Second", TEXT_FILES),
        _book(3, "Third", TEXT_FILES),
    )
    original_download = client.download_file

    def download_and_cancel_after_first(art_id, release_file_id, filename, dest, subscr=False, should_cancel=None, on_progress=None):
        result = original_download(art_id, release_file_id, filename, dest, subscr)
        if art_id == 1:
            activity.cancel()
        return result

    client.download_file = download_and_cancel_after_first
    activity.prepare(client)
    result = wait_until_idle()

    assert result["result"] == "cancelled"
    assert result["done"] == 1
    assert client.download_calls == [1]  # never reached book 2 or 3


def test_cancel_interrupts_a_download_mid_transfer():
    """Stop pressed while a large file is downloading: download_file raises
    DownloadCancelled, the partial book is dropped (not "done", not an error),
    and the queue stops -- book 2 is never attempted."""
    client = _make_client(_book(1, "Big audiobook", TEXT_FILES), _book(2, "Next up", TEXT_FILES))
    attempted = []

    def cancel_mid_transfer(art_id, release_file_id, filename, dest, subscr=False, should_cancel=None, on_progress=None):
        attempted.append(art_id)
        activity.cancel()  # user hits Stop while this transfer is in flight
        assert should_cancel is not None and should_cancel()
        raise DownloadCancelled(f"cancelled mid-transfer for art {art_id}")

    client.download_file = cancel_mid_transfer
    activity.prepare(client)
    result = wait_until_idle()

    assert result["result"] == "cancelled"
    assert result["done"] == 0  # the interrupted book didn't complete
    assert result["log"] == []  # and wasn't recorded as done or as an error
    assert attempted == [1]  # stopped immediately; book 2 never started


def test_prepare_total_reflects_selection_size_not_full_library():
    client = _make_client(
        _book(1, "Book One", TEXT_FILES),
        _book(2, "Book Two", TEXT_FILES),
        _book(3, "Book Three", TEXT_FILES),
    )
    activity.prepare(client, art_ids={1, 2})
    assert activity.snapshot()["total"] == 2
    wait_until_idle()


def test_prepare_preferred_format_is_passed_through_to_pick_best_file():
    files = [
        {"id": 10, "extension": "epub", "is_additional": False, "size": 1},
        {"id": 11, "extension": "a4.pdf", "is_additional": False, "size": 1},
    ]
    client = _make_client(_book(1, "Multi-format book", files))
    activity.prepare(client, preferred_ext="a4.pdf")
    wait_until_idle()
    assert client.download_calls == [1]
    assert activity.snapshot()["log"][0]["ext"] == "a4.pdf"


def test_prepare_title_falls_back_to_art_id_when_missing():
    client = _make_client(({"id": 42}, TEXT_FILES))
    activity.prepare(client)
    result = wait_until_idle()
    assert result["log"][0]["title"] == "42"


def test_snapshot_log_and_sizes_are_copies_not_the_live_ones():
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.prepare(client)
    wait_until_idle()
    snap = activity.snapshot()
    snap["log"].append({"title": "injected", "status": "done"})
    snap["sizes"][999] = 1.0
    assert len(activity.snapshot()["log"]) == 1  # mutation didn't leak back
    assert 999 not in activity.snapshot()["sizes"]


# ==========================================================================
# PREPARING -- caching behaviour (a warm cache means litres.ru isn't re-hit)
# ==========================================================================


def test_prepare_uses_cached_library_listing_instead_of_iter_library():
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    client.iter_library = lambda limit=100: (_ for _ in ()).throw(
        AssertionError("iter_library() should not be called when the cache is warm")
    )
    cache.set_library([{"id": 1, "title": "Book One"}])

    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 1


def test_prepare_falls_back_to_iter_library_when_cache_is_cold():
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    assert cache.get_library() is None

    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 1


def test_prepare_reuses_a_cached_file_listing_instead_of_calling_get_files():
    client = _make_client(_book(1, "Book One", []))  # no files on the fake
    cache.set_files(1, TEXT_FILES)  # ...but the cache already has them

    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"
    assert result["done"] == 1
    assert client.download_calls == [1]


def test_prepare_populates_the_cache_after_a_live_file_fetch():
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    assert cache.get_files(1) is None

    activity.prepare(client)
    wait_until_idle()
    assert cache.get_files(1) == TEXT_FILES


def _write_zip(dest, entries):
    """Write a real (STORED) zip file to `dest`, like litres serves for an
    epub or a zip_with_mp3 audiobook -- so its bytes carry a nested
    end-of-central-directory signature."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for name, data in entries:
            z.writestr(name, data)


def test_prepare_deflates_an_ebook_zip_so_extractors_do_not_break():
    """Regression: members that are themselves zips (epub, fb2.zip, ...) had
    their raw end-of-central-directory marker (PK\\x05\\x06) land verbatim in the
    outer archive under ZIP_STORED, so macOS Archive Utility saw several and
    rejected the file as "unsupported format". A non-audio zip member is kept
    as one file but DEFLATEd, which masks the nested signatures -- the outer
    archive must carry exactly one EOCD and the member must be compressed."""
    client = _make_client(_book(1, "An Ebook", TEXT_FILES))

    def download_epub(art_id, release_file_id, filename, dest, subscr=False, should_cancel=None, on_progress=None):
        _write_zip(dest, [("mimetype", b"application/epub+zip"), ("body.xhtml", b"<html/>")])
        client.download_calls.append(art_id)
        return dest

    client.download_file = download_epub
    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"

    raw = pathlib.Path(result["zip_path"]).read_bytes()
    assert raw.count(b"PK\x05\x06") == 1  # only the outer archive's own EOCD survives
    with zipfile.ZipFile(result["zip_path"]) as zf:
        assert zf.namelist() == ["An Ebook.epub"]
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in zf.infolist())


def test_prepare_unpacks_an_audiobook_zip_into_stored_tracks():
    """An audiobook (art_type == 1) arrives as a zip_with_mp3 bundle. Rather
    than re-compressing ~gigabytes of already-compressed audio, unpack it and
    add each track STORED under a per-book folder -- fast, and with no nested
    zip signature to confuse Archive Utility."""
    client = _make_client(({"id": 1, "title": "An Audiobook", "art_type": 1}, TEXT_FILES))

    def download_zip_with_mp3(art_id, release_file_id, filename, dest, subscr=False, should_cancel=None, on_progress=None):
        _write_zip(dest, [
            ("01 - intro.mp3", b"\xff\xfb" + b"chapter-one-audio" * 50),
            ("02 - outro.mp3", b"\xff\xfb" + b"chapter-two-audio" * 50),
        ])
        client.download_calls.append(art_id)
        return dest

    client.download_file = download_zip_with_mp3
    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "done"

    with zipfile.ZipFile(result["zip_path"]) as zf:
        # tracks live under a per-book folder, stored -- not the nested .zip
        assert zf.namelist() == ["An Audiobook/01 - intro.mp3", "An Audiobook/02 - outro.mp3"]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist())
    raw = pathlib.Path(result["zip_path"]).read_bytes()
    assert raw.count(b"PK\x05\x06") == 1  # no leftover nested audiobook-zip EOCD


# ==========================================================================
# CHECKING -- the paced per-book size sweep (moved from the frontend)
# ==========================================================================


def test_check_resolves_sizes_for_every_book():
    cache.set_library([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
    client = FakeLitresClient(files_by_id={1: BIG_FILES, 2: TEXT_FILES})

    assert activity.check_sizes(client) is True
    result = wait_until_idle()

    assert result["result"] == "done"
    assert result["done"] == 2 and result["total"] == 2
    assert result["sizes"] == {1: 2.4, 2: 1.0}


def test_check_uses_cached_file_listings_without_calling_get_files():
    cache.set_library([{"id": 1, "title": "A"}])
    cache.set_files(1, TEXT_FILES)
    client = FakeLitresClient(files_by_id={1: TEXT_FILES})
    calls = _record_get_files(client)

    activity.check_sizes(client)
    result = wait_until_idle()

    assert result["sizes"] == {1: 1.0}
    assert calls == []  # cache hit -- litres.ru never touched


def test_cache_only_sweep_resolves_cached_but_never_fetches_live():
    # The automatic on-load sweep (live=False) must resolve sizes already on
    # disk and touch litres.ru zero times for the rest -- so just opening the
    # app never fires a library's worth of size requests.
    cache.set_library([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
    cache.set_files(1, TEXT_FILES)  # book 1 cached; book 2 is not
    client = FakeLitresClient(files_by_id={2: TEXT_FILES})
    calls = _record_get_files(client)

    activity.check_sizes(client, live=False)
    result = wait_until_idle()

    assert calls == []  # cache-only: no live fetch for the uncached book
    assert result["sizes"] == {1: 1.0}  # only the cached book resolved
    assert 2 not in result["sizes"]  # the uncached book was left unresolved
    assert "Refresh" in result["message"]  # user is told how to fetch the rest


def test_check_resolves_selected_books_first():
    cache.set_library([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}])
    client = FakeLitresClient(files_by_id={1: TEXT_FILES, 2: TEXT_FILES, 3: TEXT_FILES})
    calls = _record_get_files(client)

    activity.check_sizes(client, selected=[2])
    wait_until_idle()

    assert calls == [2, 1, 3]  # the selected book was fetched before the rest


def test_check_size_is_none_when_no_downloadable_file():
    cache.set_library([{"id": 1, "title": "A"}])
    client = FakeLitresClient(files_by_id={1: []})

    activity.check_sizes(client)
    result = wait_until_idle()

    assert result["sizes"] == {1: None}
    assert result["result"] == "done"


def test_check_survives_a_per_book_fetch_failure():
    cache.set_library([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
    client = FakeLitresClient(files_by_id={1: TEXT_FILES, 2: TEXT_FILES})
    original = client.get_files

    def flaky(art_id, should_cancel=None):
        if art_id == 1:
            raise RuntimeError("socket hang up")
        return original(art_id)

    client.get_files = flaky
    activity.check_sizes(client)
    result = wait_until_idle()

    assert result["result"] == "done"  # one bad book doesn't sink the sweep
    assert result["sizes"] == {1: None, 2: 1.0}


def test_check_on_an_empty_library_finishes_immediately():
    cache.set_library([])
    client = FakeLitresClient()

    activity.check_sizes(client)
    result = wait_until_idle()

    assert result["result"] == "done"
    assert result["done"] == 0 and result["total"] == 0
    assert result["sizes"] == {}


def test_cancel_stops_the_size_sweep_before_the_next_book():
    cache.set_library([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}, {"id": 3, "title": "C"}])
    client = FakeLitresClient(files_by_id={1: TEXT_FILES, 2: TEXT_FILES, 3: TEXT_FILES})
    original = client.get_files

    def fetch_then_cancel_after_first(art_id, should_cancel=None):
        files = original(art_id)
        if art_id == 1:
            activity.cancel()
        return files

    client.get_files = fetch_then_cancel_after_first
    calls = []
    inner = client.get_files
    client.get_files = lambda art_id, should_cancel=None: (calls.append(art_id), inner(art_id))[1]

    activity.check_sizes(client)
    result = wait_until_idle()

    assert result["result"] == "cancelled"
    assert result["done"] == 1
    assert calls == [1]  # never reached book 2 or 3


# ==========================================================================
# REFRESHING -- reload the library list, then sweep sizes
# ==========================================================================


def test_refresh_reloads_the_library_then_sweeps_sizes():
    assert cache.get_library() is None
    client = FakeLitresClient(
        library=[{"id": 1, "title": "Fresh Book", "art_type": 0, "persons": [], "cover_url": None}],
        files_by_id={1: TEXT_FILES},
    )

    assert activity.refresh(client) is True
    result = wait_until_idle()

    assert result["result"] == "done"
    # The library was reloaded into the cache in the web-UI book shape...
    cached = cache.get_library()
    assert cached == [
        {"id": 1, "title": "Fresh Book", "authors": "", "is_audio": False, "cover_url": None}
    ]
    # ...and its sizes were swept right after.
    assert result["sizes"] == {1: 1.0}


def test_refresh_failure_sets_result_error_and_leaves_state_idle():
    client = FakeLitresClient()

    def broken_iter_library(limit=100):
        raise RuntimeError("Event loop is closed! Is Playwright already stopped?")
        yield  # pragma: no cover

    client.iter_library = broken_iter_library
    activity.refresh(client)
    result = wait_until_idle()

    assert result["result"] == "error"
    assert "session changed" in result["error"].lower()


# ==========================================================================
# Mutual exclusion -- only one activity may run at a time
# ==========================================================================


def test_only_one_activity_runs_at_a_time():
    """A second activity requested while one is running is a no-op. The
    sweep is held mid-fetch via a gate so the guard is exercised on the real
    threaded path, not by poking module state."""
    cache.set_library([{"id": 1, "title": "A"}])
    gate = threading.Event()
    client = FakeLitresClient(files_by_id={1: TEXT_FILES})

    def blocking_get_files(art_id, should_cancel=None):
        gate.wait(timeout=2.0)
        return TEXT_FILES

    client.get_files = blocking_get_files

    assert activity.check_sizes(client) is True
    # check_sizes claims CHECKING synchronously before submitting the worker.
    assert activity.snapshot()["state"] == activity.CHECKING
    assert activity.check_sizes(client) is False  # busy
    assert activity.prepare(client) is False  # busy
    assert activity.refresh(client) is False  # busy

    gate.set()
    wait_until_idle()


def test_cancel_returns_false_when_nothing_running():
    assert activity.cancel() is False


def test_cancel_returns_false_during_refresh_reload_phase():
    """Cancel only stops CHECKING/PREPARING; the REFRESHING reload itself is
    a single call that isn't interruptible, so cancel() no-ops there."""
    activity.state._state["state"] = activity.REFRESHING
    try:
        assert activity.cancel() is False
    finally:
        activity.state._state["state"] = activity.IDLE


# ==========================================================================
# Shared helpers -- pure logic, no threading
# ==========================================================================


def test_build_books_shapes_the_library_listing():
    client = FakeLitresClient(
        library=[
            {
                "id": 1,
                "title": "Book One",
                "art_type": 0,
                "persons": [
                    {"full_name": "Author A", "role": "author"},
                    {"full_name": "Translator T", "role": "translator"},
                ],
                "cover_url": "/pub/c/cover/1.jpg",
            },
            {"id": 2, "title": None, "art_type": 1, "persons": [], "cover_url": None},
        ]
    )
    books = activity.build_books(client)
    assert books[0] == {
        "id": 1,
        "title": "Book One",
        "authors": "Author A",  # translator excluded
        "is_audio": False,
        "cover_url": "https://static.litres.ru/pub/c/cover/1.jpg",
    }
    # A missing title falls back to the stringified id; art_type 1 == audio.
    assert books[1]["title"] == "2"
    assert books[1]["is_audio"] is True


def test_size_of_files_returns_mb_or_none():
    assert activity.size_of_files(TEXT_FILES) == 1.0
    assert activity.size_of_files([]) is None


def test_fetch_size_returns_size_and_raw_files():
    client = FakeLitresClient(files_by_id={1: BIG_FILES})
    size_mb, files = activity.fetch_size(client, 1)
    assert size_mb == 2.4
    assert files == BIG_FILES


# ==========================================================================
# _friendly_error -- pure translation logic
# ==========================================================================


def test_friendly_error_recognizes_ddos_guard_block():
    assert "anti-bot" in activity.state._friendly_error(RuntimeError("Download failed for art 1 (403): DDoS-Guard"))


def test_friendly_error_recognizes_stale_client_after_relogin():
    msg = activity.state._friendly_error(RuntimeError("Event loop is closed! Is Playwright already stopped?"))
    assert "session changed" in msg.lower()


def test_friendly_error_recognizes_dropped_connection():
    msg = activity.state._friendly_error(RuntimeError("APIRequestContext.get: socket hang up"))
    assert "interrupted" in msg.lower()


def test_friendly_error_falls_back_to_raw_text_for_unrecognized_errors():
    assert "something truly unexpected" in activity.state._friendly_error(RuntimeError("something truly unexpected"))


# ==========================================================================
# Durable results view -- the failed/skipped rows must outlive a reload
# ==========================================================================


def test_results_survive_the_size_check_that_runs_on_the_next_page_load():
    """A finished build's per-book outcomes are kept in `results`, so the
    failed/skipped rows the user wants to inspect don't vanish when the
    automatic cache-only size-check fires on the next page load."""
    client = _make_client(_book(1, "Will fail", TEXT_FILES), _book(2, "OK", TEXT_FILES))
    client.fail_downloads = {1}
    activity.prepare(client)
    done = wait_until_idle()
    assert [e["status"] for e in done["results"]] == ["error", "done"]

    # the size-check that every idle page load triggers
    activity.check_sizes(client, selected=[], live=False)
    after = wait_until_idle()

    assert after["log"] == []  # the live log was reset by the check ...
    # ... but the durable results (and the failure) are still there
    assert [e["title"] for e in after["results"] if e["status"] == "error"] == ["Will fail"]


def test_a_new_build_replaces_the_previous_results():
    client = _make_client(_book(1, "Will fail", TEXT_FILES), _book(2, "OK", TEXT_FILES))
    client.fail_downloads = {1}
    activity.prepare(client)
    wait_until_idle()

    client.fail_downloads = set()  # second build succeeds for both
    activity.prepare(client)
    after = wait_until_idle()
    assert len(after["results"]) == 2
    assert all(e["status"] == "done" for e in after["results"])


def test_zip_download_link_survives_the_size_check_on_reload():
    """A built zip stays downloadable after the next page load's size-check --
    the link must not vanish on reload."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.prepare(client)
    built = wait_until_idle()
    assert built["zip_path"]  # a real zip was produced

    activity.check_sizes(client, selected=[], live=False)
    after = wait_until_idle()
    assert after["zip_path"] == built["zip_path"]  # still offered, same file


def test_a_build_where_everything_failed_offers_no_empty_zip():
    client = _make_client(_book(1, "Will fail", TEXT_FILES))
    client.fail_downloads = {1}
    activity.prepare(client)
    result = wait_until_idle()
    assert result["done"] == 0
    assert result["zip_path"] is None  # nothing to download -- no empty archive


def test_results_and_zip_survive_a_refresh_too():
    """Not just the size-check: a library Refresh must also leave a finished
    build's results + download link intact."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.prepare(client)
    built = wait_until_idle()
    assert built["zip_path"] and len(built["results"]) == 1

    activity.refresh(client, selected=[])
    after = wait_until_idle()
    assert after["zip_path"] == built["zip_path"]
    assert len(after["results"]) == 1


def test_a_new_build_immediately_clears_the_previous_zip_and_results():
    """Starting a new build must drop the old zip/results at once (not show a
    stale download link while the new one is still running)."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.prepare(client)
    wait_until_idle()
    assert activity.snapshot()["zip_path"]

    # a second build that blocks so we can observe the mid-run state
    gate = threading.Event()
    client2 = _make_client(_book(2, "Book Two", TEXT_FILES))
    real = client2.download_file

    def blocking_download(*a, **kw):
        gate.wait(2)
        return real(*a, **kw)

    client2.download_file = blocking_download
    activity.prepare(client2)
    try:
        mid = activity.snapshot()
        assert mid["zip_path"] is None      # old zip dropped immediately
        assert mid["results"] == []         # old results dropped immediately
    finally:
        gate.set()
    wait_until_idle()


def test_cancelled_build_keeps_finished_books_as_a_partial_zip_and_results():
    """Stop after some books already finished: those are a valid partial zip,
    and their results (plus the durable copy) survive a later reload."""
    client = _make_client(_book(1, "Finished", TEXT_FILES), _book(2, "Interrupted", TEXT_FILES))
    real_download = client.download_file

    def finish_one_then_cancel(art_id, *a, **kw):
        if art_id == 2:
            activity.cancel()  # Stop pressed before book 2
            raise DownloadCancelled("stopped before book 2")
        return real_download(art_id, *a, **kw)

    client.download_file = finish_one_then_cancel
    activity.prepare(client)
    result = wait_until_idle()
    assert result["result"] == "cancelled"
    assert result["done"] == 1
    assert result["zip_path"]  # the one finished book is downloadable

    activity.check_sizes(client, selected=[], live=False)
    after = wait_until_idle()
    assert [e["title"] for e in after["results"] if e["status"] == "done"] == ["Finished"]
    assert after["zip_path"]  # partial zip still offered after the reload


# ==========================================================================
# Zip hygiene: member naming and workdir lifecycle
# ==========================================================================


def test_same_titled_books_get_distinct_zip_entries():
    """Two books that sanitize to the same title must not overwrite each
    other on extraction -- the second gets an ' (art_id)' suffix."""
    client = _make_client(
        _book(1, "War and Peace", TEXT_FILES),
        _book(2, "War and Peace", TEXT_FILES),
    )
    activity.prepare(client)
    snap = wait_until_idle()
    with zipfile.ZipFile(snap["zip_path"]) as zf:
        names = sorted(zf.namelist())
    assert names == ["War and Peace (2).epub", "War and Peace.epub"]


def test_unsanitizable_title_falls_back_to_art_id():
    """A title of pure punctuation sanitizes to nothing -- the entry must be
    named after the art id, not '.epub'."""
    client = _make_client(_book(77, "???!!!", TEXT_FILES))
    activity.prepare(client)
    snap = wait_until_idle()
    with zipfile.ZipFile(snap["zip_path"]) as zf:
        assert zf.namelist() == ["77.epub"]


def test_a_new_prepare_removes_the_previous_builds_workdir():
    """Every build gets its own mkdtemp; once superseded, the old zip
    (potentially many GB) must be deleted, not leaked until reboot."""
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client)
    old_zip = pathlib.Path(wait_until_idle()["zip_path"])
    assert old_zip.exists()

    activity.prepare(client)
    snap = wait_until_idle()

    assert not old_zip.parent.exists()  # previous workdir cleaned up
    assert pathlib.Path(snap["zip_path"]).exists()  # new build unaffected


def test_a_build_with_no_successes_leaves_no_workdir_behind(monkeypatch):
    """A build where every book failed offers no zip (existing behavior) --
    and must also remove its now-useless workdir."""
    import tempfile as tempfile_mod

    made = []
    real_mkdtemp = tempfile_mod.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(activity.archive.tempfile, "mkdtemp", recording_mkdtemp)
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    client.fail_downloads = {1}
    activity.prepare(client)
    snap = wait_until_idle()

    assert snap["zip_path"] is None
    assert made and not pathlib.Path(made[0]).exists()


# ==========================================================================
# Auto-saving the finished archive into the configured folder
# ==========================================================================


def test_finished_archive_is_moved_into_the_destination_folder(tmp_path):
    dest = tmp_path / "MyBooks"
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=dest)
    snap = wait_until_idle()

    saved = list(dest.glob("litres-library-*.zip"))
    assert len(saved) == 1
    # Both the download route and the "Saved to ..." line point at it.
    assert snap["zip_path"] == str(saved[0])
    assert snap["saved_path"] == str(saved[0])
    with zipfile.ZipFile(saved[0]) as zf:
        assert zf.namelist() == ["Book A.epub"]


def test_the_destination_folder_is_created_if_missing(tmp_path):
    dest = tmp_path / "nested" / "deeper"
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=dest)
    wait_until_idle()
    assert list(dest.glob("*.zip"))


def test_saving_moves_the_zip_out_and_removes_the_workdir(tmp_path):
    """Once the archive is in the user's folder the temp workdir holds
    nothing worth keeping -- it must go immediately, not at the next build."""
    dest = tmp_path / "out"
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=dest)
    wait_until_idle()
    assert activity.state._state["workdir"] is None
    assert not list(tmp_path.glob("litres-*/"))  # nothing staged left behind


def test_a_new_build_never_deletes_the_users_download_folder(tmp_path):
    """Regression: prepare() used to rmtree Path(previous_zip).parent, which
    once the archive lives in the user's own folder would delete that folder
    and everything else in it."""
    dest = tmp_path / "Downloads"
    dest.mkdir()
    bystander = dest / "tax-return.pdf"
    bystander.write_bytes(b"important")

    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=dest)
    first = pathlib.Path(wait_until_idle()["saved_path"])

    activity.prepare(client, dest_dir=dest)
    wait_until_idle()

    assert dest.exists()
    assert bystander.read_bytes() == b"important"
    assert first.exists()  # the previous archive is the user's to keep


def test_two_builds_in_the_same_second_do_not_collide(tmp_path, monkeypatch):
    """The name is timestamped to the second, so two quick builds would
    otherwise land on the same filename."""
    monkeypatch.setattr(activity.archive.time, "strftime", lambda fmt: "20260809-120000")
    dest = tmp_path / "out"
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    for _ in range(2):
        activity.prepare(client, dest_dir=dest)
        wait_until_idle()

    names = sorted(p.name for p in dest.glob("*.zip"))
    assert names == ["litres-library-20260809-120000 (2).zip", "litres-library-20260809-120000.zip"]


def test_an_unwritable_destination_keeps_the_archive_downloadable(tmp_path):
    """A build can take hours -- if the folder turns out to be unusable, the
    archive stays in temp and is still offered rather than being thrown away."""
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("I am a file")  # mkdir(parents=True) will raise

    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=blocker / "sub")
    snap = wait_until_idle()

    assert snap["saved_path"] is None
    assert snap["zip_path"] and pathlib.Path(snap["zip_path"]).exists()
    assert "Couldn't save" in snap["message"]
    # Still tracked, so the next prepare() cleans it up.
    assert activity.state._state["workdir"] == str(pathlib.Path(snap["zip_path"]).parent)


def test_a_failed_build_writes_nothing_into_the_destination(tmp_path):
    """Nothing is ever put in the user's folder until a build succeeds."""
    dest = tmp_path / "out"
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    client.fail_downloads = {1}
    activity.prepare(client, dest_dir=dest)
    snap = wait_until_idle()

    assert snap["zip_path"] is None and snap["saved_path"] is None
    assert not dest.exists()


def test_without_a_destination_the_archive_stays_in_temp(tmp_path):
    """dest_dir=None is the opt-out: unchanged pre-existing behaviour."""
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=None)
    snap = wait_until_idle()

    assert snap["saved_path"] is None
    assert pathlib.Path(snap["zip_path"]).exists()


def test_saved_path_survives_a_later_size_check(tmp_path):
    """Same durability guarantee as zip_path -- the "Saved to ..." line must
    survive the sweep that fires on the next page load."""
    dest = tmp_path / "out"
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.prepare(client, dest_dir=dest)
    saved = wait_until_idle()["saved_path"]

    activity.check_sizes(client, live=False)
    assert wait_until_idle()["saved_path"] == saved


def test_a_crashed_build_removes_its_workdir(monkeypatch):
    import tempfile as tempfile_mod

    made = []
    real_mkdtemp = tempfile_mod.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(activity.archive.tempfile, "mkdtemp", recording_mkdtemp)
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    # The zip build reaches the listing through `library._iter_books`, so the
    # patch has to land on the module that owns it.
    monkeypatch.setattr(
        activity.library, "_iter_books", lambda c: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    activity.prepare(client)
    snap = wait_until_idle()

    assert snap["result"] == "error"
    assert made and not pathlib.Path(made[0]).exists()


# ==========================================================================
# Remaining state-machine edges
# ==========================================================================


def test_check_sweep_crash_surfaces_error_and_returns_idle(monkeypatch):
    """A crash while reading the cached library must land back at IDLE with
    result=error -- not wedge the machine in CHECKING forever."""
    monkeypatch.setattr(cache, "get_library", lambda: (_ for _ in ()).throw(RuntimeError("disk gone")))
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    assert activity.check_sizes(client) is True
    snap = wait_until_idle()
    assert snap["result"] == "error"
    assert snap["error"]  # a friendly message is surfaced


def test_refresh_cancelled_during_reload_stops_before_the_sweep(monkeypatch):
    """cancel() only accepts CHECKING/PREPARING, but the cancel event may
    already be set when the reload finishes -- the refresh must then stop
    cleanly instead of rolling into the size sweep."""
    def build_and_cancel(client):
        activity.state._cancel_event.set()
        return [{"id": 1, "title": "Book A", "is_audio": False}]

    monkeypatch.setattr(activity.library, "build_books", build_and_cancel)
    client = _make_client(_book(1, "Book A", TEXT_FILES))
    activity.refresh(client)
    snap = wait_until_idle()
    assert snap["result"] == "cancelled"
    assert snap["sizes"] == {}  # the sweep never ran


def test_friendly_error_maps_common_statuses():
    cases = {
        "Download failed for art 5 (403): Forbidden": "won't serve this title",
        "Download failed for art 5 (429): slow down": "Rate-limited",
        "Library fetch failed (401): PermissionMissing": "expired",
        "Timeout 300000ms exceeded": "timed out",
    }
    for raw, expected in cases.items():
        assert expected in activity.state._friendly_error(Exception(raw)), raw


# ==========================================================================
# SYNCING -- on-disk ABS library
# ==========================================================================


def test_start_sync_requires_library_dir(monkeypatch, tmp_path):
    client = _make_client(
        (
            {
                "id": 1,
                "title": "Audio One",
                "art_type": 1,
                "persons": [{"full_name": "Author A", "role": "author"}],
                "last_released_at": "2024-01-01",
            },
            [{"id": 100, "extension": "m4b", "file_type": "mobile_version_mp4", "is_additional": False, "size": 8}],
        )
    )
    monkeypatch.delenv("LITRES_LIBRARY_DIR", raising=False)
    assert activity.start_sync(client) is False


def test_start_sync_writes_library(monkeypatch, tmp_path):
    lib = tmp_path / "lib"
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(lib))
    client = _make_client(
        (
            {
                "id": 1,
                "title": "Audio One",
                "art_type": 1,
                "persons": [{"full_name": "Author A", "role": "author"}],
                "last_released_at": "2024-01-01",
            },
            [{"id": 100, "extension": "m4b", "file_type": "mobile_version_mp4", "is_additional": False, "size": 8}],
        )
    )
    assert activity.start_sync(client, audio_only=True) is True
    snap = wait_until_idle(timeout=5.0)
    assert snap["result"] == "done"
    assert (lib / "Author A" / "Audio One" / "metadata.json").exists()


# -- sizes are durable, not per-activity ------------------------------------
# A book's file size is effectively immutable. It has no business being lost
# to a 15-minute clock meant for "did you buy anything new", nor to starting
# an unrelated operation.


def test_a_cache_only_sweep_uses_the_stale_library_when_the_fresh_one_expired(monkeypatch):
    """The library listing expires after 15 minutes; a book's file listing
    after 7 days. Sweeping an empty list because the *listing* went stale
    reported "0 of 0" and left every size showing as unknown while all of them
    sat fresh on disk."""
    from bookvault_core import cache

    books = [{"id": 1, "title": "One", "authors": "", "is_audio": False, "cover_url": None}]
    monkeypatch.setattr(cache, "get_library", lambda: None)          # fresh copy expired
    monkeypatch.setattr(cache, "get_library_stale", lambda: books)   # but we still know the ids
    monkeypatch.setattr(cache, "get_files", lambda art_id: [{"id": 9, "extension": "epub", "size": 5_000_000}])

    activity.library._run_check(object(), None, live=False)

    snap = activity.snapshot()
    assert snap["sizes"], "a stale library must still let cached sizes resolve"
    assert snap["total"] == 1 and snap["done"] == 1


def test_starting_an_activity_keeps_the_sizes_already_resolved():
    """Downloading, refreshing, or the automatic check on page load must not
    blank sizes the app already had -- they come from the durable file-listing
    cache, not from the activity that happens to be running."""
    activity.state._state["sizes"] = {1: 12.5, 2: 30.0}
    assert activity.state._begin(activity.PREPARING) is True
    assert activity.snapshot()["sizes"] == {1: 12.5, 2: 30.0}


def test_logout_drops_the_sizes_so_they_cannot_cross_accounts():
    """The flip side of surviving _begin: entries are keyed by art_id, so a
    previous account's sizes must not paint onto the next one's rows."""
    activity.state._state["sizes"] = {1: 12.5}
    activity.forget_sizes()
    assert activity.snapshot()["sizes"] == {}


# -- saving an extra copy of a finished archive -----------------------------

def test_copy_archive_leaves_the_original_where_it_was(tmp_path):
    """The point of the feature: the configured folder still has it."""
    saved = tmp_path / "configured" / "litres-library.zip"
    saved.parent.mkdir()
    saved.write_bytes(b"archive")
    activity.state._state["saved_path"] = str(saved)

    elsewhere = tmp_path / "external-drive"
    target = activity.copy_archive_to(elsewhere)

    assert target.read_bytes() == b"archive"
    assert saved.exists(), "the auto-saved original must survive a copy"
    assert activity.snapshot()["saved_path"] == str(saved)  # state untouched


def test_copy_archive_falls_back_to_the_temp_zip_when_the_save_failed(tmp_path):
    """When the auto-save failed the archive is still in its temp workdir --
    which is exactly when a copy somewhere real matters most."""
    temp_zip = tmp_path / "workdir" / "litres-library.zip"
    temp_zip.parent.mkdir()
    temp_zip.write_bytes(b"archive")
    activity.state._state["saved_path"] = None
    activity.state._state["zip_path"] = str(temp_zip)

    target = activity.copy_archive_to(tmp_path / "dest")
    assert target.exists() and temp_zip.exists()


def test_copy_archive_never_overwrites_an_existing_file(tmp_path):
    saved = tmp_path / "a" / "litres-library.zip"
    saved.parent.mkdir()
    saved.write_bytes(b"new")
    dest = tmp_path / "b"
    dest.mkdir()
    (dest / "litres-library.zip").write_bytes(b"older archive worth keeping")
    activity.state._state["saved_path"] = str(saved)

    target = activity.copy_archive_to(dest)
    assert target.name == "litres-library (2).zip"
    assert (dest / "litres-library.zip").read_bytes() == b"older archive worth keeping"


def test_copying_into_the_folder_it_already_lives_in_is_a_no_op(tmp_path):
    """Copying a file onto itself would truncate it."""
    saved = tmp_path / "litres-library.zip"
    saved.write_bytes(b"archive")
    activity.state._state["saved_path"] = str(saved)

    target = activity.copy_archive_to(tmp_path)
    assert target == saved
    assert saved.read_bytes() == b"archive"
    assert list(tmp_path.iterdir()) == [saved]  # no " (2)" litter


def test_copy_archive_without_a_finished_build_raises(tmp_path):
    activity.state._state["saved_path"] = None
    activity.state._state["zip_path"] = None
    with pytest.raises(FileNotFoundError):
        activity.copy_archive_to(tmp_path)


# -- whole-build byte progress ----------------------------------------------

def test_expected_total_sums_the_files_the_build_will_actually_pick(monkeypatch):
    """Agrees with the per-book sizes the UI shows, because it sums the same
    pick_best_file choice the download loop makes."""
    from bookvault_core import cache

    books = [{"id": 1}, {"id": 2}, {"id": 3}]
    monkeypatch.setattr(cache, "get_library", lambda: books)
    listings = {
        1: [{"id": 10, "extension": "epub", "size": 1_000_000}],
        2: [{"id": 20, "extension": "epub", "size": 2_500_000}],
        3: None,  # never had its listing cached -- contributes nothing
    }
    monkeypatch.setattr(cache, "get_files", lambda art_id: listings.get(art_id))

    assert activity.library._expected_total_bytes(None) == 3_500_000
    assert activity.library._expected_total_bytes({1}) == 1_000_000


def test_expected_total_is_none_when_nothing_is_known(monkeypatch):
    """None, not 0 -- so the UI can say "unknown" rather than draw a bar
    against a denominator of zero."""
    from bookvault_core import cache

    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: None)
    assert activity.library._expected_total_bytes(None) is None


def test_byte_progress_resets_when_a_new_activity_starts():
    """Unlike sizes, these ARE progress for one build and must not carry over."""
    activity.state._state["bytes_done"] = 500
    activity.state._state["bytes_total"] = 1000
    activity.state._begin(activity.PREPARING)
    snap = activity.snapshot()
    assert snap["bytes_done"] == 0 and snap["bytes_total"] is None


# ==========================================================================
# DOWNLOADING -- the loose-file mirror
# ==========================================================================
# The interesting cases are all about a file that is ALREADY THERE: is it the
# book, a half-written wreck, or something the user put there themselves?


def _files(size, ext="epub", fid=100):
    return [{"id": fid, "extension": ext, "is_additional": False, "size": size}]


def test_download_files_writes_books_into_the_folder(tmp_path):
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    assert activity.download_files(client, dest_root=tmp_path) is True
    result = wait_until_idle()
    assert result["result"] == "done"
    assert (tmp_path / "Book One.epub").exists()
    assert [e["status"] for e in result["log"]] == ["done"]


def test_a_complete_file_is_not_downloaded_again(tmp_path):
    """The whole point: running it twice must be nearly free."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    (tmp_path / "Book One.epub").write_bytes(b"x" * 1_000_000)
    record_in_mirror_index(tmp_path, 1, "Book One.epub", 1_000_000)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["exists"]
    assert client.download_calls == [], "a complete book must not be re-fetched"


def test_a_truncated_file_is_redownloaded_and_overwritten(tmp_path):
    """An interrupted transfer left 3 bytes where 1 MB belongs. Trusting it
    forever is the bug this check exists to prevent."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    dest = tmp_path / "Book One.epub"
    dest.write_bytes(b"abc")
    # A previous run recorded writing the full file; only 3 bytes are there now.
    record_in_mirror_index(tmp_path, 1, "Book One.epub", 1_000_000)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["replaced"]
    assert client.download_calls == [1]
    assert dest.read_bytes() != b"abc", "the partial file must be overwritten"


def test_a_file_that_is_too_LARGE_is_also_replaced(tmp_path):
    """Not just truncation -- any mismatch. A file bigger than the listing says
    is equally not the book (a double-append, a different edition)."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    (tmp_path / "Book One.epub").write_bytes(b"x" * 2_000_000)
    record_in_mirror_index(tmp_path, 1, "Book One.epub", 1_000_000)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["replaced"]
    assert client.download_calls == [1]


def test_a_zero_byte_file_counts_as_missing_not_complete(tmp_path):
    """A transfer that died before the first chunk. 0 != 1_000_000, so it must
    re-download rather than be read as "present"."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    (tmp_path / "Book One.epub").write_bytes(b"")

    activity.download_files(client, dest_root=tmp_path)
    wait_until_idle()
    assert client.download_calls == [1]


def test_a_listing_without_a_size_leaves_an_existing_file_alone(tmp_path):
    """No size to compare against. Re-downloading a whole library every run on
    a listing that omits the field would be far worse than trusting the file."""
    client = _make_client(_book(1, "Book One", _files(None)))
    dest = tmp_path / "Book One.epub"
    dest.write_bytes(b"whatever")

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["exists"]
    assert client.download_calls == []


def test_a_failed_transfer_leaves_no_file_where_the_book_belongs(tmp_path):
    """Staged through a .part file: a dead transfer must not leave wreckage at
    the destination, or the NEXT run would size-check the wreckage."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    client.fail_downloads = {1}

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["error"]
    assert not (tmp_path / "Book One.epub").exists()


def test_a_book_with_no_downloadable_file_is_skipped(tmp_path):
    client = _make_client(_book(1, "Rights limited", []))
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["skipped"]


def test_two_books_with_the_same_title_do_not_overwrite_each_other(tmp_path):
    """Both sanitize to "Same Title"; the second must not land on the first."""
    client = _make_client(
        _book(1, "Same Title", TEXT_FILES),
        _book(2, "Same Title", TEXT_FILES),
    )
    # Its own subfolder: conftest also parks the cache/state files in tmp_path,
    # and this assertion is about the *whole* directory listing.
    dest = tmp_path / "mirror"
    activity.download_files(client, dest_root=dest)
    wait_until_idle()
    names = sorted(p.name for p in dest.iterdir() if p.name != MIRROR_INDEX)
    assert names == ["Same Title (2).epub", "Same Title.epub"]


def test_a_title_of_pure_punctuation_falls_back_to_the_id(tmp_path):
    client = _make_client(_book(7, "???!!!", TEXT_FILES))
    activity.download_files(client, dest_root=tmp_path)
    wait_until_idle()
    assert (tmp_path / "7.epub").exists(), "must not write a bare '.epub'"


def test_the_mirror_folder_is_created_if_missing(tmp_path):
    """A first run into a folder that doesn't exist yet, including parents."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    dest = tmp_path / "not" / "there" / "yet"
    activity.download_files(client, dest_root=dest)
    assert wait_until_idle()["result"] == "done"
    assert (dest / "Book One.epub").exists()


def test_only_the_selected_books_are_downloaded(tmp_path):
    client = _make_client(
        _book(1, "One", TEXT_FILES), _book(2, "Two", TEXT_FILES), _book(3, "Three", TEXT_FILES),
    )
    activity.download_files(client, art_ids={1, 3}, dest_root=tmp_path)
    wait_until_idle()
    assert sorted(client.download_calls) == [1, 3]
    assert not (tmp_path / "Two.epub").exists()


def test_stop_leaves_finished_books_in_place_for_the_next_run(tmp_path):
    """Cancelling isn't a rollback -- what landed stays, and re-running skips
    it. That's what makes a mirror resumable."""
    client = _make_client(_book(1, "First", TEXT_FILES), _book(2, "Second", TEXT_FILES))
    original = client.download_file

    def stop_after_first(art_id, release_file_id, filename, dest, subscr=False, should_cancel=None, on_progress=None):
        result = original(art_id, release_file_id, filename, dest, subscr)
        if art_id == 1:
            activity.cancel()
        return result

    client.download_file = stop_after_first
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert result["result"] == "cancelled"
    assert (tmp_path / "First.epub").exists()
    assert not (tmp_path / "Second.epub").exists()


def test_a_second_run_after_a_stop_skips_what_already_landed(tmp_path):
    client = _make_client(_book(1, "First", TEXT_FILES))
    activity.download_files(client, dest_root=tmp_path)
    wait_until_idle()
    client.download_calls.clear()

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["exists"]
    assert client.download_calls == []


def test_download_files_refuses_while_something_else_runs(tmp_path):
    client = _make_client(_book(1, "One", TEXT_FILES))
    assert activity.download_files(client, dest_root=tmp_path) is True
    assert activity.download_files(client, dest_root=tmp_path) is False
    wait_until_idle()


def test_an_unwritable_destination_ends_in_error_not_a_wedged_machine(tmp_path):
    """The machine must land back at IDLE so the user can try elsewhere."""
    blocked = tmp_path / "read-only"
    blocked.mkdir()
    blocked.chmod(0o500)
    client = _make_client(_book(1, "One", TEXT_FILES))
    try:
        activity.download_files(client, dest_root=blocked / "sub")
        result = wait_until_idle()
        assert result["state"] == activity.IDLE
        assert result["result"] == "error"
    finally:
        blocked.chmod(0o700)


# -- nothing may be left behind in the user's own folder --------------------
# The mirror writes into a directory the user browses. A `.part` file left
# there is invisible (dotfile), never retried, and accumulates one per failure
# -- so every path out of a transfer has to clean up after itself.


# -- every delivered shape must work, not just the bundle ------------------
# `is_audio` describes the TITLE; it says nothing about the file litres.ru
# serves. Only zip_with_mp3 arrives as a zip of tracks -- mobile_version_mp4
# and friends are a single file, exactly like an ebook. Branching on is_audio
# instead of on the bytes made every single-file audiobook die on "File is
# not a zip file", i.e. an entire format, for every user who prefers it.

M4B_FILES = [{"id": 300, "extension": "m4b", "is_additional": False,
              "file_type": "mobile_version_mp4", "size": 1_000_000}]


def _audiobook(art_id, title, files):
    return {"id": art_id, "title": title, "art_type": 1}, files


def test_a_single_file_audiobook_is_saved_as_a_file_not_unpacked(tmp_path):
    """mobile_version_mp4: one file, no archive. The regression case."""
    client = _make_client(_audiobook(1, "An Audiobook", M4B_FILES))

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["done"], result["log"]
    assert (tmp_path / "An Audiobook.m4b").is_file()
    assert not (tmp_path / "An Audiobook").exists(), "must not be unpacked into a folder"


def test_a_bundle_audiobook_is_still_unpacked_into_tracks(tmp_path):
    """The other audio shape must keep working exactly as before."""
    client = _make_client(_audiobook(1, "Bundle Book", TEXT_FILES))

    def download_bundle(art_id, release_file_id, filename, dest, subscr=False,
                        should_cancel=None, on_progress=None):
        _write_zip(dest, [("01.mp3", b"\xff\xfbone" * 40), ("02.mp3", b"\xff\xfbtwo" * 40)])
        client.download_calls.append(art_id)
        return dest

    client.download_file = download_bundle
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["done"]
    assert sorted(p.name for p in (tmp_path / "Bundle Book").iterdir()) == ["01.mp3", "02.mp3"]


def test_an_ebook_that_is_itself_a_zip_is_never_unpacked(tmp_path):
    """epub and fb2.zip *are* zips. Unpacking on 'is it a zip' alone would
    shred every ebook into loose files -- the guard is audio AND zip."""
    client = _make_client(_book(1, "Zippy Ebook", TEXT_FILES))

    def download_epub(art_id, release_file_id, filename, dest, subscr=False,
                      should_cancel=None, on_progress=None):
        _write_zip(dest, [("mimetype", b"application/epub+zip"), ("content.opf", b"<xml/>")])
        client.download_calls.append(art_id)
        return dest

    client.download_file = download_epub
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["done"]
    assert (tmp_path / "Zippy Ebook.epub").is_file()
    assert not (tmp_path / "Zippy Ebook").is_dir()


def test_a_single_file_audiobook_is_recognised_on_the_second_run(tmp_path):
    """The round trip for this shape: it must be skipped, badged, and reused."""
    client = _make_client(_audiobook(1, "An Audiobook", M4B_FILES))
    activity.download_files(client, dest_root=tmp_path)
    assert wait_until_idle()["result"] == "done"

    client.download_calls.clear()
    activity.download_files(client, dest_root=tmp_path)
    assert [e["status"] for e in wait_until_idle()["log"]] == ["exists"]
    assert client.download_calls == [], "a single-file audiobook must not be re-fetched"


def test_a_single_file_audiobook_lights_the_badge_and_is_reused_by_the_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "get_library", lambda: [
        {"id": 1, "title": "An Audiobook", "is_audio": True},
    ])
    monkeypatch.setattr(cache, "get_files", lambda art_id: M4B_FILES)
    client = _make_client(_audiobook(1, "An Audiobook", M4B_FILES))

    activity.download_files(client, dest_root=tmp_path)
    assert wait_until_idle()["result"] == "done"

    activity.forget_books_on_disk()
    assert activity.books_on_disk(tmp_path) == [1]

    client.download_calls.clear()
    activity.prepare(client, mirror_root=tmp_path)
    zipped = wait_until_idle()
    assert [e["status"] for e in zipped["log"]] == ["reused"]
    assert client.download_calls == []
    with zipfile.ZipFile(zipped["zip_path"]) as zf:
        assert zf.namelist() == ["An Audiobook.m4b"], "packed as one file, not a folder"


def test_a_redownload_clears_a_stale_folder_of_the_other_shape(tmp_path):
    """When a re-download *does* happen and the book now arrives as a single
    file, the folder left by the previous bundle-shaped download must go --
    otherwise two copies of the same book sit in the mirror and the older one
    still looks like it."""
    stale = tmp_path / "An Audiobook"
    stale.mkdir()
    (stale / "01.mp3").write_bytes(b"old track")
    # A record that no longer matches the folder forces the re-download.
    record_in_mirror_index(tmp_path, 1, "An Audiobook", 0, tracks=9)
    client = _make_client(_audiobook(1, "An Audiobook", M4B_FILES))

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["replaced"]
    assert (tmp_path / "An Audiobook.m4b").is_file()
    assert not stale.exists(), "the old unpacked folder must not be left behind"


def test_an_audiobook_folder_from_before_the_index_is_trusted(tmp_path):
    """Upgrade path: folders written before the index existed have no record.
    Nothing to verify against is not a reason to re-download a library."""
    book = tmp_path / "Old Audiobook"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"track")
    client = _make_client(_audiobook(1, "Old Audiobook", TEXT_FILES))

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["exists"]
    assert client.download_calls == []


def test_a_corrupt_audio_bundle_leaves_no_staging_file(tmp_path):
    """The transfer succeeded, so the client has already washed its hands of
    the staging file; everything after it can still fail. The bundle not being
    a zip is the realistic version of that."""
    client = _make_client(({"id": 1, "title": "An Audiobook", "art_type": 1},
                           [{"id": 100, "extension": "zip", "is_additional": False, "size": 1000}]))

    # A real zip that unpacks to nothing -- a genuinely broken bundle, which
    # extract_audio_zip rejects. (A file that merely *isn't* a zip is no longer
    # an error: that is what a single-file audiobook looks like.)
    def empty_bundle(art_id, release_file_id, filename, dest, subscr=False,
                     should_cancel=None, on_progress=None):
        _write_zip(dest, [])
        client.download_calls.append(art_id)
        return dest

    client.download_file = empty_bundle
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert result["log"][0]["status"] == "error"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")] == []
    assert activity.books_on_disk(tmp_path) == [], "a failed extract must not look complete"


def test_a_cancelled_transfer_leaves_no_staging_file(tmp_path):
    """Stop mid-transfer is the common case, not an exotic one -- it must not
    cost the user a hidden file every time they change their mind."""
    client = _make_client(_book(1, "Book One", TEXT_FILES), _book(2, "Book Two", TEXT_FILES))

    def cancel_midway(art_id, release_file_id, filename, dest, subscr=False,
                      should_cancel=None, on_progress=None):
        client.download_calls.append(art_id)
        dest.write_bytes(b"half a book")
        raise DownloadCancelled("stopped mid-transfer")

    client.download_file = cancel_midway
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert result["result"] == "cancelled"
    # (the suite redirects its own cache file into tmp_path, so look for ours)
    strays = [p.name for p in tmp_path.iterdir() if p.name.endswith(".part") or "Book" in p.name]
    assert strays == [], f"a cancelled run must leave nothing behind, found: {strays}"


def test_the_summary_counts_each_outcome_separately(tmp_path):
    client = _make_client(
        _book(1, "Fresh", TEXT_FILES),
        _book(2, "Already", TEXT_FILES),
        _book(3, "Broken", TEXT_FILES),
    )
    (tmp_path / "Already.epub").write_bytes(b"x" * 1_000_000)   # complete
    record_in_mirror_index(tmp_path, 2, "Already.epub", 1_000_000)
    (tmp_path / "Broken.epub").write_bytes(b"x" * 12)           # partial
    record_in_mirror_index(tmp_path, 3, "Broken.epub", 1_000_000)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()
    by_status = {e["title"]: e["status"] for e in result["log"]}
    assert by_status == {"Fresh": "done", "Already": "exists", "Broken": "replaced"}
    assert "already saved" in result["message"]
    assert "re-downloaded" in result["message"]


# -- round trips: run it for real, then ask again ---------------------------
# Every test above hand-seeds the index and then checks one consumer. That is
# how a feature whose central check could NEVER fire shipped green: the seeded
# value agreed with the production code's assumption, and no test ever let a
# real download decide what the next question would see.
#
# These do. They perform an actual run and then ask the SAME question the app
# asks afterwards -- which is the only way a wrong idea of "what a finished
# download looks like" has anywhere to hide.


def test_running_the_mirror_twice_downloads_nothing_the_second_time(tmp_path):
    """The feature's entire promise, end to end and unseeded.

    This is the test whose absence let the catalogue-size check ship: it
    compared bytes on disk against the size litres.ru's listing declares, which
    that service does not honour, so the second run re-fetched the whole
    library. Hand-seeded tests passed because they seeded the same wrong
    number the code compared against."""
    client = _make_client(_book(1, "Book One", TEXT_FILES), _book(2, "Book Two", TEXT_FILES))

    activity.download_files(client, dest_root=tmp_path)
    assert wait_until_idle()["result"] == "done"
    assert sorted(client.download_calls) == [1, 2]

    client.download_calls.clear()
    activity.download_files(client, dest_root=tmp_path)
    second = wait_until_idle()

    assert [e["status"] for e in second["log"]] == ["exists", "exists"]
    assert client.download_calls == [], "a second run must re-fetch nothing"


def test_an_audiobook_mirror_run_is_also_idempotent(tmp_path):
    """Same promise for the unpacked-folder case, which has no file size to
    compare at all and so relies entirely on the recorded track count."""
    client = _make_client(({"id": 1, "title": "An Audiobook", "art_type": 1}, TEXT_FILES))

    def download_zip_with_mp3(art_id, release_file_id, filename, dest, subscr=False,
                              should_cancel=None, on_progress=None):
        _write_zip(dest, [("01.mp3", b"\xff\xfbone" * 40), ("02.mp3", b"\xff\xfbtwo" * 40)])
        client.download_calls.append(art_id)
        return dest

    client.download_file = download_zip_with_mp3
    activity.download_files(client, dest_root=tmp_path)
    assert wait_until_idle()["result"] == "done"
    tracks = sorted(p.name for p in (tmp_path / "An Audiobook").iterdir())
    assert tracks == ["01.mp3", "02.mp3"], "the bundle must land unpacked"

    client.download_calls.clear()
    activity.download_files(client, dest_root=tmp_path)
    assert [e["status"] for e in wait_until_idle()["log"]] == ["exists"]
    assert client.download_calls == []


def test_a_real_download_lights_up_the_badge_and_feeds_zip_reuse(tmp_path, monkeypatch):
    """The three consumers must agree about one folder.

    They were implemented separately and drifted: the mirror moved to the
    recorded-size index while the badge scan and the zip reuse still compared
    against the catalogue, so a book the mirror called "exists" showed no badge
    and was downloaded again by the very next zip build. Asking all three after
    one real run is what catches that."""
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "Book One", "is_audio": False}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    client = _make_client(_book(1, "Book One", TEXT_FILES))

    activity.download_files(client, dest_root=tmp_path)
    assert wait_until_idle()["result"] == "done"

    # 1. the mirror says it has it
    activity.forget_books_on_disk()
    assert activity.books_on_disk(tmp_path) == [1], "the badge must reflect a real download"

    # 2. and a zip build packs that copy rather than fetching it again
    client.download_calls.clear()
    activity.prepare(client, mirror_root=tmp_path)
    zipped = wait_until_idle()
    assert [e["status"] for e in zipped["log"]] == ["reused"]
    assert client.download_calls == [], "the zip must reuse the file the mirror wrote"


def test_the_fake_does_not_deliver_the_size_the_listing_declares(tmp_path):
    """Guards the fake itself.

    Restoring exact-size delivery would make every test above pass again while
    the product broke in the field, because litres.ru's declared size is not
    the length of what it serves. If this assertion ever needs "fixing", the
    thing to fix is the code that depends on it."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.download_files(client, dest_root=tmp_path)
    wait_until_idle()

    delivered = (tmp_path / "Book One.epub").stat().st_size
    assert delivered != TEXT_FILES[0]["size"]
    assert read_mirror_index(tmp_path)["1"]["size"] == delivered


# -- a new run supersedes the last one's results ----------------------------
# `results` is durable so the size-check that fires on every page load can't
# wipe a finished build's view. But a run that produces its OWN results must
# clear it as it starts, or the previous build's per-book list sits under a
# fresh progress bar looking like it belongs to the run in progress.


def _seed_finished_results():
    with activity.state._lock:
        activity.state._state["results"] = [{"title": "From last time", "status": "done"}]


def test_starting_a_file_download_clears_the_previous_results(tmp_path):
    _seed_finished_results()
    client = _make_client(_book(1, "Book One", TEXT_FILES))

    assert activity.download_files(client, dest_root=tmp_path) is True
    # Observed immediately, not after the run: the stale view is what the user
    # would otherwise stare at for the whole duration.
    assert activity.snapshot()["results"] == []
    wait_until_idle()


def test_starting_a_zip_build_clears_the_previous_results():
    _seed_finished_results()
    client = _make_client(_book(1, "Book One", TEXT_FILES))

    assert activity.prepare(client) is True
    assert activity.snapshot()["results"] == []
    wait_until_idle()


def test_a_size_check_does_NOT_clear_the_results(monkeypatch):
    """The reason `results` is durable at all -- this must keep working."""
    monkeypatch.setattr(cache, "get_library", list)
    _seed_finished_results()
    client = _make_client()

    activity.check_sizes(client, live=False)
    wait_until_idle()
    assert [e["title"] for e in activity.snapshot()["results"]] == ["From last time"]


def test_a_refresh_does_NOT_clear_the_results():
    _seed_finished_results()
    client = _make_client(_book(1, "Book One", TEXT_FILES))

    activity.refresh(client)
    wait_until_idle()
    assert [e["title"] for e in activity.snapshot()["results"]] == ["From last time"]


def test_a_new_zip_build_drops_the_previous_download_link():
    """The old archive may have lived in a workdir this build is about to
    delete, so its link would dangle."""
    with activity.state._lock:
        activity.state._state.update(zip_path="/tmp/old.zip", saved_path="/tmp/old.zip")
    client = _make_client(_book(1, "Book One", TEXT_FILES))

    assert activity.prepare(client) is True
    snap = activity.snapshot()
    assert snap["zip_path"] is None and snap["saved_path"] is None
    wait_until_idle()


def test_a_file_download_leaves_an_earlier_zip_reachable(tmp_path):
    """Different artefact: the zip is still on disk and still the user's, so
    starting a files run must not take the download link away."""
    with activity.state._lock:
        activity.state._state["zip_path"] = "/tmp/keep.zip"
    client = _make_client(_book(1, "Book One", TEXT_FILES))

    activity.download_files(client, dest_root=tmp_path)
    assert activity.snapshot()["zip_path"] == "/tmp/keep.zip"
    wait_until_idle()


# -- the live log shows work, the finished report shows everything -----------
# A skipped/reused book costs no transfer, so it resolves within milliseconds
# of the run starting. Streamed into the live log, a mostly-downloaded library
# filled the panel with hundreds of rows before the first byte moved -- which
# reads as the previous run's report and buries the books actually being
# fetched. They are held back and merged in when the run ends.


def test_the_live_log_stays_empty_while_only_skips_are_resolved(tmp_path):
    client = _make_client(_book(1, "Have", TEXT_FILES), _book(2, "Also have", TEXT_FILES))
    for n, name in ((1, "Have"), (2, "Also have")):
        (tmp_path / f"{name}.epub").write_bytes(b"x" * 999_936)
        record_in_mirror_index(tmp_path, n, f"{name}.epub", 999_936)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    # Progress still advanced while they were being decided...
    assert result["done"] == 2
    # ...and the finished report is complete.
    assert [e["status"] for e in result["results"]] == ["exists", "exists"]
    assert "2 already saved" in result["message"]


def test_transfers_appear_in_the_live_log_before_the_skips_do(tmp_path):
    """Ordering is the point: what is happening now comes first, and the
    already-there books are appended once the run is over."""
    client = _make_client(_book(1, "Have", TEXT_FILES), _book(2, "Need", TEXT_FILES))
    (tmp_path / "Have.epub").write_bytes(b"x" * 999_936)
    record_in_mirror_index(tmp_path, 1, "Have.epub", 999_936)

    seen_live = []

    def watch(art_id, release_file_id, filename, dest, subscr=False,
              should_cancel=None, on_progress=None):
        # Mid-run: the skip for book 1 has already been decided.
        seen_live.append([e["status"] for e in activity.snapshot()["log"]])
        dest.write_bytes(b"FAKEDATA" * 10)
        client.download_calls.append(art_id)
        return dest

    client.download_file = watch
    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert seen_live == [[]], "the panel must not fill with skips before work starts"
    assert [e["status"] for e in result["results"]] == ["done", "exists"]


def test_a_zip_build_holds_back_reused_rows_too(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "Book One", "is_audio": False}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "Book One.epub").write_bytes(b"y" * 999_936)

    activity.prepare(client, mirror_root=mirror)
    result = wait_until_idle()

    assert [e["status"] for e in result["results"]] == ["reused"]
    assert result["done"] == 1


# -- whole-build byte progress ---------------------------------------------
# `bytes_total` is the sum of every SELECTED book, on disk or not. So any book
# the run completes without transferring -- skipped in the mirror, reused by
# the zip -- must still be credited, or it raises the denominator and never
# the numerator. Re-running a finished library reported "~0.0 MB of ~600.0 MB"
# while completing instantly: the readout drifted further behind the more work
# was saved, i.e. worst exactly when the feature worked best.


def test_a_book_already_on_disk_counts_toward_the_byte_progress(tmp_path):
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    (tmp_path / "Book One.epub").write_bytes(b"x" * 999_936)
    record_in_mirror_index(tmp_path, 1, "Book One.epub", 999_936)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["exists"]
    assert client.download_calls == []
    # Credited at the LISTED size -- what the denominator counted for it, not
    # its (different) size on disk, or the bar could never reach its own total.
    assert result["bytes_done"] == TEXT_FILES[0]["size"]


def test_byte_progress_reaches_the_total_on_a_fully_cached_library(tmp_path, monkeypatch):
    """The reported case: everything already saved. The bar must read ~full,
    not zero."""
    books = [{"id": 1, "title": "One", "is_audio": False},
             {"id": 2, "title": "Two", "is_audio": False}]
    monkeypatch.setattr(cache, "get_library", lambda: books)
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    client = _make_client(_book(1, "One", TEXT_FILES), _book(2, "Two", TEXT_FILES))
    for n, name in ((1, "One"), (2, "Two")):
        (tmp_path / f"{name}.epub").write_bytes(b"x" * 999_936)
        record_in_mirror_index(tmp_path, n, f"{name}.epub", 999_936)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["exists", "exists"]
    assert result["bytes_total"] == 2_000_000
    assert result["bytes_done"] == result["bytes_total"], "a no-op run must read 100%"


def test_byte_progress_counts_downloaded_and_skipped_books_together(tmp_path, monkeypatch):
    """Mixed run: one book on disk, one fetched. Both contribute, so the
    readout lands close to the total rather than only counting the transfer."""
    books = [{"id": 1, "title": "Have", "is_audio": False},
             {"id": 2, "title": "Need", "is_audio": False}]
    monkeypatch.setattr(cache, "get_library", lambda: books)
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    client = _make_client(_book(1, "Have", TEXT_FILES), _book(2, "Need", TEXT_FILES))
    (tmp_path / "Have.epub").write_bytes(b"x" * 999_936)
    record_in_mirror_index(tmp_path, 1, "Have.epub", 999_936)

    activity.download_files(client, dest_root=tmp_path)
    result = wait_until_idle()

    assert sorted(e["status"] for e in result["log"]) == ["done", "exists"]
    assert client.download_calls == [2], "only the missing book is fetched"
    # The skipped book alone would have been ignored before this; now the
    # figure covers both and sits within a percent of the estimated total.
    assert result["bytes_done"] > TEXT_FILES[0]["size"]
    assert abs(result["bytes_done"] - result["bytes_total"]) < result["bytes_total"] * 0.01


def test_a_zip_build_credits_bytes_it_reused_instead_of_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "Book One", "is_audio": False}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "Book One.epub").write_bytes(b"y" * 999_936)

    activity.prepare(client, mirror_root=mirror)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["reused"]
    assert client.download_calls == []
    assert result["bytes_done"] == TEXT_FILES[0]["size"], "reused bytes still count"


# -- reusing already-downloaded files when building a zip -------------------
# Requests to litres.ru are the scarce resource, so a book already on disk
# should be packed, not fetched. The risk is packing a BROKEN local file.


def test_prepare_packs_a_local_copy_instead_of_downloading(tmp_path):
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "Book One.epub").write_bytes(b"y" * 1_000_000)

    activity.prepare(client, mirror_root=mirror)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["reused"]
    assert client.download_calls == [], "a local copy must not be re-fetched"
    with zipfile.ZipFile(result["zip_path"]) as zf:
        assert zf.read("Book One.epub") == b"y" * 1_000_000


def test_prepare_ignores_a_local_copy_of_the_wrong_size(tmp_path):
    """A half-written file must never be packed as though it were the book --
    that would put corruption inside an archive the user trusts."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "Book One.epub").write_bytes(b"truncated")
    # A previous run finished this file at 1 MB; 9 bytes are there now.
    record_in_mirror_index(mirror, 1, "Book One.epub", 1_000_000)

    activity.prepare(client, mirror_root=mirror)
    result = wait_until_idle()

    assert [e["status"] for e in result["log"]] == ["done"]
    assert client.download_calls == [1], "a partial local file must be re-fetched"


def test_prepare_without_a_mirror_behaves_exactly_as_before(tmp_path):
    """mirror_root=None is the old behaviour, unchanged."""
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.prepare(client, mirror_root=None)
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["done"]
    assert client.download_calls == [1]


def test_prepare_reuses_only_the_books_that_are_present(tmp_path):
    """The realistic mix: some downloaded earlier, some not."""
    client = _make_client(_book(1, "Have", TEXT_FILES), _book(2, "Missing", TEXT_FILES))
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "Have.epub").write_bytes(b"z" * 1_000_000)

    activity.prepare(client, mirror_root=mirror)
    result = wait_until_idle()

    by_status = {e["title"]: e["status"] for e in result["log"]}
    assert by_status == {"Have": "reused", "Missing": "done"}
    assert client.download_calls == [2]


def test_a_mirror_folder_that_does_not_exist_is_simply_ignored(tmp_path):
    client = _make_client(_book(1, "Book One", TEXT_FILES))
    activity.prepare(client, mirror_root=tmp_path / "never-created")
    result = wait_until_idle()
    assert [e["status"] for e in result["log"]] == ["done"]


# -- the "already on disk" badge --------------------------------------------


def test_books_on_disk_lists_only_complete_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "get_library", lambda: [
        {"id": 1, "title": "Complete", "is_audio": False},
        {"id": 2, "title": "Partial", "is_audio": False},
        {"id": 3, "title": "Absent", "is_audio": False},
    ])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    (tmp_path / "Complete.epub").write_bytes(b"c" * 1_000_000)
    record_in_mirror_index(tmp_path, 1, "Complete.epub", 1_000_000)
    (tmp_path / "Partial.epub").write_bytes(b"c" * 10)
    record_in_mirror_index(tmp_path, 2, "Partial.epub", 1_000_000)

    assert activity.books_on_disk(tmp_path) == [1]


def test_books_on_disk_is_empty_when_there_is_no_folder():
    assert activity.books_on_disk(None) == []
    assert activity.books_on_disk(pathlib.Path("/definitely/not/here")) == []


def test_books_on_disk_skips_books_whose_listing_was_never_cached(tmp_path, monkeypatch):
    """No cached listing means no size to compare -- so no badge, rather than
    a badge based on a filename alone."""
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "Book One", "is_audio": False}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: None)
    (tmp_path / "Book One.epub").write_bytes(b"x" * 1_000_000)
    assert activity.books_on_disk(tmp_path) == []


def test_books_on_disk_uses_the_same_decollided_names_a_run_would(tmp_path, monkeypatch):
    """Two identical titles: the second is written as "Same (2).epub", so the
    badge has to look under that name or it would never light up."""
    monkeypatch.setattr(cache, "get_library", lambda: [
        {"id": 1, "title": "Same", "is_audio": False},
        {"id": 2, "title": "Same", "is_audio": False},
    ])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    (tmp_path / "Same (2).epub").write_bytes(b"x" * 1_000_000)
    assert activity.books_on_disk(tmp_path) == [2]


# -- audiobook completeness --------------------------------------------------
# An audiobook is stored unpacked, so no single file has a length to check.
# The index records how many tracks the finished extract wrote; that count,
# re-counted on disk, is the equivalent evidence.


def _audio_on_disk(root, art_id, name, is_audio=True):
    return activity.mirror._is_on_disk(root, read_mirror_index(root), art_id, name, "m4b", is_audio)


def test_an_audiobook_folder_with_no_record_is_trusted(tmp_path):
    """Deliberately the same rule as for a single file: no record means there
    is nothing to verify against, not that the folder is damaged.

    This used to assert the opposite, reasoning that a folder proves nothing
    about an extract that died on track 3 of 40. True, but it made every
    audiobook that predates the index -- i.e. all of them, on upgrade -- report
    as missing and queue a full re-download of the library. Re-fetching tens of
    gigabytes because we lack a *history* is far worse than the rare stale
    folder, and it is exactly the request pattern that gets an account
    flagged. A partial extract is still caught whenever we do have a record."""
    book = tmp_path / "Audiobook"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"partial")
    assert _audio_on_disk(tmp_path, 1, "Audiobook") is True

    # An empty folder is still not a book -- there is nothing there at all.
    empty = tmp_path / "Empty"
    empty.mkdir()
    assert _audio_on_disk(tmp_path, 2, "Empty") is False


def test_an_audiobook_matching_its_recorded_track_count_is_complete(tmp_path):
    book = tmp_path / "Audiobook"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"track")
    record_in_mirror_index(tmp_path, 1, "Audiobook", 0, tracks=1)
    assert _audio_on_disk(tmp_path, 1, "Audiobook") is True


def test_an_audiobook_missing_a_track_is_not_complete(tmp_path):
    """Delete one track of forty and the folder still looks like the book.
    The recorded count is what notices."""
    book = tmp_path / "Audiobook"
    book.mkdir()
    for n in range(3):
        (book / f"0{n}.mp3").write_bytes(b"track")
    record_in_mirror_index(tmp_path, 1, "Audiobook", 0, tracks=3)
    assert _audio_on_disk(tmp_path, 1, "Audiobook") is True

    (book / "01.mp3").unlink()
    assert _audio_on_disk(tmp_path, 1, "Audiobook") is False


def test_an_audiobook_that_gained_a_track_is_not_complete(tmp_path):
    """A re-issue with an extra track, or a stray file dropped in: the folder
    is no longer what we wrote, so it is rebuilt rather than believed."""
    book = tmp_path / "Audiobook"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"track")
    record_in_mirror_index(tmp_path, 1, "Audiobook", 0, tracks=1)
    (book / "02.mp3").write_bytes(b"track")
    assert _audio_on_disk(tmp_path, 1, "Audiobook") is False


def test_a_record_with_no_track_count_is_not_trusted(tmp_path):
    """An ebook-shaped record (size, no tracks) against a folder can't prove
    anything, so it must not read as complete."""
    book = tmp_path / "Audiobook"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"track")
    record_in_mirror_index(tmp_path, 1, "Audiobook", 5_000_000)
    assert _audio_on_disk(tmp_path, 1, "Audiobook") is False


def test_bookkeeping_files_are_not_counted_as_tracks(tmp_path):
    """The index itself, and any `.part` staging file, sit alongside the
    media; counting them would make a complete folder look over-full."""
    book = tmp_path / "Audiobook"
    book.mkdir()
    (book / "01.mp3").write_bytes(b"track")
    (book / ".bookvault-index.json").write_text("{}")
    (book / ".02.mp3.part").write_bytes(b"half")
    assert activity.mirror._audio_media_count(book) == 1


# -- the badge scan must not make the app unresponsive ----------------------
# It runs on a route the browser polls once a second, over the whole library,
# taking the cache lock per book -- while a running download holds that same
# lock after every transfer. Uncached, the polls queue up and saturate the
# threadpool, and then Stop can't get a thread either.


def test_the_on_disk_scan_is_memoised_between_polls(tmp_path, monkeypatch):
    scans = []
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "One", "is_audio": False}])

    def counting_get_files(art_id):
        scans.append(art_id)
        return TEXT_FILES

    monkeypatch.setattr(cache, "get_files", counting_get_files)
    (tmp_path / "One.epub").write_bytes(b"x" * 1_000_000)

    first = activity.books_on_disk(tmp_path)
    for _ in range(20):           # what a browser does in 20 seconds
        activity.books_on_disk(tmp_path)

    assert first == [1]
    assert len(scans) == 1, f"the folder was rescanned {len(scans)} times for 21 polls"


def test_a_finished_download_drops_the_memo_so_badges_appear(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "One", "is_audio": False}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)

    assert activity.books_on_disk(tmp_path) == []      # nothing there yet, memoised
    (tmp_path / "One.epub").write_bytes(b"x" * 1_000_000)
    assert activity.books_on_disk(tmp_path) == [], "still memoised, as designed"

    activity.forget_books_on_disk()                     # what a finished run does
    assert activity.books_on_disk(tmp_path) == [1]


def test_changing_the_folder_bypasses_the_memo(tmp_path, monkeypatch):
    """A different destination must never be answered from another one's scan."""
    monkeypatch.setattr(cache, "get_library", lambda: [{"id": 1, "title": "One", "is_audio": False}])
    monkeypatch.setattr(cache, "get_files", lambda art_id: TEXT_FILES)
    other = tmp_path / "other"
    other.mkdir()
    (other / "One.epub").write_bytes(b"x" * 1_000_000)

    assert activity.books_on_disk(tmp_path) == []
    assert activity.books_on_disk(other) == [1]


def test_stop_is_answerable_while_a_download_is_running(tmp_path):
    """The regression this guards: Stop has to be reachable *during* a run.
    cancel() must not depend on anything the run holds."""
    client = _make_client(_book(1, "One", TEXT_FILES), _book(2, "Two", TEXT_FILES))
    started = threading.Event()
    release = threading.Event()
    original = client.download_file

    def slow(art_id, release_file_id, filename, dest, subscr=False, should_cancel=None, on_progress=None):
        started.set()
        release.wait(timeout=5)
        return original(art_id, release_file_id, filename, dest, subscr)

    client.download_file = slow
    activity.download_files(client, dest_root=tmp_path)
    assert started.wait(timeout=5), "the run never started"

    # Mid-transfer, exactly when a user reaches for Stop.
    assert activity.cancel() is True
    release.set()
    assert wait_until_idle(timeout=10)["result"] == "cancelled"
