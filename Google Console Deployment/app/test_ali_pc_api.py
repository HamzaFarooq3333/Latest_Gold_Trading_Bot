"""API tests for ali_pc_status / state / ali_pc_command."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import broker_live  # noqa: E402


class FakeLab:
    def __init__(self):
        self.store = {"broker_live": broker_live._default()}

    def load_store(self, model_name: str):
        return self.store

    def save_store(self, model_name: str, store: dict):
        self.store = store


def response(code, body):
    return {"statusCode": code, "body": body}


class AliPcApiTests(unittest.TestCase):
    def setUp(self):
        self.lab = FakeLab()

    def _handle(self, method, path, body=None):
        event = {"body": json.dumps(body or {})}
        return broker_live.handle(
            method, path, event, lab=self.lab, model_name="DEMO", response=response
        )

    def test_post_status_merges_and_state_exposes(self):
        r = self._handle(
            "POST",
            "/api/broker/ali_pc_status",
            {
                "github_checker": {
                    "running": True,
                    "local_sha": "aaa",
                    "remote_sha": "bbb",
                    "behind": True,
                },
                "update": {"state": "idle", "last_result": None},
                "safety_gate": {"last_ok": True},
            },
        )
        self.assertEqual(r["statusCode"], 200)
        self.assertTrue(r["body"]["ok"])
        state = self._handle("GET", "/api/broker/state")
        ali = state["body"]["broker"]["ali_pc"]
        self.assertEqual(ali["github_checker"]["local_sha"], "aaa")
        self.assertTrue(ali["github_checker"]["behind"])
        self.assertIn("updated_at", ali)
        self.assertIn("agent_offline", ali)
        self.assertFalse(ali["agent_offline"])

    def test_command_queue_idempotent(self):
        a = self._handle(
            "POST", "/api/broker/ali_pc_command", {"action": "force_check"}
        )
        self.assertEqual(a["statusCode"], 200)
        cid = a["body"]["command"]["id"]
        b = self._handle(
            "POST", "/api/broker/ali_pc_command", {"action": "force_check"}
        )
        self.assertEqual(b["body"]["command"]["id"], cid)
        status = self._handle("POST", "/api/broker/ali_pc_status", {"update": {"state": "idle"}})
        pending = status["body"]["pending_commands"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "force_check")

    def test_bad_action_rejected(self):
        r = self._handle("POST", "/api/broker/ali_pc_command", {"action": "flatten"})
        self.assertEqual(r["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
