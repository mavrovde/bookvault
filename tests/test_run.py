"""Tests for bookvault_web/run.py -- the `bookvault-web` launcher. uvicorn.run
is monkeypatched out, so these verify the wiring (localhost binding, env
overrides, the reload/no-reload split) without starting a server."""
from __future__ import annotations

import pytest

from bookvault_web import run as run_mod


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False), ("no", False), ("", False), ("FALSE", False)],
)
def test_truthy_parses_common_flag_spellings(value, expected):
    assert run_mod._truthy(value) is expected


@pytest.fixture
def captured_uvicorn_run(monkeypatch):
    calls = []
    monkeypatch.setattr(run_mod.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    # Don't let the launcher re-import the repo's real .env into the test env.
    monkeypatch.setattr(run_mod, "load_dotenv", lambda: None)
    return calls


def test_main_defaults_to_localhost_8420_with_reload(captured_uvicorn_run, monkeypatch):
    monkeypatch.delenv("LITRES_APP_HOST", raising=False)
    monkeypatch.delenv("LITRES_APP_PORT", raising=False)
    monkeypatch.delenv("LITRES_RELOAD", raising=False)

    run_mod.main()

    app, kwargs = captured_uvicorn_run[0]
    assert app == "bookvault_web.app:app"
    assert kwargs["host"] == "127.0.0.1"  # localhost-only by design
    assert kwargs["port"] == 8420
    assert kwargs["log_config"] is None  # keeps app-level log lines visible
    assert kwargs["reload"] is True
    # Reload watches only the package -- editing tests/ etc. must not restart
    # a live server mid-download.
    assert kwargs["reload_dirs"] == [str(run_mod.Path(run_mod.__file__).parent)]


def test_main_honors_env_overrides_and_container_mode(captured_uvicorn_run, monkeypatch):
    monkeypatch.setenv("LITRES_APP_HOST", "0.0.0.0")  # the Docker image's setting
    monkeypatch.setenv("LITRES_APP_PORT", "9999")
    monkeypatch.setenv("LITRES_RELOAD", "0")

    run_mod.main()

    _, kwargs = captured_uvicorn_run[0]
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999
    assert "reload" not in kwargs  # no file watcher inside a container
    assert "reload_dirs" not in kwargs
