"""Regression: duplicate_bar / entry_sent must not re-fire market entries."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "Ali PC Deployment" / "runtime" / "bridge_trader.py"


def _load_bridge():
    # Stub MetaTrader5 so the module imports offline.
    if "MetaTrader5" not in sys.modules:
        mt5 = types.ModuleType("MetaTrader5")
        mt5.TRADE_RETCODE_DONE = 10009
        mt5.ORDER_TYPE_BUY = 0
        mt5.ORDER_TYPE_SELL = 1
        mt5.TRADE_ACTION_DEAL = 1
        mt5.ORDER_TIME_GTC = 0
        mt5.ORDER_FILLING_IOC = 1
        mt5.ORDER_FILLING_FOK = 2
        mt5.ORDER_FILLING_RETURN = 3
        sys.modules["MetaTrader5"] = mt5
    # Prefer local mt5_live_engine beside bridge.
    runtime_dir = str(BRIDGE.parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    spec = importlib.util.spec_from_file_location("onyxion_bridge_trader_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class BridgeEntryDedupeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _load_bridge()

    def test_normalize_bar_time_unifies_z_and_offset(self):
        a = self.b.normalize_bar_time("2026-09-09T01:30:00Z")
        c = self.b.normalize_bar_time("2026-09-09T01:30:00+00:00")
        self.assertEqual(a, c)
        self.assertEqual(a, "2026-09-09T01:30:00Z")

    def test_duplicate_bar_skips_market_entry(self):
        result = {
            "filled_action": "BUY_ADD",
            "action": "BUY_ADD",
            "duplicate_bar": True,
            "fill_lot": 0.02,
            "sl": 1.0,
            "n_total": 1,
        }
        orders = self.b.engine_order_hints(result, "XAUUSDm")
        kinds = [o.get("actionType") for o in orders]
        self.assertNotIn("ORDER_TYPE_BUY", kinds)
        self.assertNotIn("ORDER_TYPE_SELL", kinds)

    def test_allow_entries_false_skips_market_entry(self):
        result = {
            "filled_action": "SELL",
            "action": "SELL",
            "fill_lot": 0.02,
            "sl": 1.0,
            "n_total": 1,
        }
        orders = self.b.engine_order_hints(result, "XAUUSDm", allow_entries=False)
        kinds = [o.get("actionType") for o in orders]
        self.assertEqual(kinds, [])

    def test_fresh_fill_still_emits_entry(self):
        result = {
            "filled_action": "BUY",
            "action": "BUY",
            "fill_lot": 0.02,
            "sl": 1.0,
            "n_total": 1,
        }
        orders = self.b.engine_order_hints(result, "XAUUSDm", allow_entries=True)
        self.assertEqual(orders[0]["actionType"], "ORDER_TYPE_BUY")
        self.assertEqual(orders[0]["kind"], "PRIMARY")

    def test_no_changes_retcode_is_success(self):
        # 10025 on an SL that already matches must not fail the bar pipeline:
        # a failed leg freezes the engine, killing trailing and the amber flatten.
        class _Result:
            retcode = 10025
            deal = 0
            order = 0
            price = 0.0
            comment = "No changes"
            volume = 0.0

        self.addCleanup(setattr, self.b.mt5, "order_send", getattr(self.b.mt5, "order_send", None))
        self.b.mt5.order_send = lambda req: _Result()
        out = self.b.order_send({"action": 6, "position": 1, "sl": 4401.852})
        self.assertTrue(out["ok"])
        self.assertEqual(out["retcode"], 10025)

    def test_sl_modify_skips_when_already_at_target(self):
        class _Info:
            digits = 3
            point = 0.001
            trade_stops_level = 0
            trade_freeze_level = 0

        class _Tick:
            bid = 4415.0
            ask = 4415.2

        class _Position:
            ticket = 1851738975
            type = 0
            sl = 4401.852
            tp = 0.0

        self.b.mt5.symbol_info = lambda symbol: _Info()
        self.b.mt5.symbol_info_tick = lambda symbol: _Tick()
        self.b.mt5.POSITION_TYPE_BUY = 0

        def _fail(req):
            raise AssertionError("order_send must not run for an unchanged SL")

        self.b.mt5.order_send = _fail
        out = self.b.modify_position_sl("XAUUSDm", _Position(), 4401.852)
        self.assertTrue(out["ok"])
        self.assertTrue(out["skipped"])

    def test_entry_sent_bars_roundtrip(self):
        runtime = {}
        self.b._mark_entry_sent(runtime, "2026-09-09T01:30:00+00:00")
        self.assertTrue(self.b._entry_already_sent(runtime, "2026-09-09T01:30:00Z"))
        stripped = self.b._strip_entry_orders(
            [
                {"actionType": "ORDER_TYPE_BUY"},
                {"actionType": "SL_MODIFY"},
            ]
        )
        self.assertEqual([o["actionType"] for o in stripped], ["SL_MODIFY"])


if __name__ == "__main__":
    unittest.main()
