"""Tests for litres_core/client.py: the pure format-picking logic, and the
HTTP-handling logic (pagination, error handling, header merging) exercised
against a fake Playwright request context instead of the network."""
from __future__ import annotations

import httpx
import pytest

from litres_core.client import DownloadCancelled, LitresAuthError, LitresClient
from tests.fakes import FakeAPIResponse, make_bare_client

# --------------------------------------------------------------------------
# pick_best_file / file_extension -- pure logic, no I/O at all.
# --------------------------------------------------------------------------


def test_pick_best_file_no_files_returns_none():
    assert LitresClient.pick_best_file(None, []) is None


def test_pick_best_file_prefers_epub_by_default():
    files = [
        {"id": 1, "extension": "txt", "is_additional": False},
        {"id": 2, "extension": "epub", "is_additional": False},
        {"id": 3, "extension": "fb2.zip", "is_additional": False},
    ]
    best = LitresClient.pick_best_file(None, files)
    assert best["id"] == 2


def test_pick_best_file_respects_preferred_ext_when_available():
    files = [
        {"id": 1, "extension": "epub", "is_additional": False},
        {"id": 2, "extension": "a4.pdf", "is_additional": False},
    ]
    best = LitresClient.pick_best_file(None, files, preferred_ext="a4.pdf")
    assert best["id"] == 2


def test_pick_best_file_falls_back_when_preferred_ext_unavailable():
    files = [
        {"id": 1, "extension": "epub", "is_additional": False},
        {"id": 2, "extension": "txt", "is_additional": False},
    ]
    # preferred "mobi.prc" isn't available for this book -- falls back to
    # the built-in order, which picks epub.
    best = LitresClient.pick_best_file(None, files, preferred_ext="mobi.prc")
    assert best["id"] == 1


def test_pick_best_file_audiobook_prefers_whole_bundle_over_chapters():
    files = [
        {"id": 1, "file_type": "standard_quality_mp3", "is_additional": False},
        {"id": 2, "file_type": "standard_quality_mp3", "is_additional": False},
        {"id": 3, "file_type": "introductory_fragment_mp3", "is_additional": False},
        {"id": 4, "file_type": "zip_with_mp3", "is_additional": False},
        {"id": 5, "file_type": "mobile_version_mp4", "is_additional": False},
    ]
    best = LitresClient.pick_best_file(None, files)
    assert best["id"] == 4  # zip_with_mp3 beats mobile_version_mp4 in PREFERRED_FILE_TYPES order


def test_pick_best_file_respects_preferred_file_type():
    files = [
        {"id": 4, "file_type": "zip_with_mp3", "is_additional": False},
        {"id": 5, "file_type": "mobile_version_mp4", "is_additional": False},
    ]
    best = LitresClient.pick_best_file(None, files, preferred_file_type="mobile_version_mp4")
    assert best["id"] == 5


def test_pick_best_file_excludes_additional_samples():
    files = [
        {"id": 1, "extension": "epub", "is_additional": True},
        {"id": 2, "extension": "txt", "is_additional": False},
    ]
    best = LitresClient.pick_best_file(None, files)
    assert best["id"] == 2  # the non-additional txt wins even though epub ranks higher


def test_pick_best_file_falls_back_to_additional_if_thats_all_there_is():
    files = [{"id": 1, "extension": "epub", "is_additional": True}]
    best = LitresClient.pick_best_file(None, files)
    assert best["id"] == 1


def test_pick_best_file_falls_back_to_first_candidate_when_format_unrecognized():
    files = [{"id": 1, "extension": None, "file_type": "some_new_format", "is_additional": False}]
    best = LitresClient.pick_best_file(None, files)
    assert best["id"] == 1


def test_file_extension_uses_explicit_extension():
    assert LitresClient.file_extension({"extension": "epub", "file_type": "zip_with_mp3"}) == "epub"


