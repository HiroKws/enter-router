"""Watchdog thread + slow-callback accounting (design §6-2, §6-3).

Two independent safety nets:

1. **Stale heartbeat** — the hook loop thread beats every 200ms (and on every
   callback). If the app's watchdog polls ``watchdog_interval_ms`` and finds no
   beat for ``watchdog_stale_ms``, it calls ``on_trip`` which must flush the
   pending Enter (50ms budget, best effort), log, notify and ``os._exit(70)``.
   Process death makes the OS remove the hook, so the keyboard always recovers.

2. **Slow callbacks** — the hook reports each callback duration to
   ``SafetyMonitor.observe(ms)``. ``callback_overbudget_ms`` exceeded
   ``callback_overbudget_streak`` times in a row, or a single callback over
   ``callback_hard_limit_ms``, calls ``on_overbudget`` which must self-unhook
   *before* the OS does it silently (LowLevelHooksTimeout <= 1000ms).

Both are pure bookkeeping; the decisions live in the callbacks the app wires.
"""
from __future__ import annotations

import logging
import threading
import time

from enter_router.core.config import SafetyConfig

log = logging.getLogger("enter_router.watchdog")


class SafetyMonitor:
    def __init__(self, safety: SafetyConfig, on_overbudget,
                 clock=time.monotonic) -> None:
        self._safety = safety
        self._on_overbudget = on_overbudget
        self._clock = clock
        self._last_beat_ms = int(clock() * 1000)
        self._streak = 0
        self.tripped = False
        self.slow_total = 0

    # -- hook thread calls ------------------------------------------------
    def heartbeat(self) -> None:
        self._last_beat_ms = int(self._clock() * 1000)

    def update(self, safety: SafetyConfig) -> None:
        """Reload the parameters in place.

        The hook thread and the watchdog thread both hold this one object, so a
        config reload must mutate it: replacing the monitor (as the app used to)
        left the hook reporting to the old instance and the new thresholds never
        took effect.
        """
        self._safety = safety
        self._streak = 0

    def observe(self, elapsed_ms: float) -> None:
        s = self._safety
        if elapsed_ms >= s.callback_budget_warn_ms:
            log.warning("hook_slow_callback elapsed_ms=%.1f budget=%d",
                        elapsed_ms, s.callback_budget_warn_ms)
        if elapsed_ms >= s.callback_overbudget_ms:
            self._streak += 1
            self.slow_total += 1
            log.error("hook_overbudget elapsed_ms=%.1f overbudget=%d streak=%d/%d",
                      elapsed_ms, s.callback_overbudget_ms, self._streak,
                      s.callback_overbudget_streak)
        elif elapsed_ms < s.callback_budget_warn_ms:
            self._streak = 0
        if self.tripped:
            return
        if elapsed_ms >= s.callback_hard_limit_ms:
            self._trip(f"hard_limit({elapsed_ms:.0f}ms>={s.callback_hard_limit_ms})")
        elif self._streak >= s.callback_overbudget_streak:
            self._trip(f"streak({self._streak}x>={s.callback_overbudget_ms}ms)")

    def _trip(self, reason: str) -> None:
        self.tripped = True
        log.error("hook_auto_unhook reason=%s", reason)
        try:
            self._on_overbudget(reason)
        except BaseException:
            log.exception("on_overbudget callback failed")

    # -- watchdog thread reads --------------------------------------------
    def last_beat_ms(self) -> int:
        return self._last_beat_ms


def start(heartbeat, interval_ms: int, stale_ms: int, on_trip,
          name: str = "watchdog") -> threading.Thread:
    """Poll ``heartbeat()`` (monotonic ms) and call ``on_trip`` when it is stale."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(max(0.05, interval_ms / 1000.0)):
            try:
                age = int(time.monotonic() * 1000) - int(heartbeat())
                if age >= stale_ms:
                    log.error("watchdog_trip stale_ms=%d threshold=%d", age, stale_ms)
                    on_trip()
                    return
            except BaseException:
                log.exception("watchdog loop failed")

    thread = threading.Thread(target=loop, name=name, daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.start()
    return thread


def stop(thread: threading.Thread | None) -> None:
    if thread is None:
        return
    event = getattr(thread, "stop_event", None)
    if event is not None:
        event.set()
