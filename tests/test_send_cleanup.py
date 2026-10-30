"""v2-m: the Backspace that cleans up after a double-Enter send.

A chat input that "sends and clears" without preventDefault leaves the browser's
own newline in the just-cleared box (measured in Chromium). One Backspace on the
empty box removes it; on an input that leaves nothing, a Backspace in an empty
field is ignored. These tests pin the two things that must not go wrong:

* the Backspace is sent ONLY for a double-Enter send (single Enter = newline,
  ctrl+Enter = send, a sink trigger: none of them get it);
* sending the cleaner never schedules another cleaner (no loop).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import main as app_main  # noqa: E402
from enter_router.core.config import parse_config_text  # noqa: E402
from enter_router.core.policy import Decision, Outcome, State  # noqa: E402

SAMPLE = REPO / "config" / "sample.config.json"


def outcome(action, resolved, decision=Decision.DELIVER, count=1):
    return Outcome(decision=decision, state=State.IDLE, swallow=False,
                   action=action, resolved=resolved, count=count, reason="test")


class SendCleanupTests(unittest.TestCase):
    def setUp(self):
        cfg = parse_config_text(SAMPLE.read_text(encoding="utf-8"))
        self.app = app_main.EnterRouterApp(cfg, str(SAMPLE))
        self.sent: list[tuple] = []
        self.patch = mock.patch(
            "enter_router.platform.inject.send_combo",
            side_effect=lambda combo, held=(): self.sent.append((combo, tuple(held))) or 2)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_double_enter_send_injects_the_send_key_then_a_backspace(self):
        self.app._inject(outcome("enter2", "key:enter"))
        self.assertEqual(self.sent, [("enter", ())])
        # the cleaner goes through the queue (delayed), so run it explicitly
        self.app._enqueue_send_cleanup()
        self.app._drain_injections()
        self.assertEqual(self.sent, [("enter", ()), ("backspace", ())])

    def test_a_single_enter_newline_gets_no_backspace(self):
        with mock.patch.object(self.app, "_schedule_send_cleanup") as sched:
            self.app._inject(outcome("enter", "key:shift+enter"))
        sched.assert_not_called()
        self.assertEqual(self.sent, [("shift+enter", ())])

    def test_the_cleaner_itself_never_schedules_another_cleaner(self):
        with mock.patch.object(self.app, "_schedule_send_cleanup") as sched:
            self.app._inject(outcome("send_cleanup", "key:backspace"))
        sched.assert_not_called()
        self.assertEqual(self.sent, [("backspace", ())])

    def test_the_cleaner_is_queued_as_a_backspace_key_delivery(self):
        with mock.patch.object(self.app, "_dispatch_inject") as dispatch:
            self.app._enqueue_send_cleanup()
        queued = dispatch.call_args[0][0]
        self.assertEqual(queued.resolved, "key:backspace")
        self.assertEqual(queued.action, "send_cleanup")
        self.assertEqual(queued.decision, Decision.DELIVER)


if __name__ == "__main__":
    unittest.main()
