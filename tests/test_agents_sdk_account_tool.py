import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import agent_runtime
import cognitive_engine


ACCOUNT_CAPABILITY = {
    "action": "account",
    "purpose": "读取当前黄雀账号与点数",
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "required": [], "properties": {},
    },
    "billing": "free", "external_effect": False,
    "confirmation_required": False, "risk": "read",
    "availability": {"status": "available"},
}

TALKING_HEAD_ACTIONS = (
    "ip12-project", "video-avatars", "voices", "audio-slots", "assets",
)
TALKING_HEAD_CAPABILITIES = [{
    "action": action,
    "purpose": "read " + action,
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "required": [], "properties": {},
    },
    "billing": "free", "external_effect": False,
    "confirmation_required": False, "risk": "read",
    "availability": {"status": "available"},
} for action in TALKING_HEAD_ACTIONS]


class Policy:
    agent_id = "ip12_master_agent"


try:
    from agents.agent_output import AgentOutputSchemaBase
    from agents.handoffs import Handoff
    from agents.items import ModelResponse, TResponseInputItem, TResponseOutputItem
    from agents.model_settings import ModelSettings
    from agents.models.interface import Model, ModelTracing
    from agents.tool import Tool
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText,
    )
except ImportError:
    Model = None

BaseModelInterface = Model if Model is not None else object


def _text_message(text):
    return ResponseOutputMessage(
        id="msg_final",
        content=[ResponseOutputText(
            type="output_text", text=text, annotations=[],
        )],
        role="assistant", status="completed", type="message",
    )


def _final_decision(points):
    return json.dumps({
        "schema": "ip12.semantic-master-decision/v1",
        "intent": "direct_answer", "delegate_to": "none", "tool": "none",
        "reply": "你当前还有 %s 点。" % points,
        "awaiting": "none", "confidence": 0.99,
        "reason_codes": ["account_read_completed"],
        "memory_evidence": [], "memory_updates": [], "tool_policy": "none",
        "payment_policy": {
            "quote_required": False, "explicit_confirmation_required": False,
        },
        "references": {"production_id": "", "category_id": "", "topic_id": ""},
    }, ensure_ascii=False)


@unittest.skipIf(Model is None, "optional Agents SDK is not installed")
class AgentsSDKAccountRunnerTests(unittest.TestCase):
    class AccountModel(BaseModelInterface):
        def __init__(self, repeat=False):
            self.calls = 0
            self.inputs = []
            self.tools = []
            self.repeat = repeat

        async def get_response(
            self, system_instructions, input, model_settings, tools,
            output_schema, handoffs, tracing, *, previous_response_id,
            conversation_id, prompt,
        ):
            self.calls += 1
            self.inputs.append(copy.deepcopy(input))
            self.tools.append(tools)
            if self.calls == 1 or self.repeat:
                output = [ResponseFunctionToolCall(
                    type="function_call", name="account_read",
                    call_id="call_account_%s" % self.calls,
                    arguments="{}", status="completed",
                )]
            else:
                output = [_text_message(_final_decision(94359))]
            return ModelResponse(
                output=output,
                usage=Usage(
                    requests=1, input_tokens=20 + self.calls,
                    output_tokens=5, total_tokens=25 + self.calls,
                ),
                response_id="resp_%s" % self.calls,
            )

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    def setUp(self):
        self.project = {"id": "project_1"}
        self.run = agent_runtime.start(
            self.project, "run_account_1", Policy(),
            "我的黄雀账号还有多少点？", project_id="project_1",
        )

    def test_runner_calls_real_context_executor_and_returns_final_reply(self):
        executions = []
        model = self.AccountModel()

        def execute(action, arguments):
            executions.append((action, copy.deepcopy(arguments)))
            return {
                "points": 94359, "username": "private-user",
                "account_id": "private-account",
            }

        result = cognitive_engine.agents_sdk_account_run(
            execute_action=execute,
            account_capability=ACCOUNT_CAPABILITY,
            user_message="我的黄雀账号还有多少点？",
            run=self.run,
            model=model,
            request_id="req_1",
        )

        self.assertEqual(result["final_text"], "你当前还有 94359 点。")
        self.assertEqual(result["model_rounds"], 2)
        self.assertEqual(executions, [("account", {})])
        self.assertEqual(model.calls, 2)
        self.assertEqual(
            [tool.name for tool in model.tools[0]],
            ["talking_head_video_agent", "account_read"],
        )
        account_tool = next(tool for tool in model.tools[0] if tool.name == "account_read")
        self.assertTrue(account_tool.strict_json_schema)
        schema = account_tool.params_json_schema
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"], {})
        self.assertEqual(schema["required"], [])
        self.assertIs(schema["additionalProperties"], False)
        self.assertTrue(any(
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == "call_account_1"
            for item in model.inputs[1]
        ))
        call = self.run["tool_calls"]["call_account_1"]
        self.assertEqual(call["tool"], "account")
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["observation"], {"points": 94359})
        self.assertEqual(
            [item["response_id"] for item in self.run["model_responses"]],
            ["resp_1", "resp_2"],
        )
        self.assertNotIn("private-user", str(agent_runtime.public_run(self.run)))

    def test_runner_fails_closed_on_repeated_tool_call(self):
        model = self.AccountModel(repeat=True)
        with self.assertRaises(Exception):
            cognitive_engine.agents_sdk_account_run(
                execute_action=lambda *_args: {"points": 1},
                account_capability=ACCOUNT_CAPABILITY,
                user_message="查余额", run=copy.deepcopy(self.run), model=model,
            )
        self.assertEqual(model.calls, 2)


