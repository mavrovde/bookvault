"""Tests for the native save-folder picker (bookvault_web/folder_dialog.py)
and its HTTP surface.

No test here opens a real dialog: `subprocess.run` is faked throughout, so the
suite stays offline and non-interactive. What's under test is everything
around the dialog -- which helper gets invoked, how cancel/timeout/failure are
told apart, and that a picked path is still subject to the same save-folder
guard a typed path goes through.
"""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from bookvault_core import session
from bookvault_web import folder_dialog, prefs
from bookvault_web.app import app
from fastapi.testclient import TestClient

from tests.fakes import client_factory


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def unlocked_dialog():
    """The busy-lock is module-level; make sure a test that simulates a failure
    mid-dialog can't leave it held for the next test."""
    yield
    if folder_dialog._dialog_lock.locked():  # pragma: no cover - only on a bug
        folder_dialog._dialog_lock.release()


# -- is_available: where a dialog can actually be drawn ---------------------

def test_available_on_macos_when_osascript_exists(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "/usr/bin/osascript")
    assert folder_dialog.is_available() is True


def test_unavailable_on_macos_without_osascript(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: None)
    assert folder_dialog.is_available() is False


def test_available_on_windows_with_either_powershell(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "C:\\pwsh.exe" if name == "pwsh" else None)
    assert folder_dialog.is_available() is True


def test_unavailable_on_linux_without_a_display(monkeypatch):
    """The Docker web image: zenity might even be installed, but there is no
    display to draw on -- the button must not be offered."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "/usr/bin/zenity")
    assert folder_dialog.is_available() is False


def test_unavailable_on_linux_with_a_display_but_no_helper(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: None)
    assert folder_dialog.is_available() is False


def test_available_on_wayland_with_kdialog(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "/usr/bin/kdialog" if name == "kdialog" else None)
    assert folder_dialog.is_available() is True


# -- how the helper is invoked ---------------------------------------------

def test_macos_passes_the_start_folder_as_argv_not_inside_the_script(monkeypatch, tmp_path):
    """The starting folder must never be interpolated into the AppleScript
    source: a folder named `foo" & (do shell script "...")` would otherwise be
    executable text. It travels as an argv element instead."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "/usr/bin/osascript")
    nasty = tmp_path / 'evil" folder'
    nasty.mkdir()
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["input"] = argv, kwargs.get("input")
        return _completed(stdout=str(nasty))

    monkeypatch.setattr(folder_dialog.subprocess, "run", fake_run)
    folder_dialog.choose_folder(str(nasty))

    assert seen["argv"] == ["osascript", "-", str(nasty)]
    # The script itself is constant -- the path is nowhere in it.
    assert str(nasty) not in seen["input"]


def test_windows_passes_the_start_folder_in_the_environment(monkeypatch, tmp_path):
    """Same reasoning on Windows: the path goes in the environment, so it can't
    become PowerShell code."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "powershell.exe")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        return _completed(stdout=str(tmp_path))

    monkeypatch.setattr(folder_dialog.subprocess, "run", fake_run)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    folder_dialog.choose_folder(str(tmp_path))

    assert "-STA" in seen["argv"]  # FolderBrowserDialog won't run without it
    assert seen["env"]["BOOKVAULT_DIALOG_START"] == str(tmp_path)
    assert str(tmp_path) not in " ".join(seen["argv"])


def test_a_nonexistent_start_folder_is_dropped_rather_than_passed(monkeypatch, tmp_path):
    """A saved folder that has since been deleted must not make the dialog fail
    to open -- it just starts wherever the OS defaults to."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "/usr/bin/osascript")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(stdout=str(tmp_path))

    monkeypatch.setattr(folder_dialog.subprocess, "run", fake_run)
    folder_dialog.choose_folder(str(tmp_path / "was-deleted"))
    assert seen["argv"] == ["osascript", "-", ""]


def test_zenity_gets_a_trailing_separator_so_it_opens_inside_the_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(folder_dialog.shutil, "which", lambda name: "/usr/bin/zenity" if name == "zenity" else None)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(stdout=str(tmp_path))

    monkeypatch.setattr(folder_dialog.subprocess, "run", fake_run)
    folder_dialog.choose_folder(str(tmp_path))
    assert f"--filename={tmp_path}{os.sep}" in seen["argv"]
    assert "--directory" in seen["argv"]  # folders only, never a file