def test_file_extension_falls_back_to_file_type_mapping():
    assert LitresClient.file_extension({"extension": None, "file_type": "zip_with_mp3"}) == "zip"
    assert LitresClient.file_extension({"extension": None, "file_type": "mobile_version_mp4"}) == "m4b"


def test_file_extension_falls_back_to_fb2_default():
    assert LitresClient.file_extension({"extension": None, "file_type": "unknown_thing"}) == "fb2"
    assert LitresClient.file_extension({}) == "fb2"


# --------------------------------------------------------------------------
# iter_library -- pagination and error handling against a fake HTTP layer.
# --------------------------------------------------------------------------


def _arts_response(items, next_page=None):
    return FakeAPIResponse(
        status=200,
        json_data={"payload": {"data": items, "pagination": {"next_page": next_page}}},
    )


def test_iter_library_single_page():
    def handler(url, params, headers, timeout):
        return _arts_response([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])

    client = make_bare_client(handler)
    assert list(client.iter_library()) == [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]


def test_iter_library_empty_library_yields_nothing():
    def handler(url, params, headers, timeout):
        return _arts_response([])

    client = make_bare_client(handler)
    assert list(client.iter_library()) == []


def test_iter_library_follows_pagination_cursor():
    calls = []

    def handler(url, params, headers, timeout):
        calls.append(dict(params or {}))
        if "after" not in (params or {}):
            return _arts_response(
                [{"id": 1}], next_page="/api/users/me/arts?limit=100&after=CURSOR123"
            )
        assert params["after"] == "CURSOR123"
        return _arts_response([{"id": 2}])  # no next_page -> stop

    client = make_bare_client(handler)
    items = list(client.iter_library())
    assert [i["id"] for i in items] == [1, 2]
    assert len(calls) == 2


def test_iter_library_raises_on_http_error():
    def handler(url, params, headers, timeout):
        return FakeAPIResponse(status=500, text_data="server error")

    client = make_bare_client(handler)
    with pytest.raises(LitresAuthError):
        list(client.iter_library())


# --------------------------------------------------------------------------
# get_files
# --------------------------------------------------------------------------


def test_get_files_flattens_groups_and_tags_file_type():
    def handler(url, params, headers, timeout):
        return FakeAPIResponse(
            status=200,
            json_data={
                "payload": {
                    "data": [
                        {"file_type": "unknown", "files": [{"id": 1, "extension": "epub"}]},
                        {"file_type": "zip_with_mp3", "files": [{"id": 2}, {"id": 3}]},
                    ]
                }
            },
        )

    client = make_bare_client(handler)
    files = client.get_files(12345)
    assert files == [
        {"id": 1, "extension": "epub", "file_type": "unknown"},
        {"id": 2, "file_type": "zip_with_mp3"},
        {"id": 3, "file_type": "zip_with_mp3"},
    ]


def test_get_files_raises_on_http_error():
    def handler(url, params, headers, timeout):
        return FakeAPIResponse(status=404, text_data="not found")

    client = make_bare_client(handler)
    with pytest.raises(LitresAuthError):
        client.get_files(12345)


# --------------------------------------------------------------------------
# is_logged_in
# --------------------------------------------------------------------------


def test_is_logged_in_false_without_captured_headers():
    client = make_bare_client(lambda *a: FakeAPIResponse(status=200), extra_headers={})
    assert client.is_logged_in() is False


def test_is_logged_in_true_when_users_me_succeeds():
    client = make_bare_client(lambda *a: FakeAPIResponse(status=200))
    assert client.is_logged_in() is True


def test_is_logged_in_false_when_users_me_fails():
    client = make_bare_client(lambda *a: FakeAPIResponse(status=403))
    assert client.is_logged_in() is False


# --------------------------------------------------------------------------
# download_file
# --------------------------------------------------------------------------


