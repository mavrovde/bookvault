import pytest
from bookvault_web import activity, autosync


def test_autosync_disabled_without_library_dir(monkeypatch):
    monkeypatch.delenv("LITRES_LIBRARY_DIR", raising=False)
    monkeypatch.setenv("LITRES_AUTOSYNC", "1")
    assert autosync.autosync_enabled() is False


def test_autosync_enabled_with_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("LITRES_AUTOSYNC", "1")
    assert autosync.autosync_enabled() is True


def test_try_start_sync_when_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("LITRES_AUTOSYNC", "1")
    calls = []

    def fake_start(client, **kwargs):
        calls.append(True)
        return True

    monkeypatch.setattr(activity, "start_sync", fake_start)
    assert autosync.try_start_sync(lambda: object(), dict) is True
    assert calls == [True]


# -- cadence -----------------------------------------------------------------
# A library only changes when the user buys something, and a perfectly regular
# unattended run of listing requests is the most scraper-shaped traffic this
# app can produce. These pin the conservative defaults.


def test_autosync_interval_defaults_to_hours_not_minutes(monkeypatch):
    monkeypatch.delenv("LITRES_AUTOSYNC_INTERVAL", raising=False)
    assert autosync.interval_seconds() == 6 * 60 * 60


def test_autosync_interval_is_floored(monkeypatch):
    """A typo (or an over-eager config) must not turn this into a hammer."""
    monkeypatch.setenv("LITRES_AUTOSYNC_INTERVAL", "5")
    assert autosync.interval_seconds() == 15 * 60


def test_autosync_interval_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("LITRES_AUTOSYNC_INTERVAL", "not-a-number")
    assert autosync.interval_seconds() == 6 * 60 * 60


def test_autosync_tick_is_jittered(monkeypatch):
    """Ticks must not land on a metronome -- same reasoning as the per-page
    jitter in iter_library."""
    monkeypatch.setenv("LITRES_AUTOSYNC_INTERVAL", "3600")
    delays = {autosync._next_delay() for _ in range(20)}
    assert len(delays) > 1                       # actually varies
    assert all(3600 * 0.9 <= d <= 3600 * 1.1 for d in delays)


def test_autosync_does_not_start_while_another_activity_runs(monkeypatch, tmp_path):
    """It claims the activity machine like any other caller -- it must never
    run alongside a zip build competing for the one Playwright thread."""
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("LITRES_AUTOSYNC", "1")
    monkeypatch.setattr(activity, "_state", {**activity._state, "state": activity.PREPARING})
    monkeypatch.setattr(activity, "start_sync", lambda *a, **k: pytest.fail("must not start"))
    assert autosync.try_start_sync(lambda: object(), dict) is False
