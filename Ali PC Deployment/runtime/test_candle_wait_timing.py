"""Pure datetime tests for M15 close / skip-bar calculation."""
from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parent / "github_update_agent.py"

# Point the agent at a scratch root BEFORE it is imported: with its default
# (C:\onyxion-ali) every log() call these tests trigger lands in the LIVE
# github_update_agent.log and looks like a real restart request.
import os as _os, tempfile as _tempfile
_TEST_ROOT = _tempfile.mkdtemp(prefix="onyxion-agent-test-")
for _sub in ("logs", "state"):
    _os.makedirs(_os.path.join(_TEST_ROOT, _sub), exist_ok=True)
_os.environ["MT5_ROOT"] = _TEST_ROOT


def _load():
    spec = importlib.util.spec_from_file_location("github_agent_timing", AGENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CandleWaitTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_m15_boundaries(self):
        now = datetime(2026, 9, 12, 10, 7, 30, tzinfo=timezone.utc)
        plan = self.m.candle_wait_plan(now)
        self.assertEqual(plan["waited_close_bar"], "2026-09-12T10:00:00Z")
        self.assertEqual(plan["skip_bar_time"], "2026-09-12T10:15:00Z")
        # ~7.5 min left + 3s buffer
        self.assertGreater(plan["wait_sec"], 400)
        self.assertLess(plan["wait_sec"], 500)

    def test_exactly_on_open(self):
        now = datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc)
        plan = self.m.candle_wait_plan(now)
        self.assertEqual(plan["waited_close_bar"], "2026-09-12T11:00:00Z")
        self.assertEqual(plan["skip_bar_time"], "2026-09-12T11:15:00Z")
        self.assertEqual(plan["wait_sec"], 903)  # 900 + 3


if __name__ == "__main__":
    unittest.main()
