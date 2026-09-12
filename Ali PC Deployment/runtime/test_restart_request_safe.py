"""Restart request must not clear an in-flight deploy skip wait."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

AGENT = Path(__file__).resolve().parent / "github_update_agent.py"


def _load():
    spec = importlib.util.spec_from_file_location("github_agent_restart", AGENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class RestartRequestSafeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_restart_during_wait_is_deferred(self):
        note = self.m.restart_stack_safe(in_flight_wait=True)
        self.assertTrue(note["ok"])
        self.assertTrue(note["deferred"])

    def test_handle_commands_does_not_clear_skip_state(self):
        state = {
            "update_state": "waiting_close",
            "skip_bar_time": "2026-09-12T10:15:00Z",
            "waited_close_bar": "2026-09-12T10:00:00Z",
            "requests_ack": {},
        }
        with mock.patch.object(self.m, "restart_stack_safe", return_value={"ok": True, "deferred": True}):
            self.m.handle_commands(
                state,
                {},
                [{"id": "restart_stack:1", "action": "restart_stack"}],
            )
        self.assertEqual(state["skip_bar_time"], "2026-09-12T10:15:00Z")
        self.assertEqual(state["update_state"], "waiting_close")
        self.assertTrue(state.get("pending_restart"))
        self.assertIn("restart_stack_id", state["requests_ack"])


if __name__ == "__main__":
    unittest.main()
