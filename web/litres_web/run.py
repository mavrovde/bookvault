"""Run the local LitRes library downloader (the web app).

Host is intentionally not configurable: this app holds your logged-in
litres.ru session, so it's bound to 127.0.0.1 (localhost-only) by design,
not as a tunable default. Port can be changed via LITRES_APP_PORT if 8420
is taken by something else.

Launch via the `litres-web` console script (see web/pyproject.toml) or
`python -m litres_web.run`.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LITRES_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()
    port = int(os.environ.get("LITRES_APP_PORT", "8420"))
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
    reload_dir = str(Path(__file__).parent)
    uvicorn.run(
        "litres_web.app:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        reload_dirs=[reload_dir],
        log_config=None,
    )


if __name__ == "__main__":
    main()
