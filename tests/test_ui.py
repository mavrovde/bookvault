"""Browser-layer tests: the rendered page and its JavaScript.

**Why this file is opt-in.** The rest of the suite never launches a browser
(see `.claude/project/invariants.md` §3), and that stays true by default:
these are marked `ui` and deselected like the `live` smoke tests. Run them
with `-m ui`, after `playwright install chromium`.

They are still **offline** in the sense that matters -- the app is served from
this process against a `FakeLitresClient`, and nothing touches litres.ru or
the network. What's relaxed is only "no browser", and only here.

**Why the layer exists at all.** Two bugs shipped in v1.3.3 that the 400-test
Python suite could not see, because both lived between the DOM and the JS:

- the Browse button was nested in a `<label>`, so the click was forwarded to
  the text input and its handler never ran;
- the selected-size summary claimed sizes were "still loading" when no sweep
  was running and none ever would be.

Every route test passed through both. Each has a regression test below.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from bookvault_core import session
from bookvault_web import folder_dialog, prefs
from bookvault_web.app import app

from tests.fakes import FakeLitresClient

pytestmark = pytest.mark.ui

# Imported lazily so a machine without the browser can still *collect* this
# file (pytest imports every test module even when deselecting by marker).
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


LIBRARY = [
    {"id": 1, "title": "Sized Book", "art_type": 0, "persons": [], "cover_url": None},
    {"id": 2, "title": "Unsized One", "art_type": 0, "persons": [], "cover_url": None},
    {"id": 3, "title": "Unsized Two", "art_type": 11, "persons": [], "cover_url": None},
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def never_reach_litres(monkeypatch):
    """Hard guarantee that these tests cannot talk to litres.ru.

    The app is served against a FakeLitresClient, but "we wired a fake in" is
    a promise; this makes it enforced. Constructing a real LitresClient is the
    only way to reach the site (it owns the browser, the API calls and the
    downloads), so making that a failure means a regression that reintroduces
    a live client fails loudly instead of quietly hitting the real service
    from CI. `lifespan="off"` below covers the other route in -- a session
    restore from a saved cookie file or the OS keychain."""
    def forbidden(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError(
            "a real LitresClient was constructed in a UI test -- these must never "
            "touch litres.ru; serve the app against the fake instead"
        )

    monkeypatch.setattr(session, "LitresClient", forbidden)


@pytest.fixture
def live_app(monkeypatch, tmp_path):
    """Serve the real app on a background thread against a fake client.

    Returns the base URL. uvicorn gets its own event loop in that thread;
    Playwright's sync API stays on the main thread, so the two never share a
    loop (the constraint that shapes session.py applies to the *client*, and
    this test never constructs a real one)."""
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", str(tmp_path))
    fake = FakeLitresClient(library=LIBRARY)
    session._state["client"], session._state["login"] = fake, "demo@example.com"

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - CI hiccup
        pytest.fail("the test server did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def page(live_app):
    """A loaded, logged-in page with the library rendered."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(live_app)
            # The list is fetched by JS; wait for it rather than sleeping.
            page.wait_for_function("() => typeof state !== 'undefined' && state.books.length === 3", timeout=10_000)
            yield page
        finally:
            browser.close()


def _size_text(page) -> str:
    return page.eval_on_selector("#selected-size", "el => el.textContent")


def _summary_for(page, *, selected, sized, state):
    """Drive the exact function that broke into a given situation.

    Sets the book sizes, the selection and the tracked activity state, then
    repaints -- so each branch is asserted deterministically instead of
    depending on what a sweep happened to resolve."""
    return page.evaluate(
        # Object keys are stringified crossing into the page (Playwright
        # refuses numeric keys outright), so look them up as strings.
        """([selected, sized, activityState]) => {
            for (const b of state.books) b.size_mb = sized[String(b.id)] ?? null;
            state.selected = new Set(selected);
            currentState = activityState;
            updateSelectedCount();
            return document.getElementById('selected-size').textContent;
        }""",
        [selected, {str(k): v for k, v in sized.items()}, state],
    )


# -- the selected-size summary (the v1.3.3 bug) -----------------------------

def test_idle_with_no_sizes_says_they_are_unknown_not_loading(page):
    """The shipped bug: "(~0.0 MB so far, size of 239 more still loading…)".

    The page-load sweep is cache-only and touches litres.ru zero times, so on
    an unsized library it resolves nothing and finishes at once -- "loading"
    described something that would never happen."""
    text = _summary_for(page, selected=[1, 2, 3], sized={}, state="idle")
    assert "loading" not in text.lower()
    assert "unknown" in text.lower()
    assert "Refresh" in text          # points at what actually resolves them
    assert "0.0 MB" not in text       # and doesn't imply an empty library


def test_idle_with_some_sizes_separates_known_from_unknown(page):
    text = _summary_for(page, selected=[1, 2, 3], sized={1: 100.0}, state="idle")
    assert "100.0 MB" in text and "for 1" in text
    assert "2 unknown" in text
    assert "loading" not in text.lower()


