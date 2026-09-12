"""Status payload / behind / stale age helpers for github_update_agent."""
from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parent / "github_update_agent.py"


def _load():
    spec = importlib.util.spec_from_file_location("github_agent_under_test", AGENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class GithubAgentStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_payload_shape(self):
        state = {
            "local_sha": "aaa",
            "remote_sha": "bbb",
            "last_poll_utc": "2026-09-12T12:00:00+00:00",
            "git_dir": r"C:\onyxion-src\Latest_Gold_Trading_Bot",
            "remote_url": "https://github.com/HamzaFarooq3333/Latest_Gold_Trading_Bot.git",
            "update_state": "idle",
            "last_result": None,
            "processes": {"bridge": {"running": True}},
            "mt5": {"login": 472640728},
            "desk_link": {"heartbeat_age_sec": 5},
            "code": {"version": "test"},
            "risk": {"n_positions": 0},
            "safety_gate": {"last_ok": True},
            "requests_ack": {},
        }
        payload = self.m.build_payload(state, {})
        for key in (
            "github_checker",
            "update",
            "processes",
            "mt5",
            "desk_link",
            "code",
            "risk",
            "safety_gate",
            "pc",
            "requests_ack",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["github_checker"]["behind"])
        self.assertEqual(payload["github_checker"]["local_sha"], "aaa")
        self.assertEqual(payload["update"]["state"], "idle")

    def test_behind_false_when_equal(self):
        state = {
            "local_sha": "abc",
            "remote_sha": "abc",
            "update_state": "idle",
        }
        payload = self.m.build_payload(state, {})
        self.assertFalse(payload["github_checker"]["behind"])

    def test_agent_stale_age_threshold(self):
        # Mirrors desk rule: age > 90s => AGENT OFFLINE
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(seconds=20)).isoformat()
        stale = (now - timedelta(seconds=120)).isoformat()

        def age(ts: str) -> float:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return (now - parsed).total_seconds()

        self.assertLess(age(fresh), 90)
        self.assertGreater(age(stale), 90)
        self.assertTrue(age(stale) > 90)  # agent_offline


if __name__ == "__main__":
    unittest.main()
