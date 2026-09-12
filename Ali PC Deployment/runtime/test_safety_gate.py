"""Unit tests for safety_gate_check pass/fail trees."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from safety_gate_check import run_gate


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SafetyGateTests(unittest.TestCase):
    def test_good_runtime_tree_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (
                "bridge_trader.py",
                "mt5_live_engine.py",
                "watchdog_exness.py",
            ):
                _write(root / name, "x = 1\n")
            _write(
                root / "mt5_live_engine.py",
                "def tsl_distance(a, b):\n    return abs(a - b)\nTSL_PTS = 1.0\n",
            )
            _write(root / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=False)
            self.assertTrue(result["ok"], result["errors"])

    def test_broken_py_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "bridge_trader.py", "def broken(\n")
            _write(
                root / "mt5_live_engine.py",
                "def tsl_distance(a, b):\n    return 1\nTSL_PTS = 1\n",
            )
            _write(root / "watchdog_exness.py", "x = 1\n")
            _write(root / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=False)
            self.assertFalse(result["ok"])
            self.assertTrue(any("bridge_trader" in e or "invalid" in e.lower() or "syntax" in e.lower() or "(" in e for e in result["errors"]))

    def test_missing_dashboard_fails_when_ui_tree_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Google Console Deployment" / "app"
            _write(app / "server.py", "x = 1\n")
            _write(app / "broker_live.py", "x = 1\n")
            _write(app / "gcp_main.py", "x = 1\n")
            static = app / "static"
            static.mkdir(parents=True)
            # Intentionally omit live_dashboard.html / dashboard.html
            rt = root / "Ali PC Deployment" / "runtime"
            _write(rt / "bridge_trader.py", "x = 1\n")
            _write(
                rt / "mt5_live_engine.py",
                "def tsl_distance(a, b):\n    return 1\nTSL_PTS = 1\n",
            )
            _write(rt / "watchdog_exness.py", "x = 1\n")
            _write(rt / "VERSION.json", '{"version": "test"}\n')
            result = run_gate(root, require_dashboard=True)
            self.assertFalse(result["ok"])
            self.assertTrue(any("dashboard" in e for e in result["errors"]))

    def test_repo_layout_with_dashboards_passes(self):
        # Use a minimal synthetic repo layout (not the whole workspace — faster/isolated).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rt = root / "Ali PC Deployment" / "runtime"
            app = root / "Google Console Deployment" / "app"
            static = app / "static"
            for name in (
                "bridge_trader.py",
                "watchdog_exness.py",
                "connection_monitor.py",
                "github_update_agent.py",
                "safety_gate_check.py",
            ):
                _write(rt / name, "x = 1\n")
            _write(
                rt / "mt5_live_engine.py",
                "def tsl_distance(a, b):\n    return 1\nTSL_PTS = 1\n",
            )
            _write(rt / "VERSION.json", '{"version": "test"}\n')
            for name in ("server.py", "broker_live.py", "gcp_main.py", "auth.py"):
                _write(app / name, "x = 1\n")
            _write(static / "live_dashboard.html", "<html></html>\n")
            _write(static / "dashboard.html", "<html></html>\n")
            result = run_gate(root, require_dashboard=True)
            self.assertTrue(result["ok"], result["errors"])


if __name__ == "__main__":
    unittest.main()
