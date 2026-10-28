"""Three-valued probe + route cache of ``platform/ime.py`` (runs off-Windows).

The Win32 calls themselves can only be exercised on hardware
(``scripts/win_ime_check.py`` does that). What CAN be asserted here is the logic
the hook depends on:

* the answer is one of composing / not_composing / unknown, and "could not read"
  never masquerades as "not composing";
* the ROUTE (GetGUIThreadInfo / ImmGetDefaultIMEWnd / which candidate answered)
  is discovered once per foreground window, not on every call;
* the ANSWER is re-read on every call (it changes while typing);
* a window that failed is remembered as unknown only for the retry window, so a
  transient failure never becomes permanent;
* ``query`` never raises, whatever the Win32 layer does.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.platform import ime as ime_mod, win32lib  # noqa: E402


class ClassifyCompositionTest(unittest.TestCase):
    """The pure rule (regression: a false "not_composing" broke Japanese input)."""

    def _c(self, tsf, process=(), mode=None):
        return ime_mod.classify_composition(tsf, process, mode)

    def test_a_thread_composition_window_wins(self):
        self.assertEqual(self._c("composing")[0], "composing")

    def test_a_process_wide_composition_window_wins(self):
        """A Chromium/Electron app composes on a renderer thread, not the UI one."""
        verdict, why = self._c("not_composing", (ime_mod.COMPOSITION_CLASS,), None)
        self.assertEqual((verdict, why), ("composing", "tsf_process"))

    def test_native_ime_mode_is_unknown_never_not_composing(self):
        """THE regression: open + かな must not claim "not composing"."""
        verdict, why = self._c("not_composing", (), (True, 0x9))   # NATIVE|FULLSHAPE
        self.assertIsNone(verdict)
        self.assertIn("ime_native_unobservable", why)

    def test_alphanumeric_ime_mode_is_not_composing(self):
        verdict, why = self._c("not_composing", (), (True, 0x0))
        self.assertEqual((verdict, why), ("not_composing", "ime_alnum(conv=0x0)"))

    def test_closed_ime_is_not_composing(self):
        self.assertEqual(self._c(None, (), (False, 0x9))[0], "not_composing")

    def test_anchor_only_is_unknown_without_a_readable_ime_mode(self):
        """The anchor alone never proves "no composition" (that broke 日本語入力)."""
        self.assertIsNone(self._c("not_composing", (), None)[0])

    def test_no_evidence_is_unknown(self):
        self.assertIsNone(self._c(None, (), None)[0])


class JapaneseInputSafetyTest(unittest.TestCase):
    """End of the probe: what the POLICY sees while typing Japanese.

    Measured failure (v2-h): in an app whose composition the thread scan cannot
    see, the probe answered "not composing" while the user was composing, the
    machine swallowed the Enter and injected ``key:shift+enter`` — the IME never
    committed and Japanese input was unusable. These tests pin the fix.
    """

    def setUp(self):
        self._saved = (win32lib.IS_WINDOWS, ime_mod._init_win32,
                       ime_mod._read_context, ime_mod._gui_thread_info,
                       ime_mod._tsf_scan, ime_mod.ime_mode,
                       ime_mod._process_windows, ime_mod._default_ime_wnd,
                       ime_mod._window_pid)
        win32lib.IS_WINDOWS = True
        ime_mod._init_win32 = lambda: None
        ime_mod._gui_thread_info = lambda tid: {"hwndFocus": 0, "hwndCaret": 0}
        ime_mod._read_context = lambda hwnd: (None, "ImmGetContext=0 err=5")
        ime_mod._tsf_scan = lambda tids: ("not_composing", "tsf: anchor only")
        ime_mod._default_ime_wnd = lambda hwnd: 0xABC
        ime_mod._window_pid = lambda hwnd: 4242
        self.mode: tuple[bool, int] | None = (True, 0x9)
        self.process: tuple[str, ...] = ()
        ime_mod.ime_mode = lambda wnd: self.mode
        ime_mod._process_windows = lambda pid: self.process
        self.probe = ime_mod.CompositionProbe(clock=lambda: 1000.0)

    def tearDown(self):
        (win32lib.IS_WINDOWS, ime_mod._init_win32, ime_mod._read_context,
         ime_mod._gui_thread_info, ime_mod._tsf_scan, ime_mod.ime_mode,
         ime_mod._process_windows, ime_mod._default_ime_wnd,
         ime_mod._window_pid) = self._saved

    def test_composing_in_japanese_mode_reads_as_unknown_so_enter_passes(self):
        state = self.probe.query(7)
        self.assertIsNone(state.composing)          # -> ime_unknown_policy "pass"
        self.assertIn("ime_native_unobservable", state.detail)

    def test_alphanumeric_mode_still_allows_the_enter_policy(self):
        self.mode = (True, 0x0)
        state = self.probe.query(7)
        self.assertIs(state.composing, False)
        self.assertEqual(state.source, "ime_alnum")

    def test_a_renderer_thread_composition_is_found_by_the_process_scan(self):
        self.mode = None                            # IME window unreadable here
        self.process = (ime_mod.COMPOSITION_CLASS,)
        state = self.probe.query(7)
        self.assertIs(state.composing, True)
        self.assertEqual(state.source, "tsf_process")

    def test_nothing_readable_is_still_unknown(self):
        self.mode = None
        self.process = ()
        state = self.probe.query(7)
        self.assertIsNone(state.composing)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self._saved = (win32lib.IS_WINDOWS, ime_mod._init_win32,
                       ime_mod._read_context, ime_mod._gui_thread_info,
                       ime_mod._tsf_scan)
        # Pretend we are on Windows so _query proceeds, but keep every Win32
        # call stubbed: nothing here touches a real machine.
        win32lib.IS_WINDOWS = True
        ime_mod._init_win32 = lambda: None
        ime_mod._gui_thread_info = lambda tid: {"hwndFocus": 0, "hwndCaret": 0}
        self.read_calls: list[int] = []
        self.answers: dict[int, tuple[str | None, str]] = {}
        self.read_error: BaseException | None = None

        def fake_read(hwnd):
            self.read_calls.append(hwnd)
            if self.read_error is not None:
                raise self.read_error
            return self.answers.get(hwnd, (None, "ImmGetContext=0 err=5"))

        ime_mod._read_context = fake_read
        # The TSF stage: default = "no signal", which keeps the pre-existing
        # expectations (a failed IMM route is unknown) true.
        self.tsf_calls: list[tuple[int, ...]] = []
        self.tsf_answer: tuple[str | None, str] = (None, "tsf: stubbed")
        self.tsf_error: BaseException | None = None

        def fake_tsf(tids):
            self.tsf_calls.append(tuple(tids))
            if self.tsf_error is not None:
                raise self.tsf_error
            return self.tsf_answer

        ime_mod._tsf_scan = fake_tsf
        self.now = 1000.0
        self.probe = ime_mod.CompositionProbe(unknown_retry_ms=2000,
                                              clock=lambda: self.now)

    def tearDown(self):
        (win32lib.IS_WINDOWS, ime_mod._init_win32, ime_mod._read_context,
         ime_mod._gui_thread_info, ime_mod._tsf_scan) = self._saved

    # -- three values ----------------------------------------------------
    def test_composing(self):
        self.answers[7] = ("composing", "open=1 bytes=6")
        state = self.probe.query(7)
        self.assertIs(state.composing, True)
        self.assertEqual(state.state, "composing")
        self.assertEqual(state.source, "context")

    def test_not_composing(self):
        self.answers[7] = ("not_composing", "open=1 bytes=0")
        state = self.probe.query(7)
        self.assertIs(state.composing, False)
        self.assertEqual(state.state, "not_composing")

    def test_unknown_keeps_the_failure_reason(self):
        state = self.probe.query(7)
        self.assertIsNone(state.composing)
        self.assertEqual(state.state, "unknown")
        self.assertIn("ImmGetContext=0 err=5", state.detail)

    def test_no_foreground_window_is_unknown_and_touches_nothing(self):
        state = self.probe.query(0)
        self.assertIsNone(state.composing)
        self.assertEqual(state.detail, "no-foreground-window")
        self.assertEqual(self.read_calls, [])

    # -- cache -----------------------------------------------------------
    def test_answer_is_re_read_on_every_call(self):
        """Caching the ANSWER would be a correctness bug (composition changes)."""
        self.answers[7] = ("composing", "bytes=6")
        self.assertIs(self.probe.query(7).composing, True)
        self.answers[7] = ("not_composing", "bytes=0")   # the user pressed Enter
        self.assertIs(self.probe.query(7).composing, False)
        self.assertEqual(self.read_calls, [7, 7])

    def test_route_discovery_happens_once_per_window(self):
        self.answers[7] = ("not_composing", "bytes=0")
        discoveries = []
        ime_mod._gui_thread_info = lambda tid: (discoveries.append(tid),
                                                {"hwndFocus": 0, "hwndCaret": 0})[1]
        self.probe.query(7)
        self.probe.query(7)
        self.probe.query(7)
        self.assertEqual(len(discoveries), 1)   # not once per Enter press

    def test_a_new_foreground_window_is_rediscovered(self):
        self.answers[7] = ("not_composing", "bytes=0")
        self.answers[9] = ("composing", "bytes=6")
        self.assertIs(self.probe.query(7).composing, False)
        self.assertIs(self.probe.query(9).composing, True)
        self.assertEqual(self.read_calls, [7, 9])

    def test_failed_window_is_remembered_for_the_retry_window_only(self):
        first = self.probe.query(7)
        self.assertIsNone(first.composing)
        calls_after_first = len(self.read_calls)
        second = self.probe.query(7)          # inside the retry window
        self.assertIsNone(second.composing)
        # The cached answer keeps the PROVENANCE of the failure, so the app layer
        # can still tell "かな mode, invisible composition" from "nothing readable".
        self.assertEqual(second.source, "unknown")
        self.assertEqual(len(self.read_calls), calls_after_first)  # no new Win32 work
        self.answers[7] = ("composing", "bytes=6")                # it becomes readable
        self.now += 2.1                                            # retry window over
        self.assertIs(self.probe.query(7).composing, True)

    def test_cached_failure_does_not_leak_into_a_new_window(self):
        self.probe.query(7)
        self.answers[9] = ("not_composing", "bytes=0")
        self.assertIs(self.probe.query(9).composing, False)

    # -- containment -----------------------------------------------------
    def test_query_never_raises_when_the_win32_layer_raises(self):
        self.read_error = OSError("imm32 blew up")
        state = self.probe.query(7)
        self.assertIsNone(state.composing)
        self.assertEqual(state.detail, "probe-exception")

    def test_reset_forgets_the_route(self):
        self.answers[7] = ("not_composing", "bytes=0")
        self.probe.query(7)
        self.probe.reset()
        self.probe.query(7)
        self.assertEqual(self.read_calls, [7, 7])


class FallbackOrderTest(unittest.TestCase):
    """The candidate list is what makes stage (a)/(b) of the card work."""

    def setUp(self):
        self._saved = (win32lib.IS_WINDOWS, ime_mod._init_win32,
                       ime_mod._gui_thread_info, ime_mod._window_thread)
        win32lib.IS_WINDOWS = True
        ime_mod._init_win32 = lambda: None
        ime_mod._window_thread = lambda hwnd: 42
        ime_mod._gui_thread_info = lambda tid: {"hwndFocus": 0x22, "hwndCaret": 0x33}
        self.read_calls = []
        self.answers = {0x11: (None, "ImmGetContext=0 err=5"),   # top-level fails
                        0x21: (None, "ImmGetContext=0 err=5"),   # ime wnd fails
                        0x22: ("composing", "bytes=3")}          # focus window works
        ime_mod._read_context = lambda hwnd: (self.read_calls.append(hwnd),
                                              self.answers.get(hwnd, (None, "x")))[1]
        self._saved_imewnd = ime_mod.win32lib.dll

    def tearDown(self):
        (win32lib.IS_WINDOWS, ime_mod._init_win32,
         ime_mod._gui_thread_info, ime_mod._window_thread) = self._saved
        ime_mod.win32lib.dll = self._saved_imewnd

    def test_falls_back_to_ime_window_then_focus_window(self):
        class FakeImm:
            @staticmethod
            def ImmGetDefaultIMEWnd(hwnd):
                return 0x21

        ime_mod.win32lib.dll = lambda name: FakeImm
        probe = ime_mod.CompositionProbe(clock=lambda: 0.0)
        state = probe.query(0x11)
        self.assertIs(state.composing, True)
        self.assertEqual(state.source, "gui_focus")
        self.assertEqual(self.read_calls, [0x11, 0x21, 0x22])
        # the winner is tried first next time (fewer Win32 calls per Enter)
        self.assertIn("gui_focus", state.detail)


class TsfStageTest(unittest.TestCase):
    """The stage that actually answers for another process (measured rule)."""

    def setUp(self):
        self._saved = (win32lib.IS_WINDOWS, ime_mod._init_win32,
                       ime_mod._gui_thread_info, ime_mod._window_thread,
                       ime_mod._thread_windows, ime_mod.win32lib.dll,
                       ime_mod._tsf_scan, ime_mod.ime_mode)
        win32lib.IS_WINDOWS = True
        ime_mod._init_win32 = lambda: None
        ime_mod._window_thread = lambda hwnd: 42
        # v2-h: "not composing" needs the IME window's mode as well (the anchor
        # alone is no longer accepted as proof). Default here = unreadable, i.e.
        # the blind case; a test that wants a positive answer sets self.mode.
        self.mode: tuple[bool, int] | None = None
        ime_mod.ime_mode = lambda wnd: self.mode
        ime_mod._gui_thread_info = lambda tid: {"hwndFocus": 0, "hwndCaret": 0}
        self.windows: dict[int, tuple[tuple[int, str], ...]] = {42: ()}
        ime_mod._thread_windows = lambda tid: self.windows.get(tid, ())
        self.read_calls: list[int] = []
        ime_mod._read_context = lambda hwnd: (
            self.read_calls.append(hwnd), (None, "ImmGetContext=0 err=0"))[1]

        class FakeImm:
            @staticmethod
            def ImmGetDefaultIMEWnd(hwnd):
                return 0

        ime_mod.win32lib.dll = lambda name: FakeImm
        self.tsf_calls = 0
        real_tsf = self._saved[6]

        def counting(tids):
            self.tsf_calls += 1
            return real_tsf(tids)

        ime_mod._tsf_scan = counting

    def tearDown(self):
        (win32lib.IS_WINDOWS, ime_mod._init_win32, ime_mod._gui_thread_info,
         ime_mod._window_thread, ime_mod._thread_windows,
         ime_mod.win32lib.dll, ime_mod._tsf_scan, ime_mod.ime_mode) = self._saved

    def test_composition_window_is_composing(self):
        """Measured: the window appears only while a composition is alive."""
        self.windows[42] = ((0x10, "Edit"), (0x11, "MSCTFIME Composition"),
                            (0x12, "MSCTFIME UI"))
        state = ime_mod.CompositionProbe().query(0x10)
        self.assertIs(state.composing, True)
        self.assertEqual(state.state, "composing")
        self.assertEqual(state.source, "tsf_composition")
        self.assertIn("composition=1", state.detail)

    def test_anchor_without_composition_is_not_composing_in_alnum_mode(self):
        """No composition window IS evidence — together with a readable 英数 IME.

        v2-h: the anchor alone became insufficient (it produced a false
        "not composing" while typing Japanese). The positive read that replaced it
        is the IME window's conversion mode.
        """
        self.windows[42] = ((0x10, "Edit"), (0x12, "MSCTFIME UI"))
        self.mode = (True, 0x0)                       # IME open, 英数 (no かな)
        state = ime_mod.CompositionProbe().query(0x10)
        self.assertIs(state.composing, False)
        self.assertEqual(state.state, "not_composing")
        self.assertEqual(state.source, "ime_alnum")

    def test_anchor_without_a_readable_ime_mode_is_unknown(self):
        self.windows[42] = ((0x10, "Edit"), (0x12, "MSCTFIME UI"))
        state = ime_mod.CompositionProbe().query(0x10)
        self.assertIsNone(state.composing)            # Enter goes to the IME

    def test_no_msctfime_at_all_stays_unknown(self):
        """A TSF-native app must not be reported as 'not composing'."""
        self.windows[42] = ((0x10, "Chrome_WidgetWin_1"),)
        state = ime_mod.CompositionProbe().query(0x10)
        self.assertIsNone(state.composing)
        self.assertEqual(state.state, "unknown")
        self.assertIn("ime_ui=False", state.detail)

    def test_tsf_answer_is_re_read_every_call(self):
        self.windows[42] = ((0x10, "Edit"), (0x12, "MSCTFIME UI"))
        self.mode = (True, 0x0)
        probe = ime_mod.CompositionProbe()
        self.assertIs(probe.query(0x10).composing, False)
        self.windows[42] = ((0x10, "Edit"), (0x11, "MSCTFIME Composition"),
                            (0x12, "MSCTFIME UI"))
        self.assertIs(probe.query(0x10).composing, True)
        self.windows[42] = ((0x10, "Edit"), (0x12, "MSCTFIME UI"))
        self.assertIs(probe.query(0x10).composing, False)

    def test_imm_is_skipped_after_a_structural_failure(self):
        """No attach attempt per Enter once IMM is known dead for this window."""
        self.windows[42] = ((0x10, "Edit"), (0x12, "MSCTFIME UI"))
        self.mode = (True, 0x0)                       # a stable, positive answer
        probe = ime_mod.CompositionProbe()
        probe.query(0x10)
        self.assertEqual(self.read_calls, [0x10])       # one probe of the IMM route
        probe.query(0x10)
        probe.query(0x10)
        self.assertEqual(self.read_calls, [0x10])       # no more IMM work
        self.assertEqual(self.tsf_calls, 3)             # the answer is still re-read

    def test_imm_dead_is_dropped_when_the_foreground_window_changes(self):
        self.windows[42] = ((0x10, "Edit"), (0x12, "MSCTFIME UI"))
        probe = ime_mod.CompositionProbe()
        probe.query(0x10)
        self.assertEqual(self.read_calls, [0x10])
        probe.query(0x20)                               # new foreground window
        self.assertEqual(self.read_calls, [0x10, 0x20])

    def test_imm_route_is_retried_after_the_unknown_window_expires(self):
        """A transient failure must never become permanent (no unknown answer)."""
        self.windows[42] = ((0x10, "Foo"),)             # no MSCTFIME at all
        now = [1000.0]
        probe = ime_mod.CompositionProbe(unknown_retry_ms=2000,
                                         clock=lambda: now[0])
        self.assertIsNone(probe.query(0x10).composing)
        self.assertEqual(self.read_calls, [0x10])
        ime_mod._read_context = lambda hwnd: (
            self.read_calls.append(hwnd), ("composing", "bytes=6"))[1]
        now[0] += 2.1
        self.assertIs(probe.query(0x10).composing, True)

    def test_probe_never_raises_when_the_tsf_scan_raises(self):
        def boom(tids):
            raise OSError("EnumThreadWindows blew up")

        ime_mod._tsf_scan = boom
        state = ime_mod.CompositionProbe().query(0x10)
        self.assertIsNone(state.composing)              # contained, not crashed


class TsfScanTest(unittest.TestCase):
    """``_tsf_scan`` itself: the classification rule, without the probe cache."""

    def setUp(self):
        self._saved = ime_mod._thread_windows
        self.windows: dict[int, tuple[tuple[int, str], ...]] = {}
        ime_mod._thread_windows = lambda tid: self.windows.get(tid, ())

    def tearDown(self):
        ime_mod._thread_windows = self._saved

    def test_any_composition_window_wins_even_if_hidden(self):
        self.windows = {1: ((1, "MSCTFIME Composition"), (2, "MSCTFIME UI"))}
        state, detail = ime_mod._tsf_scan((1,))
        self.assertEqual(state, "composing")
        self.assertIn("composition=1", detail)

    def test_anchor_only_is_not_composing(self):
        self.windows = {1: ((2, "MSCTFIME UI"),)}
        self.assertEqual(ime_mod._tsf_scan((1,))[0], "not_composing")

    def test_nothing_msctfime_is_unknown(self):
        self.windows = {1: ((3, "Edit"),)}
        state, detail = ime_mod._tsf_scan((1,))
        self.assertIsNone(state)
        self.assertIn("classes=[Edit]", detail)

    def test_a_composition_on_any_scanned_thread_counts(self):
        self.windows = {1: ((3, "Edit"),), 2: ((4, "MSCTFIME Composition"),)}
        self.assertEqual(ime_mod._tsf_scan((1, 2))[0], "composing")

    def test_no_thread_ids_is_unknown_and_does_nothing(self):
        self.assertIsNone(ime_mod._tsf_scan(())[0])


if __name__ == "__main__":
    unittest.main()
