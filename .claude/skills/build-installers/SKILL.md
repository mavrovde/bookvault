---
name: build-installers
description: Build or debug the macOS .dmg, Windows Setup.exe, and Linux .AppImage desktop installers. Use when packaging fails, or when changing anything under packaging/.
---

# Desktop installers

Three installers, each built by its own workflow on a matching runner and
attached to the GitHub Release. All are **unsigned dev builds**, and Chromium
is fetched on first run rather than bundled.

| Platform | Build | Workflow | Host requirement |
|---|---|---|---|
| macOS | `packaging/macos/build.sh` | `desktop-macos.yml` | — (Gatekeeper: right-click → Open) |
| Windows | PyInstaller onedir + Inno Setup (`packaging/windows/BookVault.iss`) | `desktop-windows.yml` | WebView2 runtime |
| Linux | `packaging/linux/build-appimage.sh` | `desktop-linux.yml` | **WebKitGTK 4.1** |

Only macOS and Linux can be built locally on a Mac; Windows builds in CI.

## The shared entry point

`packaging/entry.py` runs before anything imports `bookvault_web`, and that
ordering is load-bearing: **every module reads its env vars at import time**,
so a path variable set afterwards has no effect. It sets:

- the per-OS app data dir — `~/Library/Application Support/BookVault`,
  `%LOCALAPPDATA%\BookVault`, `$XDG_DATA_HOME/BookVault`
- `LITRES_SESSION_FILE`, `LITRES_CACHE_FILE`, `LITRES_STATE_FILE`,
  `LITRES_DOWNLOAD_DIR` (all via `setdefault`, so a user override wins)
- `PLAYWRIGHT_BROWSERS_PATH` → the standard per-OS `ms-playwright` cache,
  shared with a normal Playwright install and surviving reinstalls

**Adding a new `LITRES_*` path variable means adding a `setdefault` here**, or
the frozen app writes into its working directory.

## Gotchas

- **WebKitGTK can't be bundled** into the AppImage — its multiprocess helpers
  use compile-time absolute paths. It's a host dependency; on Ubuntu 24.04+:
  `libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1 gir1.2-gtk-3.0`.
- **`import webview` is lazy**, inside `main()`. Importing it at module scope
  eagerly resolves a native GUI backend and fails on a headless CI runner —
  which would also break the Linux smoke test and the desktop unit tests.
- **The Linux workflow runs a headless xvfb smoke test** that boots the frozen
  app to prove the GTK/WebKit binding works. If it fails, the binding is broken
  even though PyInstaller succeeded.
- **The window owns the main thread**; uvicorn runs on a daemon background
  thread with `timeout_graceful_shutdown=5` so closing the window during a
  long `/download/file` response still reaches lifespan shutdown and closes
  Chromium instead of orphaning it.

## Testing

Desktop tests guard with `pytest.importorskip("bookvault_desktop")` so the
web/MCP CI (which doesn't install the desktop package) skips them. They cover
`_free_port`, `build_server`, `_wait_until_serving`, and `_splash_html` — none
import `webview`.
