"""Config hot-reload (design §4.1/§4.4).

Poll mtime+size every 1000ms. On change, re-parse and validate **off the hook
thread**; on success hand the new Config to ``on_config`` (the app swaps it and
calls ``policy.reconfigure``); on ConfigError keep the OLD config, log the error
and notify. A schema_version bump is rejected like any other invalid file, so
"reload" never means "restart needed" and never means "half-applied".

The watcher is deliberately dumb: it never touches the policy machine itself.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

from enter_router.core.config import Config, ConfigError, parse_config_text

log = logging.getLogger("enter_router.config")


class ConfigWatcher:
    def __init__(self, path: str, on_config: Callable[[Config], None],
                 on_error: Callable[[str], None] | None = None,
                 interval_s: float = 1.0) -> None:
        self._path = path
        self._on_config = on_config
        self._on_error = on_error
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._signature = self._stat_signature()
        self.errors = 0
        self.reloads = 0

    # -- helpers ----------------------------------------------------------
    def _stat_signature(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self._path)
            return int(st.st_mtime_ns), int(st.st_size)
        except OSError:
            return None

    def poll_once(self) -> bool:
        """Return True when a reload was applied. Used by tests and the loop."""
        signature = self._stat_signature()
        if signature is None or signature == self._signature:
            return False
        self._signature = signature
        try:
            with open(self._path, encoding="utf-8") as handle:
                text = handle.read()
            config = parse_config_text(text)
        except ConfigError as e:
            self.errors += 1
            log.error("config_invalid kept_old_config errors=%d reason=%s",
                      self.errors, e)
            if self._on_error is not None:
                try:
                    self._on_error(str(e))
                except BaseException:
                    log.exception("config error callback failed")
            return False
        except OSError as e:
            self.errors += 1
            log.error("config_unreadable kept_old_config reason=%s", e)
            return False
        self.reloads += 1
        log.info("config_reloaded path=%s warnings=%d", self._path, len(config.warnings))
        try:
            self._on_config(config)
        except BaseException:
            log.exception("on_config callback failed — keeping the previous config")
            return False
        return True

    # -- lifecycle --------------------------------------------------------
    def start(self) -> "ConfigWatcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="config-watcher",
                                            daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.poll_once()
            except BaseException:
                log.exception("config watcher loop failed")

    def stop(self) -> None:
        self._stop.set()


def start(path, on_config, on_error=None) -> ConfigWatcher:
    """Frozen signature: start watching `path`; returns the (now running) watcher."""
    return ConfigWatcher(path, on_config, on_error).start()
