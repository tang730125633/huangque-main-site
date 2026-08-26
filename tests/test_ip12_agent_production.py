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

def fail_model(snapshot, user_message, repair_error="", timeout_seconds=180):
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
state.update(
    completed_modules=[1, 2, 3, 4, 5, 6], current_module=6, module_step=3,
    foundation_report={"status": "confirmed"},
)
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
assert "本条消息下方" in tryon_body["material_request_message"]["content"], tryon_body
assert "右侧生产画布" not in tryon_body["material_request_message"]["content"], tryon_body
assert "输入框左侧" in tryon_body["material_request_message"]["content"], tryon_body
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

    @unittest.skipUnless(
        importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
        "Hermes runtime dependencies are not installed",
    )
    def test_portrait_and_narration_upload_stay_in_ip12_and_stop_at_quote(self):
        script = r'''
import io
from unittest.mock import patch
import server
import security

def action(name, schema):
    return {
        "action": name, "family": "video", "purpose": name,
        "input_schema": {"type": "object", "additionalProperties": False, **schema},
        "constraints": [], "billing": "quote_then_confirm", "confirmation_required": True,
        "risk": "production", "result_type": "asset", "ui_route": "/workbench/video",
        "transport": {"kind": "action"}, "availability": {"status": "available"},
    }

image = {"type": "string", "pattern": "^img_[0-9a-f]{32}$"}
audio = {"type": "string", "pattern": "^aud_[0-9a-f]{32}$"}
catalog = {"version": "inline-media-v1", "actions": [
    action("digital-ip-text-generate", {"required": ["text", "voice"], "properties": {
        "avatar_id": {"type": "integer"}, "image_upload_id": image,
        "text": {"type": "string"}, "voice": {"type": "string"},
    }}),
    action("digital-ip-audio-generate", {"required": [], "properties": {
        "avatar_id": {"type": "integer"}, "image_upload_id": image,
        "audio_file": {"type": "string"}, "audio_upload_id": audio,
    }}),
]}

server.current_account_id = lambda: "acct_inline"
security._validate_token = lambda token: {"account_id": "acct_inline", "username": "inline", "role": "member"}
security.RATE_REQUESTS = 100
client = server.app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
state = server.initial_coach_state()
state.update(
    completed_modules=[1, 2, 3, 4, 5, 6], current_module=6, module_step=3,
    foundation_report={"status": "confirmed"},
)
cid = "inlinemedia01"
server.save_conversation(cid, {
    "id": cid, "title": "inline media", "messages": [], "coach_state": state,
    "reports": {}, "owner_account_id": "acct_inline", "productions": {},
    "deliverables": {"6": {"kind": "content_pack_v1", "categories": [{
        "id": "category_1", "name": "内容", "topics": [{
            "id": "topic_1", "title": "第一篇", "status": "ready",
            "versions": [{"version": 1, "content": "这是已经确认的完整口播文案。"}],
        }],
    }]}},
})

bridge_calls = []
def bridge(_account, capability, input_body, **kwargs):
    bridge_calls.append((capability, input_body, kwargs))
    if capability == "video-avatars":
        return {"items": []}
    if capability == "voices":
        return {"items": []}
    if capability == "audio-slots":
        return {"items": []}
    if capability == "digital-ip-audio-generate":
        return {"quote_token": "quote-inline", "cost": 30, "points": 100, "expires_in": 300}
    raise AssertionError(capability)

with patch.object(server, "_bridge_catalog", return_value=catalog), patch.object(server, "_bridge_action", side_effect=bridge):
    prepared = client.post("/api/ip12/productions/prepare", json={
        "conversation_id": cid, "content_target": {"category_id": "category_1", "topic_id": "topic_1"},
        "expected_revision": state["revision"], "requested_result": "video",
        "preferred_action": "digital-ip-text-generate", "options": {},
    })
    assert prepared.status_code == 200, prepared.get_data(as_text=True)
    body = prepared.get_json()
    assert set(body["missing"]) == {"image_upload_id", "audio_upload_id"}, body
    assert "/workbench/digital-ip" not in body["material_request_message"]["content"], body
    assert "本条消息下方" in body["material_request_message"]["content"], body
    assert "其他功能页" in body["material_request_message"]["content"], body
    production_id = body["production_id"]

    with patch.object(server, "_bridge_upload", return_value={"upload_id": "img_" + "a" * 32}):
        portrait = client.post("/api/ip12/productions/upload", data={
            "conversation_id": cid, "production_id": production_id,
            "expected_revision": state["revision"], "field": "image_upload_id",
            "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nportrait"), "portrait.png"),
        }, content_type="multipart/form-data")
    assert portrait.status_code == 200 and portrait.get_json()["missing"] == ["audio_upload_id"], portrait.get_data(as_text=True)

    with patch.object(server, "_bridge_upload", return_value={"upload_id": "aud_" + "b" * 32}):
        narration = client.post("/api/ip12/productions/upload", data={
            "conversation_id": cid, "production_id": production_id,
            "expected_revision": state["revision"], "field": "audio_upload_id",
            "file": (io.BytesIO(b"RIFF\x00\x00\x00\x00WAVEaudio"), "narration.wav"),
        }, content_type="multipart/form-data")
    narration_body = narration.get_json()
    assert narration.status_code == 200 and narration_body["missing"] == [], narration.get_data(as_text=True)
    assert narration_body["production"]["action"] == "digital-ip-audio-generate", narration_body
    assert narration_body["production"]["status"] == "draft", narration_body
    assert not any(call[2].get("confirm") for call in bridge_calls), bridge_calls

    quoted = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": production_id,
        "expected_revision": state["revision"],
    })
    assert quoted.status_code == 200 and quoted.get_json()["status"] == "quoted", quoted.get_data(as_text=True)
    quote_call = bridge_calls[-1]
    assert quote_call[0] == "digital-ip-audio-generate", quote_call
    assert quote_call[1] == {
        "image_upload_id": "img_" + "a" * 32,
        "audio_upload_id": "aud_" + "b" * 32,
    }, quote_call
    assert not quote_call[2].get("confirm"), quote_call
'''
        with tempfile.TemporaryDirectory(prefix="ip12-inline-media-test.") as data_dir:
            env = os.environ.copy()
            env.update(OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir)
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT / "server" / "hermes_ip12",
                env=env, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(
        importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
        "Hermes runtime dependencies are not installed",
    )
    def test_quote_reuses_one_reply_and_maps_avatar_display_name(self):
        script = r'''
from unittest.mock import patch
import server
import security

def action(name, schema):
    return {
        "action": name, "family": "video", "purpose": name,
        "input_schema": {"type": "object", "additionalProperties": False, **schema},
        "constraints": [], "billing": "quote_then_confirm", "confirmation_required": True,
        "risk": "production", "result_type": "asset", "ui_route": "/workbench/video",
        "transport": {"kind": "action"}, "availability": {"status": "available"},
    }

catalog = {"version": "quote-reply-v1", "actions": [
    action("digital-ip-text-generate", {
        "required": ["avatar_id", "text", "voice"],
        "properties": {
            "avatar_id": {"type": "integer"}, "text": {"type": "string"},
            "voice": {"type": "string"}, "ratio": {"type": "string"},
            "motion": {"type": "string"}, "subtitle": {"type": "boolean"},
            "subtitle_style": {"type": "string"},
            "subtitle_position": {"type": "string"},
        },
    }),
]}

server.current_account_id = lambda: "acct_quote_reply"
security._validate_token = lambda token: {
    "account_id": "acct_quote_reply", "username": "quote", "role": "member",
}
security.RATE_REQUESTS = 100
client = server.app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
state = server.initial_coach_state()
state.update(
    completed_modules=[1, 2, 3, 4, 5, 6], current_module=6, module_step=3,
    foundation_report={"status": "confirmed"},
)
cid = "quotereply01"
previous = {
    "id": "prod_previous", "action": "digital-ip-text-generate", "status": "quoted",
    "category_id": "category_1", "topic_id": "topic_1", "script_version": 1,
    "script_digest": server._production_digest("这是已经确认的完整口播文案。"),
    "script_title": "新手第一次接触 Agent，先弄懂什么",
    "source_text": "这是已经确认的完整口播文案。",
    "options": {"avatar_id": 7, "voice": "voice-me"},
    "idempotency_key": "stable-current-production-key", "job_id": None,
    "specialist_agent": {
        "agent_id": server.talking_head_agent.AGENT_ID,
        "delegation_id": "delegate_previous",
    },
    "quote": {"cost": 120, "points": 94509},
}
server.save_conversation(cid, {
    "id": cid, "title": "quote reply", "coach_state": state,
    "messages": [],
    "reports": {}, "owner_account_id": "acct_quote_reply",
    "active_production_id": previous["id"],
    "productions": {previous["id"]: previous},
    "deliverables": {"6": {"kind": "content_pack_v1", "categories": [{
        "id": "category_1", "name": "内容", "topics": [{
            "id": "topic_1", "title": "新手第一次接触 Agent，先弄懂什么",
            "status": "ready", "versions": [{
                "version": 1, "content": "这是已经确认的完整口播文案。",
            }],
        }],
    }]}},
})

user_message = (
    "用第一篇《新手第一次接触 Agent，先弄懂什么》制作数字人口播视频，"
    "继续使用当前已经选好的形象 7 和我的个人音色。"
    "请刷新当前报价；只刷新实时报价，不要提交生成。"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": cid, "message": user_message,
    "expected_revision": state["revision"], "request_id": "quote-reply-turn",
})
assert response.status_code == 200, response.get_data(as_text=True)
turn = response.get_json()
action_payload = turn["actions"][0]
reply_id = turn["assistant_message_id"]
assert action_payload["reply_message_id"] == reply_id, action_payload
assert action_payload["requested_avatar_name"] == "形象 7", action_payload
assert action_payload["reuse_production_id"] == previous["id"], action_payload
revision = turn["state"]["revision"]

bridge_calls = []
def bridge(_account, capability, input_body, **kwargs):
    bridge_calls.append((capability, input_body, kwargs))
    if capability == "video-avatars":
        return {"items": [
            {"id": 9, "name": "形象 6", "status": "ready", "image_url": "/avatar-6.jpg"},
            {"id": 7, "name": "形象 4", "status": "ready", "image_url": "/avatar-4.jpg"},
        ]}
    if capability == "voices":
        return {"items": [{
            "voice_key": "voice-me", "display_name": "岳磊个人音色",
            "preview_url": "/voice.mp3", "scope": "personal", "slot_id": "slot-me",
        }]}
    if capability == "digital-ip-text-generate":
        return {"quote_token": "quote-refresh", "cost": 150, "points": 94359, "expires_in": 300}
    raise AssertionError(capability)

with patch.object(server, "_bridge_catalog", return_value=catalog), patch.object(
    server, "_bridge_action", side_effect=bridge
):
    prepared = client.post("/api/ip12/productions/prepare", json={
        "conversation_id": cid,
        "content_target": action_payload["content_target"],
        "expected_revision": revision, "requested_result": action_payload["requested_result"],
        "preferred_action": action_payload["preferred_action"],
        "specialist_agent": action_payload["specialist_agent"],
        "reply_message_id": action_payload["reply_message_id"],
        "reuse_production_id": action_payload["reuse_production_id"],
        "requested_avatar_name": action_payload["requested_avatar_name"],
        "options": action_payload["options"],
    })
    assert prepared.status_code == 200, prepared.get_data(as_text=True)
    prepared_body = prepared.get_json()
    production_id = prepared_body["production_id"]
    assert production_id == previous["id"], prepared_body
    assert prepared_body["options"]["avatar_id"] == 7, prepared_body
    assert prepared_body["material_request_message"]["message_id"] == reply_id, prepared_body
    assert "没有“形象 7”" in prepared_body["material_request_message"]["content"], prepared_body
    assert "保留原先选择的“形象 4”" in prepared_body["material_request_message"]["content"], prepared_body
    assert "形象 6、形象 4" in prepared_body["material_request_message"]["content"], prepared_body
    assert server._production_choice(
        prepared_body["parameter_schema"], "avatar_id", title="形象 7"
    ) is None
    assert server._production_choice(
        prepared_body["parameter_schema"], "avatar_id", value=7
    )["title"] == "形象 4"
    record = server.load_conversation(cid)["productions"][production_id]
    uploaded_portrait = "img_" + "a" * 32
    record["parameter_schema"]["properties"]["image_upload_id"] = {
        "type": "string", "x-hq-alternative-for": "avatar_id",
    }
    server._production_set_options(record, {
        **record["options"], "avatar_id": 7, "image_upload_id": uploaded_portrait,
    })
    assert record["options"].get("avatar_id") is None, record["options"]
    assert server._production_input(record, record["options"])["image_upload_id"] == uploaded_portrait

    quoted = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": production_id,
        "expected_revision": revision,
    })
    assert quoted.status_code == 200, quoted.get_data(as_text=True)
    quote_body = quoted.get_json()

    bare = client.post("/api/chat-complete", json={
        "conversation_id": cid, "message": "刷新当前报价",
        "expected_revision": revision, "request_id": "bare-quote-refresh-turn",
    })
    assert bare.status_code == 200, bare.get_data(as_text=True)
    bare_turn = bare.get_json()
    bare_action = bare_turn["actions"][0]
    assert bare_action["reuse_production_id"] == production_id, bare_action
    reply_id = bare_turn["assistant_message_id"]
    revision = bare_turn["state"]["revision"]
    prepared = client.post("/api/ip12/productions/prepare", json={
        "conversation_id": cid,
        "content_target": bare_action["content_target"],
        "expected_revision": revision, "requested_result": bare_action["requested_result"],
        "preferred_action": bare_action["preferred_action"],
        "specialist_agent": bare_action["specialist_agent"],
        "options": bare_action["options"],
    })
    assert prepared.status_code == 200, prepared.get_data(as_text=True)
    assert prepared.get_json()["production_id"] == production_id, prepared.get_json()
    quoted = client.post("/api/ip12/productions/quote", json={
        "conversation_id": cid, "production_id": production_id,
        "expected_revision": revision,
    })
    assert quoted.status_code == 200, quoted.get_data(as_text=True)
    quote_body = quoted.get_json()

message = quote_body["material_request_message"]
assert message["message_id"] == reply_id, message
for expected in (
    "新手第一次接触 Agent，先弄懂什么", "形象 4", "岳磊个人音色",
    "本次报价：150 点", "当前余额：94359 点", "当前未扣点", "等待你确认",
):
    assert expected in message["content"], (expected, message)
saved = server.load_conversation(cid)
assert list(saved["productions"]) == [previous["id"]], saved["productions"]
assistant_messages = [item for item in saved["messages"] if item.get("role") == "assistant"]
assert len(assistant_messages) == 2, assistant_messages
assert assistant_messages[-1]["message_id"] == reply_id, assistant_messages
assert all(item["production_id"] == production_id for item in assistant_messages), assistant_messages
assert saved["productions"][production_id]["options"]["avatar_id"] == 7, saved["productions"][production_id]
assert saved["productions"][production_id]["options"]["voice"] == "voice-me", saved["productions"][production_id]
assert saved["productions"][production_id]["source_text"] == previous["source_text"], saved["productions"][production_id]
assert saved["productions"][production_id]["idempotency_key"] == "stable-current-production-key", saved["productions"][production_id]
assert not saved["productions"][production_id].get("job_id"), saved["productions"][production_id]
assert sum(1 for item in saved["messages"] if item.get("role") == "user") == 2, saved["messages"]
assert not any(call[2].get("confirm") for call in bridge_calls), bridge_calls
assert not any(call[0] == "task" for call in bridge_calls), bridge_calls
'''
        with tempfile.TemporaryDirectory(prefix="ip12-quote-reply-test.") as data_dir:
            env = os.environ.copy()
            env.update(OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir)
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=ROOT / "server" / "hermes_ip12",
                env=env, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
