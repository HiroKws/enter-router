"""Composition root: config -> sinks -> policy machine -> hook -> watchdog.

Threading model (design §1, one process, no admin rights):

    main thread    : this module's run() — waits, then tears everything down
    hook thread    : platform.hook.install() (message loop + LL callbacks)
    worker thread  : SinkWorker (capture + sink send; can block up to 30s)
    timer thread   : DeadlineTimer (fires machine.on_deadline for the hold window)
    fg thread      : ForegroundCache poller
    watchdog       : app.watchdog (heartbeat + self-exit)

The hook callback only does: query IME (cheap) -> machine.on_key -> execute the
outcome (synchronous SendInput for "key:"/"pass" deliveries, enqueue for sinks)
-> return swallow. Machine access is serialized by one lock so the timer thread
cannot interleave with a key event. Because a PASS decision executes nothing,
the callback also drains the hand-off queue *before* deciding: a held Enter the
timer thread released must reach the application ahead of the next key (I6).

Nothing in this module touches Win32 directly: every OS effect goes through
``platform/`` and every decision through ``core/``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from enter_router import __version__
from enter_router.app import config_watcher, dryrun, logsetup, watchdog
from enter_router.app.controller import RouterStatus
from enter_router.app.fgcache import ForegroundCache
from enter_router.app.ime_unknown import ImeUnknownReporter
from enter_router.app.sink_runner import SinkJob, SinkWorker
from enter_router.core import keys as keys_mod
from enter_router.core import routing
from enter_router.core.config import (Config, ConfigError, default_config_dict,
                                      parse_config, parse_config_text)
from enter_router.core.events import VK_RETURN, KeyEvent
from enter_router.core.policy import (Decision, EnterPolicyMachine, Outcome,
                                      State)
from enter_router.core.sinks import SinkContext, SinkResult
from enter_router.platform.sinks_win import build_sinks

log = logging.getLogger("enter_router.app")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_WATCHDOG = 70


# ---------------------------------------------------------------------------
# small infrastructure
# ---------------------------------------------------------------------------
def appdata_dir() -> str:
    """%APPDATA% on Windows, ~/.config elsewhere (so --once/--dry-run work in WSL)."""
    return (os.environ.get("APPDATA")
            or os.environ.get("XDG_CONFIG_HOME")
            or str(Path.home() / ".config"))


def default_config_path() -> str:
    return os.path.join(appdata_dir(), "enter-router", "config.json")


def load_config(path: str | None) -> tuple[Config, str]:
    """Load config from `path` (or the default), creating the default file once."""
    target = path or default_config_path()
    if not os.path.exists(target):
        if path:
            raise ConfigError(f"config file not found: {path}")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(default_config_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            log.info("created default config at %s", target)
        except OSError as e:
            log.error("could not create %s (%s) — using built-in defaults", target, e)
            return parse_config(default_config_dict()), target
    with open(target, encoding="utf-8") as handle:
        return parse_config_text(handle.read()), target


class DeadlineTimer:
    """One timer thread; the machine's deadline is the only schedule."""

    def __init__(self, on_due) -> None:
        self._on_due = on_due
        self._cond = threading.Condition()
        self._due_ms: int | None = None
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="deadline", daemon=True)
        self._thread.start()

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)

    def set(self, due_ms: int | None) -> None:
        with self._cond:
            self._due_ms = due_ms
            self._cond.notify_all()

    def cancel(self) -> None:
        self.set(None)

    def stop(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                if not self._running:
                    return
                if self._due_ms is None:
                    self._cond.wait(0.5)
                    continue
                remaining = self._due_ms - self._now_ms()
                if remaining > 0:
                    self._cond.wait(min(remaining / 1000.0, 0.5))
                    continue
                self._due_ms = None
            try:
                self._on_due()
            except BaseException:
                log.exception("deadline handler failed")


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------
class EnterRouterApp:
    """Implements app.controller.AppController plus the hook wiring."""

    def __init__(self, config: Config, config_path: str, logger_ready: bool = True) -> None:
        self._lock = threading.RLock()
        # Injection is deliberately NOT serialized by self._lock: SendInput from
        # a background thread waits for the LL hook callback, so a lock held
        # across SendInput deadlocks the hook until the OS timeout (measured
        # 617ms). While the hook is installed only the hook thread injects; the
        # queue below is how other threads hand it work (review round 1, item 2).
        self._inject_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._pending_inject: list[Outcome] = []
        self._cfg = config
        self._config_path = config_path
        self._state = "paused"
        self._hook_installed = False
        self._hook_thread: threading.Thread | None = None
        self._last_error = ""
        self._last_sink_result = ""
        self._panic_done = False
        self._panic_notify = True
        self._stopped = threading.Event()

        self._fg = ForegroundCache()

        def notify(title: str, body: str, level: str = "info") -> None:
            from enter_router.platform import notify_win
            notify_win.notify(title, body, level)  # type: ignore[arg-type]

        self._notify = notify
        # "ime_state=unknown" must never be silent (card t_dd246e5a): one
        # notification for the first occurrence, then a rate-limited log line.
        self._ime_unknown = ImeUnknownReporter(notify=notify)
        self._sinks: dict[str, object] = build_sinks(config, notify=notify)
        self._machine = self._make_machine(config)
        self._worker = SinkWorker(
            config_provider=lambda: self._cfg,
            sinks_provider=lambda: self._sinks,
            on_finished=self._on_sink_finished,
            notify=notify,
        )
        self._timer = DeadlineTimer(self._on_deadline)
        self._monitor = watchdog.SafetyMonitor(config.safety, self._on_overbudget)
        self._watchdog_thread: threading.Thread | None = None
        self._watcher: config_watcher.ConfigWatcher | None = None

    # -- machine plumbing -------------------------------------------------
    def _resolve(self, op: str) -> str:
        _hwnd, fg = self._fg.snapshot()
        return routing.resolve_operation(self._cfg, fg, op)

    def _meaning(self, op: str) -> str | None:
        return routing.lookup_meaning(self._cfg, op)

    def _make_machine(self, config: Config) -> EnterPolicyMachine:
        return EnterPolicyMachine(config.policy, self._resolve,
                                  meaning_of=self._meaning,
                                  ime_unknown_policy=config.ime_unknown_policy)

    def _now_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def _sync_timer(self) -> None:
        due = self._machine.deadline_ms
        if due is None:
            self._timer.cancel()
        else:
            self._timer.set(due)

    # -- hook callback (hook thread) --------------------------------------
    def _decide(self, ev: KeyEvent) -> bool:
        started = time.perf_counter()
        try:
            if ev.injected:
                # rule0 (I4, 削除禁止): our own synthesised keys are never
                # processed. Answering before the lock also keeps the hook thread
                # from ever waiting on a background thread's state lock while its
                # own SendInput is being delivered (review round 1, item 2).
                log.debug("injected passthrough vk=0x%02X %s", ev.vk, ev.kind)
                return False
            # I6 ordering (review round 2): a delivery another thread handed over
            # — the held Enter the timer thread released when its window expired —
            # must reach the application BEFORE this key. Draining here, and not
            # only in _execute(), is what prevents "the char, then the newline":
            # a PASS decision executes nothing, so without this the queued Enter
            # would be injected after the key we are about to let through.
            self._drain_before_key()
            registration = self._registration_kind(ev)
            if registration is not None:
                # v2-g: a registration hotkey writes a profile for the foreground
                # app. It is not an Enter decision, so it never reaches the policy
                # machine — only the "deliver a held Enter first" step does (I6).
                self._register_foreground(registration, ev.t_ms)
                return True
            ime = None
            if ev.vk == VK_RETURN and ev.kind == "down":
                from enter_router.platform import ime as ime_mod
                ime = ime_mod.query_composition(self._fg.hwnd())
                if ime.composing is None:
                    # Three-valued probe, the third answer is "I could not read
                    # it". Say so (log always, notification once) — the old code
                    # turned this into an unlogged pass-through (t_dd246e5a).
                    self._ime_unknown.report(self._cfg.ime_unknown_policy,
                                             detail=ime.detail, source=ime.source)
            with self._lock:
                out = self._machine.on_key(ev, ime)
                self._sync_timer()
            # Every OS effect runs OUTSIDE the state lock. This callback is what
            # a background SendInput waits for, so holding the lock while an
            # injector needs it stalls the keyboard (review round 1, item 2).
            self._execute(out, ev)
            return out.swallow
        except BaseException:
            # fail-open: the key reaches the app, the hook survives (§6-1)
            log.exception("decide failed — passing the key through")
            self._last_error = "decide exception (passed through)"
            return False
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            if elapsed > self._cfg.safety.callback_budget_warn_ms:
                log.warning("hook_slow_callback elapsed_ms=%.1f budget=%d",
                            elapsed, self._cfg.safety.callback_budget_warn_ms)

    def _execute(self, out: Outcome, ev: KeyEvent | None) -> None:
        """Run the resolved action for this outcome (sync key work, async sinks).

        Safe from any thread: the state lock is NOT held here, and injection is
        routed through the hook thread when one is installed.
        """
        if out.decision is Decision.PANIC and out.resolved.startswith("sink:"):
            # The user asked us to stop: starting a 30s Hermes job now would be
            # the opposite of that. Deliver the key the pending Enter stood for
            # (never drop it, I7) and log the dropped sink trigger.
            log.warning("panic: dropping sink trigger resolved=%s (delivering a key)",
                        out.resolved)
            out = replace(out, resolved="pass", count=out.count or 1)
        if out.decision in (Decision.TRIGGER, Decision.DELIVER, Decision.FLUSH,
                            Decision.PANIC):
            try:
                self._dispatch_inject(out)
            except BaseException:
                # An injection failure must never escape into the hook callback and
                # must never prevent the panic transition below.
                log.exception("injection failed resolved=%s (continuing)", out.resolved)
                self._last_error = f"inject failed: {out.resolved or '-'}"
                if out.resolved.startswith("sink:"):
                    # the machine already moved to BUSY; a sink that never
                    # started would leave it there forever (review item 5)
                    self._recover_busy("inject_failed")
        if out.decision is Decision.PANIC:
            self._after_panic(out.reason)
        if out.decision is Decision.IGNORE and out.reason == "busy_drop":
            log.warning("sink_busy_drop resolved=%s", out.resolved)
            self._notify("enter-router", "前の送信が処理中です（1 件破棄）", "warning")
        # A CANCEL is deliberately SILENT (no toast): cancelling a mistyped burst
        # is a light, frequent action and a notification per occurrence does not
        # fit it. The decision log line below is the record ("did it cancel or did
        # the key get lost?" is answered there, without a popup).
        if out.decision is not Decision.PASS:
            op = out.action or "-"
            log.info("decision=%s op=%s meaning=%s state=%s swallow=%s resolved=%s "
                     "count=%s reason=%s",
                     out.decision.name, op, self._meaning(op) or "-", out.state.name,
                     int(out.swallow), out.resolved or "-", out.count, out.reason)

    # -- registration hotkeys (v2-g) ---------------------------------------
    def _registration_kind(self, ev: KeyEvent) -> str | None:
        """"process" / "process_title" when this event is a registration hotkey."""
        if ev.kind != "down" or ev.injected:
            return None
        combo = keys_mod.combo_of(ev.vk, ev.ctrl, ev.alt, ev.shift, ev.win)
        if not combo:
            return None
        reg = self._cfg.registration
        if keys_mod.same_combo(combo, reg.process_key):
            return "process"
        if keys_mod.same_combo(combo, reg.process_title_key):
            return "process_title"
        return None

    def _register_foreground(self, kind: str, t_ms: int) -> None:
        """Write a profile for the CURRENT foreground app into the live config.

        Order matters: a held Enter is delivered first (I6), so the newline the
        user was typing lands in the app, not after the config write. The write
        goes through ``save_config_dict`` — the same validated, atomic path the
        settings window uses — so an invalid document can never reach the file.
        """
        try:
            with self._lock:
                out = self._machine.on_interrupt("key", t_ms)
                self._sync_timer()
            self._execute(out, None)

            _hwnd, fg = self._fg.snapshot()
            process = routing.base_name((fg.process_name or "")).lower()
            if not process:
                log.warning("register: no readable foreground process")
                self._notify("enter-router", "登録できませんでした（前面のプロセスを読めません）",
                             "warning")
                return
            title = fg.window_title if kind == "process_title" else None
            if kind == "process_title" and not title:
                log.warning("register: no window title for %s", process)
                self._notify("enter-router", "登録できませんでした（ウィンドウタイトルが空です）",
                             "warning")
                return

            doc = self.get_config_dict()
            profiles = doc.setdefault("profiles", [])
            wanted_title = routing.literal_glob(title) if title else None
            for existing in profiles:
                match = existing.get("match") or {}
                # A duplicate is the SAME rule: same exe, same title condition and
                # no URL condition. site-a (firefox + a URL glob) is NOT the same
                # rule as "every firefox window", so it must not block the write.
                if ((match.get("process") or "").lower() == process
                        and (match.get("title") or None) == wanted_title
                        and (match.get("url") or None) is None):
                    log.info("register: already defined profile=%s process=%s",
                             existing.get("id"), process)
                    self._notify("enter-router",
                                 f"既に登録済みです: {existing.get('id')}", "info")
                    return

            stem = process[:-4] if process.endswith(".exe") else process
            base_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stem) or "app"
            used = {str(p.get("id")) for p in profiles}
            pid, n = base_id, 1
            while pid in used:
                n += 1
                pid = f"{base_id}-{n}"
            profile = {
                "id": pid,
                "enabled": True,
                # chat convention by default; the user can edit it afterwards
                "match": {"process": process,
                          **({"title": wanted_title} if wanted_title else {})},
                "keymap": {"send": "enter", "newline": "shift+enter"},
            }
            profiles.append(profile)
            ok, message = self.save_config_dict(doc)
            if not ok:
                log.error("register failed: %s", message)
                self._notify("enter-router", f"登録できませんでした: {message}", "warning")
                return
            what = f"{process}" + (f" / {title}" if title else "")
            log.info("registered profile=%s match=%s config=%s", pid,
                     profile["match"], self._config_path)
            self._notify("enter-router",
                         f"登録しました: {pid} ({what}) → {os.path.basename(self._config_path)}",
                         "info")
        except BaseException:
            # fail-open: the key is swallowed either way, but the tool must live
            log.exception("registration failed")
            self._last_error = "registration failed"

    # -- injection routing -------------------------------------------------
    def _on_hook_thread(self) -> bool:
        thread = self._hook_thread
        return thread is not None and threading.current_thread() is thread

    def _dispatch_inject(self, out: Outcome) -> None:
        """Queue one injection and let the single injector deliver it, in order.

        While the hook is installed the hook thread is the single injector, so a
        background thread never blocks it: SendInput issued from elsewhere waits
        for the hook to consume the injected events, and the callback may be
        waiting for a lock. ``hook.wake()`` posts WM_APP_WAKE and the message
        loop drains this queue on the hook thread.

        Everything goes through the queue — including work produced on the hook
        thread itself — because the queue is also what preserves I6: a delivery
        the timer thread handed over (the held Enter) must never be overtaken by
        a trigger the hook thread handles a moment later.
        """
        with self._queue_lock:
            self._pending_inject.append(out)
        if self._on_hook_thread():
            self._drain_injections()     # already the injector: drain FIFO now
            return
        from enter_router.platform import hook as hook_mod
        try:
            posted = hook_mod.wake()
        except BaseException:
            log.exception("hook.wake() failed")
            posted = False
        if posted:
            return
        # No hook exists to hand it to, so nothing can interleave with us.
        self._drain_injections()

    def _on_wake(self) -> None:
        """hook thread: another thread handed us injections (WM_APP_WAKE)."""
        self._drain_injections()

    def _drain_before_key(self) -> None:
        """Deliver queued injections before the key we are about to decide (I6).

        Runs on the hook thread at the top of every non-injected key event, so a
        hand-off the wake-up message has not been processed for yet (timer thread
        -> ``_pending_inject`` -> ``hook.wake()``) still goes out first. The
        injected events land in the input queue before this key's message is
        posted, which is exactly the ``あ`` → Enter → ``い`` ordering.

        Containment: an injection failure must not cost the user this key, so it
        is caught here and the normal decision path continues (fail-open, §6-1).
        """
        try:
            self._drain_injections()
        except BaseException:
            log.exception("draining queued injections before a key failed")
            self._last_error = "pending injection failed"

    def _drain_injections(self) -> None:
        while True:
            with self._queue_lock:
                if not self._pending_inject:
                    return
                out = self._pending_inject.pop(0)
            self._inject_now(out)

    def _inject_now(self, out: Outcome) -> None:
        if not self._inject_lock.acquire(timeout=0.5):
            log.error("injection skipped: another injector is stuck (resolved=%s)",
                      out.resolved)
            self._last_error = f"inject busy: {out.resolved or '-'}"
            return
        try:
            self._inject(out)
        finally:
            self._inject_lock.release()

    def _inject(self, out: Outcome) -> None:
        held = sorted(self._machine.held_modifiers())
        if out.resolved.startswith("key:"):
            from enter_router.platform import inject
            combo = out.resolved[4:]
            sent = inject.send_combo(combo, held)
            log.info("inject combo=%s held=%s events=%d", combo, held, sent)
            return
        if out.resolved.startswith("sink:"):
            self._enqueue_sink(out)
            return
        # pass/none while resolving held Enters: replay them, in order, once
        copies = out.count or 0
        if copies:
            from enter_router.platform import inject
            sent = inject.send_enter(copies, held)
            log.info("inject enter copies=%d held=%s events=%d", copies, held, sent)

    def _enqueue_sink(self, out: Outcome) -> None:
        _hwnd, fg = self._fg.snapshot()
        profile = routing.match_profile(self._cfg, fg)
        op = out.action or "-"
        ctx = SinkContext(
            trigger=trigger_info(op=op, meaning=self._meaning(op) or "",
                                 profile_id=profile.id if profile else "",
                                 t_ms=self._now_ms()),
            foreground=fg,
            captured_text=None,
        )
        log.info("sink_queued resolved=%s op=%s profile=%s", out.resolved, op,
                 profile.id if profile else "-")
        submitted = False
        try:
            submitted = bool(self._worker.submit(SinkJob(resolved=out.resolved, ctx=ctx)))
        except BaseException:
            log.exception("sink submit raised resolved=%s", out.resolved)
        if not submitted:
            # The machine is already BUSY and no job exists: without this it
            # would stay BUSY forever and drop every later sink trigger.
            self._recover_busy("submit_failed")

    def _recover_busy(self, reason: str) -> None:
        with self._lock:
            if self._machine.state is not State.BUSY:
                return
            out = self._machine.on_sink_done(self._now_ms())
            self._sync_timer()
        log.error("busy_recovered reason=%s (state=%s)", reason, out.reason)
        self._last_error = f"busy recovered: {reason}"

    # -- callbacks from other threads -------------------------------------
    def _on_deadline(self) -> None:
        with self._lock:
            out = self._machine.on_deadline(self._now_ms())
            self._sync_timer()
        self._execute(out, None)

    def _on_sink_finished(self, result: SinkResult) -> None:
        with self._lock:
            out = self._machine.on_sink_done(self._now_ms())
            self._sync_timer()
        self._last_sink_result = f"{result.sink}: {result.status}" + (
            f" ({result.failure})" if result.failure else "")
        log.info("sink_finished sink=%s status=%s failure=%s",
                 result.sink, result.status, result.failure or "-")
        if not result.ok:
            self._last_error = self._last_sink_result
        _ = out

    def _on_overbudget(self, reason: str) -> None:
        log.error("self-unhook due to slow callbacks: %s", reason)
        self.panic_stop(f"callback_overbudget:{reason}", notify_user=True)

    def _on_hook_exit(self) -> None:
        with self._lock:
            self._hook_installed = False
            if self._state == "running":
                self._state = "paused"
        # The hook thread is gone, so nobody else will drain the hand-off queue;
        # anything still there would be a silently dropped key (I1/I7).
        try:
            self._drain_injections()
        except BaseException:
            log.exception("draining pending injections on hook exit failed")
        log.info("hook loop exited")

    def _after_panic(self, reason: str) -> None:
        if self._panic_done:
            return
        self._panic_done = True
        self._state = "panic"
        log.error("panic state entered reason=%s", reason)
        if self._panic_notify:
            self._notify("enter-router", "パニック停止: キー横取りを停止しました", "warning")
        try:
            from enter_router.platform import hook as hook_mod
            hook_mod.request_stop(f"panic:{reason}")
        except BaseException:
            log.exception("hook stop request failed")

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._fg.start()
        self._worker.start()
        self._watchdog_thread = watchdog.start(
            self._monitor.last_beat_ms, self._cfg.safety.watchdog_interval_ms,
            self._cfg.safety.watchdog_stale_ms, self._on_watchdog_trip)
        self._install_hook()
        self._watcher = config_watcher.ConfigWatcher(
            self._config_path, self._apply_config, self._on_config_error).start()
        log.info("enter-router %s started (config=%s)", __version__, self._config_path)

    def _install_hook(self) -> None:
        from enter_router.platform import hook as hook_mod

        def loop() -> None:
            try:
                hook_mod.install(self._decide, self._on_hook_exit, self._monitor,
                                 on_mouse=lambda: self._on_mouse(),
                                 on_wake=self._on_wake)
            except BaseException as e:
                log.exception("hook install failed")
                self._last_error = f"hook install failed: {e}"
                self._notify("enter-router", f"キーフックを開始できません: {e}", "error")

        self._hook_thread = threading.Thread(target=loop, name="hook", daemon=True)
        self._hook_thread.start()
        # SetWindowsHookExW reports failure immediately, but give it a moment so
        # the reported status is not racy (the UI shows running/paused).
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and hook_mod.current_handle() is None:
            if self._hook_thread.is_alive() is False:
                break
            time.sleep(0.02)
        self._hook_installed = hook_mod.current_handle() is not None
        self._monitor.heartbeat()
        with self._lock:
            self._state = "running" if self._hook_installed else "paused"

    def _on_mouse(self) -> None:
        with self._lock:
            out = self._machine.on_interrupt("mouse", self._now_ms())
            self._sync_timer()
        self._execute(out, None)

    def wait(self) -> None:
        self._stopped.wait()

    def run(self) -> int:
        self.start()
        try:
            while not self._stopped.is_set():
                self._stopped.wait(0.5)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.shutdown()
        # 70 is reserved for the watchdog self-exit (os._exit inside it);
        # a user panic is an orderly stop.
        return EXIT_OK

    def shutdown(self) -> None:
        log.info("shutting down")
        out = None
        try:
            from enter_router.platform import hook as hook_mod
            with self._lock:
                if self._machine.pending_count:
                    out = self._machine.on_panic(self._now_ms(), reason="shutdown")
                    self._sync_timer()
            # flush first: the hook thread drains it before the WM_QUIT below is
            # processed (message order), so nothing is dropped (I7)
            if out is not None:
                self._execute(out, None)
            hook_mod.request_stop("shutdown")
        except BaseException:
            log.exception("shutdown: hook stop failed")
        if self._watcher is not None:
            self._watcher.stop()
        watchdog.stop(self._watchdog_thread)
        self._timer.stop()
        self._worker.stop()
        self._fg.stop()
        self._stopped.set()

    def _on_watchdog_trip(self) -> None:
        """Belt-and-braces: flush what we can in 50ms, then die (design §6-3).

        The lock acquire is bounded: if the hook thread (or anyone) is wedged
        holding it, blocking here would mean the watchdog never reaches
        ``os._exit`` — and process death is what restores the keyboard.
        """
        log.error("watchdog trip — flushing pending and exiting(70)")
        acquired = False
        try:
            acquired = self._lock.acquire(timeout=0.05)
        except BaseException:
            log.exception("watchdog: bounded lock acquire failed")
        try:
            if acquired and self._machine.pending_count:
                out = self._machine.on_panic(self._now_ms(), reason="watchdog")
                # Best effort only: nothing may sit in the hand-off queue when
                # the process dies, so inject inline under a bounded lock.
                if self._inject_lock.acquire(timeout=0.02):
                    try:
                        self._inject(out)
                    finally:
                        self._inject_lock.release()
                else:
                    log.error("watchdog: injector busy, pending Enter not delivered")
        except BaseException:
            log.exception("watchdog flush failed")
        finally:
            if acquired:
                try:
                    self._lock.release()
                except BaseException:
                    pass
        try:
            self._notify("enter-router",
                         "応答が停止したため終了します（キーボードは復旧します）", "error")
        except BaseException:
            pass
        os._exit(EXIT_WATCHDOG)

    # -- config -----------------------------------------------------------
    def _apply_config(self, config: Config) -> None:
        with self._lock:
            self._cfg = config
            # reconfigure returns the outcome that delivers a held Enter; it MUST
            # be executed or the pending key would be dropped (I1/I7).
            out = self._machine.reconfigure(config.policy, self._resolve,
                                            ime_unknown_policy=config.ime_unknown_policy)
            self._sinks = build_sinks(config, notify=self._notify)
            self._sync_timer()
            # The hook and the watchdog hold this same object, so reloading the
            # safety parameters must update it in place (a new SafetyMonitor
            # would be invisible to both).
            self._monitor.update(config.safety)
        self._execute(out, None)
        log.info("config applied (warnings=%d)", len(config.warnings))
        for warning in config.warnings:
            log.warning("config warning: %s", warning)

    def _on_config_error(self, message: str) -> None:
        self._last_error = f"config: {message}"
        self._notify("enter-router", f"設定が不正のため旧設定を継続します: {message}",
                     "warning")

    def reload_config(self) -> tuple[bool, str]:
        try:
            with open(self._config_path, encoding="utf-8") as handle:
                config = parse_config_text(handle.read())
        except (OSError, ConfigError) as e:
            self._last_error = f"config: {e}"
            log.error("reload failed: %s", e)
            return False, str(e)
        self._apply_config(config)
        return True, "reloaded"

    def get_config_dict(self) -> dict:
        """Live config as a JSON-ready dict (settings UI reads this)."""
        from enter_router.core.config import _jsonable
        cfg = self._cfg
        return _jsonable({
            "schema_version": cfg.schema_version,
            # global.ime_unknown_policy and the registration hotkeys are part of
            # the document: dropping them here silently reset them on every save
            # from the settings window.
            "global": {"operations": cfg.operations,
                       "ime_unknown_policy": cfg.ime_unknown_policy},
            "policy": cfg.policy,
            "registration": cfg.registration,
            "safety": cfg.safety,
            "capture": cfg.capture,
            "sinks": cfg.sinks,
            "profiles": cfg.profiles,
            "logging": cfg.logging,
        })

    def save_config_dict(self, data: dict) -> tuple[bool, str]:
        """Validate then write atomically; the old file survives any error."""
        try:
            config = parse_config(data)
        except ConfigError as e:
            log.error("save rejected: %s", e)
            return False, str(e)
        target = self._config_path
        tmp = target + ".tmp"
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, target)
        except OSError as e:
            log.error("save failed: %s", e)
            self._last_error = f"save: {e}"
            return False, str(e)
        self._apply_config(config)
        return True, "saved"

    # -- AppController ----------------------------------------------------
    def get_status(self) -> RouterStatus:
        with self._lock:
            state = self._state
            if self._machine.state is State.BUSY:
                state = "busy"
            elif self._machine.state is State.PANIC:
                state = "panic"
            try:
                mtime = os.stat(self._config_path).st_mtime
            except OSError:
                mtime = 0.0
            return RouterStatus(state=state,  # type: ignore[arg-type]
                                hook_installed=self._hook_installed,
                                config_path=self._config_path,
                                config_mtime=mtime,
                                config_warnings=self._cfg.warnings,
                                last_error=self._last_error,
                                last_sink_result=self._last_sink_result)

    def set_enabled(self, enabled: bool) -> None:
        log.info("set_enabled(%s)", enabled)
        out = None
        with self._lock:
            current = self._cfg.policy
            self._cfg = replace(self._cfg, policy=replace(current, enabled=enabled))
            if self._machine.state is State.PANIC:
                # resume is a full re-arm: the panic state is terminal
                self._machine = self._make_machine(self._cfg)
            else:
                # a held Enter must be delivered before the policy disappears (I7)
                out = self._machine.reconfigure(self._cfg.policy, self._resolve,
                                                ime_unknown_policy=self._cfg.ime_unknown_policy)
            self._sync_timer()
        if out is not None:
            self._execute(out, None)          # outside the lock (review item 2)
        if enabled:
            self._panic_done = False
            if not self._hook_installed:
                self._install_hook()
        else:
            from enter_router.platform import hook as hook_mod
            hook_mod.request_stop("paused")
            self._hook_installed = False
            self._state = "paused"

    def panic_stop(self, reason: str = "user", notify_user: bool = True) -> None:
        self._panic_notify = notify_user
        try:
            with self._lock:
                out = self._machine.on_panic(self._now_ms(), reason=reason)
                self._sync_timer()
            self._execute(out, None)          # ordered flush, then unhook request
        finally:
            self._panic_notify = True
        self._state = "panic"
        self._hook_installed = False

    def test_send(self, sink_name: str, text: str) -> str:
        from enter_router.app.sink_runner import run_with_fallback
        _hwnd, fg = self._fg.snapshot()
        ctx = SinkContext(trigger=trigger_info(op="test", meaning="", profile_id="",
                                              t_ms=self._now_ms()),
                          foreground=fg, captured_text=text)
        result = run_with_fallback(self._cfg, self._sinks, f"sink:{sink_name}", text,
                                   ctx, self._notify)
        return (f"{result.sink}: {result.status}"
                + (f" ({result.failure})" if result.failure else "")
                + (f" — {result.message}" if result.message else ""))

    def open_log_dir(self) -> None:
        path = os.path.dirname(logging.getLogger("enter_router").handlers[0].baseFilename) \
            if logging.getLogger("enter_router").handlers else appdata_dir()
        try:
            os.startfile(path)          # type: ignore[attr-defined]  # Windows
        except AttributeError:
            log.info("log dir: %s", path)
        except OSError:
            log.exception("could not open log dir %s", path)


