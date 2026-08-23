import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"


class MasterAgentLiveTests(unittest.TestCase):
    def test_status_turn_uses_durable_runtime_without_submitting(self):
        script = r'''
import security
import server

security._validate_token = lambda _token: {
    "account_id": "live-account", "username": "live", "role": "member",
}
security.RATE_REQUESTS = 1000
server._run_talking_head_specialist = lambda _account, _cid, record: {
    "schema": "ip12.specialist-result/v1",
    "agent_id": "talking_head_video_agent",
    "production_id": record["id"],
    "status": "awaiting_confirmation",
    "missing": [],
    "next_action": "wait_for_explicit_confirmation",
    "job_id": None,
    "asset_refs": [],
    "tool_trace": [{"tool": "project.read", "status": "ok"}],
}
client = server.app.test_client()
headers = {"Authorization": "Bearer live-test"}
cid = client.post("/api/conversations", json={"title": "Agent live"}, headers=headers).get_json()["id"]
convo = server.load_conversation(cid)
convo["coach_state"].update(
    completed_modules=[1, 2, 3, 4, 5, 6], current_module=6, module_step=3,
    foundation_report={"status": "confirmed"},
)
convo["productions"] = {"prod_1": {
    "id": "prod_1", "action": "digital-ip-text-generate", "status": "quoted",
    "source_text": "已确认文案", "options": {"avatar_id": 6, "voice": "my_voice"},
    "quote": {"cost": 90, "token": "must-stay-private"},
    "specialist_agent": {
        "agent_id": "talking_head_video_agent", "delegation_id": "delegate_1",
    },
}}
server.save_conversation(cid, convo)
revision = server.load_conversation(cid)["coach_state"]["revision"]
response = client.post("/api/chat-complete", json={
    "conversation_id": cid, "message": "现在做到哪里啦？",
    "expected_revision": revision, "request_id": "live-status-1",
}, headers=headers)
assert response.status_code == 200, response.get_data(as_text=True)
body = response.get_json()
assert "尚未提交" in body["assistant"], body
assert body["agent_status"]["delegate"] == "口播短视频 Agent", body
assert "run_id" not in body["agent_status"] and "specialist_result" not in body["agent_status"], body
saved = server.load_conversation(cid)
run = saved["agent_runs"]["master-delegate-prod_1-live-status-1"]
assert run["result"]["status"] == "awaiting_confirmation", run
assert not any(call["tool"] == "production.submit" for call in run["tool_calls"].values()), run
assert "must-stay-private" not in str(body), body
revision = saved["coach_state"]["revision"]
sse = client.post("/api/chat", json={
    "conversation_id": cid, "message": "现在怎么样？",
    "expected_revision": revision, "request_id": "live-status-sse-1",
}, headers=headers).get_data(as_text=True)
for private_key in ("run_id", "specialist_result", "production_id", "job_id", "tool_trace"):
    assert private_key not in sse, (private_key, sse)
'''
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=root, HERMES_DATA_DIR=root,
                HERMES_MASTER_AGENT_MODE="live", HERMES_SEMANTIC_ROUTER_MODE="off",
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
