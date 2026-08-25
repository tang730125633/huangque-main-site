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
    {"result": {"points": 321}},
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


if __name__ == "__main__":
    unittest.main()
