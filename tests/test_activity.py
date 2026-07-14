"""Tests for litres_web/activity.py -- the single backend state machine.

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

from litres_core import cache
from litres_core.client import DownloadCancelled
from litres_web import activity
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

    def recording(art_id):
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

    def flaky(art_id):
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

    def fetch_then_cancel_after_first(art_id):
        files = original(art_id)
        if art_id == 1:
            activity.cancel()
        return files

    client.get_files = fetch_then_cancel_after_first
    calls = []
    inner = client.get_files
    client.get_files = lambda art_id: (calls.append(art_id), inner(art_id))[1]

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

    def blocking_get_files(art_id):
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
    activity._state["state"] = activity.REFRESHING
    try:
        assert activity.cancel() is False
    finally:
        activity._state["state"] = activity.IDLE


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
    assert "anti-bot" in activity._friendly_error(RuntimeError("Download failed for art 1 (403): DDoS-Guard"))


def test_friendly_error_recognizes_stale_client_after_relogin():
    msg = activity._friendly_error(RuntimeError("Event loop is closed! Is Playwright already stopped?"))
    assert "session changed" in msg.lower()


def test_friendly_error_recognizes_dropped_connection():
    msg = activity._friendly_error(RuntimeError("APIRequestContext.get: socket hang up"))
    assert "interrupted" in msg.lower()


def test_friendly_error_falls_back_to_raw_text_for_unrecognized_errors():
    assert "something truly unexpected" in activity._friendly_error(RuntimeError("something truly unexpected"))
