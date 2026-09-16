"""Rules-engine tests: every-candle stacking, ATR stops, trailing, amber, classic mode,
live-read controls and broker reconciliation.

Run:  python -m unittest test_engine_rules -v   (from Ali PC Deployment/runtime)
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mt5_live_engine as eng  # noqa: E402

LIVE_ENV = {
    "ENTRY_EVERY_CANDLE": "0", "TSL_ATR_MULT": "1.25", "HIST_THRESH": "15", "MAX_SUPP": "10",
    "MAXPOS": "20", "VOLUME": "0.02", "XTREND_GATE": "1", "XTREND_BUF": "0", "SKIP_WEEKENDS": "0",
    "TRAIL_EVERY_CANDLE": "1", "TRAIL_ENTRY_BAR": "1", "ENTRY_BAR_MODE": "defer",
    "DISABLE_STOP_LOSS": "0", "STOP_SLIPPAGE_PTS": "0", "SPREAD_COST": "0.06",
    "TSL_TICKS": "1111", "TSL_TICK_SIZE": "0.001",
}
CLASSIC_ENV = {**LIVE_ENV, "ENTRY_EVERY_CANDLE": "0", "MAX_SUPP": "2", "TSL_ATR_MULT": "0", "HIST_THRESH": "10"}


def set_env(values: dict) -> None:
    for k, v in values.items():
        os.environ[k] = v


class _Feed:
    """Feeds bars where raw == HA so gates are easy to reason about."""

    def __init__(self, engine: eng.LatestModsEngine):
        self.e = engine
        self.n = 0
        self.t = 1_600_000_000

    def bar(self, o, h, l, c, xt, hist, color=None, execution_prices=None):
        self.n += 1
        self.t += 900
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(self.t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.e.push(o, h, l, c, xt, hist, color, time_str=ts,
                           raw_open=o, raw_high=h, raw_low=l, raw_close=c,
                           execution_prices=execution_prices)

    def warm(self, n=20, px=4000.0):
        """n amber bars of $5 range so ATR(14) is established."""
        for _ in range(n):
            self.bar(px, px + 5, px - 0, px + 2, px - 20, 0.0)
        return self


class EveryCandleTests(unittest.TestCase):
    def setUp(self):
        set_env(LIVE_ENV)
        self.e = eng.LatestModsEngine()
        self.e.balance = 2000.0
        self.f = _Feed(self.e).warm()

    def test_atr_warm_and_stop_sized_from_atr(self):
        self.assertGreaterEqual(self.e._atr_n, 14)
        self.assertAlmostEqual(self.e._atr, 5.0, places=6)
        # green bar breaking the previous body high (4002), clear above XT
        r = self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.assertEqual(r["filled_action"], "BUY")
        p = self.e.positions[0]
        # ATR is updated with the entry bar before the fill, so the stop is
        # 1.25 x the ATR that includes this bar's range.
        self.assertAlmostEqual(p.tsl, 1.25 * self.e._atr, places=9)
        self.assertGreater(p.tsl, 6.25)
        # entry bar trails to the close: sl = close - tsl
        self.assertAlmostEqual(p.sl, 4008 - p.tsl, places=6)
        self.assertAlmostEqual(r["fill_sl"], p.sl, places=9)
        self.assertEqual(r["tsl_mode"], "atr")
        self.assertTrue(r["mode"].startswith("every_candle__atr"))

    def test_stacking_and_opposite_colour_blocked(self):
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        r2 = self.f.bar(4008, 4015, 4007, 4013, 3995, 22.0, "green")
        self.assertEqual(r2["filled_action"], "BUY_ADD")
        self.assertEqual(len(self.e.positions), 2)
        self.assertFalse(self.e.positions[1].is_primary)
        # a red candle while long that does NOT reach the stops: no entry, reason explains it
        lowest_sl = min(p.sl for p in self.e.positions)
        r3 = self.f.bar(4013, 4014, lowest_sl + 0.5, lowest_sl + 1.0, 4020, -20.0, "red")
        self.assertIsNone(r3["filled_action"])
        self.assertEqual(r3["decision_reason"], "SKIP_OPPOSITE_SIDE_OPEN")

    def test_trail_ratchets_on_close_only(self):
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        p = self.e.positions[0]
        sl1 = p.sl
        # higher close, but no body break (high not above prev body 4008)
        self.f.bar(4008, 4008, 4004, 4007.9, 3990, 20.0, "green")
        self.assertEqual(p.sl, sl1)                      # close 4007.9 < best 4008
        self.f.bar(4008, 4008, 4004, 4012, 3990, 20.0, "green")   # h == prev body high -> no break
        self.assertAlmostEqual(p.sl, 4012 - p.tsl, places=6)
        sl2 = p.sl
        self.f.bar(4012, 4012, 4009, 4010, 3990, 20.0, "green")   # pullback: stop stays
        self.assertEqual(p.sl, sl2)

    def test_stop_hit_closes_ticket_and_reports(self):
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        sl = self.e.positions[0].sl
        r = self.f.bar(4008, 4008, sl - 1, sl - 0.5, 3990, 20.0, "green")
        self.assertEqual(r["sl_exits"], 1)
        self.assertEqual(r["closed_primary"]["exit"], sl)
        self.assertEqual(r["action"], "EXIT")
        self.assertEqual(self.e.pos, 0)

    def test_amber_flattens_everything(self):
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.f.bar(4008, 4015, 4007, 4013, 3995, 22.0, "green")
        r = self.f.bar(4013, 4014, 4010, 4012, 3995, 5.0, "orange")
        self.assertEqual(r["action"], "EXIT")
        self.assertEqual(r["decision_reason"], "ORANGE_HISTOGRAM_EXIT")
        self.assertEqual(self.e.positions, [])

    def test_xtrend_gate_blocks_primary(self):
        r = self.f.bar(4003, 4010, 4002.5, 4008, 4005, 20.0, "green")   # XT inside the candle
        self.assertIsNone(r["filled_action"])
        self.assertEqual(r["decision_reason"], "SKIP_PRIMARY_XTREND_GATE")

    def test_duplicate_bar_is_replayed_not_reprocessed(self):
        r1 = self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        n = len(self.e.positions)
        self.f.t -= 900   # same timestamp again
        r2 = self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.assertTrue(r2["duplicate_bar"])
        self.assertEqual(len(self.e.positions), n)
        self.assertEqual(r2["filled_action"], r1["filled_action"])


class LiveControlTests(unittest.TestCase):
    def test_threshold_and_volume_are_read_live(self):
        set_env(LIVE_ENV)
        self.assertEqual(eng.zone(12.0), 0)
        os.environ["HIST_THRESH"] = "10"
        self.assertEqual(eng.zone(12.0), 1)
        os.environ["VOLUME"] = "0.05"
        self.assertEqual(eng.effective_volume(), 0.05)
        os.environ["MAXPOS"] = "3"
        self.assertEqual(eng.effective_maxpos(), 3)
        set_env(LIVE_ENV)

    def test_maxpos_caps_the_stack(self):
        set_env({**LIVE_ENV, "MAXPOS": "2"})
        e = eng.LatestModsEngine()
        e.balance = 2000.0
        f = _Feed(e).warm()
        f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        f.bar(4008, 4015, 4007, 4013, 3995, 22.0, "green")
        r = f.bar(4013, 4020, 4012, 4018, 3995, 22.0, "green")
        self.assertEqual(len(e.positions), 2)
        self.assertEqual(r["decision_reason"], "REJECTED_LOCAL_RISK_OR_MAXPOS")
        set_env(LIVE_ENV)


class ClassicModeTests(unittest.TestCase):
    def setUp(self):
        set_env(CLASSIC_ENV)
        self.e = eng.LatestModsEngine()
        self.e.balance = 2000.0
        self.f = _Feed(self.e).warm()

    def tearDown(self):
        set_env(LIVE_ENV)

    def test_amber_arm_then_primary_then_supp_on_wick_break(self):
        self.assertTrue(self.e.seen_amber)
        r1 = self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.assertEqual(r1["filled_action"], "BUY")
        self.assertEqual(self.e.break_level, 4010)          # raw wick of the trade candle
        self.assertAlmostEqual(self.e.positions[0].tsl, 1.111, places=6)   # fixed ticks
        r2 = self.f.bar(4008, 4012, 4007, 4011, 3990, 22.0, "green")       # breaks the 4010 wick
        self.assertEqual(r2["filled_action"], "BUY_ADD")
        r3 = self.f.bar(4011, 4011.5, 4010.5, 4011, 3990, 22.0, "green")   # no wick break, no stop
        self.assertIsNone(r3["filled_action"])
        self.assertEqual(r3["decision_reason"], "SKIP_SUPPLEMENTARY_TRADE_WICK_GATE")

    def test_second_primary_needs_new_amber(self):
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.f.bar(4008, 4009, 4000, 4001, 3990, 5.0, "orange")          # flatten + arm
        self.assertTrue(self.e.seen_amber)
        r = self.f.bar(4001, 4009, 4000.5, 4005, 3990, 20.0, "green")   # breaks prev body high 4008
        self.assertEqual(r["filled_action"], "BUY")

    def test_max_supp_respected(self):
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.f.bar(4008, 4012, 4007, 4011, 3990, 22.0, "green")
        self.f.bar(4011, 4014, 4010, 4013, 3990, 22.0, "green")
        r = self.f.bar(4013, 4016, 4012, 4015, 3990, 22.0, "green")
        self.assertEqual(self.e._n_supp(), 2)
        self.assertEqual(r["decision_reason"], "REJECTED_MAX_SUPPLEMENTARY_POSITIONS")


class ReconcileAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        set_env(LIVE_ENV)
        self.e = eng.LatestModsEngine()
        self.e.balance = 2000.0
        self.f = _Feed(self.e).warm()
        self.f.bar(4003, 4010, 4002.5, 4008, 3990, 20.0, "green")
        self.f.bar(4008, 4015, 4007, 4013, 3995, 22.0, "green")

    def test_bind_then_reconcile_drops_broker_closed_ticket(self):
        self.assertTrue(self.e.bind_ticket(111))
        self.assertTrue(self.e.bind_ticket(222))
        self.assertEqual([p.ticket for p in self.e.positions], [222, 111])
        dropped = self.e.reconcile({222}, mark_price=4010.0)
        self.assertEqual([d["ticket"] for d in dropped], [111])
        self.assertEqual([p.ticket for p in self.e.positions], [222])
        self.assertEqual(self.e.reconcile({222}, 4010.0), [])
        self.e.reconcile(set(), 4010.0)
        self.assertEqual(self.e.positions, [])
        self.assertEqual(self.e.pos, 0)

    def test_snapshot_roundtrip_and_legacy_snapshot(self):
        self.e.bind_ticket(5)
        snap = self.e.snapshot()
        e2 = eng.LatestModsEngine()
        e2.restore(snap)
        self.assertEqual([p.ticket for p in e2.positions], [p.ticket for p in self.e.positions])
        self.assertAlmostEqual(e2._atr, self.e._atr)
        # snapshot written before tsl/ticket existed, with the old pending key
        e3 = eng.LatestModsEngine()
        e3.restore({"pos": 1, "positions": [{"entry": 4000.0, "sl": 3990.0, "is_primary": True, "lot": 0.02}],
                    "pending": None, "seen_amber": True})
        self.assertEqual(e3.positions[0].ticket, 0)
        self.assertEqual(e3.positions[0].tsl, 0.0)
        e3._advance_best(e3.positions[0], 4001.0, 1)
        self.assertAlmostEqual(e3.positions[0].sl, 4001.0 - 1.111, places=6)


class Mt5AdapterTests(unittest.TestCase):
    def test_warmup_keeps_atr_but_no_trades(self):
        set_env(LIVE_ENV)
        m = eng.Mt5LiveEngine()
        bars = []
        t = 1_600_000_000
        for i in range(30):
            t += 900
            px = 4000 + i
            from datetime import datetime, timezone
            bars.append({"time": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "open": px, "high": px + 6, "low": px - 1, "close": px + 4,
                         "signal_open": px, "signal_high": px + 6, "signal_low": px - 1, "signal_close": px + 4,
                         "raw_open": px, "raw_high": px + 6, "raw_low": px - 1, "raw_close": px + 4,
                         "xtrend": px - 30, "hist": 20.0, "histcolor": "green"})
        m.warmup(bars, balance=2037.0)
        self.assertGreaterEqual(m.engine._atr_n, 14)
        self.assertEqual(m.engine.positions, [])
        self.assertEqual(m.engine.balance, 2037.0)


if __name__ == "__main__":
    unittest.main()
