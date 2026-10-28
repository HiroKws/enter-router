"""v2-h: かな入力中の Enter を「確定」と「普通の Enter」に分ける統合テスト.

Background (measured on the user's machine): in an Electron app the MS-IME
composition lives on a renderer thread, so no probe can see it. The router,
therefore, must not claim an Enter WHILE a composition exists — it can only use
the consequence: an Enter always ends a composition, so

  * text-producing keys pressed since the last Enter  => this Enter is a COMMIT
    (hand it to the IME, and never count it as a burst press), and
  * no text since the last Enter                     => there is nothing to
    commit, so it is an ordinary Enter the router may take over
    (1回 = 改行, 2回 = 送信, per the profile's keymap).

These tests drive the real composition root with the probe forced to answer
"unknown because かな mode" and assert exactly what the user asked for.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import main as app_main                      # noqa: E402
from enter_router.core.config import parse_config_text             # noqa: E402
from enter_router.core.events import (VK_RETURN, ForegroundInfo,    # noqa: E402
                                      ImeState, KeyEvent)
from enter_router.core.policy import Decision, State               # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"
VK_A = 0x41
# The sample config carries a live "site-a" profile (firefox.exe + "Site A"); a
# foreground that matches nothing would leave the router switched off.
_SITE_A = ForegroundInfo(process_name="firefox.exe", window_title="Site A",
                         url="https://site-a.example.com/chat")
VK_SHIFT = 0x10


class _StaticFg:
    """Foreground cache stand-in with a settable hwnd (0 off Windows)."""

    def __init__(self, info: ForegroundInfo):
        self._info = info
        self.hwnd_value = 0

    def hwnd(self) -> int:
        return self.hwnd_value

    def snapshot(self):
        return self.hwnd_value, self._info

    def current(self) -> ForegroundInfo:
        return self._info

    def start(self):
        return None

    def stop(self):
        return None


class NativeModeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.app = app_main.EnterRouterApp(self.cfg, str(SAMPLE))
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        self.sent: list[tuple] = []
        self._saved = (ime_mod.query_composition, inject.send_combo,
                       inject.send_enter, hook_mod.wake, hook_mod.request_stop)
        # かな入力中で、変換そのものは観測できない（= v2-h の "ime_native"）
        ime_mod.query_composition = lambda hwnd: ImeState(
            composing=None, source="ime_native", detail="ime_mode=(True, 9)")
        inject.send_combo = lambda combo, held=(): self.sent.append(("combo", combo)) or 4
        inject.send_enter = lambda n, held=(): self.sent.append(("enter", n)) or 2 * n
        hook_mod.wake = lambda: False
        hook_mod.request_stop = lambda reason="stop": None
        self.app._fg = _StaticFg(_SITE_A)

    def tearDown(self):
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        (ime_mod.query_composition, inject.send_combo, inject.send_enter,
         hook_mod.wake, hook_mod.request_stop) = self._saved
        self.app._timer.stop()

    # -- helpers ----------------------------------------------------------
    def _text(self, t: int, vk: int = VK_A) -> None:
        """A text key down+up (goes through the hook, not decided as an Enter)."""
        self.app._decide(KeyEvent(vk=vk, kind="down", t_ms=t))
        self.app._decide(KeyEvent(vk=vk, kind="up", t_ms=t + 20))

    def _enter(self, t: int):
        swallowed = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=t))
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="up", t_ms=t + 20))
        return swallowed

    def _deadline(self, t: int):
        out = self.app._machine.on_deadline(t)
        self.app._execute(out, None)
        return out

    # -- the contract -----------------------------------------------------
    def test_enter_after_typing_is_a_commit_not_a_press(self):
        """Text typed => the Enter belongs to the IME (commit), never counted."""
        self._text(1000)
        self.assertFalse(self._enter(1200))                # not swallowed
        self.assertIs(self.app._machine.state, State.IDLE)
        self.assertEqual(self.app._machine.pending_count, 0)
        self.assertEqual(self.sent, [])                    # nothing injected

    def test_after_a_commit_one_enter_is_a_newline(self):
        """The user's requirement: 確定後 1回 = 改行."""
        self._text(1000)
        self._enter(1200)                                  # commit
        self._text(1400, vk=0x42)                          # next word...
        self._enter(1600)                                  # ...commit again
        self.assertTrue(self._enter(3000))                 # 1回目 = 改行 (held)
        self.assertEqual(self.app._machine.pending_count, 1)
        out = self._deadline(3000 + self.cfg.policy.hold_window_ms)
        self.assertEqual(out.resolved, "key:shift+enter")  # sample profile's newline

    def test_after_a_commit_two_enters_are_a_send(self):
        """確定後 2回 = 送信."""
        self._text(1000)
        self._enter(1200)                                  # commit
        self.assertTrue(self._enter(3000))                 # 1回目
        self.assertTrue(self._enter(3120))                 # 2回目 (within 250ms)
        self.assertEqual(self.app._machine.pending_count, 2)
        out = self._deadline(3120 + self.cfg.policy.triple_enter_window_ms)
        self.assertEqual(out.resolved, "key:ctrl+enter")   # sample profile's send

    def test_typing_between_enters_never_steals_the_commit(self):
        """Every Enter with fresh text is a commit, however fast the typing is."""
        t = 1000
        for i in range(6):
            self._text(t, vk=VK_A + i)
            self.assertFalse(self._enter(t + 100))         # never swallowed
            t += 300
        self.assertEqual(self.app._machine.pending_count, 0)
        self.assertEqual(self.sent, [])

    def test_shift_and_other_modifiers_are_not_text(self):
        """A modifier alone must not look like typed text."""
        self.app._decide(KeyEvent(vk=VK_SHIFT, kind="down", t_ms=1000))
        self.app._decide(KeyEvent(vk=VK_SHIFT, kind="up", t_ms=1010))
        self.assertTrue(self._enter(1200))                 # ordinary Enter
        self.assertEqual(self.app._machine.pending_count, 1)

    def test_text_in_another_window_does_not_leak(self):
        """Text typed elsewhere must not turn this window's Enter into a commit."""
        self._text(1000)
        self.app._fg.hwnd_value = 777                      # focus moved
        self.assertTrue(self._enter(1200))
        self.assertEqual(self.app._machine.pending_count, 1)


