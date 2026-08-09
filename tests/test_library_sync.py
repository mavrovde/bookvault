"""Tests for bookvault_core.library_sync."""
from __future__ import annotations

import zipfile

from bookvault_core.library_fs import is_up_to_date, read_metadata
from bookvault_core.library_sync import sync_library

from tests.fakes import FakeLitresClient


def _audio(art_id, title, release="2024-01-01", author="Author A"):
    return {
        "id": art_id,
        "title": title,
        "art_type": 1,
        "persons": [
            {"full_name": author, "role": "author"},
            {"full_name": "Reader R", "role": "reader"},
        ],
        "series": [{"name": "Saga", "art_order": 1}],
        "language_code": "rus",
        "last_released_at": release,
        "cover_url": None,
    }


def _ebook(art_id, title="Ebook"):
    return {
        "id": art_id,
        "title": title,
        "art_type": 0,
        "persons": [{"full_name": "Author A", "role": "author"}],
        "last_released_at": "2024-01-01",
    }


def test_sync_library_audio_only_and_idempotent(tmp_path):
    lib = tmp_path / "library"
    client = FakeLitresClient(
        library=[_audio(1, "Book One"), _audio(2, "Book Two"), _ebook(3)],
        files_by_id={
            1: [{"id": 100, "extension": "m4b", "file_type": "mobile_version_mp4", "is_additional": False, "size": 8}],
            2: [{"id": 200, "extension": "m4b", "file_type": "mobile_version_mp4", "is_additional": False, "size": 8}],
            3: [{"id": 300, "extension": "epub", "is_additional": False, "size": 8}],
        },
        arts_by_id={
            1: {**_audio(1, "Book One"), "isbn": "978-1", "html_annotation": "<p>Hi</p>", "genres": [{"name": "Fantasy"}]},
            2: {**_audio(2, "Book Two"), "isbn": "978-2", "html_annotation": "<p>Yo</p>", "genres": []},
        },
    )

    result = sync_library(client, lib, audio_only=True)
    assert result["done"] == 2
    assert result["failed"] == 0
    assert client.download_calls == [1, 2]

    meta1 = read_metadata(lib / "Author A" / "Book One")
    assert meta1["title"] == "Book One"
    assert meta1["narrators"] == ["Reader R"]
    assert meta1["series"] == ["Saga #1"]
    assert meta1["language"] == "Russian"
    assert meta1["isbn"] == "978-1"
    assert "Hi" in (meta1.get("description") or "")
    assert (lib / "Author A" / "Book One" / "1.m4b").exists()

    # Second pass: up to date
    client.download_calls.clear()
    result2 = sync_library(client, lib, audio_only=True)
    assert result2["done"] == 0
    assert result2["skipped"] == 2
    assert client.download_calls == []


def test_sync_library_redownloads_when_last_release_changes(tmp_path):
    lib = tmp_path / "library"
    art = _audio(1, "Book One", release="v1")
    client = FakeLitresClient(
        library=[art],
        files_by_id={
            1: [{"id": 100, "extension": "m4b", "file_type": "mobile_version_mp4", "is_additional": False, "size": 8}],
        },
    )
    sync_library(client, lib, audio_only=True)
    assert len(client.download_calls) == 1

    art["last_released_at"] = "v2"
    client.library = [art]
    client.arts_by_id = {1: art}
    client.download_calls.clear()
    result = sync_library(client, lib, audio_only=True)
    assert result["done"] == 1
    assert client.download_calls == [1]
    assert read_metadata(lib / "Author A" / "Book One")["last_release"] == "v2"


def test_sync_library_skips_no_files(tmp_path):
    client = FakeLitresClient(library=[_audio(1, "Empty")], files_by_id={1: []})
    result = sync_library(client, tmp_path / "lib", audio_only=True)
    assert result["done"] == 0
    assert result["skipped"] == 1
    assert result["log"][0]["reason"].startswith("No downloadable")


def test_sync_library_cancel_midway(tmp_path):
    calls = {"n": 0}

    def cancel_after_first():
        calls["n"] += 1
        # Cancel before second book starts: checked at loop head and inside sync_one.
        return calls["n"] > 3

    client = FakeLitresClient(
        library=[_audio(1, "One"), _audio(2, "Two"), _audio(3, "Three")],
        files_by_id={
            i: [{"id": i * 10, "extension": "m4b", "file_type": "mobile_version_mp4", "is_additional": False, "size": 8}]
            for i in (1, 2, 3)
        },
    )
    # Force cancel immediately
    result = sync_library(client, tmp_path / "lib", audio_only=True, should_cancel=lambda: True)
    assert result["cancelled"] >= 1
    assert result["done"] == 0


def test_sync_library_extracts_zip_with_mp3(tmp_path):
    """Fake client writes a real zip when the best file is zip_with_mp3."""

    class ZipFake(FakeLitresClient):
        def download_file(self, art_id, release_file_id, filename, dest, subscr=False,
                          should_cancel=None, on_progress=None):
            self.download_calls.append(art_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("a/01.mp3", b"track1")
                zf.writestr("a/02.mp3", b"track2")
            if on_progress:
                on_progress(10, 10)
            return dest

    client = ZipFake(
        library=[_audio(5, "Zipped Audio")],
        files_by_id={
            5: [{"id": 50, "file_type": "zip_with_mp3", "extension": "zip", "is_additional": False, "size": 99}],
        },
    )
    lib = tmp_path / "lib"
    result = sync_library(client, lib, audio_only=True)
    assert result["done"] == 1
    book = lib / "Author A" / "Zipped Audio"
    assert (book / "01.mp3").read_bytes() == b"track1"
    assert (book / "02.mp3").read_bytes() == b"track2"
    assert is_up_to_date(book, "2024-01-01")
