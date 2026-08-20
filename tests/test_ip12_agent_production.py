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

    @unittest.skipUnless(
        importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
        "Hermes runtime dependencies are not installed",
    )
    def test_full_catalog_actions_are_selected_and_kept_inside_the_contract(self):
        script = r'''
import io
from unittest.mock import patch
import server
import security

cases = {
    "批量生成三位数字人口播": "digital-ip-batch-generate",
    "用我的音频驱动数字人": "digital-ip-audio-generate",
    "做电影化身动作模仿": "cinematic-motion-generate",
    "把这个人物快速换装": "tryon-fast-generate",
    "查看文案成片模板": "text-video-templates",
    "列出我的 Canvas 画布": "canvas-list",
    "请用 Canvas Agent 规划当前画布；只打开 Canvas 页面": "canvas-agent-plan",
    "生成数字主持人": "digital-presenter-create",
    "上传参考视频素材": "video-upload",
}
for message, action in cases.items():
    intent = server._expanded_production_intent(message)
    assert intent and intent["recommended_action"] == action, (message, intent)
for question, expected in (
    ("电影化身是什么？", "cinematic-open-generate"),
    ("换装怎么用？", "tryon-fast-generate"),
    ("一键成片多少钱？", "video-compose-create"),
):
    intent = server._expanded_production_intent(question)
    assert intent and intent["help_only"] and intent["recommended_action"] == expected, (question, intent)

catalog = {"version": "test-v2", "actions": [{
    "action": "voices", "family": "audio", "purpose": "读取音色",
    "input_schema": {"type": "object", "additionalProperties": False, "required": [], "properties": {}},
    "billing": "free", "confirmation_required": False, "risk": "read", "result_type": "json",
    "ui_route": "/workbench/audio", "transport": {"kind": "action"},
    "availability": {"status": "available"},
}, {
    "action": "tryon-fast-generate", "family": "video", "purpose": "生成快速换装视频",
    "input_schema": {"type": "object", "additionalProperties": False,
        "required": ["person_image_upload_id", "clothes_upload_id"], "properties": {
            "person_image_upload_id": {"type": "string", "pattern": "^img_[0-9a-f]{32}$"},
            "clothes_upload_id": {"type": "string", "pattern": "^img_[0-9a-f]{32}$"},
        }},
    "billing": "quote_then_confirm", "confirmation_required": True,
    "risk": "production", "result_type": "asset", "ui_route": "/workbench/video",
    "transport": {"kind": "action"}, "availability": {"status": "available"},
}]}
with patch.object(server, "_bridge_catalog", return_value=catalog):
    selected = server._production_recommendation("acct_a", "audio", "voices")
assert selected["recommended_action"] == "voices" and selected["catalog_version"] == "test-v2", selected

record = {
    "id": "prod_contract", "action": "audio-generate", "source_text": "字" * 1001,
    "parameter_schema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
    }},
    "script_digest": "sha256:test", "options": {}, "risk": "production",
}
valid, error, _ = server._production_plan_or_error(record, {})
assert not valid and "太长" in error, (valid, error)

read_record = {"action": "voices", "capability_family": "audio", "risk": "read", "asset_refs": []}
server._set_production_result(read_record, {"items": [{"voice_key": "demo"}]})
assert read_record["status"] == "done" and read_record["action_result"]["items"][0]["voice_key"] == "demo"

server.current_account_id = lambda: "acct_a"
security._validate_token = lambda token: {"account_id": "acct_a", "username": "alice", "role": "member"}
security.RATE_REQUESTS = 100
client = server.app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
cid = "catalogread01"
state = server.initial_coach_state()
server.save_conversation(cid, {
    "id": cid, "title": "catalog read", "messages": [], "coach_state": state,
    "reports": {}, "deliverables": {}, "owner_account_id": "acct_a",
})
with patch.object(server, "_bridge_catalog", return_value=catalog):
    turn = client.post("/api/chat-complete", json={
        "conversation_id": cid, "message": "有哪些可用音色？",
        "expected_revision": state["revision"], "request_id": "catalog-read-turn-01",
    })
    assert turn.status_code == 200, turn.get_data(as_text=True)
    action = turn.get_json()["actions"][0]
    assert action["preferred_action"] == "voices" and action["content_target"] == {"category_id": "", "topic_id": ""}, action
    revision = turn.get_json()["state"]["revision"]
    prepared = client.post("/api/ip12/productions/prepare", json={
        "conversation_id": cid,
        "content_target": {"category_id": "", "topic_id": ""},
        "expected_revision": revision,
        "requested_result": "audio", "preferred_action": "voices", "options": {},
    })
    assert prepared.status_code == 200 and prepared.get_json()["confirmation_required"] is False, prepared.get_data(as_text=True)
    production_id = prepared.get_json()["production_id"]
    with patch.object(server, "_bridge_action", return_value={"items": [{"voice_key": "demo"}]}) as bridge:
        completed = client.post("/api/ip12/productions/quote", json={
            "conversation_id": cid, "production_id": production_id,
            "expected_revision": revision, "options": {},
        })
    assert completed.status_code == 200, completed.get_data(as_text=True)
    body = completed.get_json()
    assert body["confirmation_required"] is False and body["production"]["status"] == "done", body
    assert bridge.call_args.args[1] == "voices" and not bridge.call_args.kwargs.get("confirm"), bridge.call_args

before = len(server.load_conversation(cid).get("productions") or {})
revision = server.load_conversation(cid)["coach_state"]["revision"]
with patch.object(server, "_bridge_catalog", return_value=catalog):
    help_turn = client.post("/api/chat-complete", json={
        "conversation_id": cid, "message": "换装怎么用？只解释，不要创建任务。",
        "expected_revision": revision, "request_id": "catalog-help-turn-01",
    })
assert help_turn.status_code == 200, help_turn.get_data(as_text=True)
help_body = help_turn.get_json()
assert not help_body.get("actions"), help_body
assert "快速换装" in help_body["assistant"] and "人物图片" in help_body["assistant"] and "服装图片" in help_body["assistant"], help_body
assert "没有创建任务" in help_body["assistant"], help_body
assert len(server.load_conversation(cid).get("productions") or {}) == before

revision = server.load_conversation(cid)["coach_state"]["revision"]
with patch.object(server, "_bridge_catalog", return_value=catalog):
    tryon_turn = client.post("/api/chat-complete", json={
        "conversation_id": cid, "message": "帮我快速换装，先准备所需素材，不要确认付费生成。",
        "expected_revision": revision, "request_id": "catalog-tryon-turn-01",
    })
    assert tryon_turn.status_code == 200, tryon_turn.get_data(as_text=True)
    tryon_turn_body = tryon_turn.get_json()
    assert "快速换装" in tryon_turn_body["assistant"], tryon_turn_body
    assert "当前这篇口播" not in tryon_turn_body["assistant"], tryon_turn_body
    tryon_action = tryon_turn_body["actions"][0]
    assert tryon_action["preferred_action"] == "tryon-fast-generate", tryon_action
    assert tryon_action["content_target"] == {"category_id": "", "topic_id": ""}, tryon_action
    revision = tryon_turn_body["state"]["revision"]
    tryon_prepared = client.post("/api/ip12/productions/prepare", json={
        "conversation_id": cid, "content_target": tryon_action["content_target"],
        "expected_revision": revision, "requested_result": "video",
        "preferred_action": "tryon-fast-generate", "options": {},
    })
assert tryon_prepared.status_code == 200, tryon_prepared.get_data(as_text=True)
tryon_body = tryon_prepared.get_json()
assert set(tryon_body["missing"]) == {"person_image_upload_id", "clothes_upload_id"}, tryon_body
assert "人物图片、服装图片" in tryon_body["material_request_message"]["content"], tryon_body
tryon_record = server.load_conversation(cid)["productions"][tryon_body["production_id"]]
assert tryon_record["source_bound"] is False, tryon_record
assert tryon_record["material_request_message_id"], tryon_record

with patch.object(server, "_bridge_upload", return_value={"upload_id": "img_" + "a" * 32}):
    person_upload = client.post("/api/ip12/productions/upload", data={
        "conversation_id": cid, "production_id": tryon_body["production_id"],
        "expected_revision": revision, "field": "person_image_upload_id",
        "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nprivate"), "person.png"),
    }, content_type="multipart/form-data")
assert person_upload.status_code == 200, person_upload.get_data(as_text=True)
person_body = person_upload.get_json()
assert person_body["missing"] == ["clothes_upload_id"], person_body
assert "还需要：服装图片" in person_body["material_message"]["content"], person_body

with patch.object(server, "_bridge_upload", return_value={"upload_id": "img_" + "b" * 32}):
    clothes_upload = client.post("/api/ip12/productions/upload", data={
        "conversation_id": cid, "production_id": tryon_body["production_id"],
        "expected_revision": revision, "field": "clothes_upload_id",
        "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nprivate-2"), "clothes.png"),
    }, content_type="multipart/form-data")
assert clothes_upload.status_code == 200, clothes_upload.get_data(as_text=True)
clothes_body = clothes_upload.get_json()
assert clothes_body["missing"] == [] and clothes_body["production"]["status"] == "draft", clothes_body
assert "素材已经齐了" in clothes_body["material_message"]["content"], clothes_body
saved_tryon = server.load_conversation(cid)["productions"][tryon_body["production_id"]]
assert saved_tryon["options"] == {
    "person_image_upload_id": "img_" + "a" * 32,
    "clothes_upload_id": "img_" + "b" * 32,
}, saved_tryon
'''
        with tempfile.TemporaryDirectory(prefix="ip12-catalog-contract-test.") as data_dir:
            env = os.environ.copy()
            env.update(OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir)
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT / "server" / "hermes_ip12",
                env=env, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
