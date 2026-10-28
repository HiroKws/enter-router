"""Capture-chain policy (pure part only).

The actual capture STEPS are Windows adapters (platform/textcap_win.py):
    uia_focused           read text of the focused element via UIA
    clipboard_select_copy Ctrl+A, Ctrl+C into a stash, then restore clipboard
This module owns the ordering/selection policy so it is testable on Linux.

Privacy (research R9): captured text lives in memory only, is handed to one
sink call, and is never written to the log. Only a short preview (up to
`preview_chars`) may appear in the user notification.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import CaptureConfig

CAPTURE_STEPS = ("uia_focused", "clipboard_select_copy")


@dataclass(frozen=True)
class CaptureOutcome:
    text: str | None       # None = capture failed or produced empty text
    step: str | None       # which step succeeded (for logging)
    detail: str = ""       # failure detail for logging (no typed text)


def validate_chain(chain: tuple[str, ...]) -> bool:
    return bool(chain) and all(s in CAPTURE_STEPS for s in chain)


def select_outcome(config: CaptureConfig, results: dict[str, str | None]) -> CaptureOutcome:
    """Pick the first successful step result in configured chain order.

    `results` maps step name -> captured text or None (step failed/skipped).
    Empty-string captures count as failures (nothing to route).
    """
    for step in config.chain:
        text = results.get(step)
        if text:
            return CaptureOutcome(text=text, step=step)
    return CaptureOutcome(text=None, step=None, detail="all capture steps failed or empty")
