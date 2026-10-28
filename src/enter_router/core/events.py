"""Event and context value types shared across layers (pure, stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VK_RETURN = 0x0D

KeyKind = Literal["down", "up"]


@dataclass(frozen=True)
class KeyEvent:
    """One keyboard event as observed by the low-level hook (pre-queue).

    Timestamps are monotonic milliseconds supplied by the platform adapter;
    the pure core never reads a clock itself, so tests can inject exact times.
    """

    vk: int
    kind: KeyKind
    t_ms: int
    ctrl: bool = False
    shift: bool = False
    alt: bool = False
    win: bool = False
    injected: bool = False  # True for events we synthesized (SendInput)
    os_repeat: bool = False  # True when no keyup of this vk happened since last keydown

    @property
    def modified(self) -> bool:
        return self.ctrl or self.shift or self.alt or self.win

    @property
    def is_plain_enter_down(self) -> bool:
        return self.vk == VK_RETURN and self.kind == "down" and not self.modified


@dataclass(frozen=True)
class ForegroundInfo:
    """Snapshot of the foreground window at decision time (may be partial)."""

    process_name: str = ""  # e.g. "firefox.exe"; "" = unknown
    window_title: str = ""
    url: str | None = None  # browsers only; None = not readable (UIA Document Value)
    # Native window handle, so a sink can re-check / restore the exact window
    # (AiChatSink S3 takeover). Additive with a default: existing construction
    # and equality comparisons keep working; 0 = unknown.
    hwnd: int = 0


@dataclass(frozen=True)
class ImeState:
    """Result of the cross-process composition query.

    The query has THREE outcomes, never two (card t_dd246e5a): "the IME is
    composing" and "I could not find out" must never collapse into one value,
    because a failed probe used to look exactly like "no composition" and the
    product then silently did nothing.

    composing=True  -> uncommitted composition string exists (Enter belongs to the IME)
    composing=False -> readable, and no composition
    composing=None  -> query FAILED; the policy applies ``global.ime_unknown_policy``

    ``source`` names the probe that answered ("context" / "ime_wnd" /
    "gui_focus" / "gui_caret") and ``detail`` carries the API + error code when
    the answer is unknown, so the log can say WHY instead of just "unknown".
    Both are additive defaults: ``ImeState(composing=True)`` still compares equal
    to itself and existing call sites keep working.
    """

    composing: bool | None
    source: str = ""
    detail: str = ""

    @property
    def state(self) -> str:
        """One of ``"composing"`` / ``"not_composing"`` / ``"unknown"``."""
        if self.composing is None:
            return "unknown"
        return "composing" if self.composing else "not_composing"


TriggerKind = Literal["double_enter", "triple_enter", "hotkey"]


@dataclass(frozen=True)
class TriggerInfo:
    kind: TriggerKind
    count: int  # 1/2/3 for enter counts; 1 for hotkey
    profile_id: str
    t_ms: int
    hotkey: str | None = None  # e.g. "ctrl+alt+enter"
    # Layer-1 operation key and its meaning, carried to the sink so the sink
    # context matches design §5.1 ("trigger には op/meaning/profile_id を載せる").
    # Additive with defaults: existing construction keeps working.
    op: str = ""
    meaning: str = ""
