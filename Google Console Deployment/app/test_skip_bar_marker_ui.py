"""deploy_skip bar flag produces chart marker fields for the live desk."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "Ali PC Deployment" / "runtime" / "bridge_trader.py"


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
    spec = importlib.util.spec_from_file_location("bridge_marker_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def marker_fields_from_bars(bars: list[dict]) -> list[dict]:
    """Mirrors live_dashboard skip-marker selection logic."""
    out = []
    for b in bars:
        if not b or not (b.get("deploy_skip") is True or b.get("deploy_skip") == 1):
            continue
        out.append(
            {
                "time": b.get("time"),
                "shape": "square",
                "text_prefix": "X skip",
                "deploy_skip": True,
            }
        )
    return out


class SkipBarMarkerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = _load_bridge()

    def test_flagged_bar_produces_marker_fields(self):
        bars = [
            {
                "time": "2026-09-12T12:00:00Z",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.2,
                "hist": 0,
                "xtrend": 1,
            },
            {
                "time": "2026-09-12T12:15:00Z",
                "open": 1.2,
                "high": 2,
                "low": 1,
                "close": 1.5,
                "hist": 11,
                "xtrend": 1,
            },
        ]
        annotated = self.b.annotate_bars(bars, deploy_skip_times=["2026-09-12T12:15:00Z"])
        markers = marker_fields_from_bars(annotated)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["time"], "2026-09-12T12:15:00Z")
        self.assertEqual(markers[0]["shape"], "square")
        self.assertTrue(markers[0]["text_prefix"].startswith("X"))


if __name__ == "__main__":
    unittest.main()
