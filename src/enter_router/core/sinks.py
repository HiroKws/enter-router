"""ISink contract and routing value types (pure, stdlib only).

Three v1 sinks implement this same interface and are interchangeable:
    ClipboardSink, AiChatSink, HermesSink
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .events import ForegroundInfo, TriggerInfo

# Failure taxonomy. HermesSink must be able to report all three of
# cli_spawn_failed / model_connection_failed / timeout separately (design.md 5.3).
FailureKind = Literal[
    "not_acceptable",        # can_accept() was False at send time
    "capture_failed",        # no text could be captured (upstream of sink)
    "injected_blocked",      # SendInput silently blocked (UIPI) / 0 events sent
    "focus_failed",          # foreground takeover (S3) failed
    "clipboard_busy",        # clipboard could not be claimed
    "cli_spawn_failed",      # wsl.exe/hermes could not be started
    "model_connection_failed",  # exit != 0 (e.g. invalid_params ~2s)
    "timeout",               # child exceeded timeout_sec (default 30)
    "empty_response",        # exit == 0 but stdout empty
    "internal_error",        # unexpected exception (must still be visible)
]

SinkStatus = Literal["ok", "skipped", "failed"]


@dataclass(frozen=True)
class SinkContext:
    trigger: TriggerInfo
    foreground: ForegroundInfo
    captured_text: str | None  # None when capture chain failed


@dataclass(frozen=True)
class SinkResult:
    sink: str
    status: SinkStatus
    failure: FailureKind | None = None
    message: str = ""  # human-readable, shown in notification; no typed text bodies
    data: dict[str, Any] = field(default_factory=dict)
    # HermesSink v2 extension point: if a session id (local-<64hex>) is
    # observed in CLI output, put it in data["session_id"]; v1 logs it only.

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class VerifyResult:
    sink: str
    ok: bool
    detail: str = ""


@runtime_checkable
class ISink(Protocol):
    """Contract every sink adapter implements.

    can_accept(): cheap, non-blocking precheck (window still foreground,
        sink enabled, dependency reachable). Must not raise; on doubt return False.
    send(): blocking call, executed on the worker thread (NEVER on the hook
        thread). Must never raise: convert any internal error into
        SinkResult(status="failed", failure="internal_error").
        On failure the caller (router) applies the fallback chain
        (ClipboardSink stash + notification + log) per design.md 5.4.
    verify(): reachability check for the settings-UI "test send" button.
    """

    name: str

    def can_accept(self, ctx: SinkContext) -> bool: ...

    def send(self, text: str, ctx: SinkContext) -> SinkResult: ...

    def verify(self) -> VerifyResult: ...