class NativeModeOffTest(unittest.TestCase):
    """The same app, but the probe says 英数 (no かな): the old path must stay."""

    def setUp(self):
        self.cfg = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.app = app_main.EnterRouterApp(self.cfg, str(SAMPLE))
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        self.sent: list[tuple] = []
        self._saved = (ime_mod.query_composition, inject.send_combo,
                       inject.send_enter, hook_mod.wake, hook_mod.request_stop)
        ime_mod.query_composition = lambda hwnd: ImeState(
            composing=False, source="ime_alnum")
        inject.send_combo = lambda combo, held=(): self.sent.append(("combo", combo)) or 4
        inject.send_enter = lambda n, held=(): self.sent.append(("enter", n)) or 2 * n
        hook_mod.wake = lambda: False
        hook_mod.request_stop = lambda reason="stop": None
        self.app._fg = _StaticFg(_SITE_A)

    def tearDown(self):
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        (ime_mod.query_composition, inject.send_combo, inject.send_enter,
         hook_mod.wake, hook_mod.request_stop) = self._saved
        self.app._timer.stop()

    def test_typing_does_not_prevent_the_router_from_counting(self):
        self.app._decide(KeyEvent(vk=VK_A, kind="down", t_ms=1000))
        self.app._decide(KeyEvent(vk=VK_A, kind="up", t_ms=1020))
        swallowed = self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1200))
        self.assertTrue(swallowed)                         # counted, as before
        self.assertEqual(self.app._machine.pending_count, 1)


if __name__ == "__main__":
    unittest.main()
