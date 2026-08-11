"""Tests for bookvault_core.library_fs (ABS on-disk layout)."""
from __future__ import annotations

import json
import zipfile

import pytest
from bookvault_core.library_fs import (
    abs_metadata,
    book_dir,
    extract_audio_zip,
    install_book,
    is_up_to_date,
    read_metadata,
    sanitize_component,
    write_metadata,
)


def test_sanitize_replaces_invalid_chars():
    assert sanitize_component('A<B>:C"/D\\|E?F*G') == "A_B__C__D__E_F_G"


def test_book_dir_uses_first_author_and_title(tmp_path):
    meta = {"authors": ["Лев Толстой"], "title": "Война и мир", "id": 1}
    p = book_dir(tmp_path, meta)
    assert p == tmp_path / "Лев Толстой" / "Война и мир"


def test_book_dir_unknown_author(tmp_path):
    p = book_dir(tmp_path, {"authors": [], "title": "X", "id": 9})
    assert p.parts[-2] == "Unknown"


def test_abs_metadata_series_and_rus_language():
    item = {
        "id": 42,
        "title": "T",
        "authors": ["A"],
        "narrators": ["N"],
        "language_code": "rus",
        "series_list": [{"name": "Saga", "art_order": 2}],
        "last_released_at": "2024-01-02T00:00:00",
        "is_adult_content": False,
        "genres": ["Fantasy"],
        "isbn": "978-1",
        "description": "Hello",
    }
    m = abs_metadata(item)
    assert m["language"] == "Russian"
    assert m["series"] == ["Saga #2"]
    assert m["last_release"] == "2024-01-02T00:00:00"
    assert m["authors"] == ["A"]
    assert m["narrators"] == ["N"]
    assert m["genres"] == ["Fantasy"]
    assert m["isbn"] == "978-1"
    assert m["_art_id"] == 42


def test_is_up_to_date_requires_media_file(tmp_path):
    meta = abs_metadata(
        {
            "authors": ["A"],
            "title": "T",
            "last_released_at": "v1",
            "id": 1,
        }
    )
    d = book_dir(tmp_path, meta)
    d.mkdir(parents=True)
    write_metadata(d, meta)
    assert is_up_to_date(d, "v1") is False
    (d / "a.mp3").write_bytes(b"x")
    assert is_up_to_date(d, "v1") is True
    assert is_up_to_date(d, "v2") is False


def test_extract_audio_zip_and_blocks_slip(tmp_path):
    zpath = tmp_path / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("folder/01.mp3", b"one")
        zf.writestr("folder/02.mp3", b"two")
    out = tmp_path / "dest"
    files = extract_audio_zip(zpath, out)
    names = sorted(p.name for p in files)
    assert names == ["01.mp3", "02.mp3"]

    # Traversal members are reduced to basenames (never leave dest).
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.mp3", b"nope")
    written = extract_audio_zip(bad, out)
    assert written[0].name == "evil.mp3"
    assert written[0].parent == out.resolve()
    assert not (tmp_path / "evil.mp3").exists()


def test_extract_rejects_empty_basename(tmp_path):
    bad = tmp_path / "empty.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        # Directory-only / empty name shouldn't produce files; crafted oddity:
        zf.writestr(".", b"x")
    with pytest.raises(ValueError):
        extract_audio_zip(bad, tmp_path / "d")


def test_install_book_extracts_audio_zip(tmp_path):
    zpath = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("t/01.mp3", b"aaa")
        zf.writestr("t/02.mp3", b"bbb")
    meta = abs_metadata(
        {
            "id": 7,
            "title": "Audio Book",
            "authors": ["Narrator Author"],
            "last_released_at": "2024-06-01",
            "is_audio": True,
        }
    )
    dest = install_book(tmp_path / "lib", meta, zpath, is_audio=True)
    assert (dest / "01.mp3").read_bytes() == b"aaa"
    assert (dest / "02.mp3").read_bytes() == b"bbb"
    disk = read_metadata(dest)
    assert disk["title"] == "Audio Book"
    assert disk["last_release"] == "2024-06-01"
    assert not zpath.exists()  # temp cleaned


def test_install_book_single_file(tmp_path):
    src = tmp_path / "9.m4b"
    src.write_bytes(b"m4bdata")
    meta = abs_metadata(
        {
            "id": 9,
            "title": "Single",
            "authors": ["A"],
            "last_released_at": "r1",
        }
    )
    dest = install_book(tmp_path / "lib", meta, src, is_audio=True)
    assert (dest / "9.m4b").read_bytes() == b"m4bdata"
    assert json.loads((dest / "metadata.json").read_text())["last_release"] == "r1"


def test_sanitize_component_never_yields_a_traversal():
    """A component of only dots is path traversal, not a name -- it degrades to
    "_" so a crafted author/title can't walk out of the library root."""
    assert sanitize_component("..") == "_"
    assert sanitize_component(".") == "_"
    assert sanitize_component("...") == "_"
    assert sanitize_component("  ..  ") == "_"
    # A legitimate title that merely contains a dot is untouched.
    assert sanitize_component("Vol. 1") == "Vol. 1"
    assert sanitize_component("Book.epub") == "Book.epub"