def test_a_running_sweep_is_the_only_thing_that_says_loading(page):
    text = _summary_for(page, selected=[1, 2, 3], sized={1: 100.0}, state="checking")
    assert "loading" in text.lower()
    assert "2 more" in text
    assert "Refresh" not in text       # it's already happening


def test_a_fully_sized_selection_shows_a_plain_total(page):
    text = _summary_for(page, selected=[1, 2], sized={1: 100.0, 2: 50.0}, state="idle")
    assert "150.0 MB" in text
    assert "so far" not in text and "unknown" not in text.lower()


def test_selecting_nothing_shows_no_size_at_all(page):
    assert _summary_for(page, selected=[], sized={1: 100.0}, state="idle") == ""


def test_the_summary_is_repainted_when_a_sweep_ends_resolving_nothing(page):
    """The second half of the bug: the poll only repainted when a size
    actually arrived, so a sweep that resolved none left "checking sizes…" up
    for good. Simulates the poll's state transition."""
    _summary_for(page, selected=[1, 2, 3], sized={}, state="checking")
    assert "loading" in _size_text(page).lower() or "checking" in _size_text(page).lower()

    after = page.evaluate(
        """() => {
            const prev = currentState;
            currentState = 'idle';
            if (prev !== currentState) updateSelectedCount();
            return document.getElementById('selected-size').textContent;
        }"""
    )
    assert "unknown" in after.lower()


# -- the Browse button (the other v1.3.3 bug) -------------------------------

def test_the_browse_button_click_reaches_its_handler(page, monkeypatch):
    """The shipped bug: nested in a <label>, so the click was forwarded to the
    text input and the handler never ran. Asserted through a real click."""
    chosen = []
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: chosen.append(1) or "/tmp")

    page.reload()
    page.wait_for_selector("#browse-dir")
    page.click("#browse-dir")
    page.wait_for_function("() => !document.getElementById('browse-dir').disabled", timeout=10_000)
    assert chosen, "the click never reached browseForDownloadDir"


def test_the_browse_button_is_not_inside_the_label(page, monkeypatch):
    """The structural cause, pinned directly: a <label> forwards activation to
    the control it labels, so any button nested in one is dead.

    is_available() has to be forced: on a headless Linux runner there is no
    DISPLAY and no zenity, so the template correctly omits the button
    entirely, and without this the test quietly only ran on macOS."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    page.reload()
    page.wait_for_selector("#browse-dir")
    assert page.eval_on_selector("#browse-dir", "el => el.closest('label') === null")


def test_the_button_is_absent_where_no_dialog_can_be_drawn(page, monkeypatch):
    """The headless-Linux/Docker case, asserted rather than assumed: no button
    offered where clicking it could only fail, and the typed field still
    there as the way in."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: False)
    page.reload()
    page.wait_for_selector("#download-dir")
    assert page.query_selector("#browse-dir") is None


def test_a_picked_folder_lands_in_the_field(page, monkeypatch, tmp_path):
    picked = tmp_path / "My Library"
    picked.mkdir()
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: str(picked))

    page.reload()
    page.wait_for_selector("#browse-dir")
    page.click("#browse-dir")
    page.wait_for_function(
        "expected => document.getElementById('download-dir').value === expected",
        arg=str(picked), timeout=10_000,
    )
    assert page.input_value("#download-dir") == str(picked)


def test_cancelling_the_dialog_changes_nothing(page, monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: None)

    page.reload()
    page.wait_for_selector("#browse-dir")
    before = page.input_value("#download-dir")
    page.click("#browse-dir")
    page.wait_for_function("() => !document.getElementById('browse-dir').disabled", timeout=10_000)

    assert page.input_value("#download-dir") == before
    assert page.eval_on_selector("#download-dir-error", "el => el.style.display") == "none"


def test_the_button_shows_progress_and_recovers(page, monkeypatch):
    """It stays disabled while a dialog is open -- a second click must not be
    able to stack up another one -- and comes back afterwards."""
    release = threading.Event()

    def slow_dialog(initial=None):
        # Blocks like a real dialog waiting on a human, then falls through --
        # an implicit None, which is how choose_folder reports "cancelled".
        release.wait(timeout=10)

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", slow_dialog)

    page.reload()
    page.wait_for_selector("#browse-dir")
    page.click("#browse-dir")
    page.wait_for_function("() => document.getElementById('browse-dir').disabled", timeout=5_000)
    assert "Choosing" in page.text_content("#browse-dir")

    release.set()
    page.wait_for_function("() => !document.getElementById('browse-dir').disabled", timeout=10_000)
    assert page.text_content("#browse-dir").strip().startswith("Browse")


def test_a_failed_picker_shows_an_error_without_internal_detail(page, monkeypatch):
    def boom(initial=None):
        raise folder_dialog.FolderDialogError("zenity: symbol lookup error in /usr/lib/x.so")

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", boom)

    page.reload()
    page.wait_for_selector("#browse-dir")
    page.click("#browse-dir")
    page.wait_for_function(
        "() => document.getElementById('download-dir-error').style.display === 'block'",
        timeout=10_000,
    )
    shown = page.text_content("#download-dir-error")
    assert "symbol lookup" not in shown and "/usr/lib" not in shown


