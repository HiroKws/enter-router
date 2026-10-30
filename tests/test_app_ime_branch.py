"""The IME decision must be readable from the log (v2-l follow-up).

Why: in Chromium/Electron and Firefox the composition cannot be read from outside
the process, so "commit vs hijack" is decided by a heuristic (text typed since the
last Enter). When a user reports "Enter no longer commits 変換", those heuristic
inputs are the ONLY evidence — they used to be logged at DEBUG and therefore
never reached the log file.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import main as app_main  # noqa: E402
from enter_router.core.config import parse_config_text  # noqa: E402
from enter_router.core.events import ImeState, KeyEvent  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"
LOGGER = "enter_router.app"
VK_A = 0x41
VK_RETURN = 0x0D


def enter(t_ms=1000):
    return KeyEvent(vk=VK_RETURN, kind="down", t_ms=t_ms,
                    ctrl=False, alt=False, shift=False, win=False,
                    injected=False, os_repeat=False)


def letter(t_ms=900):
    return KeyEvent(vk=VK_A, kind="down", t_ms=t_ms,
                    ctrl=False, alt=False, shift=False, win=False,
                    injected=False, os_repeat=False)


class ImeBranchLogTests(unittest.TestCase):
    def setUp(self):
        cfg = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.app = app_main.EnterRouterApp(cfg, str(SAMPLE))
        self.app._hook_thread = None
        self.patch = mock.patch(
            "enter_router.platform.ime.query_composition",
            return_value=ImeState(composing=None, source="ime_native",
                                  detail="ime_mode=(True, 9); tsf: composition=0"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_a_bare_enter_records_why_it_was_not_treated_as_a_commit(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.app._decide(enter())
        line = next((l for l in logs.output if "ime_unobservable" in l), "")
        self.assertTrue(line, logs.output)
        self.assertIn("commit=0", line)
        self.assertIn("text_count=0", line)
        self.assertIn("source=ime_native", line)
        self.assertIn("profile=", line)

    def test_typing_before_the_enter_records_a_commit_decision(self):
        self.app._decide(letter())
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.app._decide(enter(t_ms=1200))
        line = next((l for l in logs.output if "ime_unobservable" in l), "")
        self.assertTrue(line, logs.output)
        self.assertIn("commit=1", line)
        self.assertIn("text_count=1", line)
        self.assertIn("same_window=1", line)


if __name__ == "__main__":
    unittest.main()
