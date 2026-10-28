"""Report an unreadable IME state instead of silencing it (card t_dd246e5a).

The composition probe answers with three values (``core/events.ImeState``):
composing / not_composing / unknown. ``unknown`` means "this process cannot tell
whether the user is in the middle of a conversion". The old build turned that
into a plain PASS and logged nothing, so to the user the product simply did
nothing — the exact defect this module removes.

Contract:

* the FIRST unknown of this process shows one notification (tray balloon/toast
  through ``platform.notify_win``). Once only: a repeating balloon becomes
  background noise the user learns to ignore, and the log carries the rest.
* every unknown is logged as ``ime_state=unknown action=<pass|process>`` together
  with the probe's ``detail`` (which API failed and with what Win32 error), so the
  log answers "why" without a debugger.
* while unknown keeps happening the LOG line is rate limited to one per
  ``log_interval_ms`` (60s default) and carries an occurrence counter. The
  decision itself is never rate limited — every Enter is still decided by the
  configured policy.

The reporter is pure stdlib and takes its clock and notifier by injection, so the
once-only and rate-limit behaviour is unit-testable off Windows.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("enter_router.ime")

NOTIFY_TITLE = "enter-router: IME の状態を判定できません"
LOG_INTERVAL_MS = 60_000


def body_for(action: str) -> str:
    """Notification body: what happened AND what the app just did about it."""
    if action == "process":
        return ("IME の変換状態を読み取れませんでした。設定 ime_unknown_policy=process のため、"
                "通常の Enter 判定を続けます（変換中の確定 Enter も数える可能性があります）。"
                "詳細はログの ime_state=unknown を参照してください。")
    return ("IME の変換状態を読み取れませんでした。設定 ime_unknown_policy=pass のため、"
            "Enter はそのまま通します（変換中の確定はアプリ/IME 側で行われます）。"
            "この通知は初回のみです。詳細はログの ime_state=unknown を参照してください。")


class ImeUnknownReporter:
    """Once-only notification + rate-limited log for ``ime_state=unknown``."""

    def __init__(self, notify: Callable[..., None] | None = None,
                 log_interval_ms: int = LOG_INTERVAL_MS,
                 clock: Callable[[], float] | None = None) -> None:
        self._notify = notify
        self._log_interval_ms = int(log_interval_ms)
        self._clock = clock or time.monotonic
        self._notified = False
        self._last_log_at: float | None = None
        self._occurrences = 0

    @property
    def occurrences(self) -> int:
        """How many unknown outcomes were reported (diagnostics/tests)."""
        return self._occurrences

    @property
    def notified(self) -> bool:
        return self._notified

    def report(self, action: str, detail: str = "", source: str = "") -> bool:
        """Record one unknown outcome; returns True when the user was notified."""
        self._occurrences += 1
        now = self._clock()
        due = (self._last_log_at is None
               or (now - self._last_log_at) * 1000.0 >= self._log_interval_ms)
        fields = (f"ime_state=unknown action={action} "
                  f"source={source or '-'} occurrences={self._occurrences} "
                  f"detail={detail or '-'}")
        if due:
            self._last_log_at = now
            log.warning("%s", fields)
        else:
            # Still recorded, just not repeated into the log every keystroke.
            log.debug("%s (suppressed)", fields)
        if self._notified:
            return False
        self._notified = True
        log.warning("ime_state=unknown notified to the user (first occurrence; "
                    "action=%s)", action)
        if self._notify is not None:
            try:
                self._notify(NOTIFY_TITLE, body_for(action), "warning")
            except BaseException:          # a notification never breaks the hook
                log.debug("ime unknown notification failed", exc_info=True)
        return True

    def reset(self) -> None:
        """Forget the once-only state (config reload / tests)."""
        self._notified = False
        self._last_log_at = None
        self._occurrences = 0
