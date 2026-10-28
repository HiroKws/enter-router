"""Logging setup (design.md §6-7).

RotatingFileHandler at ``%APPDATA%\\enter-router\\logs\\enter-router.log`` (or
``config.logging.dir``), ``max_bytes`` x ``backup_count``, level from config.
One line per event: ISO timestamp + level + component + key=value pairs.

PRIVACY RULE (research R9): never log typed text, captured text or clipboard
contents. Decisions, reasons, timings and sink results only — sinks log
character COUNTS, never the text. This is a caller discipline, not something the
handler can enforce, so the rule is repeated at every call site in app/.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from enter_router.core.config import LoggingConfig

LOGGER_NAME = "enter_router"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


class _ConsoleHandler(logging.StreamHandler):
    """stderr handler used by --dry-run/--once (never installed for pythonw)."""


def setup(cfg: LoggingConfig, appdata: str, console: bool = False) -> str:
    """Configure the 'enter_router' logger; returns the log file path."""
    log_dir = cfg.dir or os.path.join(appdata, "enter-router", "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "enter-router.log")

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(getattr(logging, str(cfg.level).upper(), logging.INFO))
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except BaseException:
            pass

    formatter = logging.Formatter(_FORMAT)
    file_handler = RotatingFileHandler(path, maxBytes=cfg.max_bytes,
                                       backupCount=cfg.backup_count, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = _ConsoleHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    root.info("logging started file=%s level=%s console=%s",
              path, cfg.level, console)
    return path