def trigger_info(op: str, meaning: str, profile_id: str, t_ms: int):
    """Build the SinkContext.trigger for one sink action (design §5.1)."""
    from enter_router.core.events import TriggerInfo
    count = 3 if op.endswith("3") else 2 if op.endswith("2") else 1
    if op in ("enter2", "enter3"):
        kind = "double_enter" if count == 2 else "triple_enter"
    else:
        # a single counted Enter has no dedicated TriggerKind; "hotkey" carries
        # op/count so the sink can still tell what fired.
        kind = "hotkey"
    return TriggerInfo(kind=kind, count=count, profile_id=profile_id, t_ms=t_ms,
                       hotkey=None if op.startswith("enter") else op,
                       op=op, meaning=meaning)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@dataclass
class RunOptions:
    config: str | None = None
    dry_run: bool = False
    once: bool = False
    settings: bool = False


def run(options: RunOptions, argv: list[str] | None = None) -> int:
    # 1) config first: a bad config must never install a hook
    try:
        config, config_path = load_config(options.config)
    except (ConfigError, OSError) as e:
        print(f"enter-router: config error: {e}", file=sys.stderr)
        return EXIT_CONFIG

    console = sys.stderr is not None
    try:
        log_path = logsetup.setup(config.logging, appdata_dir(), console=console)
    except OSError as e:
        print(f"enter-router: cannot write logs ({e})", file=sys.stderr)
        log_path = ""
    for warning in config.warnings:
        log.warning("config warning: %s", warning)

    if options.once:
        for line in dryrun.once_report(config, config_path, log_path):
            print(line)
        return EXIT_OK

    if options.dry_run:
        result = dryrun.run(config, emit=print, config_path=config_path)
        dryrun.log_summary(result)
        print(f"enter-router {__version__}: dry-run complete "
              f"(no hook installed, nothing injected)")
        return EXIT_OK

    if options.settings:
        # The settings window belongs to the frontend card (ui/). This process
        # must not create a second, divergent UI, so ask the resident instance to
        # show its window instead (WM_APP message to its tray window).
        try:
            from enter_router.ui.tray import post_open_settings
            opened = post_open_settings()
        except BaseException:
            log.exception("could not ask the running instance for its window")
            opened = False
        if opened:
            print("enter-router: asked the running instance to open its settings window.")
            return EXIT_OK
        print("enter-router: no running instance with a tray window was found.\n"
              "Start it once (pythonw src\\enter_router\\main.py), then retry.\n"
              "The window is also reachable from the tray menu (設定を開く). Windows 11\n"
              "keeps *new* tray icons in the notification-area overflow until you pin\n"
              "them: Settings > Personalization > Taskbar > Other system tray icons.")
        return EXIT_OK

    app = EnterRouterApp(config, config_path)
    log.info("enter-router %s starting (version=%s)", __version__, __version__)
    return run_resident(app)