def test_download_file_streams_bytes_to_disk_on_success(tmp_path):
    # download_file streams over httpx -- drive it offline via a MockTransport.
    client = make_bare_client(lambda *a: None)
    client._httpx_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"hello world")
    )
    dest = tmp_path / "book.epub"
    result = client.download_file(1, 2, "book.epub", dest)
    assert result == dest
    assert dest.read_bytes() == b"hello world"


def test_download_file_streams_in_chunks_without_buffering_whole_file(tmp_path):
    # A larger body must arrive intact even though it's read in 1 MiB chunks.
    big = b"\xab" * (3 * 1024 * 1024 + 7)
    client = make_bare_client(lambda *a: None)
    client._httpx_transport = httpx.MockTransport(lambda request: httpx.Response(200, content=big))
    dest = tmp_path / "audiobook.zip"
    client.download_file(1, 2, "audiobook.zip", dest)
    assert dest.stat().st_size == len(big)
    assert dest.read_bytes() == big


def test_download_file_reports_progress_per_chunk(tmp_path):
    # on_progress must be called after every 1 MiB chunk with the cumulative
    # bytes written so far and the total from the response's Content-Length,
    # so the UI can show a live "written / total" MB readout.
    body = b"\xab" * (2 * 1024 * 1024 + 100)  # 3 chunks: 1 MiB, 1 MiB, 100 B
    client = make_bare_client(lambda *a: None)
    client._httpx_transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    calls = []
    client.download_file(1, 2, "book.epub", tmp_path / "book.epub",
                         on_progress=lambda written, total: calls.append((written, total)))
    assert [w for w, _ in calls] == [1024 * 1024, 2 * 1024 * 1024, len(body)]  # cumulative
    assert all(total == len(body) for _, total in calls)  # Content-Length total on every call


def test_download_file_reports_none_total_without_content_length(tmp_path):
    # A streamed response with no Content-Length must still report progress,
    # with total=None so the UI falls back to showing bytes-so-far only.
    def handler(request):
        resp = httpx.Response(200, content=iter([b"\x00" * 1024]))
        resp.headers.pop("content-length", None)
        return resp

    client = make_bare_client(lambda *a: None)
    client._httpx_transport = httpx.MockTransport(handler)
    calls = []
    client.download_file(1, 2, "book.epub", tmp_path / "book.epub",
                         on_progress=lambda written, total: calls.append((written, total)))
    assert calls and all(total is None for _, total in calls)


def test_download_file_raises_on_failure_status(tmp_path):
    client = make_bare_client(lambda *a: None)
    client._httpx_transport = httpx.MockTransport(
        lambda request: httpx.Response(403, text="DDoS-Guard")
    )
    dest = tmp_path / "should-not-be-created.epub"
    with pytest.raises(LitresAuthError, match="403"):
        client.download_file(1, 2, "book.epub", dest)
    assert not dest.exists()


def test_download_file_cancels_mid_transfer_and_discards_the_partial(tmp_path):
    # A multi-chunk body; should_cancel flips True after the first chunk, so the
    # transfer must abort mid-stream and leave no partial file behind.
    client = make_bare_client(lambda *a: None)
    client._httpx_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"\xcd" * (5 * 1024 * 1024))
    )
    polls = {"n": 0}

    def should_cancel():
        polls["n"] += 1
        return polls["n"] >= 2  # let the first chunk through, then cancel

    dest = tmp_path / "audiobook.zip"
    with pytest.raises(DownloadCancelled):
        client.download_file(1, 2, "audiobook.zip", dest, should_cancel=should_cancel)
    assert not dest.exists()  # the partial write was discarded


def test_get_merges_extra_headers_with_explicit_headers():
    seen = {}

    def handler(url, params, headers, timeout):
        seen.update(headers or {})
        return FakeAPIResponse(status=200)

    client = make_bare_client(handler, extra_headers={"app-id": "115", "session-id": "abc"})
    client._get("https://api.litres.ru/foundation/api/users/me", headers={"X-Extra": "1"})
    assert seen == {"app-id": "115", "session-id": "abc", "X-Extra": "1"}
