"""Log retention: the folder must not grow forever (v2-m follow-up).

The rotating handler bounds ONE file (max_bytes x backup_count), but nothing ever
removed the generations themselves. These tests pin the rule: rotated files older
than KEEP_DAYS go, everything else stays, and the file the handler (or
faulthandler) is writing to is never touched.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.app import logsetup  # noqa: E402

DAY = 86400.0


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name, age_days):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def test_old_rotated_generations_are_deleted_and_recent_ones_stay(self):
        old = self.touch("enter-router.log.1", 9)
        old_crash = self.touch("crash.log.1", 9)
        recent = self.touch("enter-router.log.2", 1)
        live = self.touch("enter-router.log", 30)      # live file: never deleted
        removed = logsetup.prune(self.dir, keep_days=7)
        self.assertIn(old, removed)
        self.assertIn(old_crash, removed)
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(live))

    def test_unrelated_files_are_left_alone(self):
        other = self.touch("ime_check_pids.json", 90)
        logsetup.prune(self.dir, keep_days=7)
        self.assertTrue(os.path.exists(other))

    def test_the_live_crash_log_is_not_removed_while_faulthandler_holds_it(self):
        crash = self.touch("crash.log", 30)
        previous = logsetup._crash_file
        logsetup._crash_file = object()                # pretend it is open
        try:
            logsetup.prune(self.dir, keep_days=7)
        finally:
            logsetup._crash_file = previous
        self.assertTrue(os.path.exists(crash))

    def test_relative_days_are_based_on_the_given_clock(self):
        old = self.touch("enter-router.log.3", 3)
        removed = logsetup.prune(self.dir, keep_days=7,
                                 now=time.time() + 10 * DAY)   # 10 days later
        self.assertIn(old, removed)

    def test_a_missing_folder_is_not_an_error(self):
        self.assertEqual(logsetup.prune(os.path.join(self.dir, "nope")), [])


if __name__ == "__main__":
    unittest.main()
