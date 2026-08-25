import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import project_memory
import semantic_router
import eval_contract


def decision(**changes):
    value = {
        "schema": semantic_router.SCHEMA,
        "intent": "direct_answer",
        "delegate_to": "none",
        "tool": "none",
        "reply": "好的",
        "awaiting": "none",
        "confidence": 0.9,
        "reason_codes": [],
        "memory_evidence": [],
        "memory_updates": [],
        "tool_policy": "none",
        "payment_policy": {
            "quote_required": False,
            "explicit_confirmation_required": False,
        },
        "references": {"production_id": "", "category_id": "", "topic_id": ""},
    }
    value.update(changes)
    return value


class SemanticDecisionContractTests(unittest.TestCase):
    def test_live_prompt_closes_t3_audio_privacy_and_missing_voice_gaps(self):
        self.assertIn("用户未指定文案时也不要澄清选题", semantic_router.SYSTEM_PROMPT)
        self.assertIn("不要复述用户提到的内部字段原名", semantic_router.SYSTEM_PROMPT)
        self.assertIn("reply 必须明确说“声音”尚未准备好", semantic_router.SYSTEM_PROMPT)

    def test_permanent_semantic_corpus_is_valid(self):
        corpus = json.loads(
            (Path(__file__).parent / "fixtures" / "ip12_semantic_router_cases.json").read_text()
        )
        self.assertGreaterEqual(len(corpus), 20)
        self.assertEqual(len({case["id"] for case in corpus}), len(corpus))
        for case in corpus:
            with self.subTest(case=case["id"]):
                self.assertTrue(set(case["expected_intents"]) <= semantic_router.INTENTS)
                self.assertIn(case["tool"], semantic_router.TOOLS)

    @unittest.skipUnless(
        os.environ.get("HERMES_SEMANTIC_LIVE_EVAL") == "1",
        "set HERMES_SEMANTIC_LIVE_EVAL=1 to run the real-model semantic corpus",
    )
    def test_live_model_routes_permanent_semantic_corpus(self):
        import server

        corpus = json.loads(
            (Path(__file__).parent / "fixtures" / "ip12_semantic_router_cases.json").read_text()
        )
        for case in corpus:
            with self.subTest(case=case["id"]):
                memory = eval_contract.memory_for_case(case)
                routed = server._semantic_master_decision(memory, case["message"])
                self.assertIn(routed["intent"], case["expected_intents"], routed)
                expected_tools = {case["tool"]}
                if {"clarify", "continue_ip12"}.intersection(case["expected_intents"]):
                    expected_tools.add("none")
                self.assertIn(routed["tool"], expected_tools)
                if case.get("topic_id") and routed["intent"] != "clarify":
                    self.assertEqual(routed["references"]["topic_id"], case["topic_id"])

    def test_rejects_cross_field_mismatches(self):
        invalid = [
            decision(intent="status", tool="none", tool_policy="prepare_only"),
            decision(intent="revise_content", tool="content.revise", tool_policy="none"),
            decision(intent="direct_answer", tool="voice_clone.open"),
            decision(payment_policy={"quote_required": 1, "explicit_confirmation_required": 1}),
            decision(payment_policy={"quote_required": "false", "explicit_confirmation_required": False}),
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(semantic_router.DecisionCombinationError):
                    semantic_router.parse(value)
        with self.assertRaises(semantic_router.DecisionCombinationError):
            semantic_router.validate_combination(decision(
                payment_policy={"quote_required": 1, "explicit_confirmation_required": 1}
            ))

    def test_accepts_only_declared_legal_combinations(self):
        for key in semantic_router.LEGAL_COMBINATIONS:
            intent, delegate, tool, policy, awaiting, quote, explicit = key
            value = decision(
                intent=intent, delegate_to=delegate, tool=tool, tool_policy=policy,
                awaiting=awaiting,
                payment_policy={
                    "quote_required": quote,
                    "explicit_confirmation_required": explicit,
                },
            )
            self.assertEqual(semantic_router.combination_key(semantic_router.parse(value)), key)


class ProjectMemoryTests(unittest.TestCase):
    def test_active_production_requires_persisted_id(self):
        records = {
            "prod_a": {"id": "prod_a", "action": "audio-generate", "status": "quoted"},
            "prod_b": {"id": "prod_b", "action": "digital-ip-text-generate", "status": "running", "job_id": "6916"},
        }
        state = {"revision": 1, "current_module": 6, "completed_modules": [1, 2, 3, 4, 5, 6]}
        no_active = project_memory.build({"id": "p", "productions": records}, state)
        reversed_order = project_memory.build(
            {"id": "p", "productions": dict(reversed(list(records.items())))}, state
        )
        self.assertIsNone(no_active["active_production"])
        self.assertIsNone(reversed_order["active_production"])
        self.assertEqual(
            {item["production_id"] for item in no_active["active_production_candidates"]},
            {"prod_a", "prod_b"},
        )
        running = next(item for item in no_active["productions"] if item["production_id"] == "prod_b")
        self.assertTrue(running["job_present"])
        self.assertNotIn("job_id", running)
        explicit = project_memory.build(
            {"id": "p", "productions": records, "active_production_id": "prod_a"}, state
        )
        self.assertEqual(explicit["active_production"]["production_id"], "prod_a")

    def test_runtime_delegation_resolves_active_talking_head_without_active_id(self):
        state = {"completed_modules": [1, 2, 3, 4, 5, 6]}
        project = {
            "id": "p", "active_production_id": None,
            "agent_runtime": {
                "active_delegation_id": "delegate_video",
                "last_delegation_id": "delegate_video",
            },
            "productions": {
                "prod_audio": {
                    "id": "prod_audio", "action": "audio-generate", "status": "stale",
                },
                "prod_video": {
                    "id": "prod_video", "action": "digital-ip-text-generate",
                    "status": "quoted", "source_text": "已确认文案",
                    "options": {"avatar_id": 6, "voice": "my_voice"},
                    "quote": {"cost": 90},
                    "specialist_agent": {
                        "agent_id": "talking_head_video_agent",
                        "delegation_id": "delegate_video",
                        "stage": "awaiting_confirmation",
                        "next_action": "等待用户确认报价",
                    },
                },
            },
            "voice_clone_ui": {"status": "complete", "voice_name": "我的个人音色"},
        }
        memory = project_memory.build(project, state)
        self.assertEqual(memory["active_production"]["production_id"], "prod_video")
        self.assertEqual(memory["active_production"]["status"], "quoted")
        self.assertEqual(memory["available_assets"], {
            "avatar_ready": True, "voice_ready": True,
        })

    def test_project_memory_exposes_conservative_asset_readiness(self):
        state = {"revision": 1, "current_module": 6, "completed_modules": [1, 2, 3, 4, 5, 6]}
        project = {
            "id": "p", "active_production_id": "prod_video",
            "voice_clone_ui": {"status": "complete", "voice_name": "我的音色"},
            "productions": {"prod_video": {
                "id": "prod_video", "action": "digital-ip-text-generate",
                "status": "draft", "options": {"avatar_id": 7},
            }},
        }
        memory = project_memory.build(project, state)
        self.assertEqual(memory["available_assets"], {
            "avatar_ready": True, "voice_ready": True,
        })
        empty = project_memory.build({"id": "empty"}, state)
        self.assertEqual(empty["available_assets"], {
            "avatar_ready": False, "voice_ready": False,
        })

    def test_content_reference_requires_explicit_or_persisted_target(self):
        memory = {
            "content_topics": [
                {"category_id": "c1", "topic_id": "t1", "title": "第一篇"},
                {"category_id": "c2", "topic_id": "t2", "title": "第二篇"},
                {"category_id": "c3", "topic_id": "t3", "title": "第三篇"},
            ],
            "active_content_target": {"category_id": "", "topic_id": ""},
        }
        self.assertIsNone(project_memory.resolve_content_reference(memory, "把这个改一下"))
        self.assertEqual(
            project_memory.resolve_content_reference(memory, "把第三篇开头改短"),
            {"category_id": "c3", "topic_id": "t3"},
        )
        memory["active_content_target"] = {"category_id": "c2", "topic_id": "t2"}
        self.assertEqual(
            project_memory.resolve_content_reference(memory, "把这篇改自然一点"),
            {"category_id": "c2", "topic_id": "t2"},
        )


class SemanticServerIntegrationTests(unittest.TestCase):
    def test_status_catalog_redaction_trace_and_no_submit(self):
        script = r'''
import json
import security
import server

security._validate_token = lambda _token: {
    "account_id": "semantic-account", "username": "semantic", "role": "member",
}
security.RATE_REQUESTS = 1000
client = server.app.test_client()
headers = {"Authorization": "Bearer semantic-test"}

def state():
    value = server.initial_coach_state()
    value.update(
        revision=1, current_module=6, module_step=3,
        completed_modules=[1, 2, 3, 4, 5, 6],
        foundation_report={"status": "confirmed"},
    )
    return value

def put(cid, productions=None, active_id="", deliverables=None):
    project = {
        "id": cid, "title": cid, "owner_account_id": "semantic-account",
        "messages": [], "coach_state": state(), "reports": {},
        "deliverables": deliverables or {},
        "productions": productions or {}, "updated": "",
        "turn_receipts": [{"request_id": "private", "result": {}}],
        "agent_runs": {"private": {"_private": {"token": "secret"}}},
        "master_agent_shadow": {"private": True}, "semantic_master": {"private": True},
    }
    if active_id:
        project["active_production_id"] = active_id
    server.save_conversation(cid, project)
    return project

def semantic(intent, tool, policy, awaiting="none", delegate="none", production_id="", quote=False, explicit=False, reply="ok"):
    return {
        "schema": "ip12.semantic-master-decision/v1", "intent": intent,
        "delegate_to": delegate, "tool": tool, "reply": reply,
        "awaiting": awaiting, "confidence": 0.95, "reason_codes": ["test"],
        "memory_evidence": [], "memory_updates": [], "tool_policy": policy,
        "payment_policy": {"quote_required": quote, "explicit_confirmation_required": explicit},
        "references": {"production_id": production_id, "category_id": "", "topic_id": ""},
    }

catalog = {
    "actions": [
        {"action": "audio-slots", "availability": {"status": "available"}},
        {"action": "audio-generate", "availability": {"status": "available"}},
        {"action": "digital-ip-text-generate", "availability": {"status": "available"}},
    ]
}
server._bridge_catalog = lambda _account: copy.deepcopy(catalog)

# Voice-clone status is read from the original account slot, never guessed by the model.
put("voice-status")
voice_project = server.load_conversation("voice-status")
voice_project["voice_clone_ui"] = {
    "status": "training", "slot_id": "slot_voice_ready",
    "voice_name": "我的个人音色", "error": "",
}
server.save_conversation("voice-status", voice_project)
voice_calls = []
def voice_bridge(_account, action, payload, **_kwargs):
    voice_calls.append((action, payload))
    assert action == "audio-slots", action
    return {"items": [{
        "slot_id": "slot_voice_ready", "status": "ready",
        "voice_name": "我的个人音色", "preview_url": "https://media.example/voice.mp3",
    }]}
server._bridge_action = voice_bridge
server._semantic_master_decision = lambda *_: semantic(
    "status", "voice_clone.status", "read_only", reply="unsafe model status"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "voice-status", "message": "我的音频已经在克隆了吗？",
    "expected_revision": 1, "request_id": "voice-status-1",
}, headers=headers)
body = response.get_json()
assert response.status_code == 200, body
assert "已经复刻完成" in body["assistant"] and "unsafe model status" not in body["assistant"], body
assert voice_calls == [("audio-slots", {})], voice_calls
saved_voice = server.load_conversation("voice-status")
assert saved_voice["voice_clone_ui"]["status"] == "complete", saved_voice["voice_clone_ui"]
assert not saved_voice.get("productions"), saved_voice.get("productions")

# Deterministic status refreshes the original job and writes the result back.
put("status", {
    "prod_old": {"id": "prod_old", "action": "audio-generate", "capability_family": "audio", "status": "quoted", "quote": {"cost": 10}},
    "prod_run": {"id": "prod_run", "action": "digital-ip-text-generate", "capability_family": "video", "status": "running", "job_id": "123", "asset_refs": []},
}, "prod_run")
calls = []
def bridge(_account, action, payload, **_kwargs):
    calls.append((action, payload))
    if action == "task":
        return {"job_id": 123, "status": "done", "asset_refs": [{"kind": "video", "url": "https://media.example/out.mp4"}]}
    raise AssertionError(action)
server._bridge_action = bridge
server._semantic_master_decision = lambda _memory, _message: semantic(
    "status", "project.status", "read_only", production_id="prod_run", reply="stale model text"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "status", "message": "现在怎么样", "expected_revision": 1,
    "request_id": "status-1",
}, headers=headers)
body = response.get_json()
assert response.status_code == 200, body
assert "已经完成" in body["assistant"], body
assert "stale model text" not in body["assistant"], body
assert calls == [("task", {"job_id": 123})], calls
saved = server.load_conversation("status")
assert saved["productions"]["prod_run"]["status"] == "done", saved
assert saved["active_production_id"] == "prod_run", saved
assert saved["messages"][-1]["agent_trace"]["skills"][0]["id"] == "semantic_master_agent", saved["messages"][-1]
assert "master_decision" not in body, body

# Multiple active productions without a persisted or explicit target clarify and do not poll.
put("ambiguous", {
    "prod_audio": {"id": "prod_audio", "action": "audio-generate", "capability_family": "audio", "status": "quoted"},
    "prod_video": {"id": "prod_video", "action": "digital-ip-text-generate", "capability_family": "video", "status": "quoted"},
})
server._bridge_action = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call"))
server._semantic_master_decision = lambda *_: semantic(
    "status", "project.status", "read_only", production_id="prod_video", reply="wrong"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "ambiguous", "message": "现在怎么样", "expected_revision": 1,
    "request_id": "ambiguous-1",
}, headers=headers)
body = response.get_json()
assert "多个待处理项目" in body["assistant"], body

# A natural-language object reference selects one candidate without trusting model IDs.
revision = server.load_conversation("ambiguous")["coach_state"]["revision"]
response = client.post("/api/chat-complete", json={
    "conversation_id": "ambiguous", "message": "试听音频现在怎么样", "expected_revision": revision,
    "request_id": "ambiguous-audio-1",
}, headers=headers)
body = response.get_json()
assert "已经准备好报价" in body["assistant"], body
assert server.load_conversation("ambiguous")["active_production_id"] == "prod_audio", body

# Multiple explicit candidates of the same type never fall back to an unrelated active item.
put("same-family", {
    "prod_audio_1": {"id": "prod_audio_1", "action": "audio-generate", "capability_family": "audio", "status": "quoted"},
    "prod_audio_2": {"id": "prod_audio_2", "action": "audio-generate", "capability_family": "audio", "status": "quoted"},
    "prod_video": {"id": "prod_video", "action": "digital-ip-text-generate", "capability_family": "video", "status": "quoted"},
}, "prod_video")
server._semantic_master_decision = lambda *_: semantic(
    "status", "project.status", "read_only", production_id="prod_video", reply="wrong"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "same-family", "message": "试听音频现在怎么样", "expected_revision": 1,
    "request_id": "same-family-1",
}, headers=headers)
body = response.get_json()
assert "多个待处理项目" in body["assistant"], body
assert server.load_conversation("same-family")["active_production_id"] == "prod_video", body

# Model-authored customer replies cannot expose production or job IDs.
redaction_pack = {"6": {"kind": "content_pack_v1", "categories": [{
    "id": "category-internal-01", "name": "实操类", "topics": [{
        "id": "topic-internal-01", "title": "第一篇", "versions": [],
    }],
}]}}
put("reply-redaction", {
    "prod_audio": {
        "id": "prod_audio", "job_id": "6916", "action": "audio-generate",
        "capability_family": "audio", "status": "quoted",
    },
}, deliverables=redaction_pack)
server._semantic_master_decision = lambda *_: semantic(
    "clarify", "none", "none", awaiting="user_input",
    reply=("reply-redaction / prod_audio / 6916 / category-internal-01 / "
           "topic-internal-01 / ip12.project-memory/v1"),
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "reply-redaction", "message": "现在怎么样", "expected_revision": 1,
    "request_id": "reply-redaction-1",
}, headers=headers)
body = response.get_json()
for internal_id in (
    "reply-redaction", "prod_audio", "6916", "category-internal-01",
    "topic-internal-01", "ip12.project-memory/v1",
):
    assert internal_id not in body["assistant"], (internal_id, body)
public_reply = client.get("/api/conversations/reply-redaction", headers=headers).get_json()
assert not any(internal_id in public_reply["messages"][-1]["content"] for internal_id in (
    "reply-redaction", "prod_audio", "6916", "category-internal-01", "topic-internal-01",
)), public_reply["messages"][-1]

public_status = server._public_chat_result({"agent_status": {
    "run_id": "master-delegate-prod-secret", "status": "awaiting",
    "awaiting": "confirmation", "next_action": "wait",
    "delegate_to": "talking_head_video_agent",
    "specialist_result": {"production_id": "prod-secret", "job_id": "123"},
}})
assert public_status["agent_status"] == {
    "status": "awaiting", "awaiting": "confirmation", "next_action": "wait",
    "delegate": "口播短视频 Agent",
}, public_status

# Status reads deterministically expire an old quote without refreshing it.
put("expired", {
    "prod_expired": {
        "id": "prod_expired", "action": "audio-generate", "capability_family": "audio",
        "status": "quoted", "quote": {"cost": 10, "expires_at": 1},
    },
}, "prod_expired")
server._semantic_master_decision = lambda *_: semantic(
    "status", "project.status", "read_only", production_id="prod_expired", reply="wrong"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "expired", "message": "报价还有效吗", "expected_revision": 1,
    "request_id": "expired-1",
}, headers=headers)
body = response.get_json()
assert "过期" in body["assistant"], body
assert server.load_conversation("expired")["productions"]["prod_expired"]["status"] == "stale"

# A model-selected unavailable tool fails closed before prepare/quote.
put("disabled")
server._bridge_catalog = lambda _account: {
    "actions": [{"action": "audio-generate", "availability": {"status": "disabled"}}]
}
server._semantic_master_decision = lambda *_: semantic(
    "delegate", "audio_preview.prepare", "prepare_only", awaiting="confirmation",
    delegate="audio_preview_agent", quote=True, explicit=True,
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "disabled", "message": "做个试听", "expected_revision": 1,
    "request_id": "disabled-1",
}, headers=headers)
body = response.get_json()
assert not body.get("actions"), body
assert "不可用" in body["assistant"] or "没有解锁" in body["assistant"], body
assert not server.load_conversation("disabled").get("productions"), body

# An available account capability is still blocked when the Project gate is locked.
put("locked")
locked = server.load_conversation("locked")
locked["coach_state"].update(completed_modules=[], current_module=1, foundation_report={})
server.save_conversation("locked", locked)
server._bridge_catalog = lambda _account: copy.deepcopy(catalog)
response = client.post("/api/chat-complete", json={
    "conversation_id": "locked", "message": "做个试听", "expected_revision": 1,
    "request_id": "locked-1",
}, headers=headers)
body = response.get_json()
assert not body.get("actions"), body
assert not server.load_conversation("locked").get("productions"), body

# Direct production APIs use the same live catalog and Project gate, even without specialist_agent.
put("direct-locked")
direct_locked = server.load_conversation("direct-locked")
direct_locked["coach_state"].update(completed_modules=[], current_module=1, foundation_report={})
server.save_conversation("direct-locked", direct_locked)
server._bridge_catalog = lambda _account: copy.deepcopy(catalog)
response = client.post("/api/ip12/productions/prepare", json={
    "conversation_id": "direct-locked", "expected_revision": 1,
    "requested_result": "audio", "preferred_action": "audio-generate",
    "options": {"text": "测试试听"},
}, headers=headers)
assert response.status_code == 400, response.get_json()
assert not server.load_conversation("direct-locked").get("productions"), response.get_json()

put("direct-unlocked")
server._production_parameter_context = lambda *_args, **_kwargs: ({
    "type": "object", "additionalProperties": False,
    "properties": {"text": {"type": "string"}}, "required": ["text"],
}, {})
response = client.post("/api/ip12/productions/prepare", json={
    "conversation_id": "direct-unlocked", "expected_revision": 1,
    "requested_result": "audio", "preferred_action": "audio-generate",
    "options": {"text": "测试试听"},
}, headers=headers)
prepared = response.get_json()
assert response.status_code == 200, prepared
direct_id = prepared["production_id"]
server._bridge_catalog = lambda _account: {
    "actions": [{"action": "audio-generate", "availability": {"status": "disabled"}}]
}
response = client.post("/api/ip12/productions/quote", json={
    "conversation_id": "direct-unlocked", "production_id": direct_id,
    "expected_revision": 1,
}, headers=headers)
assert response.status_code == 400, response.get_json()
direct = server.load_conversation("direct-unlocked")
record = direct["productions"][direct_id]
record["status"] = "quoted"
record["quote"] = {
    "token": "private", "billing": "paid", "expires_at": server._utc_timestamp() + 600,
    "input_digest": record["input_digest"],
}
server.save_conversation("direct-unlocked", direct)
response = client.post("/api/ip12/productions/confirm", json={
    "conversation_id": "direct-unlocked", "production_id": direct_id,
    "expected_revision": 1, "confirmation_id": "confirm-gate-01",
}, headers=headers)
assert response.status_code == 400, response.get_json()
assert server.load_conversation("direct-unlocked")["productions"][direct_id]["status"] == "quoted"

# Quote always rechecks the live gate, even when the first snapshot looks submitted.
put("quote-preflight", {
    "prod_queued": {
        "id": "prod_queued", "action": "audio-generate", "capability_family": "audio",
        "status": "queued", "job_id": "6916", "options": {}, "source_bound": False,
    },
})
gate_calls = []
original_gate = server._validate_production_action
server._validate_production_action = lambda account, current_state, action: gate_calls.append(
    (account, action)
)
response = client.post("/api/ip12/productions/quote", json={
    "conversation_id": "quote-preflight", "production_id": "prod_queued",
    "expected_revision": 1,
}, headers=headers)
server._validate_production_action = original_gate
assert gate_calls == [("semantic-account", "audio-generate")], gate_calls
assert response.status_code == 409, response.get_json()

# Semantic delegation is traceable to the master without calling a production tool.
put("delegate-trace")
server._bridge_catalog = lambda _account: copy.deepcopy(catalog)
server._semantic_master_decision = lambda *_: semantic(
    "delegate", "voice_clone.open", "prepare_only", awaiting="user_input",
    delegate="voice_clone_agent", explicit=True,
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "delegate-trace", "message": "重新克隆声音", "expected_revision": 1,
    "request_id": "delegate-trace-1",
}, headers=headers)
body = response.get_json()
assert response.status_code == 200, body
trace_ids = [item["id"] for item in server.load_conversation("delegate-trace")["messages"][-1]["agent_trace"]["skills"]]
assert trace_ids[:2] == ["semantic_master_agent", "production_bridge"], trace_ids

# Execution-time matrix validation catches a mocked parser bypass.
put("invalid")
server._bridge_catalog = lambda _account: copy.deepcopy(catalog)
invalid = semantic("status", "none", "prepare_only", reply="unsafe")
server._semantic_master_decision = lambda *_: invalid
response = client.post("/api/chat-complete", json={
    "conversation_id": "invalid", "message": "test", "expected_revision": 1,
    "request_id": "invalid-1",
}, headers=headers)
body = response.get_json()
assert "unsafe" not in body["assistant"], body
assert not body.get("actions"), body

# Text confirmation remains a non-tool answer; default JSON/SSE never expose decisions.
put("redaction")
server._semantic_master_decision = lambda *_: semantic(
    "direct_answer", "none", "none", reply="请使用报价卡确认"
)
response = client.post("/api/chat-complete", json={
    "conversation_id": "redaction", "message": "确认提交10点", "expected_revision": 1,
    "request_id": "redact-json",
}, headers=headers)
body = response.get_json()
assert "master_decision" not in body, body
revision = server.load_conversation("redaction")["coach_state"]["revision"]
sse = client.post("/api/chat", json={
    "conversation_id": "redaction", "message": "确认提交10点", "expected_revision": revision,
    "request_id": "redact-sse",
}, headers=headers).get_data(as_text=True)
assert "master_decision" not in sse, sse
public = client.get("/api/conversations/redaction", headers=headers).get_json()
for key in ("owner_account_id", "turn_receipts", "agent_runs", "master_agent_shadow", "semantic_master"):
    assert key not in public, (key, public)

# Historical receipts are sanitized on replay and receipt polling too.
old = server.load_conversation("redaction")
old["turn_receipts"] = [{
    "request_id": "old-debug",
    "result": {
        "ok": True, "assistant": "old", "state": old["coach_state"],
        "master_decision": {"tool": "internal", "references": {"production_id": "secret"}},
    },
}]
server.save_conversation("redaction", old)
replay = client.post("/api/chat-complete", json={
    "conversation_id": "redaction", "message": "old", "request_id": "old-debug",
}, headers=headers).get_json()
polled = client.get("/api/conversations/redaction?receipt=old-debug", headers=headers).get_json()
assert "master_decision" not in replay, replay
assert "master_decision" not in polled, polled

print("SEMANTIC_SERVER_INTEGRATION_OK")
'''
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=str(HERMES), HERMES_DATA_DIR=root,
                HERMES_MASTER_AGENT_MODE="live", HERMES_SEMANTIC_ROUTER_MODE="live",
                HERMES_SEMANTIC_DEBUG="0",
            )
            result = subprocess.run(
                [sys.executable, "-c", "import copy\n" + script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
