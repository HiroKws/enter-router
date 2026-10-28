"""App-level safety tests: panic stops the hook, hook callback never throws.

These exercise the real composition root (``app.main.EnterRouterApp``) on Linux
by replacing the two Windows effects it calls (``hook.request_stop`` and
``inject.send_combo``/``send_enter``). Everything else — policy machine, config,
routing, sink registry — is the production object graph.

What is proven here (the acceptance criteria that cannot be shown by the pure
state-machine tests alone):

* the panic key flushes the held Enter and then asks the hook to uninstall, in
  that order, exactly once (I7);
* a panic is idempotent and afterwards the status reports "panic";
* an exception inside the decision path is contained and the key passes through
  (the hook contract: return 0 == not swallowed).
"""
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import main as app_main  # noqa: E402
from enter_router.core.config import parse_config_text  # noqa: E402
from enter_router.core.events import VK_RETURN, ForegroundInfo, KeyEvent  # noqa: E402
from enter_router.core.policy import Decision, Outcome, State  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"
VK_F1 = 0x70
VK_F12 = 0x7B


class Recorder:
    def __init__(self):
        self.events: list[tuple] = []
        self.held: list[tuple] = []

    def combo(self, combo, held=()):
        self.events.append(("combo", combo))
        self.held.append(tuple(held))
        return 4

    def enter(self, copies, held=()):
        self.events.append(("enter", copies))
        self.held.append(tuple(held))
        return 2 * copies

    def stop_hook(self, reason="stop"):
        self.events.append(("stop", reason))


class AppSafetyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.rec = Recorder()
        self.app = app_main.EnterRouterApp(self.cfg, str(SAMPLE))
        # replace the Windows effects with recorders
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        from enter_router.core.events import ImeState
        self._saved = (hook_mod.request_stop, inject.send_combo, inject.send_enter,
                       ime_mod.query_composition, hook_mod.wake)
        hook_mod.request_stop = self.rec.stop_hook
        hook_mod.wake = lambda: False            # no hook installed in these tests
        inject.send_combo = self.rec.combo
        inject.send_enter = self.rec.enter
        # there is no IMM context off-Windows; the production code would fail open
        # (pass every Enter), which is exactly what design §3.4 asks for. For these
        # wiring tests we want the normal path, so report "not composing".
        ime_mod.query_composition = lambda hwnd: ImeState(composing=False)
        # the foreground cache is polled by a thread in production; pin it here
        self.app._fg = _StaticFg(ForegroundInfo(
            process_name="firefox.exe", window_title="Site A",
            url="https://site-a.example.com/chat"))

    def tearDown(self):
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        (hook_mod.request_stop, inject.send_combo, inject.send_enter,
         ime_mod.query_composition, hook_mod.wake) = self._saved
        self.app._timer.stop()

    # -- panic ------------------------------------------------------------
    def test_panic_flushes_then_stops_the_hook(self):
        hold = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.assertTrue(hold)                       # counted Enter is swallowed
        self.app.panic_stop("test")
        self.assertEqual(self.rec.events[0], ("combo", "shift+enter"))  # flush first
        self.assertTrue(any(kind == "stop" for kind, *_ in self.rec.events))
        self.assertEqual(self.rec.events[-1][0], "stop")                # ... then stop
        self.assertEqual(self.app.get_status().state, "panic")

    def test_panic_is_idempotent(self):
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.app.panic_stop("test")
        flushed = [e for e in self.rec.events if e[0] in ("combo", "enter")]
        self.app.panic_stop("test again")
        self.assertEqual([e for e in self.rec.events if e[0] in ("combo", "enter")],
                         flushed)                   # never delivered twice (I1)

    def test_panic_key_from_the_hook_path(self):
        """v2-g: F1 x3 (burst) is the stop; a single F1 is just a key."""
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        for i in range(3):
            t = 1100 + i * 60
            self.app._decide(KeyEvent(vk=VK_F1, kind="down", t_ms=t))
            self.app._decide(KeyEvent(vk=VK_F1, kind="up", t_ms=t + 10))
        self.assertIn(("combo", "shift+enter"), self.rec.events)
        self.assertTrue(any(kind == "stop" for kind, *_ in self.rec.events))
        self.assertEqual(self.app._machine.state, State.PANIC)

    # -- unknown IME state is never silent (card t_dd246e5a) --------------
    def test_unknown_ime_state_is_logged_and_notified_once(self):
        """policy "pass" still passes the Enter, but the user and the log are told."""
        from enter_router.app.ime_unknown import ImeUnknownReporter
        from enter_router.core.events import ImeState
        from enter_router.platform import ime as ime_mod

        ime_mod.query_composition = lambda hwnd: ImeState(
            composing=None, detail="ImmGetContext=0 err=5", source="")
        notes: list[tuple] = []
        self.app._ime_unknown = ImeUnknownReporter(
            notify=lambda title, body, level="info": notes.append((title, body, level)))
        with self.assertLogs("enter_router.ime", level="WARNING") as logs:
            first = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
            self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=1010))
            second = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=2000))
            self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=2010))
        self.assertFalse(first)                  # passed through (policy "pass")
        self.assertFalse(second)
        self.assertEqual(len(notes), 1)          # ONCE, not per keystroke
        self.assertIn("IME", notes[0][0])
        self.assertTrue(any("ime_state=unknown action=pass" in line
                            for line in logs.output))
        self.assertTrue(any("ImmGetContext=0 err=5" in line for line in logs.output))

    def test_unknown_ime_state_with_policy_process_runs_the_policy(self):
        from enter_router.app.ime_unknown import ImeUnknownReporter
        from enter_router.core.events import ImeState
        from enter_router.platform import ime as ime_mod

        self.cfg = replace(self.cfg, ime_unknown_policy="process")
        self.app._cfg = self.cfg
        self.app._machine = self.app._make_machine(self.app._cfg)
        ime_mod.query_composition = lambda hwnd: ImeState(composing=None, detail="x")
        self.app._ime_unknown = ImeUnknownReporter(notify=lambda *a, **k: None)
        swallowed = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.assertTrue(swallowed)               # held, i.e. the policy ran
        self.assertIs(self.app._machine.state, State.HOLDING)

    # -- exception containment -------------------------------------------
    def test_hook_callback_never_raises_and_passes_the_key(self):
        def boom(ev, ime=None):
            raise RuntimeError("state machine exploded")

        self.app._machine.on_key = boom
        swallowed = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.assertFalse(swallowed)                 # fail-open
        self.assertIn("decide exception", self.app.get_status().last_error)

    def test_injection_failure_does_not_escape_or_block_the_panic(self):
        from enter_router.platform import inject

        attempted: list[str] = []

        def broken(combo, held=()):
            attempted.append(combo)              # real signature, deliberate failure
            raise OSError("SendInput unavailable")

        inject.send_combo = broken
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.app.panic_stop("test")                 # must not raise
        self.assertEqual(self.app.get_status().state, "panic")
        self.assertTrue(any(kind == "stop" for kind, *_ in self.rec.events))
        self.assertIn("inject failed", self.app.get_status().last_error)
        self.assertTrue(attempted, "the injection path was never reached")

    # -- natural expiry ---------------------------------------------------
    def test_deadline_delivers_without_a_panic(self):
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=1015))
        self.app._on_deadline()                      # timer thread calls this
        self.assertIn(("combo", "shift+enter"), self.rec.events)

    def test_config_reload_while_holding_delivers_the_pending_key(self):
        """I7: a reload must not silently drop the held Enter."""
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        reloaded = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.app._apply_config(reloaded)
        self.assertIn(("combo", "shift+enter"), self.rec.events)
        self.assertEqual(self.app._machine.pending_count, 0)

    def test_pause_while_holding_delivers_the_pending_key(self):
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.app.set_enabled(False)
        self.assertIn(("combo", "shift+enter"), self.rec.events)
        self.assertEqual(self.app.get_status().state, "paused")

    def test_undefined_app_passes_after_the_hold(self):
        self.app._fg = _StaticFg(ForegroundInfo(process_name="notepad.exe",
                                                window_title="Untitled"))
        swallowed = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.assertTrue(swallowed)                   # held
        self.app._on_deadline()
        self.assertEqual([e for e in self.rec.events if e[0] == "enter"], [("enter", 1)])

    # -- config save ------------------------------------------------------
    def test_save_config_dict_rejects_invalid_without_touching_the_file(self):
        original = SAMPLE.read_bytes()
        bad = self.app.get_config_dict()
        bad["global"]["operations"]["enter"] = "bogus"
        ok, message = self.app.save_config_dict(bad)
        self.assertFalse(ok)
        self.assertIn("unknown meaning", message)
        self.assertEqual(SAMPLE.read_bytes(), original)

    # -- injection routing (review round 1, item 2) ------------------------
    def _t0(self, deadline_after_ms: int = 5000) -> int:
        """A timestamp on the SAME clock the deadline timer uses.

        Hard-coded t_ms values (1000, ...) are far in the past for the real
        timer thread, which then fires immediately; and a real "now" is only a
        few ms after the hold started, so ``on_deadline`` would (correctly)
        refuse to deliver before the window elapsed. Pinning the app clock a few
        seconds ahead lets a test fire the real deadline path deterministically.
        """
        t0 = int(time.monotonic() * 1000)
        self.app._now_ms = lambda: t0 + deadline_after_ms
        return t0

    def test_injection_never_runs_while_the_state_lock_is_held(self):
        """Item 2: SendInput under the lock deadlocks the hook (measured 617ms).

        The lock a background injector holds is the very lock the hook callback
        takes, so an injector that held it while calling SendInput would stall
        every key press until the OS hook timeout.
        """
        from enter_router.platform import inject
        owned: list[bool] = []

        def combo(name, held=()):
            owned.append(self.app._lock._is_owned())
            return 4

        inject.send_combo = combo
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=self._t0()))
        self.app._on_deadline()
        self.assertEqual(owned, [False], "SendInput ran while _lock was held")

    def test_background_delivery_is_handed_to_the_hook_thread(self):
        """Item 2: while the hook runs, only the hook thread injects."""
        from enter_router.platform import hook as hook_mod
        wake_calls: list[int] = []
        hook_mod.wake = lambda: (wake_calls.append(1), True)[1]
        try:
            self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=self._t0()))
            self.app._on_deadline()
            self.assertEqual(wake_calls, [1])
            self.assertEqual(self.rec.events, [], "injected off the hook thread")
            self.assertEqual(len(self.app._pending_inject), 1)
            self.app._on_wake()                      # the hook thread drains it
            self.assertEqual(self.rec.events, [("combo", "shift+enter")])
            self.assertEqual(self.app._pending_inject, [])
        finally:
            self.app._pending_inject.clear()

    def test_a_failed_wake_injects_inline_so_the_key_is_not_stranded(self):
        from enter_router.platform import hook as hook_mod
        hook_mod.wake = lambda: False
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=self._t0()))
        self.app._on_deadline()
        self.assertEqual(self.rec.events, [("combo", "shift+enter")])
        self.assertEqual(self.app._pending_inject, [])

    def test_a_queued_delivery_is_not_overtaken_by_a_hook_thread_trigger(self):
        """I6 across threads: FIFO is the queue, not the thread that produced it.

        The timer thread queues the held Enter's delivery; a trigger the hook
        thread handles next is queued behind it, so the application sees the
        flushed Enter first. Injecting directly on the hook thread (the shape
        this test forbids) would reverse that order.
        """
        from enter_router.platform import hook as hook_mod
        hook_mod.wake = lambda: True
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=self._t0()))
        self.app._on_deadline()                      # timer thread hands it over
        self.assertEqual(self.rec.events, [], "injected on the timer thread")
        self.app._hook_thread = threading.current_thread()   # we are the hook thread
        try:
            now = Outcome(decision=Decision.TRIGGER, state=State.IDLE, swallow=True,
                          resolved="key:enter", count=1, reason="trigger")
            self.app._execute(now, None)
        finally:
            self.app._hook_thread = None
            self.app._pending_inject.clear()
        self.assertEqual(self.rec.events,
                         [("combo", "shift+enter"), ("combo", "enter")])

    def test_a_plain_key_does_not_overtake_a_queued_held_enter(self):
        """I6 (review round 2): the typed key must not reach the app first.

        The timer thread releases the held Enter by handing it to the hook thread
        (queue + wake). If the user's next key arrives before WM_APP_WAKE is
        processed, that key's decision is PASS — and PASS executes nothing — so
        only a drain inside ``_decide`` keeps the order. Observed before the fix:
        the app saw "a" then a newline; required: newline then "a".
        """
        from enter_router.platform import hook as hook_mod
        hook_mod.wake = lambda: True            # handed over, wake not processed yet
        t0 = self._t0()
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t0))
        self.app._on_deadline()                  # timer thread: queued only
        self.assertEqual(self.rec.events, [], "injected on the timer thread")
        self.assertEqual(len(self.app._pending_inject), 1)

        # The very next key reaches the hook before the wake-up message.
        passthrough = self.app._decide(KeyEvent(vk=0x41, kind="down", t_ms=t0 + 60))

        # Returning False is the instant the application receives the "a", so by
        # then the held Enter must already have been injected.
        self.assertFalse(passthrough)
        self.assertEqual(self.rec.events, [("combo", "shift+enter")],
                         "the typed key overtook the queued held Enter")
        self.assertEqual(self.app._pending_inject, [])

    def test_a_failed_drain_before_a_key_still_passes_the_key(self):
        """I6 drain is fail-open: a broken injector must not swallow the key."""
        from enter_router.platform import hook as hook_mod
        hook_mod.wake = lambda: True
        t0 = self._t0()
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t0))
        self.app._on_deadline()
        self.assertEqual(len(self.app._pending_inject), 1)

        def boom(out):
            raise OSError("injector exploded")

        self.app._inject_now = boom
        try:
            passthrough = self.app._decide(KeyEvent(vk=0x41, kind="down", t_ms=t0 + 60))
        finally:
            self.app._pending_inject.clear()
        self.assertFalse(passthrough)
        self.assertIn("pending injection failed", self.app.get_status().last_error)

    def test_an_injected_event_never_drains_the_queue(self):
        """rule0 comes before the I6 drain: our own keys must not trigger work.

        Draining inside a callback for an event we ourselves synthesised would
        nest a SendInput in the middle of delivering one; rule0 (I4) therefore
        wins, and the queued delivery waits for the next real key or the wake-up.
        """
        from enter_router.platform import hook as hook_mod
        hook_mod.wake = lambda: True
        t0 = self._t0()
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t0))
        self.app._on_deadline()
        self.assertEqual(len(self.app._pending_inject), 1)
        try:
            passed = self.app._decide(KeyEvent(vk=0x41, kind="down", t_ms=t0 + 60,
                                               injected=True))
            self.assertFalse(passed)
            self.assertEqual(self.rec.events, [], "an injected event triggered work")
            self.assertEqual(len(self.app._pending_inject), 1)
        finally:
            self.app._pending_inject.clear()

    def test_injected_events_are_never_processed_by_the_app(self):
        """rule0 (I4) at the composition root: no count, no trigger, no injection."""
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=self._t0(),
                                  injected=True))
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=self._t0(),
                                  injected=True))
        self.assertEqual(self.rec.events, [])
        self.assertEqual(self.app._machine.pending_count, 0)
        self.assertEqual(self.app._pending_inject, [])

    def test_queued_injections_are_drained_when_the_hook_exits(self):
        """A hand-off that arrived just before the hook died must not be lost."""
        from enter_router.platform import hook as hook_mod
        hook_mod.wake = lambda: True
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=self._t0()))
        self.app._on_deadline()
        self.assertEqual(self.rec.events, [])
        self.app._on_hook_exit()                     # hook loop teardown
        self.assertEqual(self.rec.events, [("combo", "shift+enter")])

    def test_the_physically_held_modifier_reaches_the_injection(self):
        """Item 4 wiring: the trigger's held Ctrl is what inject must release."""
        t0 = self._t0()
        self.app._decide(KeyEvent(vk=0xA2, kind="down", t_ms=t0, ctrl=True))
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t0 + 1, ctrl=True))
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=t0 + 5, ctrl=True))
        self.assertIn(0xA2, self.rec.held[-1])

    # -- BUSY recovery (review round 1, item 5) ----------------------------
    def test_busy_recovers_when_the_sink_queue_rejects_the_job(self):
        state = self.app._machine
        state._state = State.BUSY                   # as if a sink were running
        self.app._worker.submit = lambda job: False
        self.app._recover_busy("submit_failed")
        self.assertIsNot(state.state, State.BUSY)
        self.assertIn("busy recovered", self.app.get_status().last_error)

    def test_failed_sink_submit_does_not_leave_the_machine_busy(self):
        """The real path: a full burst whose sink job the queue rejects."""
        t0 = self._t0(deadline_after_ms=400)     # past the 3rd press's window
        self.app._worker.submit = lambda job: False
        for offset in (0, 60, 120):
            self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t0 + offset))
            self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=t0 + offset + 5))
        self.app._on_deadline()          # the 3rd press fires on its window
        self.assertIsNot(self.app._machine.state, State.BUSY)
        self.assertIn("busy recovered", self.app.get_status().last_error)

    # -- watchdog (review round 1, item 6) ---------------------------------
    def test_watchdog_trip_does_not_block_on_a_held_lock(self):
        codes: list[int] = []
        original = app_main.os._exit
        app_main.os._exit = lambda code: codes.append(code)
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.app._lock:
                held.set()
                release.wait(2.0)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(2.0))
        try:
            started = time.monotonic()
            self.app._on_watchdog_trip()
            elapsed = time.monotonic() - started
        finally:
            release.set()
            thread.join(2.0)
            app_main.os._exit = original
        self.assertEqual(codes, [app_main.EXIT_WATCHDOG])
        self.assertLess(elapsed, 0.5, "the watchdog blocked on the state lock")

    # -- panic must not start a 30s job (minor item) -----------------------
    def test_panic_does_not_enqueue_a_sink_job(self):
        from enter_router.core.policy import Decision, Outcome
        submitted: list = []
        self.app._worker.submit = lambda job: submitted.append(job) or True
        out = Outcome(decision=Decision.PANIC, state=State.PANIC, swallow=False,
                      resolved="sink:hermes", count=1, reason="panic_key")
        self.app._execute(out, None)
        self.assertEqual(submitted, [])
        self.assertIn(("enter", 1), self.rec.events)   # the key is still delivered

    # -- safety reload (minor item) ---------------------------------------
    def test_safety_reload_updates_the_monitor_the_hook_already_holds(self):
        import json
        monitor = self.app._monitor
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["safety"]["callback_hard_limit_ms"] = 890
        self.app._apply_config(parse_config_text(json.dumps(raw)))
        self.assertIs(self.app._monitor, monitor)      # same object, updated
        self.assertEqual(monitor._safety.callback_hard_limit_ms, 890)


class _StaticFg:
    """ForegroundCache stand-in (no polling thread, no Win32)."""

    def __init__(self, info: ForegroundInfo):
        self._info = info

    def snapshot(self):
        return 0, self._info

    def hwnd(self):
        return 0

    def current(self):
        return self._info

    def start(self):
        return None

    def stop(self):
        return None


if __name__ == "__main__":
    unittest.main()
