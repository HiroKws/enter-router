"""Foreground window cache (design §1: "前景情報はキャッシュ").

The hook callback must never block, so it cannot ask Windows for the process
name / title / URL on every key press. A worker-side poller keeps a snapshot and
the hook reads it (microseconds).

* ``poll_foreground()`` (platform) supplies hwnd + process basename + title.
* The URL is only read for browser processes, is refreshed at most every
  ``url_refresh_s``, and stays ``None`` when it cannot be read. A URL-scoped
  profile then does not match — the safe side (design §4.3 "never guess").
* The hwnd is kept beside the ForegroundInfo because the sinks need it for the
  S3 foreground takeover (ForegroundInfo is a frozen core type).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

from enter_router.core.events import ForegroundInfo

log = logging.getLogger("enter_router.fgcache")

BROWSER_PROCESSES = frozenset({
    "firefox.exe", "chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe",
    "opera.exe", "librewolf.exe", "chromium.exe",
})


class ForegroundProvider(Protocol):
    def __call__(self) -> tuple[int, str, str]: ...


def _default_provider() -> tuple[int, str, str]:
    from enter_router.platform import hook
    return hook.poll_foreground()


def _default_url_provider(hwnd: int) -> str | None:
    from enter_router.platform import textcap_win
    return textcap_win.get_document_url(hwnd)


class ForegroundCache:
    def __init__(self, poll_interval_s: float = 0.2, url_refresh_s: float = 0.5,
                 provider: ForegroundProvider | None = None,
                 url_provider: Callable[[int], str | None] | None = None) -> None:
        self._poll_interval_s = poll_interval_s
        self._url_refresh_s = url_refresh_s
        self._provider = provider or _default_provider
        self._url_provider = url_provider or _default_url_provider
        self._lock = threading.Lock()
        self._hwnd = 0
        self._info = ForegroundInfo()
        self._url_hwnd = 0
        self._url_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.refresh()
        self._thread = threading.Thread(target=self._loop, name="fgcache", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_interval_s):
            try:
                self.refresh()
            except BaseException:
                log.exception("foreground cache refresh failed")

    # -- data -------------------------------------------------------------
    def refresh(self) -> None:
        hwnd, process, title = self._provider()
        url = self._info.url
        now = time.monotonic()
        if hwnd != self._url_hwnd or (now - self._url_at) >= self._url_refresh_s:
            url = None
            if process.lower() in BROWSER_PROCESSES:
                try:
                    url = self._url_provider(hwnd)
                except BaseException:
                    log.debug("URL lookup failed", exc_info=True)
                    url = None
            self._url_hwnd = hwnd
            self._url_at = now
        info = ForegroundInfo(process_name=process, window_title=title, url=url,
                              hwnd=hwnd)
        with self._lock:
            changed = (info.process_name, info.window_title) != (
                self._info.process_name, self._info.window_title)
            self._hwnd = hwnd
            self._info = info
        if changed:
            log.debug("foreground changed process=%s title_len=%d url=%s",
                      process, len(title or ""), bool(url))

    def snapshot(self) -> tuple[int, ForegroundInfo]:
        with self._lock:
            return self._hwnd, self._info

    def hwnd(self) -> int:
        with self._lock:
            return self._hwnd

    def current(self) -> ForegroundInfo:
        with self._lock:
            return self._info

    def same_window(self, fg: ForegroundInfo) -> bool:
        """Cheap identity check used by the sinks before injecting."""
        now = self.current()
        return (now.process_name == fg.process_name
                and now.window_title == fg.window_title)


class StaticForeground:
    """Test double / --dry-run helper: one fixed snapshot."""

    def __init__(self, hwnd: int = 0, info: ForegroundInfo | None = None) -> None:
        self._hwnd = hwnd
        self._info = info or ForegroundInfo()

    def __call__(self) -> tuple[int, str, str]:
        return self._hwnd, self._info.process_name, self._info.window_title
