"""Tests for deploy_skip_bar marker write/read/clear and entry skip."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parent / "bridge_trader.py"


def _load_bridge():
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
    runtime_dir = str(BRIDGE.parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
    spec = importlib.util.spec_from_file_location("bridge_skip_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class DeploySkipBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _load_bridge()

    def test_write_read_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with mock.patch.object(self.b, "STATE_DIR", state):
                path = state / "deploy_skip_bar.json"
                payload = {
                    "skip_bar_time": "2026-09-12T10:15:00Z",
                    "waited_close_bar": "2026-09-12T10:00:00Z",
                    "no_flatten": True,
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                data = self.b.read_deploy_skip_bar()
                self.assertEqual(data["skip_bar_time"], "2026-09-12T10:15:00Z")
                self.assertTrue(self.b._should_skip_entries_for_deploy("2026-09-12T10:15:00Z"))
                self.assertFalse(self.b._should_skip_entries_for_deploy("2026-09-12T10:00:00Z"))
                # Past skip candle clears marker
                self.assertFalse(self.b._should_skip_entries_for_deploy("2026-09-12T10:30:00Z"))
                self.assertIsNone(self.b.read_deploy_skip_bar())

    def test_entry_skip_only_via_hints(self):
        result = {
            "filled_action": "BUY",
            "action": "BUY",
            "fill_lot": 0.01,
            "sl": 1.0,
            "n_total": 0,
        }
        orders = self.b.engine_order_hints(result, "XAUUSDm", allow_entries=False)
        self.assertEqual(orders, [])

    def test_annotate_sets_deploy_skip_flag(self):
        bars = [
            {
                "time": "2026-09-12T10:00:00Z",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "hist": 0,
                "xtrend": 1,
            },
            {
                "time": "2026-09-12T10:15:00Z",
                "open": 1.5,
                "high": 2,
                "low": 1,
                "close": 1.8,
                "hist": 12,
                "xtrend": 1,
            },
        ]
        out = self.b.annotate_bars(bars, deploy_skip_times=["2026-09-12T10:15:00Z"])
        self.assertFalse(out[0].get("deploy_skip"))
        self.assertTrue(out[1].get("deploy_skip"))
        self.assertIn("DEPLOY SKIP", out[1].get("why") or "")


if __name__ == "__main__":
    unittest.main()
