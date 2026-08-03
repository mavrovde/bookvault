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
    assert autosync.try_start_sync(lambda: object(), lambda: {}) is True
    assert calls == [True]