@unittest.skipIf(Model is None, "optional Agents SDK is not installed")
class AgentsSDKTalkingHeadRunnerTests(unittest.TestCase):
    class TalkingHeadModel(BaseModelInterface):
        def __init__(self, ready=False):
            self.calls = []
            self.master_rounds = 0
            self.specialist_rounds = 0
            self.ready = ready

        async def get_response(
            self, system_instructions, input, model_settings, tools,
            output_schema, handoffs, tracing, *, previous_response_id,
            conversation_id, prompt,
        ):
            names = [tool.name for tool in tools]
            self.calls.append({"tools": names, "input": copy.deepcopy(input)})
            if names == ["talking_head_video_agent"]:
                self.master_rounds += 1
                if self.master_rounds == 1:
                    output = [ResponseFunctionToolCall(
                        type="function_call", name="talking_head_video_agent",
                        call_id="call_specialist_1",
                        arguments=json.dumps({"input": "读取真实状态并解释失败原因"}),
                        status="completed",
                    )]
                else:
                    self.assert_specialist_result(input)
                    output = [_text_message(json.dumps({
                        "schema": "ip12.semantic-master-decision/v1",
                        "intent": "status", "delegate_to": "none",
                        "tool": "project.status",
                        "reply": (
                            "你刚克隆成功的个人音色、已有形象和已确认文案都已经选好，当前也已有报价等待确认，"
                            "不需要重复上传图片或音频。你可以直接确认当前报价，或者先修改文案、形象或声音；"
                            "未经确认不会提交或扣点。"
                            if self.ready else
                            "这次口播任务已经失败并完成退款；形象和声音仍可用。建议先检查失败原因，再决定是否重新报价，不会自动重试。"
                        ),
                        "awaiting": "none", "confidence": 0.98,
                        "reason_codes": ["specialist_read_completed"],
                        "memory_evidence": [], "memory_updates": [],
                        "tool_policy": "read_only",
                        "payment_policy": {
                            "quote_required": False,
                            "explicit_confirmation_required": False,
                        },
                        "references": {
                            "production_id": "", "category_id": "", "topic_id": "",
                        },
                    }, ensure_ascii=False))]
            else:
                if set(names) != set(TALKING_HEAD_ACTIONS):
                    raise AssertionError("Specialist did not receive the five HQ read tools")
                self.specialist_rounds += 1
                if self.specialist_rounds == 1:
                    output = [ResponseFunctionToolCall(
                        type="function_call", name=name,
                        call_id="call_" + name.replace("-", "_"),
                        arguments="{}", status="completed",
                    ) for name in TALKING_HEAD_ACTIONS]
                else:
                    outputs = {
                        item.get("call_id") for item in input
                        if isinstance(item, dict)
                        and item.get("type") == "function_call_output"
                    }
                    if outputs != {
                        "call_" + name.replace("-", "_")
                        for name in TALKING_HEAD_ACTIONS
                    }:
                        raise AssertionError("HQ read outputs were not returned to Specialist")
                    output = [_text_message(json.dumps({
                        "status": "awaiting_confirmation" if self.ready else "failed",
                        "missing": [], "ready_to_quote": False,
                        "next_action": (
                            "wait_for_explicit_confirmation" if self.ready
                            else "explain_failure_without_retry"
                        ),
                        "production_status": "quoted" if self.ready else "failed",
                        "refund_status": "none" if self.ready else "refunded",
                        "error_code": "" if self.ready else "provider_failed",
                    }, ensure_ascii=False))]
            index = len(self.calls)
            return ModelResponse(
                output=output,
                usage=Usage(requests=1, input_tokens=20, output_tokens=5, total_tokens=25),
                response_id="talking_resp_%s" % index,
            )

        def assert_specialist_result(self, input):
            outputs = [
                item for item in input if isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == "call_specialist_1"
            ]
            expected = "awaiting_confirmation" if self.ready else "provider_failed"
            if len(outputs) != 1 or expected not in str(outputs[0].get("output")):
                raise AssertionError("specialist result was not returned to Master")

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    class DirectAnswerModel(BaseModelInterface):
        def __init__(self):
            self.calls = 0

        async def get_response(
            self, system_instructions, input, model_settings, tools,
            output_schema, handoffs, tracing, *, previous_response_id,
            conversation_id, prompt,
        ):
            self.calls += 1
            if [tool.name for tool in tools] != ["talking_head_video_agent"]:
                raise AssertionError("Master tool surface changed")
            return ModelResponse(
                output=[_text_message(json.dumps({
                    "schema": "ip12.semantic-master-decision/v1",
                    "intent": "direct_answer", "delegate_to": "none", "tool": "none",
                    "reply": "下午好，周岚。", "awaiting": "none", "confidence": 0.99,
                    "reason_codes": ["friendly_reply"],
                    "memory_evidence": [], "memory_updates": [], "tool_policy": "none",
                    "payment_policy": {
                        "quote_required": False, "explicit_confirmation_required": False,
                    },
                    "references": {
                        "production_id": "", "category_id": "", "topic_id": "",
                    },
                }, ensure_ascii=False))],
                usage=Usage(requests=1, input_tokens=20, output_tokens=5, total_tokens=25),
                response_id="talking_direct_1",
            )

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    def test_runner_uses_specialist_as_tool_and_five_real_read_executors(self):
        model = self.TalkingHeadModel()
        project = {"id": "project_talking_head"}
        run = agent_runtime.start(
            project, "run_talking_head_read_1", Policy(),
            "为什么口播失败、现在该怎么办", project_id=project["id"],
        )
        executed = []

        def execute(action, payload):
            executed.append((action, copy.deepcopy(payload)))
            if action == "ip12-project":
                return {"active_production": {
                    "status": "failed", "refund_status": "refunded",
                    "error_code": "provider_failed",
                    "next_action": "explain_failure_without_retry",
                }}
            if action == "video-avatars":
                return {"items": [{"id": 6, "status": "ready"}]}
            if action == "voices":
                return {"items": [{"voice_key": "my_voice", "status": "ready"}]}
            if action == "audio-slots":
                return {"items": [{"slot_id": "slot_1", "status": "ready"}]}
            return {"items": []}

        result = cognitive_engine.agents_sdk_talking_head_run(
            execute_action=execute,
            capabilities=TALKING_HEAD_CAPABILITIES,
            user_message="为什么口播失败、现在该怎么办",
            runtime_facts={
                "production_status": "failed", "refund_status": "refunded",
                "error_code": "provider_failed",
                "next_actions": ["explain_failure_without_retry"],
            },
            run=run, model=model, request_id="talking-head-read-1",
        )

        self.assertIn("失败并完成退款", result["final_text"])
        self.assertEqual(model.master_rounds, 2)
        self.assertEqual(model.specialist_rounds, 2)
        self.assertEqual([name for name, _payload in executed], list(TALKING_HEAD_ACTIONS))
        self.assertEqual(dict(executed)["ip12-project"], {"project_id": project["id"]})
        self.assertEqual(set(call["tool"] for call in run["tool_calls"].values()), set(TALKING_HEAD_ACTIONS))
        self.assertTrue(all(call["status"] == "completed" for call in run["tool_calls"].values()))
        self.assertFalse(any(call["billing"] != "free" for call in run["tool_calls"].values()))

    def test_adapter_rejects_non_read_capability_before_runner(self):
        capabilities = copy.deepcopy(TALKING_HEAD_CAPABILITIES)
        capabilities[0]["billing"] = "paid"
        run = agent_runtime.start(
            {"id": "unsafe"}, "run_unsafe", Policy(), "查状态", project_id="unsafe",
        )
        with self.assertRaisesRegex(RuntimeError, "capability_not_safe"):
            cognitive_engine.agents_sdk_talking_head_run(
                execute_action=lambda *_args: {}, capabilities=capabilities,
                user_message="查状态", runtime_facts={}, run=run,
                model=self.TalkingHeadModel(),
            )

    def test_non_talking_message_stays_with_master_without_specialist(self):
        model = self.DirectAnswerModel()
        run = agent_runtime.start(
            {"id": "direct"}, "run_direct", Policy(), "下午好",
            project_id="direct",
        )
        result = cognitive_engine.agents_sdk_talking_head_run(
            execute_action=lambda *_args: (_ for _ in ()).throw(
                AssertionError("non-talking message must not call HQ tools")
            ),
            capabilities=TALKING_HEAD_CAPABILITIES,
            user_message="下午好", runtime_facts={"production_status": "failed"},
            run=run, model=model,
        )
        self.assertEqual(result["final_text"], "下午好，周岚。")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(model.calls, 1)

    def test_original_voice_to_talking_head_questions_use_ready_quoted_state(self):
        messages = (
            "我可以用这段音频制作我的数字人视频吗？",
            "对呀，就是我刚刚克隆成功的这个个人音色，然后我现在需要制作数字人口播视频，我应该如何做呢？我需要向你提供些什么？",
        )
        expected_reply_parts = (
            "个人音色", "已有形象", "已确认文案", "已有报价等待确认",
            "不需要重复上传", "确认当前报价", "修改文案、形象或声音", "不会提交或扣点",
        )
        for index, message in enumerate(messages, 1):
            with self.subTest(message=message):
                model = self.TalkingHeadModel(ready=True)
                run = agent_runtime.start(
                    {"id": "feedback_%s" % index}, "run_feedback_%s" % index,
                    Policy(), message, project_id="feedback_%s" % index,
                )
                executed = []

                def execute(action, payload):
                    executed.append(action)
                    if action == "ip12-project":
                        return {"active_production": {
                            "status": "quoted", "quote_present": True,
                            "selected_fields": ["avatar_id", "voice", "text"],
                            "next_action": "wait_for_explicit_confirmation",
                        }}
                    if action == "video-avatars":
                        return {"items": [{"id": 6, "status": "ready"}]}
                    if action == "voices":
                        return {"items": [{"voice_key": "my_voice", "status": "ready"}]}
                    if action == "audio-slots":
                        return {"items": [{"slot_id": "slot_1", "status": "ready"}]}
                    return {"items": []}

                result = cognitive_engine.agents_sdk_talking_head_run(
                    execute_action=execute,
                    capabilities=TALKING_HEAD_CAPABILITIES,
                    user_message=message,
                    runtime_facts={
                        "production_status": "quoted", "voice_clone_status": "complete",
                        "script_ready": True, "avatar_ready": True, "voice_ready": True,
                        "quote_present": True,
                        "next_actions": ["wait_for_explicit_confirmation"],
                    },
                    run=run, model=model,
                )
                self.assertEqual(executed, list(TALKING_HEAD_ACTIONS))
                for part in expected_reply_parts:
                    self.assertIn(part, result["final_text"])


