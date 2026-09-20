"""Every candle rule runs on the Heikin-Ashi bar; the raw bar is record-only.

Ali's rule since 2026-09-20: the bridge converts the raw MT5 bar to Heikin-Ashi
and gates, fills, stops, trailing and ATR all use that candle. These tests feed
the engine HA and raw values that disagree and check the raw ones never change
a decision, while still being reported and kept as the reconcile mark.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt5_live_engine as eng  # noqa: E402
from test_engine_rules import CLASSIC_ENV, LIVE_ENV, _Feed, set_env  # noqa: E402


class _SplitFeed(_Feed):
    """Like _Feed but lets a bar carry a raw candle that differs from the HA one."""

    def split(self, o, h, l, c, xt, hist, raw, color=None):
        self.n += 1
        self.t += 900
        ts = datetime.fromtimestamp(self.t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ro, rh, rl, rc = raw
        return self.e.push(o, h, l, c, xt, hist, color, time_str=ts,
                           raw_open=ro, raw_high=rh, raw_low=rl, raw_close=rc)


class HaStopTests(unittest.TestCase):
    def setUp(self):
        set_env({**LIVE_ENV, "ENTRY_EVERY_CANDLE": "1"})
        self.e = eng.LatestModsEngine()
        self.e.balance = 2000.0  # START_BALANCE defaults to 100: too small to stack
        self.f = _SplitFeed(self.e).warm()

    def _open_long(self):
        r = self.f.bar(4002, 4010, 4001, 4008, 3980, 20.0)
        self.assertEqual(r["filled_action"], "BUY")
        return self.e.positions[-1]

    def test_raw_wick_through_stop_does_not_close_when_ha_low_holds(self):
        p = self._open_long()
        sl1 = p.sl
        # HA low stays above the stop; the raw low is $100 through it.
        r = self.f.split(4008, 4012, sl1 + 0.5, 4010, 3980, 20.0, raw=(4008, 4012, sl1 - 100, 4010))
        self.assertEqual(r["closed_units"], [])
        self.assertEqual(len(self.e.positions), 2)  # every-candle stacked another
        # Bar 2's HA close (4010) trailed the primary's stop; the raw close is irrelevant.
        sl2 = self.e.positions[0].sl
        self.assertAlmostEqual(sl2, 4010 - p.tsl, places=6)
        # Now the HA low breaks the primary's stop -> exit filled at that stop.
        # (The second ticket has its own, slightly wider ATR distance and survives;
        # a same-bar re-entry after a stop-out is by design.)
        r = self.f.split(4010, 4011, sl2 - 0.01, 4010.5, 3980, 20.0, raw=(4010, 4011, 4010, 4010.5))
        pri = [u for u in r["closed_units"] if u["reason"] == "primary_tsl"]
        self.assertEqual(len(pri), 1)
        self.assertAlmostEqual(pri[0]["exit"], sl2, places=6)

    def test_trailing_uses_ha_close(self):
        p = self._open_long()
        d = p.tsl
        r = self.f.split(4008, 4020, 4007, 4018, 3980, 20.0, raw=(4008, 4020, 4007, 4030))
        # Stop ratchets to the HA close 4018, not the raw close 4030.
        self.assertAlmostEqual(self.e.positions[0].sl, 4018 - d, places=6)
        self.assertEqual(r["raw_close"], 4030)
        self.assertEqual(r["close"], 4018)


class HaSuppTests(unittest.TestCase):
    def setUp(self):
        set_env(CLASSIC_ENV)
        self.e = eng.LatestModsEngine()
        self.e.balance = 2000.0
        self.f = _SplitFeed(self.e).warm()  # 20 amber bars arm the primary

    def test_supp_needs_ha_wick_cross_not_raw(self):
        r = self.f.bar(4002, 4010, 4001, 4008, 3980, 20.0)
        self.assertEqual(r["filled_action"], "BUY")
        self.assertEqual(self.e.break_level, 4010)  # HA high of the trade candle
        # Raw high 4020 would have crossed 4010; the HA high 4009 does not.
        r = self.f.split(4008, 4009, 4007, 4008.5, 3980, 20.0, raw=(4008, 4020, 4007, 4008.5))
        self.assertIsNone(r["filled_action"])
        self.assertEqual(r["decision_reason"], "SKIP_SUPPLEMENTARY_TRADE_WICK_GATE")
        self.assertEqual(self.e._n_total(), 1)
        # HA body/wick crosses 4010 -> SUPP, and the break level moves to this HA high.
        r = self.f.split(4008.5, 4011, 4008, 4010.5, 3980, 20.0, raw=(4008.5, 4009, 4008, 4009))
        self.assertEqual(r["filled_action"], "BUY_ADD")
        self.assertEqual(self.e._n_total(), 2)
        self.assertEqual(self.e.break_level, 4011)


class HaAtrAndRecordTests(unittest.TestCase):
    def test_atr_ignores_raw_range(self):
        set_env({**LIVE_ENV, "ENTRY_EVERY_CANDLE": "1"})
        a, b = eng.LatestModsEngine(), eng.LatestModsEngine()
        fa, fb = _SplitFeed(a), _SplitFeed(b)
        for _ in range(20):
            fa.split(4000, 4005, 4000, 4002, 3980, 0.0, raw=(4000, 4005, 4000, 4002))
            fb.split(4000, 4005, 4000, 4002, 3980, 0.0, raw=(4000, 4050, 3950, 4002))
        self.assertAlmostEqual(a._atr, b._atr)
        self.assertAlmostEqual(a._atr, 5.0)

    def test_raw_is_reported_and_kept_as_mark(self):
        set_env({**LIVE_ENV, "ENTRY_EVERY_CANDLE": "1"})
        e = eng.LatestModsEngine()
        f = _SplitFeed(e)
        r = f.split(4000, 4005, 4000, 4002, 3980, 0.0, raw=(4001, 4007, 3999, 4003))
        self.assertEqual((r["open"], r["high"], r["low"], r["close"]), (4000, 4005, 4000, 4002))
        self.assertEqual((r["raw_open"], r["raw_high"], r["raw_low"], r["raw_close"]), (4001, 4007, 3999, 4003))
        self.assertEqual(r["execution_price_source"], "ha_ohlc")
        self.assertEqual(e.last_raw_close, 4003)
        # Round-trips through a snapshot; a pre-2026-09-20 snapshot key still restores the ATR seed.
        snap = e.snapshot()
        self.assertEqual(snap["last_raw_close"], 4003)
        self.assertEqual(snap["atr_prev_close"], 4002)
        e2 = eng.LatestModsEngine()
        e2.restore({"prev_raw_close": 4111.0})
        self.assertEqual(e2._atr_prev_close, 4111.0)
        self.assertIsNone(e2.last_raw_close)

    def test_no_raw_falls_back_to_ha_for_the_record(self):
        set_env({**LIVE_ENV, "ENTRY_EVERY_CANDLE": "1"})
        e = eng.LatestModsEngine()
        r = e.push(4000, 4005, 4000, 4002, 3980, 0.0, None, time_str="2026-09-20T00:00:00Z")
        self.assertEqual(r["raw_close"], 4002)
        self.assertEqual(e.last_raw_close, 4002)


if __name__ == "__main__":
    unittest.main()
