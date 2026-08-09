"""Run the local LitRes library downloader (the web app).

Host defaults to 127.0.0.1: this app holds your logged-in litres.ru session,
so it's localhost-only by design, NOT a service to expose. The default is
never changed for a normal local run. `LITRES_APP_HOST` exists only so the
Docker image can bind 0.0.0.0 *inside the container* -- the container is then
published to `127.0.0.1:8420` on the host (see docker-compose.yml), so the
localhost-only posture is preserved at the host boundary. Don't set it to
0.0.0.0 outside a container. Port can be changed via LITRES_APP_PORT.

Launch via the `bookvault-web` console script (see web/pyproject.toml) or
`python -m bookvault_web.run`.
"""
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def _truthy(value: str) -> bool:
    return value.lower() not in ("0", "false", "no", "")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LITRES_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()
    host = os.environ.get("LITRES_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("LITRES_APP_PORT", "8420"))
    # Reload is great for local dev but wrong in a container: it spawns a file
    # watcher/subprocess and would restart on nothing. Default on; the image
    # sets LITRES_RELOAD=0.
    reload = _truthy(os.environ.get("LITRES_RELOAD", "1"))
    # log_config=None: uvicorn's own dictConfig would otherwise reset/replace
    # the logging setup above (root handler + format) once it starts, which
    # is why app-level log lines (session restore, download progress, ...)
    # wouldn't show up even at INFO level.
    #
    # reload_dirs: watch only this package, not the whole project (tests etc.)
    # -- editing an unrelated file then restarts the live server mid-session,
    # which silently kills anything in progress (e.g. a zip build): the reload
    # replaces the whole process, wiping every module-level in-memory state
    # (session, activity, cache).
    kwargs = {"host": host, "port": port, "log_config": None}
    if reload:
        kwargs["reload"] = True
        kwargs["reload_dirs"] = [str(Path(__file__).parent)]
    uvicorn.run("bookvault_web.app:app", **kwargs)


if __name__ == "__main__":
    main()
