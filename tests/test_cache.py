"""Tests for litres_core/cache.py: TTL expiry, disk persistence across the
in-memory singleton being dropped, and clear(). CACHE_PATH is redirected to
a tmp_path by the autouse isolated_module_state fixture in conftest.py."""
from __future__ import annotations

from litres_core import cache


def test_get_library_is_none_when_nothing_cached():
    assert cache.get_library() is None


def test_set_then_get_library_round_trips():
    books = [{"id": 1, "title": "Book One"}]
    cache.set_library(books)
    assert cache.get_library() == books


def test_get_library_is_none_once_ttl_expires(monkeypatch):
    monkeypatch.setattr(cache, "LIBRARY_TTL", 0)
    cache.set_library([{"id": 1, "title": "Book One"}])
    assert cache.get_library() is None


def test_get_library_stale_returns_data_past_ttl(monkeypatch):
    # get_library_stale ignores TTL freshness -- it's what lets the /library
    # route serve a usable list instead of blocking on the busy worker thread.
    monkeypatch.setattr(cache, "LIBRARY_TTL", 0)
    books = [{"id": 1, "title": "Book One"}]
    cache.set_library(books)
    assert cache.get_library() is None      # TTL-expired: fresh accessor says None
    assert cache.get_library_stale() == books  # but the stale accessor still has it


def test_get_library_stale_is_none_when_nothing_cached():
    assert cache.get_library_stale() is None


def test_get_files_is_none_when_nothing_cached():
    assert cache.get_files(1) is None


def test_set_then_get_files_round_trips():
    files = [{"id": 100, "extension": "epub", "size": 12345}]
    cache.set_files(1, files)
    assert cache.get_files(1) == files


def test_files_are_cached_independently_per_art_id():
    cache.set_files(1, [{"id": 100}])
    cache.set_files(2, [{"id": 200}])
    assert cache.get_files(1) == [{"id": 100}]
    assert cache.get_files(2) == [{"id": 200}]


def test_get_files_is_none_once_ttl_expires(monkeypatch):
    monkeypatch.setattr(cache, "FILES_TTL", 0)
    cache.set_files(1, [{"id": 100}])
    assert cache.get_files(1) is None


def test_cache_survives_the_in_memory_singleton_being_dropped():
    # Simulates a fresh process reading back what a previous one wrote --
    # the whole point of this being disk-backed, not just an in-memory dict.
    cache.set_library([{"id": 1, "title": "Book One"}])
    cache.set_files(1, [{"id": 100}])
    cache._state = None  # force the next call to reload from disk

    assert cache.get_library() == [{"id": 1, "title": "Book One"}]
    assert cache.get_files(1) == [{"id": 100}]


def test_clear_drops_both_library_and_files_and_removes_the_file():
    cache.set_library([{"id": 1, "title": "Book One"}])
    cache.set_files(1, [{"id": 100}])

    cache.clear()

    assert cache.get_library() is None
    assert cache.get_files(1) is None
    assert not cache.CACHE_PATH.exists()


def test_corrupt_cache_file_is_ignored_not_fatal(monkeypatch):
    cache.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache.CACHE_PATH.write_text("not valid json{{{")
    cache._state = None

    assert cache.get_library() is None  # must not raise
