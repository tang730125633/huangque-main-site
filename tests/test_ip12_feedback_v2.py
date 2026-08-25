import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "server" / "hermes_ip12"


class IP12FeedbackV2Tests(unittest.TestCase):
    def test_intake_and_module_three_revision_keep_stateful_harness_priority(self):
        script = r'''
import copy
import security
import server

security._validate_token = lambda _token: {
    "account_id": "acct-feedback", "username": "feedback", "role": "member",
}
security.RATE_REQUESTS = 100
client = server.app.test_client()
headers = {"Authorization": "Bearer test"}

def forbidden(name):
    return lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError(name + " must be bypassed")
    )

server._semantic_master_decision = forbidden("semantic router")
server._process_semantic_reply = forbidden("fixed semantic reply")
server._process_production_intent_turn = forbidden("production intent")
server._bridge_catalog = forbidden("capability catalog")
server._bridge_action = forbidden("HQ action")

# A. The current asked field owns the next intake answer even when it reads like a question.
intake_cid = client.post("/api/conversations", json={"title": "岳磊 intake 回归"}, headers=headers).get_json()["id"]
intake = server.load_conversation(intake_cid)
state = intake["coach_state"]
prior_evidence = (
    "我叫岳磊；我负责功能开发；我参与黄雀AI的开发；"
    "我擅长AI编程；我擅长前端设计；我关注Agent的发展；"
    "我希望帮助新手小白和想入行的人"
)
state["intake"].update(
    status="collecting", round=1, asked_field="help_goal",
    asked_follow_ups=["help_goal"],
    profile_updates=[
        {"field": "preferred_name", "value": "岳磊", "kind": "user_fact", "evidence_quote": "我叫岳磊"},
        {"field": "current_role", "value": "功能开发", "kind": "user_fact", "evidence_quote": "我负责功能开发"},
        {"field": "project_experience", "value": "黄雀AI的开发", "kind": "user_fact", "evidence_quote": "我参与黄雀AI的开发"},
        {"field": "core_skill_1", "value": "AI编程", "kind": "user_fact", "evidence_quote": "我擅长AI编程"},
        {"field": "core_skill_2", "value": "前端设计", "kind": "user_fact", "evidence_quote": "我擅长前端设计"},
        {"field": "long_term_interest", "value": "Agent的发展", "kind": "user_fact", "evidence_quote": "我关注Agent的发展"},
        {"field": "target_audience", "value": "新手小白和想入行的人", "kind": "user_fact", "evidence_quote": "我希望帮助新手小白和想入行的人"},
    ],
)
intake["messages"] = [
    {"role": "user", "content": prior_evidence},
    {"role": "assistant", "content": "你最希望帮他们解决的一个核心问题是什么？"},
]
intake["coach_state"] = state
server.save_conversation(intake_cid, intake)
server.call_ai = forbidden("generic model answer")
intake_message = "如何使用agent去独自完成一个项目"
response = client.post("/api/chat-complete", json={
    "conversation_id": intake_cid, "message": intake_message,
    "expected_revision": state["revision"], "request_id": "feedback-intake-original",
}, headers=headers)
body = response.get_json()
assert response.status_code == 200, body
assert body["state"]["intake"]["status"] == "awaiting_confirmation", body
assert "基础定位核对稿" in body["assistant"] and "请确认资料" in body["assistant"], body
assert "需求澄清、方案设计" not in body["assistant"], body
saved = server.load_conversation(intake_cid)
help_goal = next(
    item for item in saved["coach_state"]["intake"]["profile_updates"]
    if item["field"] == "help_goal"
)
assert help_goal["value"] == intake_message and help_goal["evidence_quote"] == intake_message
assert len(saved["turn_receipts"]) == 1, saved["turn_receipts"]
assert sum(1 for item in saved["messages"] if item.get("role") == "user" and item.get("content") == intake_message) == 1

# B. Module checkpoint editing outranks semantic/capability/production routes.
revision_messages = (
    "最开始带他们来了解agent是什么",
    "先带新手认识agent是什么",
)
module_cid = client.post("/api/conversations", json={"title": "模块3修改回归"}, headers=headers).get_json()["id"]
for index, revision_message in enumerate(revision_messages, 1):
    cid = module_cid
    convo = server.load_conversation(cid)
    state = server.initial_coach_state()
    state.update(
        current_module=3, module_step=0, completed_modules=[1, 2],
        intake={"status": "complete", "round": 3, "answers": {}, "asked_follow_ups": [], "asked_field": ""},
        pending={
            "id": "module3-edit-%s" % index, "kind": "checkpoint",
            "status": "editing", "module": 3, "step": 1,
            "draft": "旧价值关键词：直接教新手用 Agent 完成项目。",
            "self_review": "旧稿待修改", "profile_updates": [], "confidence": 0.8,
        },
    )
    convo["coach_state"] = state
    convo["turn_receipts"] = []
    convo["agent_runs"] = {}
    convo["productions"] = {}
    convo["messages"] = [
        {"role": "user", "content": "修改当前内容"},
        {"role": "assistant", "content": "请直接说希望怎样改"},
    ]
    server.save_conversation(cid, convo)

    def module_three_decision(_snapshot, message, **_kwargs):
        assert message == revision_message
        return ({
            "decision": "propose_checkpoint", "checkpoint": 1,
            "reply": "我刚才把实践放得太靠前；现在已改为先建立 Agent 认知，再进入项目实践。",
            "draft": (
                "### 更新后的模块 3：价值关键词\n\n"
                "1. **Agent 认知**：先帮助新手认识 Agent 是什么、能做什么。\n"
                "2. **新手友好**：从容易理解的概念和场景开始。\n"
                "3. **实操入门**：再逐步教他们用 Agent 推进具体项目。\n"
                "4. **独立完成**：最终帮助新手独自完成一个项目。\n\n"
                "**差异**：从“直接做项目”调整为“先懂 Agent，再逐步实践”。"
            ),
            "self_review": "只调整价值表达顺序，没有增加既有成果。",
            "choices": [], "profile_updates": [], "confidence": 1.0,
        }, revision_message)

    server._coach_model_decision = module_three_decision
    response = client.post("/api/chat-complete", json={
        "conversation_id": cid, "message": revision_message,
        "expected_revision": state["revision"],
        "request_id": "feedback-module3-original-%s" % index,
    }, headers=headers)
    body = response.get_json()
    assert response.status_code == 200, body
    assert "能力当前没有解锁" not in body["assistant"], body
    assert "更新后的模块 3" in body["assistant"] and "差异" in body["assistant"], body
    assert "先帮助新手认识 Agent 是什么" in body["state"]["pending"]["draft"], body
    assert body["state"]["pending"]["status"] == "awaiting_confirmation", body
    saved = server.load_conversation(cid)
    assert len(saved["turn_receipts"]) == 1, saved["turn_receipts"]
    assert sum(1 for item in saved["messages"] if item.get("role") == "user" and item.get("content") == revision_message) == 1
    assert not saved.get("productions") and not saved.get("artifacts")

# Ordinary questions without an asked field remain outside deterministic intake consumption.
ordinary = server.initial_coach_state()
assert server.coach_harness.compile_asked_intake_answer(ordinary, "Agent 是什么？") is None
print("IP12_FEEDBACK_V2_OK")
'''
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=root, HERMES_DATA_DIR=root,
                HERMES_MASTER_AGENT_MODE="live", HERMES_SEMANTIC_ROUTER_MODE="live",
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IP12_FEEDBACK_V2_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
