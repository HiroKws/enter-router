"""UI runtime: one hidden Tk root on the main thread + the tray thread.

Wiring (design §1: main thread = message loop + tray + tkinter; the hook has its
own thread):

    main thread   UiRuntime.run_forever() -> tkinter mainloop, owns every widget
    tray thread   TrayIcon (Shell_NotifyIcon + its own message loop)
    tray click  -> command name -> queue.Queue -> Tk thread -> controller call

Why a queue and not a direct call: ``AppController`` methods must run on one
thread while tkinter is being used, otherwise the settings window and the click
can race on the document. The tray thread therefore only *names* a command; the
Tk thread performs it.

``on_icon_created`` exists so the composition root (``app/main.py``) can call
``platform.notify_win.register_icon(hwnd)``: the UI itself never imports
``platform/`` (design §2/§7).
"""
from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
from typing import Any, Callable

from .settings import SettingsWindow
from .tray import TrayIcon, status_tooltip, tray_available

log = logging.getLogger("enter_router.ui.runtime")

POLL_STATUS_MS = 500
POLL_COMMANDS_MS = 120


class UiRuntime:
    """Tray + settings window over an ``AppController``. Main-thread only."""

    def __init__(self, controller: Any, *,
                 on_icon_created: Callable[[int], None] | None = None,
                 on_quit: Callable[[], None] | None = None) -> None:
        self.controller = controller
        self.on_icon_created = on_icon_created
        self.on_quit = on_quit
        self.root: tk.Tk | None = None
        self.tray: TrayIcon | None = None
        self.settings: SettingsWindow | None = None
        self._commands: "queue.Queue[str]" = queue.Queue()
        self._running = False
        self._stopped = False
        self.tray_ok = False

    # -- lifecycle ------------------------------------------------------
    def start(self) -> bool:
        """Build the root + tray. Must be called from the main thread."""
        self.root = tk.Tk()
        self.root.withdraw()                       # hidden; only Toplevels show
        self.root.title("enter-router")
        self.root.report_callback_exception = self._on_callback_error
        self._running = True
        if tray_available():
            self.tray = TrayIcon(on_command=self._enqueue)
            self.tray_ok = self.tray.start()
            if self.tray_ok and self.on_icon_created is not None and self.tray.hwnd:
                try:
                    self.on_icon_created(self.tray.hwnd)
                except Exception:                  # noqa: BLE001 - notifications only
                    log.exception("tray registration callback failed")
            if not self.tray_ok:
                log.error("tray icon unavailable (%s) — opening the settings window "
                          "so the process is not invisible", self.tray.error)
                self.open_settings()
        else:
            log.warning("tray is Windows-only; the settings window runs standalone")
        self.refresh_now()
        self.root.after(POLL_COMMANDS_MS, self._poll_commands)
        self.root.after(POLL_STATUS_MS, self._poll_status)
        return self.tray_ok

    def run_forever(self) -> None:
        if self.root is None:
            raise RuntimeError("start() must be called first")
        try:
            self.root.mainloop()
        finally:
            self._teardown()

    def stop(self) -> None:
        """Idempotent shutdown; safe from any thread (queues a quit)."""
        if self._stopped:
            return
        self._stopped = True
        self._running = False
        root = self.root
        if root is not None:
            try:
                root.quit()
            except Exception:                  # pragma: no cover - already gone
                pass
        self._teardown()

    def _teardown(self) -> None:
        if self.settings is not None:
            self.settings.destroy()
            self.settings = None
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:                  # pragma: no cover - already gone
                pass
            self.root = None
        if self.on_quit is not None:
            callback, self.on_quit = self.on_quit, None
            try:
                callback()
            except Exception:                  # noqa: BLE001 - shutdown best effort
                log.exception("on_quit callback failed")

    # -- commands -------------------------------------------------------
    def _enqueue(self, command: str) -> None:
        self._commands.put(command)

    def _poll_commands(self) -> None:
        if not self._running or self.root is None:
            return
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle_command(command)
            except Exception:                  # noqa: BLE001 - never kill the loop
                log.exception("tray command %s failed", command)
        self.root.after(POLL_COMMANDS_MS, self._poll_commands)

    def handle_command(self, command: str) -> None:
        """The single dispatch point for tray menu items (also used by tests)."""
        log.info("ui command: %s", command)
        if command == "settings":
            self.open_settings()
            return
        if command == "panic":
            self.controller.panic_stop()
        elif command == "toggle":
            status = self.controller.get_status()
            self.controller.set_enabled(status.state in ("paused", "panic"))
        elif command == "logs":
            self.controller.open_log_dir()
        elif command == "quit":
            self.shutdown_app()
            return
        else:
            log.warning("unknown ui command %r", command)
            return
        self.refresh_now()

    def shutdown_app(self) -> None:
        """退出: stop the UI, then let the composition root stop the app."""
        self.stop()

    # -- status ---------------------------------------------------------
    def _poll_status(self) -> None:
        if not self._running or self.root is None:
            return
        try:
            self.refresh_now()
        except Exception:                      # noqa: BLE001 - status is best effort
            log.exception("status refresh failed")
        self.root.after(POLL_STATUS_MS, self._poll_status)

    def refresh_now(self) -> None:
        status = self.controller.get_status()
        state = getattr(status, "state", "running")
        tooltip = status_tooltip(state,
                                 config_path=getattr(status, "config_path", ""),
                                 warnings=len(getattr(status, "config_warnings", ()) or ()),
                                 last_error=getattr(status, "last_error", ""))
        if self.tray is not None:
            self.tray.refresh(state=state, tooltip=tooltip)
        if self.settings is not None:
            self.settings.refresh_status(status)

    # -- settings window ------------------------------------------------
    def open_settings(self) -> SettingsWindow | None:
        if self.root is None:
            return None
        if self.settings is not None and self.settings.win.winfo_exists():
            self.settings.focus()
            self.settings.refresh_status(self.controller.get_status())
            return self.settings
        self.settings = SettingsWindow(self.root, self.controller,
                                       on_close=self._on_settings_closed)
        self.settings.refresh_status(self.controller.get_status())
        self.settings.focus()
        return self.settings

    def _on_settings_closed(self) -> None:
        self.settings = None

    # -- helpers for scripts / self tests ------------------------------
    def pump(self, seconds: float) -> None:
        """Run the Tk event loop for a bounded time (deterministic scripts)."""
        if self.root is None:
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.root is not None:
            try:
                self.root.update()
            except tk.TclError:                # window already destroyed
                break
            time.sleep(0.02)

    def _on_callback_error(self, exc_type, exc_value, exc_tb) -> None:
        log.error("tkinter callback failed", exc_info=(exc_type, exc_value, exc_tb))
