"""``app/ime_unknown.py`` — unknown IME state must never be silent (t_dd246e5a).

What is asserted here is exactly the acceptance criterion: an unknown state
notifies the user ONCE, always leaves an ``ime_state=unknown action=...`` line in
the log, and stops flooding the log while the condition persists.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app.ime_unknown import (  # noqa: E402
    LOG_INTERVAL_MS, NOTIFY_TITLE, ImeUnknownReporter, body_for,
)

LOGGER = "enter_router.ime"


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.notifications: list[tuple] = []

        def notify(title, body, level="info"):
            self.notifications.append((title, body, level))

        self.reporter = ImeUnknownReporter(notify=notify, clock=self.clock)

    # -- once-only notification ------------------------------------------
    def test_first_unknown_notifies_once_when_the_enter_is_still_converted(self):
        """policy=process is the risky one: the user must be told about it."""
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertTrue(self.reporter.report("process", detail="ImmGetContext=0 err=5"))
        self.assertEqual(len(self.notifications), 1)
        title, body, level = self.notifications[0]
        self.assertEqual(title, NOTIFY_TITLE)
        self.assertIn("process", body)
        self.assertEqual(level, "warning")
        self.assertTrue(any("ime_state=unknown action=process" in line
                            for line in logs.output))

    def test_the_safe_policy_is_logged_but_never_announced(self):
        """policy=pass (default) makes unknown normal — a toast would be noise."""
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertFalse(self.reporter.report("pass"))
        self.assertEqual(self.notifications, [])
        self.assertTrue(any("ime_state=unknown action=pass" in line
                            for line in logs.output))

    def test_second_and_third_unknown_do_not_notify_again(self):
        self.reporter.report("process")
        self.clock.advance_ms(5_000)
        self.assertFalse(self.reporter.report("process"))
        self.clock.advance_ms(5_000)
        self.assertFalse(self.reporter.report("process"))
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.reporter.occurrences, 3)

    def test_notification_fires_even_when_the_log_is_suppressed(self):
        """The user is told once; suppression only trims the LOG, not the notice."""
        self.reporter.report("process")            # logs + notifies
        self.clock.advance_ms(100)                 # inside the log interval
        with self.assertNoLogs(LOGGER, level="WARNING"):
            notified = self.reporter.report("process")
        self.assertFalse(notified)

    # -- log contract ------------------------------------------------------
    def test_log_line_carries_the_action_and_the_probe_detail(self):
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.reporter.report("process", detail="ImmGetContext=0 err=5",
                                 source="context")
        line = "\n".join(logs.output)
        self.assertIn("ime_state=unknown action=process", line)
        self.assertIn("ImmGetContext=0 err=5", line)
        self.assertIn("source=context", line)

    def test_log_is_rate_limited_to_one_per_interval(self):
        with self.assertLogs(LOGGER, level="DEBUG") as logs:
            self.reporter.report("pass")           # warning
            self.clock.advance_ms(1_000)
            self.reporter.report("pass")           # debug (suppressed)
            self.clock.advance_ms(1_000)
            self.reporter.report("pass")           # debug (suppressed)
            self.clock.advance_ms(LOG_INTERVAL_MS)
            self.reporter.report("pass")           # warning again (interval passed)
        warnings = [line for line in logs.output if "WARNING" in line
                    and "ime_state=unknown action=" in line]
        self.assertEqual(len(warnings), 2)
        self.assertIn("occurrences=4", warnings[-1])

    # -- containment -------------------------------------------------------
    def test_a_failing_notifier_never_raises(self):
        def boom(title, body, level="info"):
            raise RuntimeError("tray exploded")

        reporter = ImeUnknownReporter(notify=boom, clock=self.clock)
        self.assertTrue(reporter.report("process"))    # still reported as notified

    def test_without_a_notifier_it_still_logs(self):
        reporter = ImeUnknownReporter(clock=self.clock)
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertTrue(reporter.report("process"))
        self.assertTrue(reporter.notified)

    def test_reset_allows_one_more_notification(self):
        self.reporter.report("process")
        self.reporter.reset()
        self.assertEqual(self.reporter.occurrences, 0)
        self.assertFalse(self.reporter.notified)
        self.assertTrue(self.reporter.report("process"))
        self.assertEqual(len(self.notifications), 2)

    def test_body_mentions_the_configured_behaviour(self):
        self.assertIn("pass", body_for("pass"))
        self.assertIn("process", body_for("process"))


if __name__ == "__main__":
    unittest.main()
