import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import agent_runtime


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.calls = {name: 0 for name in ("project", "assets", "quote", "submit", "task", "quality")}
        self.task_states = ["running", "done"]
        self.tools = agent_runtime.ToolRegistry()
        self.tools.register("project.read", self._project)
        self.tools.register("assets.read", self._assets)
        self.tools.register("production.quote", self._quote, private_fields=("quote_token",))
        self.tools.register("production.submit", self._submit, requires_confirmation=True)
        self.tools.register("task.read", self._task)
        self.tools.register("quality.verify", self._quality)
        self.policy = agent_runtime.TalkingHeadPolicy()
        self.project = {}
        agent_runtime.start(self.project, "run_1", self.policy, "制作第一条口播视频")

    def _project(self, _payload):
        self.calls["project"] += 1
        return {"script": "已经确认的口播文案"}

    def _assets(self, _payload):
        self.calls["assets"] += 1
        return {"avatar_id": 6, "voice": "vip_voice"}

    def _quote(self, _payload):
        self.calls["quote"] += 1
        return {"cost": 90, "quote_token": "private-quote"}

    def _submit(self, payload):
        self.calls["submit"] += 1
        self.assertEqual(payload["quote_token"], "private-quote")
        self.assertEqual(payload["idempotency_key"], "agent-run_1")
        return {"job_id": 8801, "status": "queued"}

    def _task(self, payload):
        self.calls["task"] += 1
        self.assertEqual(payload["job_id"], 8801)
        status = self.task_states.pop(0)
        return {
            "job_id": 8801,
            "status": status,
            **({"asset_refs": [{"kind": "video", "url": "https://media.example/first.mp4"}]} if status == "done" else {}),
        }

    def _quality(self, payload):
        self.calls["quality"] += 1
        self.assertEqual(payload["asset_refs"][0]["kind"], "video")
        return {"decision": "pass", "issues": []}

    def test_plan_tool_observe_confirmation_async_and_quality_loop(self):
        run = agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(run["awaiting"], "confirmation")
        self.assertEqual(self.calls["submit"], 0)
        self.assertNotIn("private-quote", str(agent_runtime.public_run(run)))

        run = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools,
            {"type": "confirm", "approval": "current_quote"},
        )
        self.assertEqual(run["awaiting"], "external")
        self.assertEqual(self.calls["submit"], 1)
        self.assertEqual(self.calls["task"], 1)

        run = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools, {"type": "tick"}
        )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["result"]["job_id"], 8801)
        self.assertEqual(self.calls["submit"], 1)
        self.assertEqual(self.calls["quality"], 1)

        replayed = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools,
            {"type": "confirm", "approval": "current_quote"},
        )
        self.assertEqual(replayed["status"], "completed")
        self.assertEqual(self.calls["submit"], 1)

    def test_missing_material_waits_without_quoting(self):
        self.tools._tools["assets.read"]["handler"] = lambda _payload: {
            "avatar_id": None, "voice": None,
        }
        run = agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        self.assertEqual(run["awaiting"], "material")
        self.assertIn("avatar_id", run["next_action"])
        self.assertEqual(self.calls["quote"], 0)

    def test_confirmed_tool_cannot_run_without_runtime_approval(self):
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "confirmation required"):
            self.tools.execute("production.submit", {"quote_token": "x"})

    def test_quality_failure_stops_delivery(self):
        self.tools._tools["quality.verify"]["handler"] = lambda _payload: {
            "decision": "fail", "issues": [{"code": "video_unplayable"}],
        }
        agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools,
            {"type": "confirm", "approval": "current_quote"},
        )
        run = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools, {"type": "tick"}
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "quality_failed")


if __name__ == "__main__":
    unittest.main()
