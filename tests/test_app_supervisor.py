"""Hook liveness supervisor (``app/supervisor.py`` + the app wiring).

The bug these pin down: the hook could be gone while the app reported
"running" — keys passed through, the tray icon stayed, and nothing in the log
said the tool had stopped intercepting. Two separate wrong behaviours are
covered:

* an exit *nobody asked for* must NOT be recorded as "paused" (that made the
  silent stop look intentional and permanent);
* an exit we asked for (pause / panic / shutdown) must never be "repaired" —
  stopping on purpose has to stay stopped.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import main as app_main  # noqa: E402
from enter_router.app import supervisor  # noqa: E402
from enter_router.core.config import parse_config_text  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"


class NeedsRepairTests(unittest.TestCase):
    def test_running_without_a_hook_needs_repair(self):
        self.assertTrue(supervisor.needs_repair("running", False, False))
        self.assertTrue(supervisor.needs_repair("running", True, False))

    def test_running_with_a_live_hook_needs_nothing(self):
        self.assertFalse(supervisor.needs_repair("running", True, True))

    def test_intentional_pass_through_states_are_never_repaired(self):
        for state in ("paused", "panic", "busy"):
            self.assertFalse(supervisor.needs_repair(state, False, False), state)

    def test_give_up_after_the_attempt_budget(self):
        self.assertFalse(supervisor.give_up(supervisor.MAX_REPAIR_ATTEMPTS - 1))
        self.assertTrue(supervisor.give_up(supervisor.MAX_REPAIR_ATTEMPTS))


class HookExitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.app = app_main.EnterRouterApp(self.cfg, str(SAMPLE))
        self.app._state = "running"
        self.app._hook_installed = True
        self.repairs: list[int] = []
        self.app._install_hook = lambda: self.repairs.append(1)
        self.notices: list[tuple] = []
        self.app._notify = lambda title, body, level="info": self.notices.append(
            (title, body, level))

    def test_unrequested_exit_keeps_running_so_the_supervisor_repairs_it(self):
        self.app._on_hook_exit()
        self.assertEqual(self.app._state, "running")
        self.assertFalse(self.app._hook_installed)
        self.app._check_hook()
        self.assertEqual(len(self.repairs), 1)

    def test_pause_is_not_repaired(self):
        self.app._hook_stop_intent = "paused"
        self.app._on_hook_exit()
        self.assertEqual(self.app._state, "paused")
        self.app._check_hook()
        self.assertEqual(self.repairs, [])

    def test_panic_is_not_repaired(self):
        self.app._hook_stop_intent = "panic"
        self.app._state = "panic"
        self.app._on_hook_exit()
        self.app._check_hook()
        self.assertEqual(self.repairs, [])

    def test_repair_gives_up_loudly_instead_of_looping_forever(self):
        self.app._on_hook_exit()                     # unexpected -> "running"
        for _ in range(supervisor.MAX_REPAIR_ATTEMPTS):
            self.app._check_hook()
        self.assertEqual(self.app._state, "paused")
        self.assertTrue(any(level == "error" for _t, _b, level in self.notices))

    def test_successful_repair_resets_the_attempt_count(self):
        self.app._on_hook_exit()
        self.app._repair_attempts = 3
        self.app._install_hook = lambda: (
            self.repairs.append(1), setattr(self.app, "_hook_installed", True))
        self.app._check_hook()
        self.assertEqual(self.app._repair_attempts, 0)

    def test_a_stale_hook_thread_does_not_mark_the_live_hook_as_gone(self):
        class Dummy:            # stands in for the live hook thread
            name = "hook"

        self.app._hook_thread = Dummy()
        self.app._on_hook_exit()
        self.assertEqual(self.app._state, "running")
        self.assertTrue(self.app._hook_installed)

    def test_watchdog_can_be_started_again_after_it_was_stopped(self):
        # `_on_hook_exit` stops the watchdog (a missing hook is not a hang), so a
        # re-install must bring it back: without that the stale-heartbeat net was
        # gone for the rest of the process lifetime after the first pause.
        self.app._on_hook_exit()
        self.assertIsNone(self.app._watchdog_thread)
        self.app._start_watchdog()
        try:
            self.assertIsNotNone(self.app._watchdog_thread)
            self.assertTrue(self.app._watchdog_thread.is_alive())
        finally:
            self.app._stopped.set()


if __name__ == "__main__":
    unittest.main()
