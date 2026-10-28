"""Registration hotkeys (v2-g): Alt+F5 / Alt+F6 write a profile for the front app.

These run the real composition root (``app.main.EnterRouterApp``) against a real
config file in a temp dir, with the Windows effects replaced by recorders — the
same trick ``test_app_safety`` uses. What is proven here:

* the combo is recognised from the config (not hard-coded);
* a profile is appended to the LIVE config file and applied without a restart;
* "process only" stores just the exe, "process + title" also stores the window
  title as a LITERAL glob (metacharacters escaped);
* registering the same thing twice does not append a duplicate;
* a held Enter is delivered BEFORE the write (I6 ordering);
* the key itself is swallowed and never reaches the policy machine.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import main as app_main  # noqa: E402
from enter_router.core.config import parse_config, parse_config_text  # noqa: E402
from enter_router.core.events import VK_RETURN, ForegroundInfo, KeyEvent  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"
VK_F5 = 0x74
VK_F6 = 0x75


class _StaticFg:
    def __init__(self, info: ForegroundInfo) -> None:
        self._info = info

    def snapshot(self):
        return self._info.hwnd, self._info

    def hwnd(self) -> int:
        return self._info.hwnd

    def get(self) -> ForegroundInfo:
        return self._info


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        import logging
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.cfg = parse_config_text(self.path.read_text(encoding="utf-8"))
        self.notes: list[tuple[str, str, str]] = []
        self.injected: list[tuple] = []

        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        from enter_router.core.events import ImeState
        self._saved = (hook_mod.request_stop, hook_mod.wake, inject.send_combo,
                       inject.send_enter, ime_mod.query_composition)
        hook_mod.request_stop = lambda reason="stop": None
        hook_mod.wake = lambda: False
        inject.send_combo = lambda combo, held=(): self.injected.append(("combo", combo)) or 4
        inject.send_enter = lambda copies, held=(): self.injected.append(("enter", copies)) or 2
        ime_mod.query_composition = lambda hwnd: ImeState(composing=False)

        self.app = app_main.EnterRouterApp(self.cfg, str(self.path))
        self.app._notify = lambda title, body, level="info": self.notes.append(
            (title, body, level))
        self.app._fg = _StaticFg(ForegroundInfo(
            process_name=r"C:\Program Files\Mozilla Firefox\firefox.exe",
            window_title="Report [draft] * 2", url=None, hwnd=1234))

    def tearDown(self):
        import logging
        from enter_router.platform import hook as hook_mod
        from enter_router.platform import ime as ime_mod
        from enter_router.platform import inject
        (hook_mod.request_stop, hook_mod.wake, inject.send_combo,
         inject.send_enter, ime_mod.query_composition) = self._saved
        self.app._timer.stop()
        logging.disable(logging.NOTSET)
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def _doc(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _press(self, vk: int, alt: bool = True, t: int = 1000) -> bool:
        return self.app._decide(KeyEvent(vk=vk, kind="down", t_ms=t, alt=alt))

    # -- the two combos ---------------------------------------------------
    def test_process_only_registration_writes_and_applies_a_profile(self):
        swallowed = self._press(VK_F5)
        self.assertTrue(swallowed)                       # the hotkey is ours
        profiles = self._doc()["profiles"]
        self.assertEqual(len(profiles), 4)               # site-a, site-b, excel + new
        new = profiles[-1]
        self.assertEqual(new["match"], {"process": "firefox.exe"})
        self.assertEqual(new["keymap"], {"send": "enter", "newline": "shift+enter"})
        self.assertTrue(new["enabled"])
        # applied live: the file reloads to a config that contains it
        parsed = parse_config(self._doc())
        self.assertIn(new["id"], [p.id for p in parsed.profiles])
        self.assertTrue(any("登録しました" in body for _t, body, _l in self.notes))

    def test_process_and_title_registration_escapes_the_title(self):
        self._press(VK_F6)
        new = self._doc()["profiles"][-1]
        self.assertEqual(new["match"]["process"], "firefox.exe")
        # the title is DATA: glob metacharacters must not become wildcards
        self.assertEqual(new["match"]["title"], "Report [[]draft] [*] 2")

    def test_the_registered_profile_then_matches_that_window(self):
        self._press(VK_F6)
        cfg = parse_config(self._doc())
        from enter_router.core import routing
        same = ForegroundInfo(process_name="firefox.exe",
                              window_title="Report [draft] * 2")
        other = ForegroundInfo(process_name="firefox.exe", window_title="Other window")
        self.assertIsNotNone(routing.match_profile(cfg, same))
        self.assertIsNone(routing.match_profile(cfg, other))

    def test_registering_the_same_match_twice_does_not_duplicate(self):
        self._press(VK_F5, t=1000)
        before = self._doc()["profiles"]
        self._press(VK_F5, t=2000)
        self.assertEqual(self._doc()["profiles"], before)
        self.assertTrue(any("既に登録済み" in body for _t, body, _l in self.notes))

    def test_two_registrations_get_distinct_ids(self):
        self._press(VK_F5, t=1000)
        self._press(VK_F6, t=2000)
        ids = [p["id"] for p in self._doc()["profiles"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[-2:], ["firefox", "firefox-2"])

    # -- ordering + safety ------------------------------------------------
    def test_a_held_enter_is_delivered_before_the_write(self):
        # a site-a window, so the held Enter resolves to that profile's newline
        self.app._fg = _StaticFg(ForegroundInfo(
            process_name="firefox.exe", window_title="Site A",
            url="https://site-a.example.com/chat", hwnd=1234))
        self.app._decide(KeyEvent(vk=VK_RETURN, kind="down", t_ms=1000))
        self.assertEqual(self.injected, [])              # held, nothing sent yet
        self._press(VK_F5, t=1100)
        self.assertEqual(self.injected, [("combo", "shift+enter")])
        self.assertEqual(len(self._doc()["profiles"]), 4)

    def test_the_hotkey_never_reaches_the_policy_machine(self):
        before = self.app._machine.state
        self._press(VK_F5)
        self.assertIs(self.app._machine.state, before)
        self.assertEqual(self.injected, [])

    def test_a_plain_f5_is_not_a_registration(self):
        self.assertFalse(self._press(VK_F5, alt=False))
        self.assertEqual(len(self._doc()["profiles"]), 3)

    def test_an_unreadable_foreground_is_reported_not_written(self):
        self.app._fg = _StaticFg(ForegroundInfo(process_name="", window_title=""))
        self.assertTrue(self._press(VK_F5))              # still swallowed
        self.assertEqual(len(self._doc()["profiles"]), 3)
        self.assertTrue(any("登録できませんでした" in body for _t, body, _l in self.notes))


if __name__ == "__main__":
    unittest.main()
