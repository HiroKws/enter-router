"""AppController protocol — SHARED CONTRACT between backend and frontend cards.

The tray/settings UI (frontend card, ui/) talks ONLY to this protocol; it
never imports platform code or policy internals. The backend card implements
it in app/main.py (EnterRouterApp). Both cards may read this file; neither
may change it without noting the reason in the card handoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

RouterState = Literal["running", "paused", "panic", "busy"]


@dataclass(frozen=True)
class RouterStatus:
    state: RouterState
    hook_installed: bool
    config_path: str
    #: mtime of the config file, so the settings window can notice an EXTERNAL
    #: change (the registration hotkey writes a profile while it is open).
    config_mtime: float = 0.0
    config_warnings: tuple[str, ...] = ()
    last_error: str = ""          # short, no typed text
    last_sink_result: str = ""    # e.g. "hermes: ok (3.8s)" / "hermes: timeout -> clipboard fallback"


@runtime_checkable
class AppController(Protocol):
    def get_status(self) -> RouterStatus: ...

    def set_enabled(self, enabled: bool) -> None:
        """Pause/resume: uninstall/reinstall the hook. Resume is a full re-arm."""

    def panic_stop(self) -> None:
        """Immediate unhook (1 operation, also reachable from the panic hotkey).

        Design v2 §6-4 / invariant I7: the pending held Enter is delivered
        ORDER-PRESERVINGLY first (never silently dropped) and exactly once, then
        the hook is removed. Nothing is ever re-sent afterwards.
        Idempotent.
        """

    def reload_config(self) -> tuple[bool, str]:
        """Re-read config file. Returns (ok, message). On failure old config stays."""

    def test_send(self, sink_name: str, text: str) -> str:
        """Run one sink send with `text` (settings-UI test button).
        Returns a human-readable result line (success/failure reason)."""

    def open_log_dir(self) -> None: ...

    # -- additions for the settings UI (allowed by design.md §8) --------------
    def get_config_dict(self) -> dict:
        """Live config as a JSON-ready dict (the UI edits this, never re-invents it)."""
        ...

    def save_config_dict(self, data: dict) -> tuple[bool, str]:
        """Validate + write atomically; returns (ok, message). Old file survives errors."""
        ...
