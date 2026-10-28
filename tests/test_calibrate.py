"""Tests for core/calibrate.py — the measured burst window must be reproducible.

Run on Linux/WSL:  python3 -m unittest discover -s tests -v

The numbers below are the rule, not a measurement: a burst sample is a gap the
operator produced while pressing consecutively, a separated sample is the gap
they produced while deliberately NOT doing so. The window has to sit above all
of the first and below all of the second.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.core import calibrate  # noqa: E402


class TestRecommendWindow(unittest.TestCase):
    def test_clean_clusters_take_headroom_over_the_slowest_burst(self):
        cal = calibrate.recommend_window([96, 113, 135, 157], [900, 1200, 1500])
        self.assertTrue(cal.ok)
        self.assertEqual(cal.burst_max_ms, 157)
        self.assertEqual(cal.separated_min_ms, 900)
        # 157 * 1.25 = 196 -> rounded to 200, well below 900 * 0.8 = 720
        self.assertEqual(cal.window_ms, 200)
        self.assertIn("slowest burst", cal.note)

    def test_the_window_is_never_at_or_below_the_slowest_burst_gap(self):
        cal = calibrate.recommend_window([100, 200], [2000])
        self.assertGreater(cal.window_ms, cal.burst_max_ms)

    def test_the_window_is_never_at_or_above_the_fastest_pause(self):
        cal = calibrate.recommend_window([96, 200], [330])
        self.assertLess(cal.window_ms, 330)

    def test_tight_but_separable_clusters_take_the_midpoint(self):
        cal = calibrate.recommend_window([200], [260])
        self.assertTrue(cal.ok)
        self.assertEqual(cal.window_ms, 230)     # (200 + 260) / 2
        self.assertIn("only just separate", cal.note)

    def test_overlapping_clusters_are_flagged_not_hidden(self):
        cal = calibrate.recommend_window([300, 420], [380, 900])
        self.assertFalse(cal.ok)
        self.assertIn("OVERLAP", cal.note)
        self.assertEqual(cal.window_ms, 520)     # burst_max 420 * 1.25 = 525 -> 520
        self.assertGreaterEqual(cal.window_ms, calibrate.WINDOW_LOW_MS)

    def test_only_burst_samples(self):
        cal = calibrate.recommend_window([120, 160], [])
        self.assertEqual(cal.window_ms, 200)
        self.assertTrue(cal.ok)
        self.assertEqual(cal.separated_samples, 0)

    def test_only_separated_samples(self):
        cal = calibrate.recommend_window([], [1000])
        self.assertEqual(cal.window_ms, 600)     # 800 clamped to the schema maximum
        self.assertTrue(cal.ok)

    def test_no_samples_keeps_the_default_and_says_so(self):
        cal = calibrate.recommend_window([], [])
        self.assertFalse(cal.ok)
        self.assertEqual(cal.window_ms, 250)
        self.assertIn("no samples", cal.note)

    def test_clamped_to_the_config_schema_range(self):
        low = calibrate.recommend_window([40], [5000])
        self.assertEqual(low.window_ms, calibrate.WINDOW_LOW_MS)      # 50 -> 120
        high = calibrate.recommend_window([700], [5000])
        self.assertEqual(high.window_ms, calibrate.WINDOW_HIGH_MS)    # 875 -> 600

    def test_zero_and_negative_samples_are_ignored(self):
        cal = calibrate.recommend_window([0, -5, 100], [0, 1000])
        self.assertEqual(cal.burst_samples, 1)
        self.assertEqual(cal.separated_samples, 1)

    def test_report_names_every_number_it_used(self):
        text = calibrate.recommend_window([96, 157], [900]).report()
        for needle in ("burst n=2", "max=157ms", "separated n=1", "min=900ms", "window="):
            self.assertIn(needle, text)


class TestGapsFromPresses(unittest.TestCase):
    def test_gaps_between_presses(self):
        self.assertEqual(calibrate.gaps_from_presses([0, 150, 1900, 2050]),
                         [150, 150])     # the 1750ms set boundary is the only drop

    def test_everything_kept_when_the_limit_is_high(self):
        self.assertEqual(calibrate.gaps_from_presses([0, 150, 900, 1050], max_gap_ms=3000),
                         [150, 750, 150])

    def test_single_and_empty_inputs(self):
        self.assertEqual(calibrate.gaps_from_presses([]), [])
        self.assertEqual(calibrate.gaps_from_presses([500]), [])

    def test_zero_and_negative_gaps_are_dropped(self):
        self.assertEqual(calibrate.gaps_from_presses([100, 100, 50, 200]), [150])


if __name__ == "__main__":
    unittest.main()