def _register_tray_icon(hwnd: int) -> None:
    """Let platform notifications reuse the tray icon the UI created (design §7).

    ``ui/`` must not import ``platform/`` (§2), so the composition root bridges
    the two: without this the sink/safety notifications fall back to a log line
    plus a beep instead of a balloon.
    """
    from enter_router.platform import notify_win
    notify_win.register_icon(hwnd)


def run_resident(app: "EnterRouterApp") -> int:
    """Start the resident app AND its tray/settings UI (design §1/§7).

    This function is the whole frontend wiring in the backend module (allowed by
    design §8: "frontend is the tray startup wiring only"). Everything else the
    UI does goes through ``AppController``.

    Degradation: if tkinter or the tray is unavailable (non-Windows, broken Tk),
    we log it and fall back to the headless loop, so the backend keeps working
    exactly as it did before the UI existed.
    """
    app.start()
    runtime = None
    try:
        from enter_router.ui.runtime import UiRuntime
        runtime = UiRuntime(app, on_icon_created=_register_tray_icon)
        runtime.start()
        if not runtime.tray_ok:
            log.warning("running without a tray icon (settings window opened instead)")
    except BaseException:
        log.exception("UI could not start — continuing headless")
        runtime = None

    if runtime is None:
        try:
            app.wait()
        finally:
            app.shutdown()
        return EXIT_OK
    try:
        runtime.run_forever()
    finally:
        runtime.stop()
        app.shutdown()
    return EXIT_OK
