"""Unit tests for safety_gate_check pass/fail trees."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from safety_gate_check import run_gate

# Minimal engine that satisfies the gate's symbol check.
ENGINE_STUB = "\n".join([
    "def tsl_distance():",
    "    return 1.111",
    "def effective_hist_thresh():",
    "    return 15.0",
    # Required since Hamza's TRAIL_LIVE commit (564bd09): the gate looks for both.
    "def effective_trail_live():",
    "    return True",
    "class LatestModsEngine:",
    "    def trail_live(self, mark):",
    "        return {}",
    "class Mt5LiveEngine:",
    "    pass",
    "",
])
APP_FILES = ("server.py", "broker_live.py", "gcp_main.py", "auth.py", "lab_api.py", "lab_core.py")
STATIC_FILES = ("live_dashboard.html", "dashboard.html", "login.html")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SafetyGateTests(unittest.TestCase):
    def test_good_runtime_tree_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "bridge_trader.py", "x = 1\n")
            _write(root / "watchdog_exness.py", "x = 1\n")
            _write(root / "mt5_live_engine.py", ENGINE_STUB)
            _write(root / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=False)
            self.assertTrue(result["ok"], result["errors"])

    def test_broken_py_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "bridge_trader.py", "def broken(\n")
            _write(root / "mt5_live_engine.py", ENGINE_STUB)
            _write(root / "watchdog_exness.py", "x = 1\n")
            _write(root / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=False)
            self.assertFalse(result["ok"])
            self.assertTrue(any("bridge_trader" in e for e in result["errors"]))

    def test_engine_missing_symbol_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "bridge_trader.py", "x = 1\n")
            _write(root / "watchdog_exness.py", "x = 1\n")
            _write(root / "mt5_live_engine.py", "class LatestModsEngine:\n    pass\n")
            _write(root / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=False)
            self.assertFalse(result["ok"])
            self.assertTrue(any("Mt5LiveEngine" in e for e in result["errors"]))

    def test_missing_dashboard_fails_when_ui_tree_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Google Console Deployment" / "app"
            for name in APP_FILES:
                _write(app / name, "x = 1\n")
            (app / "static").mkdir(parents=True)
            # Intentionally omit the dashboard pages
            rt = root / "Ali PC Deployment" / "runtime"
            _write(rt / "bridge_trader.py", "x = 1\n")
            _write(rt / "mt5_live_engine.py", ENGINE_STUB)
            _write(rt / "watchdog_exness.py", "x = 1\n")
            _write(rt / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=True)
            self.assertFalse(result["ok"])
            self.assertTrue(any("dashboard" in e for e in result["errors"]))

    def test_repo_layout_with_dashboards_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rt = root / "Ali PC Deployment" / "runtime"
            app = root / "Google Console Deployment" / "app"
            for name in ("bridge_trader.py", "watchdog_exness.py", "connection_monitor.py",
                         "github_update_agent.py", "safety_gate_check.py"):
                _write(rt / name, "x = 1\n")
            _write(rt / "mt5_live_engine.py", ENGINE_STUB)
            _write(rt / "VERSION.json", '{"version": "test"}\n')
            for name in APP_FILES:
                _write(app / name, "x = 1\n")
            for name in STATIC_FILES:
                _write(app / "static" / name, "<html></html>\n")
            result = run_gate(root, require_dashboard=True)
            self.assertTrue(result["ok"], result["errors"])


if __name__ == "__main__":
    unittest.main()
