"""The OS's own "choose a folder" dialog, opened on the machine the server
runs on.

Why this exists server-side at all: a *browser* cannot hand back a real
filesystem path. `<input type="file" webkitdirectory>` exposes only relative
names and `showDirectoryPicker()` a sandboxed handle -- both deliberately hide
the absolute path, so neither can fill in a save folder. That left typing the
path by hand, which is a miserable way to pick a directory.

But this app is local and single-user by design (bound to 127.0.0.1, one
logged-in account), so the server *is* the user's machine: it can open the
native picker and read the chosen path directly. The desktop app gets this for
free too, since it embeds the web app verbatim.

Nothing here is trusted: the chosen path goes back through `prefs.update()`,
so the allowed-roots guard applies to a picked folder exactly as it does to a
typed one. This module only *obtains* a path; prefs decides if it's usable.

Every backend receives the starting directory as a separate argv element or an
environment variable -- never interpolated into a shell command or a script
body -- so a path containing quotes can't turn into code.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

PROMPT = "Choose where BookVault saves your library"

# A dialog waits for a human, so the timeout is generous -- it exists only so a
# forgotten-about dialog eventually releases the worker thread it's holding.
DIALOG_TIMEOUT = 300

# One dialog at a time. The route is reachable by an unauthenticated POST from
# any page the user happens to have open (127.0.0.1, no CSRF token, by design
# -- see prefs.allowed_download_roots), so without this a page could spawn a
# pile of native dialogs. It still can't *set* anything: the user has to pick
# a folder themselves, and the prefs guard runs on the result either way.
_dialog_lock = threading.Lock()


class FolderDialogError(RuntimeError):
    """The picker could not be shown, or failed while showing."""


class DialogBusy(FolderDialogError):
    """A folder dialog is already open and waiting on the user."""


def is_available() -> bool:
    """Whether a native folder picker can actually be shown here.

    False in Docker and over a plain SSH session -- there's no desktop to draw
    a dialog on -- so the UI can hide the button and leave the text field as
    the way in."""
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    if sys.platform.startswith("win"):
        return shutil.which("powershell") is not None or shutil.which("pwsh") is not None
    # Linux/BSD: needs both a display server and a dialog helper.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return shutil.which("zenity") is not None or shutil.which("kdialog") is not None


# AppleScript reads the starting folder from argv rather than having it pasted
# into the source, so a path with a quote in it can't break out of the string.
_OSASCRIPT = f"""
on run argv
    set startAt to item 1 of argv
    if startAt is "" then
        set chosen to choose folder with prompt "{PROMPT}"
    else
        set chosen to choose folder with prompt "{PROMPT}" default location (POSIX file startAt)
    end if
    return POSIX path of chosen
end run
"""

# Same idea on Windows: the initial path arrives in the environment, not in the
# script text. -STA is required -- FolderBrowserDialog won't run on an MTA thread.
_POWERSHELL = """
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:BOOKVAULT_DIALOG_PROMPT
$dialog.ShowNewFolderButton = $true
if ($env:BOOKVAULT_DIALOG_START) { $dialog.SelectedPath = $env:BOOKVAULT_DIALOG_START }
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.SelectedPath)
    exit 0
}
exit 1
"""


def _command(initial: str) -> tuple[list[str], dict[str, str] | None]:
    """The picker invocation for this OS: (argv, extra environment)."""
    if sys.platform == "darwin":
        return ["osascript", "-", initial], None
    if sys.platform.startswith("win"):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        env = {
            **os.environ,
            "BOOKVAULT_DIALOG_PROMPT": PROMPT,
            "BOOKVAULT_DIALOG_START": initial,
        }
        return [shell, "-NoProfile", "-STA", "-Command", _POWERSHELL], env
    if shutil.which("zenity"):
        argv = ["zenity", "--file-selection", "--directory", f"--title={PROMPT}"]
        if initial:
            # Trailing separator: zenity treats the value as a starting file
            # name otherwise, and opens the *parent* of the folder.
            argv.append(f"--filename={initial.rstrip(os.sep)}{os.sep}")
        return argv, None
    if shutil.which("kdialog"):
        return ["kdialog", "--title", PROMPT, "--getexistingdirectory", initial or os.path.expanduser("~")], None
    raise FolderDialogError("No native folder picker is available on this machine.")


def choose_folder(initial: str | None = None) -> str | None:
    """Show the picker and return the chosen absolute path, or None if the
    user cancelled. Raises FolderDialogError if the dialog can't be shown."""
    if not is_available():
        raise FolderDialogError("No native folder picker is available on this machine.")
    # Non-blocking: a second request must be told "busy" immediately, not
    # queued up behind a dialog someone is already staring at.
    if not _dialog_lock.acquire(blocking=False):
        raise DialogBusy("A folder dialog is already open.")
    try:
        start = initial if initial and os.path.isdir(initial) else ""
        argv, env = _command(start)
        # argv[0] only: the rest can contain the user's paths, and this logs at
        # INFO. Enough to answer "which helper ran, and did it start where I
        # expected" without putting a home directory in the log by default.
        logger.info("Opening the %s folder picker (start=%s)", argv[0], "default" if not start else "current folder")
        try:
            done = subprocess.run(
                argv,
                # A non-zero exit is how every backend says "cancelled", so it
                # is inspected below rather than raised on.
                check=False,
                # AppleScript is fed on stdin ("osascript -"); the others read
                # their script from argv/env and get nothing.
                input=_OSASCRIPT if sys.platform == "darwin" else None,
                capture_output=True,
                text=True,
                timeout=DIALOG_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run has already killed the child by this point.
            raise FolderDialogError("The folder dialog timed out.") from exc
        except OSError as exc:
            raise FolderDialogError("The folder dialog could not be started.") from exc
    finally:
        _dialog_lock.release()

    if done.returncode != 0:
        # Every backend uses a non-zero exit for "cancelled", and none of them
        # distinguishes it from a real failure in a portable way. Treating an
        # empty stdout as a cancel is the honest reading: a successful pick
        # always prints a path. Log stderr so a genuine failure is diagnosable.
        if done.stderr.strip():
            logger.debug("Folder dialog exited %s: %s", done.returncode, done.stderr.strip())
        logger.info("Folder picker closed without a choice (cancelled)")
        return None
    chosen = done.stdout.strip()
    if not chosen:
        return None
    # osascript's POSIX path of a folder comes back with a trailing slash;
    # strip it so the stored value matches a typed one (but keep a bare "/").
    chosen = chosen.rstrip(os.sep) or os.sep
    # Every backend above is folder-only by construction ("choose folder",
    # FolderBrowserDialog, --directory, --getexistingdirectory), so this should
    # never fire -- it's here so that a swapped-in or misconfigured helper
    # can't hand back a file path that then reaches prefs as a save folder.
    if not os.path.isdir(chosen):
        logger.warning("Folder picker returned a path that is not a directory -- rejecting it")
        raise FolderDialogError("The folder picker returned something that isn't a folder.")
    logger.info("Folder picker returned a folder (%d chars)", len(chosen))
    return chosen
