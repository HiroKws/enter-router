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

import atexit
import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from enter_router.core.config import LoggingConfig

LOGGER_NAME = "enter_router"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"

#: Kept open for the process lifetime: faulthandler writes to the file object,
#: so it must not be garbage collected (and must not be a rotating handler).
_crash_file = None
_hooks_installed = False
#: Above this size the crash log is truncated before it is opened again.
CRASH_LOG_MAX_BYTES = 256 * 1024


class _ConsoleHandler(logging.StreamHandler):
    """stderr handler used by --dry-run/--once (never installed for pythonw)."""


def _install_process_hooks(log_dir: str) -> None:
    """Make a dying process leave a trace (there is no console under pythonw).

    Two gaps this closes, both of which look like "the tool stopped by itself"
    and leave *nothing* in the decision log:

    * a hard crash (an access violation inside a ctypes callback, a stack
      overflow): ``faulthandler`` dumps every thread's stack to ``crash.log``;
    * an uncaught exception in the hook/tray/worker thread: the default handler
      writes to ``sys.stderr``, which is ``None`` under pythonw — so the thread
      died silently. ``threading.excepthook`` puts it in the normal log instead.

    The atexit line distinguishes an orderly exit from a kill (``TerminateProcess``
    and ``os._exit`` skip atexit): "no 「process exiting」 line after the last
    decision" means the process was ended from outside.
    """
    global _crash_file, _hooks_installed
    log_dir = log_dir or "."
    if not _hooks_installed:
        def thread_hook(args) -> None:
            logging.getLogger(LOGGER_NAME).critical(
                "thread %s died: %s: %s",
                getattr(args.thread, "name", "?"), args.exc_type.__name__, args.exc_value,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        threading.excepthook = thread_hook
        atexit.register(lambda: logging.getLogger(LOGGER_NAME).info(
            "process exiting (atexit)"))
        _hooks_installed = True
    if _crash_file is not None:
        return
    path = os.path.join(log_dir, "crash.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > CRASH_LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        _crash_file = open(path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_crash_file, all_threads=True)
        _crash_file.write(f"\n--- enter-router crash log opened ---\n")
    except (OSError, ValueError):
        _crash_file = None


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
    _install_process_hooks(log_dir)
    return path