class AgentsSDKAccountServerWiringTests(unittest.TestCase):
    @staticmethod
    def _flask_python():
        candidates = [
            os.environ.get("HQ_SERVER_TEST_PYTHON"),
            "/usr/bin/python3",
            "/Library/Developer/CommandLineTools/usr/bin/python3",
            sys.executable,
        ]
        for candidate in dict.fromkeys(item for item in candidates if item):
            if not Path(candidate).is_file():
                continue
            probe = subprocess.run(
                [candidate, "-c", "import flask"],
                capture_output=True, text=True,
            )
            if probe.returncode == 0:
                return candidate
        raise unittest.SkipTest("no Python interpreter with Flask is available")

    def test_canary_wires_sdk_executor_to_bridge_without_real_provider(self):
        script = r'''
import copy
import security
import server

security._validate_token = lambda _token: {
    "account_id": "acct-1", "username": "tester", "role": "member",
}
security.RATE_REQUESTS = 100
client = server.app.test_client()
headers = {"Authorization": "Bearer test"}
health = client.get("/healthz").get_json()
assert health["agents_sdk_account_requested"] is False, health
assert health["agents_sdk_account_enabled"] is False, health
cid = client.post("/api/conversations", json={"title": "SDK account"}, headers=headers).get_json()["id"]
server.AGENTS_SDK_ACCOUNT_REQUESTED = True
server.AGENTS_SDK_ACCOUNT_PROJECT_ID = cid
server.AGENTS_SDK_ACCOUNT_ENABLED = True
server._bridge_catalog = lambda _account: {"actions": [copy.deepcopy({account_capability})]}
bridge_calls = []
server._bridge_action = lambda account, action, payload, **kwargs: (
    bridge_calls.append((account, action, payload)),
    {"user": {"points": 321}, "scopes": ["profile:read"]},
)[1]
server._master_runtime_reply = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError("fixed runtime reply must be bypassed")
)

def fake_sdk(**kwargs):
    assert kwargs["account_capability"]["action"] == "account"
    result = kwargs["execute_action"]("account", {})
    assert result == {"points": 321}
    run = kwargs["run"]
    server.agent_runtime.record_tool(
        run, "account", phase="started", input_value={},
        call_id="call_sdk_1", request_id=kwargs["request_id"],
    )
    server.agent_runtime.record_tool(
        run, "account", phase="completed", output=result,
        call_id="call_sdk_1", request_id=kwargs["request_id"],
    )
    return {"final_text": "你当前还有 321 点。", "tool_called": True, "model_rounds": 2}

server.cognitive_engine.agents_sdk_account_run = fake_sdk
convo = server.load_conversation(cid)
revision = convo["coach_state"]["revision"]
response = client.post("/api/chat-complete", json={
    "conversation_id": cid,
    "message": "我的黄雀账号还有多少点？",
    "expected_revision": revision,
    "request_id": "sdk-account-1",
}, headers=headers)
assert response.status_code == 200, response.get_data(as_text=True)
body = response.get_json()
assert body["assistant"] == "你当前还有 321 点。", body
assert bridge_calls == [("acct-1", "account", {})], bridge_calls
saved = server.load_conversation(cid)
runs = list(saved["agent_runs"].values())
assert len(runs) == 1, runs
call = runs[0]["tool_calls"]["call_sdk_1"]
assert call["status"] == "completed" and call["observation"] == {"points": 321}, call
assert not saved.get("productions") and not saved.get("artifacts")
'''.replace("{account_capability}", repr(ACCOUNT_CAPABILITY))
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=root, HERMES_DATA_DIR=root,
                HERMES_MASTER_AGENT_MODE="live", HERMES_SEMANTIC_ROUTER_MODE="off",
            )
            result = subprocess.run(
                [self._flask_python(), "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_talking_head_canary_bypasses_prefetch_and_fixed_reply(self):
        script = r'''
import copy
import security
import server

security._validate_token = lambda _token: {
    "account_id": "acct-1", "username": "tester", "role": "member",
}
security.RATE_REQUESTS = 100
client = server.app.test_client()
headers = {"Authorization": "Bearer test"}
health = client.get("/healthz").get_json()
assert health["agents_sdk_talking_head_requested"] is False, health
assert health["agents_sdk_talking_head_enabled"] is False, health
cid = client.post("/api/conversations", json={"title": "SDK talking"}, headers=headers).get_json()["id"]
convo = server.load_conversation(cid)
record = {
    "id": "prod_failed", "action": "digital-ip-text-generate",
    "status": "failed", "last_error_code": "provider_failed",
    "refund_status": "refunded", "job_id": 77,
    "specialist_agent": {
        "delegation_id": "delegate_failed",
        "agent_id": "talking_head_video_agent",
        "stage": "failed", "status": "failed",
        "next_action": "explain_failure_without_retry",
    },
}
convo["productions"] = {record["id"]: copy.deepcopy(record)}
convo["active_production_id"] = record["id"]
server.save_conversation(cid, convo)
server.AGENTS_SDK_TALKING_HEAD_REQUESTED = True
server.AGENTS_SDK_TALKING_HEAD_PROJECT_ID = cid
server.AGENTS_SDK_TALKING_HEAD_ENABLED = True
server._bridge_catalog = lambda _account: {"actions": copy.deepcopy({talking_capabilities})}
bridge_calls = []

def bridge(_account, action, payload, **_kwargs):
    bridge_calls.append((action, copy.deepcopy(payload)))
    if action == "ip12-project":
        return {
            "id": cid, "active_production_id": record["id"],
            "productions": {record["id"]: copy.deepcopy(record)},
            "messages": [{"role": "user", "content": "private history"}],
        }
    return {"items": []}

server._bridge_action = bridge
server._run_talking_head_specialist = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError("server prefetch must be bypassed")
)
server._master_runtime_reply = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    AssertionError("fixed runtime reply must be bypassed")
)

def fake_sdk(**kwargs):
    run = kwargs["run"]
    for action in {talking_actions}:
        payload = {
            "ip12-project": {"project_id": cid},
            "video-avatars": {"limit": 120}, "voices": {}, "audio-slots": {},
            "assets": {"kind": "audio", "limit": 120, "offset": 0},
        }[action]
        result = kwargs["execute_action"](action, payload)
        if action == "ip12-project":
            assert "messages" not in result, result
            assert result["active_production"]["status"] == "failed", result
        server.agent_runtime.record_tool(
            run, action, phase="started", input_value=payload,
            call_id="call_" + action, request_id=kwargs["request_id"],
        )
        server.agent_runtime.record_tool(
            run, action, phase="completed", output={"status": "ok"},
            call_id="call_" + action, request_id=kwargs["request_id"],
        )
    return {
        "final_text": "这次口播已失败并退款；我建议先检查失败原因，再决定是否重新报价，不会自动重试。",
        "tool_calls": list({talking_actions}), "model_rounds": 2,
    }

server.cognitive_engine.agents_sdk_talking_head_run = fake_sdk
revision = server.load_conversation(cid)["coach_state"]["revision"]
response = client.post("/api/chat-complete", json={
    "conversation_id": cid,
    "message": "为什么口播失败、现在该怎么办",
    "expected_revision": revision,
    "request_id": "sdk-talking-read-1",
}, headers=headers)
assert response.status_code == 200, response.get_data(as_text=True)
body = response.get_json()
assert "失败并退款" in body["assistant"], body
assert [name for name, _payload in bridge_calls] == list({talking_actions}), bridge_calls
saved = server.load_conversation(cid)
assert len(saved["productions"]) == 1 and saved["productions"][record["id"]]["status"] == "failed"
assert not saved.get("artifacts")
runs = [run for run in saved["agent_runs"].values() if run["run_id"].startswith("run_talking_read_")]
assert len(runs) == 1 and runs[0]["status"] == "completed", runs
assert set(call["tool"] for call in runs[0]["tool_calls"].values()) == set({talking_actions})
'''.replace("{talking_capabilities}", repr(TALKING_HEAD_CAPABILITIES)).replace(
    "{talking_actions}", repr(TALKING_HEAD_ACTIONS)
)
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=root, HERMES_DATA_DIR=root,
                HERMES_MASTER_AGENT_MODE="live", HERMES_SEMANTIC_ROUTER_MODE="off",
            )
            result = subprocess.run(
                [self._flask_python(), "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_original_feedback_uses_runtime_delegation_and_never_asks_object_again(self):
        script = r'''
import copy
import security
import server

security._validate_token = lambda _token: {
    "account_id": "acct-1", "username": "tester", "role": "member",
}
security.RATE_REQUESTS = 100
client = server.app.test_client()
headers = {"Authorization": "Bearer test"}
cid = client.post("/api/conversations", json={"title": "林安反馈回归"}, headers=headers).get_json()["id"]
record = {
    "id": "prod_quoted", "action": "digital-ip-text-generate",
    "capability_family": "video", "status": "quoted",
    "source_text": "已确认口播文案",
    "options": {"avatar_id": 6, "voice": "my_voice", "text": "已确认口播文案"},
    "quote": {"cost": 90, "expires_at": 9999999999},
    "specialist_agent": {
        "delegation_id": "delegate_quoted",
        "agent_id": "talking_head_video_agent",
        "stage": "awaiting_confirmation", "status": "waiting_user",
        "next_action": "等待用户确认报价",
    },
}
convo = server.load_conversation(cid)
convo["active_production_id"] = None
convo["voice_clone_ui"] = {"status": "complete", "voice_name": "我的个人音色"}
convo["productions"] = {
    "prod_stale_audio": {
        "id": "prod_stale_audio", "action": "audio-generate", "status": "stale",
    },
    record["id"]: copy.deepcopy(record),
}
convo["agent_runtime"] = {
    "orchestrator_id": "ip12_master_agent",
    "active_delegation_id": "delegate_quoted",
    "last_delegation_id": "delegate_quoted",
    "specialist_agent_id": "talking_head_video_agent",
    "phase": "awaiting_confirmation",
    "next_action": "等待用户确认报价",
}
server.save_conversation(cid, convo)
server.AGENTS_SDK_TALKING_HEAD_REQUESTED = True
server.AGENTS_SDK_TALKING_HEAD_PROJECT_ID = cid
server.AGENTS_SDK_TALKING_HEAD_ENABLED = True
server._bridge_catalog = lambda _account: {"actions": copy.deepcopy({talking_capabilities})}
bridge_calls = []

def bridge(_account, action, payload, **_kwargs):
    bridge_calls.append(action)
    if action == "ip12-project":
        return copy.deepcopy(server.load_conversation(cid))
    if action == "video-avatars":
        return {"items": [{"id": 6, "status": "ready"}]}
    if action == "voices":
        return {"items": [{"voice_key": "my_voice", "status": "ready"}]}
    if action == "audio-slots":
        return {"items": [{"slot_id": "slot_1", "status": "ready"}]}
    return {"items": []}

server._bridge_action = bridge
for name in (
    "_run_talking_head_specialist", "_master_runtime_reply",
    "_process_production_intent_turn", "_process_semantic_reply",
):
    setattr(server, name, lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
        AssertionError(_name + " must be bypassed")
    ))

reply = (
    "你刚克隆成功的个人音色、已有形象和已确认文案都已经选好，当前也已有报价等待确认，"
    "不需要重复上传图片或音频。你可以直接确认当前报价，或者先修改文案、形象或声音；"
    "未经确认不会提交或扣点。"
)

def fake_sdk(**kwargs):
    run = kwargs["run"]
    for action in {talking_actions}:
        payload = {
            "ip12-project": {"project_id": cid},
            "video-avatars": {"limit": 120}, "voices": {}, "audio-slots": {},
            "assets": {"kind": "audio", "limit": 120, "offset": 0},
        }[action]
        result = kwargs["execute_action"](action, payload)
        if action == "ip12-project":
            active = result["active_production"]
            assert active["status"] == "quoted" and active["quote_present"], result
            assert set(active["selected_fields"]) >= {"avatar_id", "voice", "text"}, result
        server.agent_runtime.record_tool(
            run, action, phase="started", input_value=payload,
            call_id="call_" + action, request_id=kwargs["request_id"],
        )
        server.agent_runtime.record_tool(
            run, action, phase="completed", output={"status": "ok"},
            call_id="call_" + action, request_id=kwargs["request_id"],
        )
    return {"final_text": reply, "tool_calls": list({talking_actions}), "model_rounds": 2}

server.cognitive_engine.agents_sdk_talking_head_run = fake_sdk
messages = (
    "我可以用这段音频制作我的数字人视频吗？",
    "对呀，就是我刚刚克隆成功的这个个人音色，然后我现在需要制作数字人口播视频，我应该如何做呢？我需要向你提供些什么？",
)
before_productions = copy.deepcopy(server.load_conversation(cid)["productions"])
for index, message in enumerate(messages, 1):
    revision = server.load_conversation(cid)["coach_state"]["revision"]
    response = client.post("/api/chat-complete", json={
        "conversation_id": cid, "message": message,
        "expected_revision": revision, "request_id": "feedback-original-%s" % index,
    }, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    assistant = response.get_json()["assistant"]
    assert assistant == reply, assistant
    assert "不能安全确定" not in assistant and "哪个对象" not in assistant

saved = server.load_conversation(cid)
assert saved["productions"] == before_productions
assert not saved.get("artifacts")
assert bridge_calls == list({talking_actions}) * 2, bridge_calls
runs = [x for x in saved["agent_runs"].values() if x["run_id"].startswith("run_talking_read_")]
assert len(runs) == 2 and all(x["status"] == "completed" for x in runs), runs

new_cid = client.post("/api/conversations", json={"title": "岳磊新客访谈"}, headers=headers).get_json()["id"]
server.AGENTS_SDK_TALKING_HEAD_PROJECT_ID = new_cid
new_convo = server.load_conversation(new_cid)
assert server._agents_sdk_talking_head_turn_allowed(new_cid, "model_turn", new_convo) is False
'''.replace("{talking_capabilities}", repr(TALKING_HEAD_CAPABILITIES)).replace(
    "{talking_actions}", repr(TALKING_HEAD_ACTIONS)
)
        with tempfile.TemporaryDirectory() as root:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=root, HERMES_DATA_DIR=root,
                HERMES_MASTER_AGENT_MODE="live", HERMES_SEMANTIC_ROUTER_MODE="off",
            )
            result = subprocess.run(
                [self._flask_python(), "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