# -- outcomes ---------------------------------------------------------------

def test_returns_the_chosen_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))
    monkeypatch.setattr(folder_dialog.subprocess, "run", lambda *a, **k: _completed(stdout=str(tmp_path)))
    assert folder_dialog.choose_folder(None) == str(tmp_path)


def test_strips_the_trailing_slash_applescript_adds(monkeypatch, tmp_path):
    """`POSIX path of` returns "/Users/x/Books/" -- stored as-is it wouldn't
    compare equal to the same folder typed by hand."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))
    monkeypatch.setattr(folder_dialog.subprocess, "run", lambda *a, **k: _completed(stdout=f"{tmp_path}{os.sep}\n"))
    assert folder_dialog.choose_folder(None) == str(tmp_path)


def test_cancel_is_not_an_error(monkeypatch):
    """Every backend signals cancel with a non-zero exit; it must read as
    "nothing chosen", not as a failure the UI shouts about."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))
    monkeypatch.setattr(
        folder_dialog.subprocess, "run",
        lambda *a, **k: _completed(returncode=1, stderr="execution error: User canceled. (-128)"),
    )
    assert folder_dialog.choose_folder(None) is None


def test_empty_output_with_a_zero_exit_is_also_a_cancel(monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))
    monkeypatch.setattr(folder_dialog.subprocess, "run", lambda *a, **k: _completed(stdout="  \n"))
    assert folder_dialog.choose_folder(None) is None


def test_a_returned_file_is_refused(monkeypatch, tmp_path):
    """Belt-and-braces: the backends are folder-only, but if a helper ever
    hands back a file it must not reach prefs as a save folder."""
    a_file = tmp_path / "book.epub"
    a_file.write_text("x")
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))
    monkeypatch.setattr(folder_dialog.subprocess, "run", lambda *a, **k: _completed(stdout=str(a_file)))
    with pytest.raises(folder_dialog.FolderDialogError, match="isn't a folder"):
        folder_dialog.choose_folder(None)


def test_timeout_raises_rather_than_hanging_forever(monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))

    def timing_out(*a, **k):
        raise subprocess.TimeoutExpired(cmd="true", timeout=1)

    monkeypatch.setattr(folder_dialog.subprocess, "run", timing_out)
    with pytest.raises(folder_dialog.FolderDialogError, match="timed out"):
        folder_dialog.choose_folder(None)


def test_a_missing_helper_binary_raises_the_typed_error(monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["nope"], None))

    def missing(*a, **k):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(folder_dialog.subprocess, "run", missing)
    with pytest.raises(folder_dialog.FolderDialogError, match="could not be started"):
        folder_dialog.choose_folder(None)


def test_choose_folder_refuses_when_no_picker_is_available(monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: False)
    with pytest.raises(folder_dialog.FolderDialogError):
        folder_dialog.choose_folder(None)


def test_the_lock_is_released_after_a_failure(monkeypatch):
    """A dialog that blew up must not wedge every later attempt as "busy"."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "_command", lambda initial: (["true"], None))
    monkeypatch.setattr(folder_dialog.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(folder_dialog.FolderDialogError):
        folder_dialog.choose_folder(None)
    assert not folder_dialog._dialog_lock.locked()


def test_a_second_dialog_while_one_is_open_is_refused(monkeypatch):
    """Any page on the machine can POST here (127.0.0.1, no CSRF by design), so
    a second request must be told "busy" rather than stacking up dialogs."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    folder_dialog._dialog_lock.acquire()
    try:
        with pytest.raises(folder_dialog.DialogBusy):
            folder_dialog.choose_folder(None)
    finally:
        folder_dialog._dialog_lock.release()


# -- the route --------------------------------------------------------------

