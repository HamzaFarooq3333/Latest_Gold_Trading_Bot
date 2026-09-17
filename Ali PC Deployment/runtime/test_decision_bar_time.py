"""Decision-candle mapping for trade arrows (fill clock → prior M15)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_trader as bt  # noqa: E402


class DecisionBarTimeTests(unittest.TestCase):
    def test_early_fill_maps_to_previous_m15(self):
        # Fill 09:00:04 UTC → decision bar 08:45:00Z
        self.assertEqual(
            bt.decision_bar_time_from_fill("2026-09-16T09:00:04Z"),
            "2026-09-16T08:45:00Z",
        )

    def test_fill_at_bar_open_plus_3min_still_prior(self):
        self.assertEqual(
            bt.decision_bar_time_from_fill("2026-09-16T09:03:00Z"),
            "2026-09-16T08:45:00Z",
        )

    def test_late_in_bar_stays_on_same_m15(self):
        # 09:05 fill is >180s into the bar → stay on 09:00
        self.assertEqual(
            bt.decision_bar_time_from_fill("2026-09-16T09:05:00Z"),
            "2026-09-16T09:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
