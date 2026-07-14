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
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.sync_api import BrowserContext, sync_playwright

# curl_cffi lets the (streaming) download carry a real Chrome TLS/JA3+JA4
# fingerprint, so it matches the Chromium session that solved DDoS-Guard's
# challenge -- a plain httpx client has a Python/OpenSSL fingerprint that,
# even with valid __ddg* cookies, can be re-challenged/403'd (see the
# download path below). Optional: if it isn't importable we fall back to
# httpx so the app still works, just with a less browser-like fingerprint.
try:
    from curl_cffi import requests as cffi_requests

    _CURL_CFFI_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only where curl_cffi is absent
    cffi_requests = None
    _CURL_CFFI_AVAILABLE = False

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

# Retry/backoff for transient anti-bot blocks (DDoS-Guard 403 / 429 / 503).
# A block is soft: waiting a moment (and re-warming the __ddg* cookies via a
# page visit) usually clears it, so we retry rather than failing the item and
# marching on -- marching on is exactly the hammering pattern that escalates a
# soft block into a hard one. A genuine litres 403 (rights-limited book) is
# NOT a DDoS-Guard block and is not retried (see _is_block).
MAX_TRANSIENT_RETRIES = int(os.environ.get("LITRES_MAX_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.environ.get("LITRES_RETRY_BASE_DELAY", "2.0"))
RETRY_MAX_DELAY = float(os.environ.get("LITRES_RETRY_MAX_DELAY", "30.0"))
# Statuses worth retrying *when accompanied by a DDoS-Guard signature* (403)
# or on their own (429/503 are always transient rate-limit/unavailable).
_ALWAYS_RETRY_STATUS = {429, 503}

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


class LitresBlocked(LitresAuthError):
    """An anti-bot check (DDoS-Guard) or rate limit turned us away -- a
    *transient* block distinct from a real auth failure or a rights-limited
    book. Subclasses LitresAuthError so existing callers still treat it as a
    failure, but the retry layer can catch it specifically. `retry_after` is
    the server's Retry-After hint in seconds when it sent one."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class DownloadCancelled(RuntimeError):
    """Raised by download_file when cancellation is requested mid-transfer --
    distinct from a real failure so the caller can treat it as a clean stop."""


def _is_ddos_guard(headers, body: bytes = b"") -> bool:
    """True if a response looks like a DDoS-Guard challenge/block rather than
    litres.ru's own reply. DDoS-Guard fronts the site, so its responses carry
    a `Server: ddos-guard` header (and its challenge HTML mentions it); a
    genuine litres 403 (e.g. a rights-limited book) does neither."""
    server = ""
    try:
        server = (headers.get("server") or headers.get("Server") or "").lower()
    except Exception:
        server = ""
    if "ddos-guard" in server:
        return True
    return b"ddos-guard" in (body[:2048] or b"").lower()


def _is_block(status: int, headers=None, body: bytes = b"") -> bool:
    """Whether a status should be treated as a transient anti-bot/rate-limit
    block worth retrying: 429/503 always, 403 only when it carries a
    DDoS-Guard signature (a bare 403 is usually a rights-limited item)."""
    if status in _ALWAYS_RETRY_STATUS:
        return True
    if status == 403:
        return _is_ddos_guard(headers or {}, body)
    return False


def _retry_after_seconds(headers) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds form) into a float, or None."""
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None  # HTTP-date form is rare here; fall back to our own backoff


def _backoff_delay(attempt: int, retry_after: Optional[float]) -> float:
    """Delay before retry `attempt` (0-based): honor Retry-After if given,
    else exponential (RETRY_BASE_DELAY * 2**attempt) capped at RETRY_MAX_DELAY,
    plus jitter. Jitter matters twice over: it avoids a thundering-herd retry
    and makes our timing look less mechanically scripted to the anti-bot."""
    if retry_after is not None:
        base = min(retry_after, RETRY_MAX_DELAY)
    else:
        base = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    return base + random.uniform(0, min(1.0, base * 0.25))


def _sleep_interruptible(seconds: float, should_cancel=None) -> bool:
    """Sleep up to `seconds`, checking should_cancel() ~10x/sec so a backoff
    pause doesn't swallow a Stop. Returns True if it slept the whole time,
    False if cancellation was requested partway through."""
    if should_cancel is None:
        time.sleep(seconds)
        return True
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_cancel():
            return False
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return True


# Headers the curl_cffi Chrome impersonation should own, so the browser-shaped
# headers (User-Agent, client hints, encoding) stay internally consistent with
# the impersonated TLS/JA4 fingerprint rather than being overwritten by ones
# captured from a possibly different Chromium build. Auth/app headers
# (app-id, session-id, ...) are still forwarded.
_IMPERSONATE_OWNS = {
    "user-agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "accept-encoding",
}


class _StreamResp:
    """A tiny transport-agnostic view over a streamed HTTP response, so the
    download loop is identical whether the bytes come from curl_cffi (real
    Chrome fingerprint, production) or httpx (test MockTransport / fallback).
    `iter_chunks(size)` yields body chunks; `read_all()` buffers the whole
    body (only used to snippet an error/challenge page)."""

    def __init__(self, status_code, headers, iter_chunks, read_all):
        self.status_code = int(status_code)
        self.headers = headers
        self.iter_chunks = iter_chunks
        self.read_all = read_all


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

    @staticmethod
    def _resp_body(resp) -> bytes:
        """Best-effort body bytes from a Playwright APIResponse for block
        detection / error snippets -- never raises."""
        try:
            return resp.body() or b""
        except Exception:
            return b""

    def _get_retrying(self, url: str, *, should_cancel=None, **kwargs):
        """GET through the browser request context, retrying transient
        anti-bot blocks (DDoS-Guard 403 / 429 / 503) with jittered backoff and
        a __ddg* cookie re-warm between tries. Returns the APIResponse; the
        caller still handles ordinary (non-block) error statuses. Raises
        LitresBlocked if still blocked after MAX_TRANSIENT_RETRIES."""
        resp = self._get(url, **kwargs)
        for attempt in range(MAX_TRANSIENT_RETRIES):
            if resp.ok:
                return resp
            body = self._resp_body(resp)
            if not _is_block(resp.status, resp.headers, body):
                return resp  # ordinary error -- let the caller shape the message
            delay = _backoff_delay(attempt, _retry_after_seconds(resp.headers))
            logger.warning(
                "Anti-bot block on GET %s (HTTP %s, server=%s) -- retry %d/%d in %.1fs",
                url, resp.status, (resp.headers or {}).get("server", "?"),
                attempt + 1, MAX_TRANSIENT_RETRIES, delay,
            )
            self._rewarm_cookies()
            _sleep_interruptible(delay, should_cancel)
            resp = self._get(url, **kwargs)
        if not resp.ok and _is_block(resp.status, resp.headers, self._resp_body(resp)):
            raise LitresBlocked(
                f"Blocked by litres.ru anti-bot/rate-limit ({resp.status}) after "
                f"{MAX_TRANSIENT_RETRIES} retries -- wait a bit and try again",
                retry_after=_retry_after_seconds(resp.headers),
            )
        return resp

    def _rewarm_cookies(self) -> None:
        """Refresh the DDoS-Guard __ddg* cookies by visiting the site in the
        real browser (the same trick as _recapture_headers). The cookies
        rotate by time/request-count and are bound to the browser that
        obtained them, so a fresh visit after a block hands the retry a clean,
        consistent set. Best-effort: any failure just leaves the old cookies."""
        try:
            page = self.context.new_page()
        except Exception as exc:  # tests' fake context, or a closed browser
            logger.debug("Cookie re-warm could not open a page: %s", exc)
            return
        try:
            page.goto(LOGIN_PAGE, wait_until="networkidle", timeout=20000)
            logger.info("Re-warmed DDoS-Guard cookies via a page visit")
        except Exception as exc:
            logger.debug("Cookie re-warm navigation failed: %s", exc)
        finally:
            try:
                page.close()
            except Exception:
                pass

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
            resp = self._get_retrying(url, params=params)
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
            # Personal-use client, not a scraper -- don't hammer the API, and
            # jitter the gap so the cadence doesn't look mechanically scripted.
            time.sleep(random.uniform(0.3, 0.7))

    def get_files(self, art_id, should_cancel=None) -> list:
        """Flat list of {id, extension, file_type, mime, size, is_additional} for one art."""
        resp = self._get_retrying(f"{API_BASE}/arts/{art_id}/files/grouped", should_cancel=should_cancel)
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

    @contextmanager
    def _http_stream(self, url: str, headers: dict, cookies: dict, timeout_s: float):
        """Open a streaming GET and yield a transport-agnostic `_StreamResp`.

        Production uses curl_cffi impersonating Chrome, so the download's
        TLS/JA3+JA4 fingerprint matches the Chromium session that solved
        DDoS-Guard's challenge -- a plain-Python fingerprint, even with valid
        __ddg* cookies, is a common cause of download-only 403s. Tests inject
        an httpx MockTransport (`self._httpx_transport`) to run offline, and if
        curl_cffi isn't importable we fall back to plain httpx (works, just a
        less browser-like fingerprint). Either way the caller's streaming loop
        is identical. Whole-audiobook bundles can be ~2GB, so we never buffer
        the body -- both engines stream in chunks."""
        use_httpx = self._httpx_transport is not None or not _CURL_CFFI_AVAILABLE
        if use_httpx:
            timeout = httpx.Timeout(timeout_s)
            with httpx.Client(
                transport=self._httpx_transport, follow_redirects=True, timeout=timeout, cookies=cookies
            ) as http:
                with http.stream("GET", url, headers=headers) as resp:
                    yield _StreamResp(
                        resp.status_code,
                        resp.headers,
                        lambda size: resp.iter_bytes(chunk_size=size),
                        lambda: resp.read(),
                    )
        else:
            # Let the impersonation own the browser-shaped headers so they stay
            # consistent with its TLS/JA4 fingerprint; still forward auth/app
            # headers (app-id, session-id, ...).
            fwd = {k: v for k, v in headers.items() if k.lower() not in _IMPERSONATE_OWNS}
            with cffi_requests.Session() as s:
                resp = s.get(
                    url, headers=fwd, cookies=cookies, impersonate="chrome",
                    timeout=timeout_s, stream=True, allow_redirects=True,
                )
                try:
                    yield _StreamResp(
                        resp.status_code,
                        resp.headers,
                        lambda size: resp.iter_content(chunk_size=size),
                        lambda: resp.content,
                    )
                finally:
                    resp.close()

    def _download_once(self, url, headers, cookies, dest, should_cancel, on_progress, art_id) -> Path:
        """One streaming attempt: raises LitresBlocked on an anti-bot/rate-limit
        block (so the caller can back off and retry), LitresAuthError on any
        other bad status, DownloadCancelled if Stop fires mid-transfer (the
        partial file is discarded), else streams to `dest` and returns it."""
        written = 0
        timeout_s = DOWNLOAD_TIMEOUT_MS / 1000
        try:
            with self._http_stream(url, headers, cookies, timeout_s) as resp:
                if resp.status_code != 200:
                    body = resp.read_all() or b""
                    server = (resp.headers or {}).get("server", "?")
                    if _is_block(resp.status_code, resp.headers, body):
                        logger.warning(
                            "Download blocked for art %s: HTTP %s (server=%s)",
                            art_id, resp.status_code, server,
                        )
                        raise LitresBlocked(
                            f"Download blocked for art {art_id} ({resp.status_code})",
                            retry_after=_retry_after_seconds(resp.headers),
                        )
                    snippet = body[:300].decode("utf-8", "replace")
                    logger.warning(
                        "Download request failed for art %s: HTTP %s (server=%s)",
                        art_id, resp.status_code, server,
                    )
                    exc = LitresAuthError(
                        f"Download failed for art {art_id} ({resp.status_code}): {snippet}"
                    )
                    exc.status = resp.status_code  # lets the caller try the alternate endpoint on a 403
                    raise exc
                total = int((resp.headers or {}).get("content-length") or 0) or None
                with open(dest, "wb") as f:
                    for chunk in resp.iter_chunks(1024 * 1024):
                        if should_cancel is not None and should_cancel():
                            raise DownloadCancelled(f"Download cancelled mid-transfer for art {art_id}")
                        f.write(chunk)
                        written += len(chunk)
                        if on_progress is not None:
                            on_progress(written, total)
        except DownloadCancelled:
            dest.unlink(missing_ok=True)  # discard the partial file
            logger.info("Cancelled download of art %s mid-transfer (%d bytes discarded)", art_id, written)
            raise
        logger.debug("Streamed %d bytes to %s", written, dest)
        return dest

    def _download_url_with_backoff(self, url, dest, should_cancel, on_progress, art_id) -> Path:
        """Stream one URL to `dest`, retrying transient anti-bot blocks
        (DDoS-Guard 403 / 429 / 503) with jittered backoff and a __ddg* cookie
        re-warm between tries -- rather than failing and moving on, the very
        pattern that hardens a soft block. Re-raises LitresBlocked once retries
        are exhausted, LitresAuthError (with .status) on an ordinary bad status,
        DownloadCancelled on Stop."""
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            cookies = {c["name"]: c["value"] for c in self.context.cookies()}
            try:
                return self._download_once(
                    url, dict(self._extra_headers), cookies, dest, should_cancel, on_progress, art_id
                )
            except LitresBlocked as exc:
                if attempt >= MAX_TRANSIENT_RETRIES:
                    logger.warning("Download still blocked for art %s after %d attempts", art_id, attempt + 1)
                    raise
                delay = _backoff_delay(attempt, exc.retry_after)
                logger.warning(
                    "Download blocked for art %s -- retry %d/%d in %.1fs",
                    art_id, attempt + 1, MAX_TRANSIENT_RETRIES, delay,
                )
                self._rewarm_cookies()  # fresh __ddg* cookies for the retry
                if not _sleep_interruptible(delay, should_cancel):
                    raise DownloadCancelled(f"Download cancelled during backoff for art {art_id}")
        raise LitresBlocked(f"Download blocked for art {art_id}")  # unreachable, keeps type-checkers happy

    def download_file(
        self, art_id, release_file_id, filename: str, dest: Path, subscr: bool = False,
        should_cancel=None, on_progress=None,
    ) -> Path:
        """Stream a file to `dest`, handling both ways litres.ru can turn us
        away without the user having to intervene:

        - **Anti-bot blocks** (DDoS-Guard 403 / 429 / 503) are retried with
          backoff + a cookie re-warm (see _download_url_with_backoff).
        - **Endpoint mismatch:** litres serves purchases from `download_book`
          and subscription/abonement titles from `download_book_subscr`. We
          can't tell which a given art needs up front, so if the first endpoint
          returns a plain 403 (litres itself refusing, *not* an anti-bot block)
          we automatically try the other one before giving up. `subscr` just
          picks which to try first.

        `should_cancel` interrupts an in-flight transfer/backoff within ~one
        chunk; `on_progress(written, total)` reports live byte progress."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        segments = (
            ["download_book_subscr", "download_book"] if subscr
            else ["download_book", "download_book_subscr"]
        )
        last_exc = None
        for i, segment in enumerate(segments):
            url = f"{DOWNLOAD_BASE}/{segment}/{art_id}/{release_file_id}/{filename}"
            logger.debug("Streaming GET %s (timeout=%dms)", url, DOWNLOAD_TIMEOUT_MS)
            try:
                return self._download_url_with_backoff(url, dest, should_cancel, on_progress, art_id)
            except LitresBlocked:
                raise  # a persistent anti-bot block won't be fixed by the other endpoint
            except LitresAuthError as exc:
                last_exc = exc
                # Only a plain 403 (litres refusing this endpoint) is worth
                # trying the alternate endpoint for; other errors (500, timeout)
                # it wouldn't fix either.
                if getattr(exc, "status", None) == 403 and i + 1 < len(segments):
                    logger.info(
                        "Endpoint '%s' refused art %s (403) -- trying '%s'",
                        segment, art_id, segments[i + 1],
                    )
                    continue
                raise
        raise last_exc  # pragma: no cover - loop always returns or raises above