def test_browse_saves_the_picked_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: str(tmp_path))
    with TestClient(app) as client:
        resp = client.post("/prefs/browse")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["cancelled"] is False
    assert body["download_dir"] == str(tmp_path)
    # ...and it really is persisted, not just echoed back.
    assert prefs.snapshot()["download_dir"] == str(tmp_path)


def test_browse_cancel_leaves_the_saved_folder_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", str(tmp_path))
    prefs.update(download_dir=str(tmp_path))
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: None)
    with TestClient(app) as client:
        resp = client.post("/prefs/browse")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True
    assert prefs.snapshot()["download_dir"] == str(tmp_path)  # unchanged


def test_browse_starts_the_dialog_in_the_folder_already_in_force(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", str(tmp_path))
    seen = {}

    def fake_choose(initial=None):
        seen["initial"] = initial
        return str(tmp_path)

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", fake_choose)
    with TestClient(app) as client:
        client.post("/prefs/browse")
    assert seen["initial"] == str(tmp_path)


def test_browse_is_501_where_no_picker_exists(monkeypatch):
    """Docker: the UI hides the button, but the route must still answer
    sensibly if something posts to it anyway."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: False)
    with TestClient(app) as client:
        resp = client.post("/prefs/browse")
    assert resp.status_code == 501
    assert resp.json()["ok"] is False


def test_browse_reports_a_busy_dialog_as_409(monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)

    def busy(initial=None):
        raise folder_dialog.DialogBusy("already open")

    monkeypatch.setattr(folder_dialog, "choose_folder", busy)
    with TestClient(app) as client:
        resp = client.post("/prefs/browse")
    assert resp.status_code == 409


def test_browse_reports_a_broken_dialog_as_503_without_internal_detail(monkeypatch):
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)

    def boom(initial=None):
        raise folder_dialog.FolderDialogError("zenity: symbol lookup error in /usr/lib/x.so")

    monkeypatch.setattr(folder_dialog, "choose_folder", boom)
    with TestClient(app) as client:
        resp = client.post("/prefs/browse")
    assert resp.status_code == 503
    # The internal detail stays in the log, not the response.
    assert "symbol lookup" not in resp.text
    assert "/usr/lib" not in resp.text


def test_a_picked_folder_is_still_subject_to_the_allowed_roots_guard(monkeypatch):
    """The picker can reach anywhere the user can click, including places the
    guard refuses (/Library, /etc). Picking is not a way around it."""
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: "/etc")
    with TestClient(app) as client:
        resp = client.post("/prefs/browse")
    assert resp.status_code == 400
    assert resp.json()["error"] == prefs.DOWNLOAD_DIR_ERRORS["outside_allowed_roots"]
    assert prefs.snapshot()["download_dir"] is None  # nothing was stored


def test_browse_button_is_rendered_only_when_a_picker_exists(monkeypatch):
    """The template guard: no button where no dialog can be drawn, so the user
    isn't offered something that can only fail."""
    monkeypatch.setattr(session, "login", lambda *a: None)
    monkeypatch.setattr(session, "current_client", lambda: object())

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    with TestClient(app) as client:
        assert 'id="browse-dir"' in client.get("/").text

    monkeypatch.setattr(folder_dialog, "is_available", lambda: False)
    with TestClient(app) as client:
        page = client.get("/").text
    assert 'id="browse-dir"' not in page
    assert 'id="download-dir"' in page  # the typed field is still there


def test_the_browse_button_is_not_nested_inside_the_label(monkeypatch):
    """It was, and the click did nothing.

    A <label> forwards activation to the control it labels, so a <button>
    nested inside one never runs its own handler -- the click lands on the text
    input instead. Everything else (route, dialog, JS handler) was correct, so
    this rendered as "the button does nothing"."""
    monkeypatch.setattr(session, "current_client", lambda: object())
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    with TestClient(app) as client:
        page = client.get("/").text

    assert 'id="browse-dir"' in page
    # The save-folder <label>...</label> must not contain the button.
    start = page.index('<label class="format-control" title="Folder the finished zip')
    label = page[start:page.index("</label>", start)]
    assert "browse-dir" not in label, "the Browse button is nested in the label again"


def test_browse_does_not_run_on_the_playwright_worker_thread(monkeypatch, tmp_path):
    """A dialog waits on a human. Parking the single Playwright worker behind
    it would stall any running download until the user clicked something."""
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: str(tmp_path))

    # Recorded rather than forbidden outright: startup (restore_session) and
    # shutdown legitimately use the worker, so what's asserted is that nothing
    # goes through it *during the request itself*.
    real_run, used = session.run, []

    def recording_run(fn, *a, **k):
        used.append(fn)
        return real_run(fn, *a, **k)

    with TestClient(app) as client:
        monkeypatch.setattr(session, "run", recording_run)
        used.clear()
        assert client.post("/prefs/browse").status_code == 200
        assert used == []


