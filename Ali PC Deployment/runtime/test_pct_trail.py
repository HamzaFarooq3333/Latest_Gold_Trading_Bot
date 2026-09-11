"""Local unit tests for 0.25% per-trade trailing stop-loss."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

os.environ["TSL_PCT"] = "0.0025"
os.environ["TRAIL_EVERY_CANDLE"] = "1"
os.environ["TRAIL_ENTRY_BAR"] = "1"
os.environ["DISABLE_STOP_LOSS"] = "0"
os.environ["ENTRY_BAR_MODE"] = "defer"

import mt5_live_engine as eng  # noqa: E402


class PctTrailTests(unittest.TestCase):
    def setUp(self):
        os.environ["TSL_PCT"] = "0.0025"
        self.e = eng.LatestModsEngine()
        self.e.seen_amber = True

    def test_effective_trail_pct_fraction_and_percent(self):
        os.environ["TSL_PCT"] = "0.0025"
        self.assertAlmostEqual(eng.effective_trail_pct(), 0.0025)
        os.environ["TSL_PCT"] = "0.25"
        self.assertAlmostEqual(eng.effective_trail_pct(), 0.0025)

    def test_sell_init_sl_is_025_pct_above_entry(self):
        entry = 4000.0
        sl = self.e._init_sl(entry, -1)
        self.assertAlmostEqual(sl, entry * 1.0025, places=6)

    def test_buy_init_sl_is_025_pct_below_entry(self):
        entry = 4000.0
        sl = self.e._init_sl(entry, 1)
        self.assertAlmostEqual(sl, entry * 0.9975, places=6)

    def test_sell_trails_down_independently_never_up(self):
        self.e.pos = -1
        p1 = eng.Position(entry=4000.0, sl=4010.0, is_primary=True, lot=0.01, best_price=4000.0, trade_id="PRIMARY")
        p2 = eng.Position(entry=3990.0, sl=3999.975, is_primary=False, lot=0.01, best_price=3990.0, trade_id="SUPP1")
        # Fix init SLs to formula
        p1.sl = self.e._init_sl(p1.entry, -1)
        p2.sl = self.e._init_sl(p2.entry, -1)
        self.e.positions = [p1, p2]

        self.e._apply_pct_trail(p1, 3980.0)
        self.e._apply_pct_trail(p2, 3980.0)
        self.assertAlmostEqual(p1.best_price, 3980.0)
        self.assertAlmostEqual(p1.sl, 3980.0 * 1.0025, places=6)
        self.assertAlmostEqual(p2.best_price, 3980.0)
        self.assertAlmostEqual(p2.sl, 3980.0 * 1.0025, places=6)

        # Stall / tick up: SL must not loosen
        prev1, prev2 = p1.sl, p2.sl
        self.e._apply_pct_trail(p1, 3985.0)
        self.e._apply_pct_trail(p2, 3985.0)
        self.assertEqual(p1.best_price, 3980.0)
        self.assertEqual(p1.sl, prev1)
        self.assertEqual(p2.best_price, 3980.0)
        self.assertEqual(p2.sl, prev2)

    def test_buy_trails_up_never_down(self):
        self.e.pos = 1
        p = eng.Position(entry=4000.0, sl=3990.0, is_primary=True, lot=0.01, best_price=4000.0)
        p.sl = self.e._init_sl(p.entry, 1)
        self.e.positions = [p]
        self.e._apply_pct_trail(p, 4020.0)
        self.assertAlmostEqual(p.sl, 4020.0 * 0.9975, places=6)
        prev = p.sl
        self.e._apply_pct_trail(p, 4010.0)
        self.assertEqual(p.best_price, 4020.0)
        self.assertEqual(p.sl, prev)

    def test_tick_exit_closes_only_hit_trade(self):
        self.e.pos = -1
        # Primary less trailed (higher SL); SUPP already chased lower.
        p1 = eng.Position(
            entry=4000.0,
            sl=4000.0 * 1.0025,
            is_primary=True,
            lot=0.01,
            best_price=4000.0,
            trade_id="PRIMARY",
        )
        p2 = eng.Position(
            entry=4000.0,
            sl=3980.0 * 1.0025,
            is_primary=False,
            lot=0.01,
            best_price=3980.0,
            trade_id="SUPP1",
        )
        self.e.positions = [p1, p2]
        # Ask rises through SUPP SL (~3989.95) but not PRIMARY SL (~4010)
        out = self.e.update_trails_from_tick(bid=3995.0, ask=4000.0)
        self.assertEqual(out["sl_exits"], 1)
        self.assertIsNone(out.get("closed_primary"))
        self.assertEqual(len(out.get("closed_supps") or []), 1)
        self.assertEqual(len(self.e.positions), 1)
        self.assertEqual(self.e.positions[0].trade_id, "PRIMARY")
        self.assertEqual(self.e.pos, -1)

    def test_restore_old_snapshot_without_best_price(self):
        self.e.restore(
            {
                "pos": -1,
                "positions": [{"entry": 4000.0, "sl": 4010.0, "is_primary": True, "lot": 0.01}],
                "seen_amber": True,
            }
        )
        self.assertEqual(len(self.e.positions), 1)
        self.assertEqual(self.e.positions[0].best_price, 4000.0)
        self.assertEqual(self.e.positions[0].trade_id, "PRIMARY")


if __name__ == "__main__":
    unittest.main()
