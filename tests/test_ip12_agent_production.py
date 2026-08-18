import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES_SERVER = ROOT / "server" / "hermes_ip12" / "server.py"
AUTH_SERVER = ROOT / "server" / "auth_server.py"


class ProductionBridgeContractTests(unittest.TestCase):
    def test_default_bridge_path_matches_the_registered_auth_route(self):
        hermes = HERMES_SERVER.read_text(encoding="utf-8")
        auth = AUTH_SERVER.read_text(encoding="utf-8")
        expected_route = "/api/auth/internal/ip12/agent/action"
        self.assertIn(expected_route, auth)
        self.assertIn(expected_route, hermes)

    def test_auth_bridge_accepts_the_confirm_envelope_sent_by_ip12(self):
        hermes = HERMES_SERVER.read_text(encoding="utf-8")
        auth = AUTH_SERVER.read_text(encoding="utf-8")
        self.assertIn('"confirm": bool(confirm)', hermes)
        self.assertIn('"quote_token": quote_token', hermes)
        self.assertIn('"idempotency_key": idempotency_key', hermes)
        bridge = auth[auth.index("def _internal_ip12_agent_action"):auth.index("if p == \"/api/auth/internal/ip12/agent/catalog\"")]
        for field in ("confirm", "quote_token", "idempotency_key"):
            self.assertIn('"' + field + '"', bridge)

    @unittest.skipUnless(
        importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
        "Hermes runtime dependencies are not installed",
    )
    def test_user_message_is_persisted_before_the_model_is_called(self):
        script = r'''
from unittest.mock import patch
import server

server.current_account_id = lambda: "acct_a"
cid = "prepersistcheck"
state = server.initial_coach_state()
server.save_conversation(cid, {
    "id": cid, "title": "prepersist", "messages": [],
    "coach_state": state, "reports": {}, "deliverables": {},
    "owner_account_id": "acct_a",
})
message = "这条原话必须先落盘"
seen_before_model = []

def fail_model(snapshot, user_message, repair_error=""):
    saved = server.load_conversation(cid).get("messages", [])
    seen_before_model.append(any(
        item.get("role") == "user" and item.get("content") == message
        for item in saved
    ))
    raise RuntimeError("simulated model failure")

with patch.object(server, "_coach_model_decision", side_effect=fail_model):
    result, status = server._process_model_turn(cid, message, state["revision"])

assert status == 200, (status, result)
assert seen_before_model == [True], seen_before_model
'''
        with tempfile.TemporaryDirectory(prefix="ip12-prepersist-test.") as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy",
                HERMES_HOME=data_dir,
                HERMES_DATA_DIR=data_dir,
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT / "server" / "hermes_ip12",
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