def test_browse_works_while_a_library_is_loaded(monkeypatch, tmp_path):
    """The realistic path: logged in, library loaded, user picks a folder."""
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", str(tmp_path))
    client_factory(monkeypatch, session, library=[])
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    chosen = tmp_path / "My Books"
    chosen.mkdir()
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: str(chosen))
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.post("/prefs/browse")
        assert resp.status_code == 200
        # The poll every browser shares reflects it immediately.
        assert client.get("/activity").json()["prefs"]["download_dir"] == str(chosen)


# -- saving an extra copy of a finished archive -----------------------------

def test_save_copy_puts_a_copy_where_the_user_picked(monkeypatch, tmp_path):
    from bookvault_web import activity

    saved = tmp_path / "configured" / "litres-library.zip"
    saved.parent.mkdir()
    saved.write_bytes(b"archive")
    activity.state._state["zip_path"] = str(saved)
    activity.state._state["saved_path"] = str(saved)
    dest = tmp_path / "external"
    dest.mkdir()

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: str(dest))
    monkeypatch.setattr(prefs, "allowed_download_roots", lambda: [tmp_path.resolve()])

    with TestClient(app) as client:
        resp = client.post("/download/save-copy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["cancelled"] is False
    assert (dest / "litres-library.zip").read_bytes() == b"archive"
    assert saved.exists()  # the configured folder keeps its copy


def test_save_copy_does_not_change_the_configured_save_folder(monkeypatch, tmp_path):
    """Saving a copy somewhere is a one-off, not a new default."""
    from bookvault_web import activity

    saved = tmp_path / "configured" / "litres-library.zip"
    saved.parent.mkdir()
    saved.write_bytes(b"archive")
    activity.state._state["zip_path"] = activity.state._state["saved_path"] = str(saved)
    dest = tmp_path / "elsewhere"
    dest.mkdir()

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: str(dest))
    monkeypatch.setattr(prefs, "allowed_download_roots", lambda: [tmp_path.resolve()])

    before = prefs.snapshot()["download_dir"]
    with TestClient(app) as client:
        client.post("/download/save-copy")
    assert prefs.snapshot()["download_dir"] == before


def test_save_copy_without_a_finished_build_is_refused(monkeypatch):
    from bookvault_web import activity

    activity.state._state["zip_path"] = None
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    with TestClient(app) as client:
        resp = client.post("/download/save-copy")
    assert resp.status_code == 409


def test_save_copy_destination_is_subject_to_the_allowed_roots_guard(monkeypatch, tmp_path):
    from bookvault_web import activity

    saved = tmp_path / "litres-library.zip"
    saved.write_bytes(b"archive")
    activity.state._state["zip_path"] = activity.state._state["saved_path"] = str(saved)

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: "/etc")
    with TestClient(app) as client:
        resp = client.post("/download/save-copy")
    assert resp.status_code == 400
    assert resp.json()["error"] == prefs.DOWNLOAD_DIR_ERRORS["outside_allowed_roots"]


def test_save_copy_cancel_copies_nothing(monkeypatch, tmp_path):
    from bookvault_web import activity

    saved = tmp_path / "litres-library.zip"
    saved.write_bytes(b"archive")
    activity.state._state["zip_path"] = activity.state._state["saved_path"] = str(saved)

    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: None)
    with TestClient(app) as client:
        resp = client.post("/download/save-copy")
    assert resp.status_code == 200 and resp.json()["cancelled"] is True
    assert list(tmp_path.iterdir()) == [saved]