# -- the rest of the page still works --------------------------------------

def test_the_page_loads_without_console_errors(page):
    """A JS exception during init silently kills every handler after it -- the
    failure mode both shipped bugs looked like from the outside."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.reload()
    page.wait_for_function("() => typeof state !== 'undefined' && state.books.length === 3", timeout=10_000)
    assert errors == []


def test_typing_a_rejected_folder_shows_the_error_inline(page):
    page.fill("#download-dir", "relative/path")
    page.wait_for_function(
        "() => document.getElementById('download-dir-error').style.display === 'block'",
        timeout=10_000,
    )
    assert "full folder path" in page.text_content("#download-dir-error")


def test_selecting_books_updates_the_count(page):
    page.evaluate("() => { state.selected = new Set([1, 2]); updateSelectedCount(); }")
    assert "2 of 3 selected" in page.text_content("#selected-count")


# -- switching the type filter clears the selection -------------------------
# Selection is not scoped to the filter, so books hidden by it stayed selected
# and were still downloaded: pick All, select everything, switch to Books,
# start -- and every audiobook came too. The screen said one thing and the run
# did another, with nothing visible to reveal the difference.


def _selected_ids(page):
    return sorted(page.evaluate("() => Array.from(state.selected)"))


def test_switching_the_type_filter_clears_the_selection(page):
    """The reported bug, end to end through the real click handler."""
    page.click("#select-all")
    assert _selected_ids(page) == [1, 2, 3]

    page.click("#type-filter .pill[data-type='book']")

    assert _selected_ids(page) == [], "hidden books must not stay selected"
    assert "0 of 3 selected" in page.text_content("#selected-count")


def test_clicking_the_filter_already_active_keeps_the_selection(page):
    """Only a real change clears. Re-clicking the current pill must not wipe a
    selection the user just made."""
    page.click("#select-all")
    page.click("#type-filter .pill[data-type='all']")
    assert _selected_ids(page) == [1, 2, 3]


def test_the_cleared_selection_reaches_the_server(page):
    """Selection lives on the server, so clearing it in the page is not enough
    -- a build started from another tab would still use the stale set."""
    page.click("#select-all")
    page.click("#type-filter .pill[data-type='audio']")
    page.wait_for_function(
        "() => fetch('/prefs').then(r => r.json()).then(p => p.selected.length === 0)",
        timeout=10_000,
    )

# -- the progress log reveals a burst step by step --------------------------
# A book already on disk needs no transfer, so a run over a mostly-saved
# library decides hundreds of them within milliseconds and every row arrives in
# a single poll. Rendered as-is that reads as a block that was already there,
# not as the run working through them. The rows are therefore queued and
# revealed one at a time -- in the browser, so the download is never paced.


def _push_snapshot(page, *, state, log):
    """Drive renderActivity exactly as a poll would."""
    return page.evaluate(
        """([activityState, log]) => {
            renderActivity({
                state: activityState, result: null, message: '', current_title: null,
                current_downloaded: null, current_total: null,
                done: log.length, total: log.length,
                bytes_done: 0, bytes_total: null,
                log: activityState === 'idle' ? [] : log,
                results: activityState === 'idle' ? log : [],
                error: null, sizes: {}, zip_path: null, saved_path: null,
            });
            return document.querySelectorAll('#progress-log > *').length;
        }""",
        [state, log],
    )


def _rows(page) -> int:
    return page.evaluate("() => document.querySelectorAll('#progress-log > *').length")


def _burst(n):
    return [{"title": f"Book {i}", "ext": "epub", "size_mb": 1.0, "status": "exists"}
            for i in range(n)]


def test_a_burst_of_rows_is_not_dumped_all_at_once(page):
    """The reported complaint: the whole list appearing the instant a run
    starts. One poll delivers 40 rows; the screen must not show 40."""
    shown = _push_snapshot(page, state="downloading", log=_burst(40))
    assert shown < 40, f"all {shown} rows appeared immediately"


def test_the_rows_then_catch_up_on_their_own(page):
    """Revealed progressively, not dropped: everything must arrive."""
    _push_snapshot(page, state="downloading", log=_burst(12))
    page.wait_for_function(
        "() => document.querySelectorAll('#progress-log > *').length === 12", timeout=10_000
    )
    assert _rows(page) == 12


def test_a_finished_run_shows_its_whole_report_immediately(page):
    """Nothing is still happening, so drip-feeding a complete report would only
    make the user wait to read what is already available."""
    shown = _push_snapshot(page, state="idle", log=_burst(25))
    assert shown == 25


def test_a_new_run_rewinds_the_reveal(page):
    """A shrinking log means `_begin` cleared it for a fresh run -- the previous
    run's revealed rows must not stay on screen."""
    _push_snapshot(page, state="idle", log=_burst(15))
    assert _rows(page) == 15
    assert _push_snapshot(page, state="downloading", log=[]) == 0
