"""Sink execution: capture chain + the mandatory fallback chain (design §5.4).

Two responsibilities, both OS-injected so they are testable on Linux:

``capture_text``      run the configured capture chain (core/capture.py owns the
                      ordering policy) and return a CaptureOutcome.
``run_with_fallback`` run one ``sink:<name>`` action and, on ANY failure, stash
                      the text on the clipboard + notify + log. "Silently did
                      nothing" is the failure mode this module exists to prevent.
``SinkWorker``        one worker thread with a small FIFO queue; the hook thread
                      only enqueues (design §1: フックを絶対にブロックしない).

The fallback is applied twice on purpose: HermesSink already stashes the prompt
inside ``send()`` (so a direct caller — the settings-UI test button — cannot lose
it), and the runner checks ``result.data["fallback"] == "clipboard"`` to avoid
stashing the same text twice.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from enter_router.core import capture as capture_policy
from enter_router.core import routing
from enter_router.core.capture import CaptureOutcome
from enter_router.core.config import CaptureConfig, Config
from enter_router.core.sinks import FailureKind, SinkContext, SinkResult

log = logging.getLogger("enter_router.sinks")


# ---------------------------------------------------------------------------
# capture chain
# ---------------------------------------------------------------------------
def _default_steps() -> dict[str, Callable[[], str | None]]:
    from enter_router.platform import textcap_win
    return {
        "uia_focused": textcap_win.read_focused_text,
        "clipboard_select_copy": lambda: textcap_win.clipboard_select_copy(restore=True),
    }


def capture_text(config: CaptureConfig,
                 steps: dict[str, Callable[[], str | None]] | None = None) -> CaptureOutcome:
    """Run the configured chain; the first non-empty step wins (core policy)."""
    steps = steps or _default_steps()
    results: dict[str, str | None] = {}
    for step in config.chain:
        fn = steps.get(step)
        if fn is None:
            results[step] = None
            continue
        try:
            results[step] = fn()
        except BaseException:
            log.exception("capture step %s raised", step)
            results[step] = None
    outcome = capture_policy.select_outcome(config, results)
    log.info("capture step=%s ok=%s len=%s", outcome.step or "-", bool(outcome.text),
             len(outcome.text or ""))
    return outcome


# ---------------------------------------------------------------------------
# one sink action + fallback
# ---------------------------------------------------------------------------
def _fallback(sinks: dict[str, object], text: str, ctx: SinkContext,
              reason: FailureKind,
              notify: Callable[[str, str, str], None] | None) -> SinkResult:
    """Stash the text on the clipboard, tell the user, log. Never raises.

    The clipboard write is asked for with ``notify=False`` when the sink is a
    ClipboardSink: this function sends the single user-visible notification for
    the failure, and two toasts for one failure is noise, not safety.
    """
    clipboard = sinks.get("clipboard")
    stashed = False
    if clipboard is not None and text:
        try:
            stash_fn = getattr(clipboard, "stash", None)
            if callable(stash_fn):
                result = stash_fn(text, notify=False)
            else:
                result = clipboard.send(text, ctx)      # type: ignore[attr-defined]
            stashed = bool(getattr(result, "ok", False))
        except BaseException:
            log.exception("fallback clipboard send raised")
    log.error("sink fallback reason=%s stashed=%s chars=%d", reason, stashed, len(text or ""))
    if notify is not None:
        notify("enter-router",
               "シンク送信に失敗しました。テキストはクリップボードに退避しました。"
               if stashed else
               "シンク送信に失敗し、退避にも失敗しました（ログ参照）。",
               "warning" if stashed else "error")
    # The stashed case keeps the ORIGINAL classification (focus_failed,
    # model_connection_failed, ...): "internal_error" hid the cause from
    # ``sink_finished`` while the clipboard fallback had in fact worked.
    return SinkResult(sink=reason, status="failed",
                      failure=reason if stashed else "clipboard_busy",
                      message=f"{reason} -> clipboard fallback"
                              if stashed else f"{reason} -> stash failed",
                      data={"fallback": "clipboard" if stashed else "none",
                            "reason": reason})


def run_with_fallback(config: Config, sinks: dict[str, object], resolved: str,
                      text: str, ctx: SinkContext,
                      notify: Callable[[str, str, str], None] | None = None) -> SinkResult:
    """Execute ``sink:<name>`` with the mandatory fallback. Never raises."""
    sink = routing.select_sink(config, sinks, resolved)   # type: ignore[arg-type]
    if sink is None:
        return _fallback(sinks, text, ctx, "not_acceptable", notify)
    try:
        if not sink.can_accept(ctx):
            return _fallback(sinks, text, ctx, "not_acceptable", notify)
        result = sink.send(text, ctx)
    except BaseException:
        log.exception("sink=%s raised despite the ISink contract", getattr(sink, "name", "?"))
        return _fallback(sinks, text, ctx, "internal_error", notify)
    if result.ok:
        return result
    if result.data.get("fallback") == "clipboard":
        # the sink already stashed the payload itself (HermesSink); do not double-stash
        log.error("sink=%s failed failure=%s (payload already stashed)",
                  result.sink, result.failure)
        return result
    return _fallback(sinks, text, ctx, result.failure or "internal_error", notify)


# ---------------------------------------------------------------------------
# worker thread
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SinkJob:
    resolved: str
    ctx: SinkContext


class SinkWorker:
    """One daemon thread; the hook thread only calls ``submit``."""

    def __init__(self, config_provider: Callable[[], Config],
                 sinks_provider: Callable[[], dict[str, object]],
                 on_finished: Callable[[SinkResult], None],
                 notify: Callable[[str, str, str], None] | None = None,
                 capture_fn: Callable[[], CaptureOutcome] | None = None,
                 queue_size: int = 8) -> None:
        self._config_provider = config_provider
        self._sinks_provider = sinks_provider
        self._on_finished = on_finished
        self._notify = notify
        self._capture_fn = capture_fn
        self._queue: "queue.Queue[SinkJob | None]" = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.dropped = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="sink-worker", daemon=True)
        self._thread.start()

    def submit(self, job: SinkJob) -> bool:
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            self.dropped += 1
            log.error("sink queue full — job dropped (resolved=%s, dropped=%d)",
                      job.resolved, self.dropped)
            if self._notify is not None:
                self._notify("enter-router", "処理待ちが上限に達しました（1 件破棄）", "warning")
            return False

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout)

    def drain(self) -> int:
        """Process everything currently queued (used by tests/--once)."""
        processed = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return processed
            if job is None:
                return processed
            self._run_job(job)
            processed += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                break
            self._run_job(job)

    def _run_job(self, job: SinkJob) -> None:
        result: SinkResult
        try:
            config = self._config_provider()
            if job.ctx.captured_text is None:
                outcome = (self._capture_fn() if self._capture_fn is not None
                           else capture_text(config.capture))
                text = outcome.text
            else:
                text = job.ctx.captured_text
            if not text:
                # design §5.4-3: capture failure => the trigger is consumed, NEVER re-sent
                log.error("capture failed for resolved=%s — nothing sent", job.resolved)
                if self._notify is not None:
                    self._notify("enter-router",
                                 "入力テキストを取得できませんでした（送信せず破棄）", "warning")
                result = SinkResult(sink=job.resolved, status="skipped",
                                    failure="capture_failed",
                                    message="capture failed; nothing sent")
            else:
                ctx = SinkContext(trigger=job.ctx.trigger,
                                  foreground=job.ctx.foreground,
                                  captured_text=text)
                result = run_with_fallback(config, self._sinks_provider(), job.resolved,
                                           text, ctx, self._notify)
        except BaseException:
            log.exception("sink job crashed")
            result = SinkResult(sink="?", status="failed", failure="internal_error",
                                message="worker crashed")
        try:
            self._on_finished(result)
        except BaseException:
            log.exception("on_finished callback failed")
