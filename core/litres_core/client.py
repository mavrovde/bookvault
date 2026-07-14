"""Client for litres.ru driven through a real Chromium session (Playwright).

Plain `requests` POSTs to the login endpoint get rejected with a generic
"Incorrect user data" error regardless of whether the credentials are right
-- litres.ru sets DataDome-style bot-protection cookies (`__ddg9_`,
`__ddg1_`) that only a real, JS-executing browser can obtain. So login is
driven through an actual headless Chromium page (filling the real login
form, confirmed selectors: `input[name=email]` then `input[name=pwd]`).

Being logged in isn't enough either: the API gateway also requires a set of
app-level headers (`app-id`, `session-id`, `client-host`, `ui-currency`,
`ui-language-code`, `ab-tests-flags`, `basket`, `wishlist`,
`safemode-enabled`, ...) that the site's own frontend code attaches to every
call -- Playwright's request client doesn't add these on its own, and they
aren't things a script can safely invent (they 403 with "PermissionMissing"
if missing/wrong). So right after login we capture the header set from a
request the site's *own* JS makes automatically (`GET .../users/me`) and
replay it on subsequent calls -- this is our own already-authenticated
traffic, just reused, not a forged fingerprint.

Endpoints and response shapes below were all confirmed against a real
account: `GET .../users/me/arts` (paginated via `payload.pagination.next_page`,
items in `payload.data`), `GET .../arts/{id}/files/grouped` (format options
grouped under `payload.data[].files[]`), and
`GET /download_book/{art_id}/{release_file_id}/{name}.{ext}` (streams the
actual file -- verified against a real purchased epub).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.sync_api import BrowserContext, sync_playwright

logger = logging.getLogger(__name__)

# These are facts about litres.ru itself, not settings -- not configurable.
API_BASE = "https://api.litres.ru/foundation/api"
DOWNLOAD_BASE = "https://www.litres.ru"
LOGIN_PAGE = "https://www.litres.ru/auth/login"

# Set LITRES_HEADLESS=0 to watch the login flow in a real Chromium window
# (useful for debugging a login/selector problem).
HEADLESS = os.environ.get("LITRES_HEADLESS", "1").lower() not in ("0", "false", "no")

# Whole-audiobook bundles can be a few hundred MB to ~2GB -- the default 30s
# Playwright request timeout isn't enough even on a healthy transfer, but
# litres.ru's CDN can also just stall on a specific file (observed: a 350MB
# file that never sent a byte and had to be killed after 20s). Override via
# LITRES_DOWNLOAD_TIMEOUT_MS if your connection or the CDN needs longer/shorter.
DOWNLOAD_TIMEOUT_MS = int(os.environ.get("LITRES_DOWNLOAD_TIMEOUT_MS", "300000"))

# Ebook formats a user can pick as their default, in the order offered to
# pick_best_file as a fallback if their choice isn't available for a given
# book. The full set (for the UI dropdown) is EBOOK_EXTENSIONS below.
PREFERRED_EXTENSIONS = ("epub", "fb2.zip", "mobi.prc", "a4.pdf", "fb3", "txt.zip")
EBOOK_EXTENSIONS = ("epub", "ios.epub", "fb2.zip", "fb3", "mobi.prc", "a4.pdf", "a6.pdf", "txt.zip", "txt", "rtf.zip")

# Audiobooks are grouped by `file_type` instead of a per-file `extension`
# (single chapters live under e.g. "standard_quality_mp3"/31 files -- these
# whole-book bundle types are what we actually want, not one chapter).
PREFERRED_FILE_TYPES = ("zip_with_mp3", "mobile_version_mp4")
EXT_BY_FILE_TYPE = {"zip_with_mp3": "zip", "mobile_version_mp4": "m4b"}
AUDIOBOOK_FILE_TYPES = ("zip_with_mp3", "mobile_version_mp4")

# Headers Playwright/the transport layer already manages correctly on its
# own -- copying them from a captured request would just fight the request
# library (stale content-length, wrong host on a different endpoint, etc).
_DROP_HEADERS = {
    "cookie",
    "host",
    "content-length",
    "content-type",
    "connection",
    "accept-encoding",
}


class LitresAuthError(RuntimeError):
    """Login failed, or an existing session is no longer valid."""


class DownloadCancelled(RuntimeError):
    """Raised by download_file when cancellation is requested mid-transfer --
    distinct from a real failure so the caller can treat it as a clean stop."""


class LitresClient:
    """Logs into litres.ru and pulls the caller's own purchased library.

    One instance = one Chromium browser context. The context's cookies
    (including the DataDome challenge cookies) can be persisted to disk via
    `save_state`/loaded via `storage_state_path` so a fresh login isn't
    needed on every run. The app-level headers captured during `login()`
    are *not* persisted -- they're cheap to recapture and may rotate.
    """

    def __init__(self, storage_state_path: Optional[Path] = None):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=HEADLESS)
        state = str(storage_state_path) if storage_state_path and storage_state_path.exists() else None
        self.context: BrowserContext = self._browser.new_context(storage_state=state)
        self._extra_headers: dict = {}
        # Injected by tests to drive download_file's httpx client offline; None
        # means the real network transport.
        self._httpx_transport = None

    def close(self) -> None:
        self.context.close()
        self._browser.close()
        self._pw.stop()

    def save_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(path))

    def _get(self, url: str, **kwargs):
        headers = {**self._extra_headers, **kwargs.pop("headers", {})}
        return self.context.request.get(url, headers=headers, **kwargs)

    def is_logged_in(self) -> bool:
        if not self._extra_headers and not self._recapture_headers():
            return False
        resp = self._get(f"{API_BASE}/users/me")
        return resp.ok

    def _recapture_headers(self, timeout_ms: int = 20000) -> bool:
        """A client restored from `storage_state_path` has valid session
        cookies but no app-level headers -- those aren't persisted (see
        class docstring) and are normally only captured during login()'s
        request-sniffing. Replay that trick via a plain page visit instead
        of the login form: with valid cookies already in place, visiting
        the login page redirects straight to the logged-in homepage, whose
        SPA fires several `.../users/me/...` calls carrying the same
        globally-attached headers (confirmed against a real account: the
        bare `/users/me` endpoint itself isn't among them on this path, but
        siblings like `/users/me/monetization-details` are, and they carry
        the same header set). Without this, every fresh process would
        silently discard the saved session and re-login from scratch --
        defeating the point of persisting it, and needlessly increasing
        exposure to litres.ru's anti-bot checks."""
        captured = {}
        try:
            page = self.context.new_page()
        except Exception as exc:
            logger.debug("Header recapture could not open a page: %s", exc)
            return False

        def on_request(req):
            if not captured and req.method == "GET" and "/foundation/api/users/me" in req.url:
                captured.update(req.headers)

        page.on("request", on_request)
        try:
            page.goto(LOGIN_PAGE, wait_until="networkidle", timeout=timeout_ms)
        except Exception as exc:
            logger.debug("Header recapture navigation failed: %s", exc)
        finally:
            page.remove_listener("request", on_request)
            page.close()

        if not captured:
            logger.debug("Header recapture found no users/me request -- session cookies are likely invalid")
            return False
        self._extra_headers = {k: v for k, v in captured.items() if k.lower() not in _DROP_HEADERS}
        logger.info("Recaptured app-level headers from a restored session (no fresh login needed)")
        return True

    def login(self, login: str, password: str) -> None:
        logger.info("Driving litres.ru login flow for %s", login)
        page = self.context.new_page()
        captured = {}

        def on_request(req):
            if not captured and req.method == "GET" and req.url.endswith("/foundation/api/users/me"):
                captured.update(req.headers)

        page.on("request", on_request)
        try:
            page.goto(LOGIN_PAGE, wait_until="networkidle", timeout=30000)
            page.fill("input[name=email]", login)
            page.click("button[type=submit]")
            page.wait_for_selector("input[name=pwd]", timeout=15000)
            page.fill("input[name=pwd]", password)
            with page.expect_response(
                lambda r: "auth/login" in r.url and r.request.method == "POST",
                timeout=15000,
            ) as resp_info:
                page.click("button[type=submit]")
            resp = resp_info.value
            if resp.status != 200:
                logger.warning("Login POST for %s returned %s", login, resp.status)
                raise LitresAuthError(f"Login failed ({resp.status}): {resp.text()[:300]}")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            if not captured:
                # The SPA didn't auto-fetch the profile this time -- force it.
                try:
                    page.reload(wait_until="networkidle", timeout=30000)
                except Exception:
                    pass
            self._extra_headers = {k: v for k, v in captured.items() if k.lower() not in _DROP_HEADERS}
            logger.info("Login succeeded for %s (%d app-level headers captured)", login, len(self._extra_headers))
        finally:
            page.remove_listener("request", on_request)
            page.close()

    def iter_library(self, limit: int = 100) -> Iterator[dict]:
        """Yield every art (book/audiobook/...) the user owns."""
        url = f"{API_BASE}/users/me/arts"
        params = {"limit": limit}
        page_count, item_count = 0, 0
        while True:
            resp = self._get(url, params=params)
            if not resp.ok:
                logger.warning("Library fetch failed: HTTP %s", resp.status)
                raise LitresAuthError(f"Library fetch failed ({resp.status}): {resp.text()[:300]}")
            payload = resp.json().get("payload") or {}
            items = payload.get("data") or []
            page_count += 1
            item_count += len(items)
            logger.debug("Library page %d: %d item(s) (%d total so far)", page_count, len(items), item_count)
            if not items:
                return
            for item in items:
                yield item
            next_page = (payload.get("pagination") or {}).get("next_page")
            if not next_page:
                logger.info("Library listing complete: %d item(s) across %d page(s)", item_count, page_count)
                return
            # Reuse only the query params (e.g. the `after` cursor) from the
            # server's next-page link against our known-good endpoint --
            # the path portion of `next_page` doesn't match our base URL.
            params = {k: v[0] for k, v in parse_qs(urlparse(next_page).query).items()}
            time.sleep(0.3)  # personal-use client, not a scraper -- don't hammer the API

    def get_files(self, art_id) -> list:
        """Flat list of {id, extension, file_type, mime, size, is_additional} for one art."""
        resp = self._get(f"{API_BASE}/arts/{art_id}/files/grouped")
        if not resp.ok:
            logger.warning("File listing failed for art %s: HTTP %s", art_id, resp.status)
            raise LitresAuthError(f"Could not list files for art {art_id} ({resp.status}): {resp.text()[:300]}")
        groups = (resp.json().get("payload") or {}).get("data") or []
        flat = []
        for group in groups:
            file_type = group.get("file_type")
            for f in group.get("files") or []:
                flat.append({**f, "file_type": file_type})
        logger.debug("Art %s: %d file(s) across %d group(s)", art_id, len(flat), len(groups))
        return flat

    def pick_best_file(
        self,
        files: list,
        preferred_ext: Optional[str] = None,
        preferred_file_type: Optional[str] = None,
    ) -> Optional[dict]:
        """Pick one file per book. `preferred_ext`/`preferred_file_type` (the
        user's chosen default format) are tried first; if the book doesn't
        have that format, falls back to the built-in preference order."""
        candidates = [f for f in files if not f.get("is_additional")] or files

        # Whole-book bundles (audiobooks) take priority over per-chapter files.
        by_type = {f["file_type"]: f for f in candidates if f.get("file_type")}
        type_order = ([preferred_file_type] if preferred_file_type else []) + list(PREFERRED_FILE_TYPES)
        for file_type in type_order:
            if file_type in by_type:
                return by_type[file_type]

        by_ext = {f["extension"]: f for f in candidates if f.get("extension")}
        ext_order = ([preferred_ext] if preferred_ext else []) + list(PREFERRED_EXTENSIONS)
        for ext in ext_order:
            if ext in by_ext:
                return by_ext[ext]
        return candidates[0] if candidates else None

    @staticmethod
    def file_extension(file_entry: dict) -> str:
        return file_entry.get("extension") or EXT_BY_FILE_TYPE.get(file_entry.get("file_type")) or "fb2"

    def download_file(
        self, art_id, release_file_id, filename: str, dest: Path, subscr: bool = False, should_cancel=None
    ) -> Path:
        segment = "download_book_subscr" if subscr else "download_book"
        url = f"{DOWNLOAD_BASE}/{segment}/{art_id}/{release_file_id}/{filename}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Stream the response straight to disk in fixed-size chunks rather than
        # buffering the whole file in memory. Whole-audiobook bundles can be
        # ~2GB; Playwright's request client only exposes a full-body read
        # (resp.body()), which meant ~2GB resident per download and the machine
        # swapping. We reuse the browser context's cookies (incl. the DataDome
        # __ddg* anti-bot cookies) and the captured app-level headers, so this
        # carries the same authentication/anti-bot profile as the rest of the
        # client -- Playwright's request client is itself a separate HTTP client
        # from the browser, so nothing about the bot fingerprint changes here.
        #
        # `should_cancel`, if given, is polled between chunks so a Stop can
        # interrupt an in-flight transfer within ~one chunk rather than having
        # to wait for the whole (possibly ~2GB) file to finish -- the streaming
        # loop is what makes mid-transfer cancellation possible at all.
        cookies = {c["name"]: c["value"] for c in self.context.cookies()}
        timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_MS / 1000)
        logger.debug("Streaming GET %s (timeout=%dms)", url, DOWNLOAD_TIMEOUT_MS)
        written = 0
        try:
            with httpx.Client(
                transport=self._httpx_transport, follow_redirects=True, timeout=timeout, cookies=cookies
            ) as http:
                with http.stream("GET", url, headers=self._extra_headers) as resp:
                    if resp.status_code != 200:
                        snippet = resp.read()[:300].decode("utf-8", "replace")
                        logger.warning("Download request failed for art %s: HTTP %s", art_id, resp.status_code)
                        raise LitresAuthError(
                            f"Download failed for art {art_id} ({resp.status_code}): {snippet}"
                        )
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                            if should_cancel is not None and should_cancel():
                                raise DownloadCancelled(f"Download cancelled mid-transfer for art {art_id}")
                            f.write(chunk)
                            written += len(chunk)
        except DownloadCancelled:
            dest.unlink(missing_ok=True)  # discard the partial file
            logger.info("Cancelled download of art %s mid-transfer (%d bytes discarded)", art_id, written)
            raise
        logger.debug("Streamed %d bytes to %s", written, dest)
        return dest
