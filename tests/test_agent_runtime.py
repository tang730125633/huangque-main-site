import copy
import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import agent_runtime


class AgentRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.calls = {name: 0 for name in agent_runtime.TOOL_CONTRACTS}
        self.task_states = ["running", "done"]
        self.tools = agent_runtime.ToolRegistry()
        self.tools.register("project.read", self._project)
        self.tools.register("capability.read", self._capability)
        self.tools.register("assets.read", self._assets)
        self.tools.register("production.quote", self._quote)
        self.tools.register("production.submit", self._submit)
        self.tools.register("task.read", self._task)
        self.tools.register("artifact.verify", self._verify)
        self.tools.register("project.writeback", self._writeback)
        self.policy = agent_runtime.TalkingHeadPolicy()
        self.project = {"id": "project_1"}
        agent_runtime.start(
            self.project, "run_1", self.policy, "制作第一条口播视频",
            project_id="project_1", production_id="prod_1",
            inputs={"input_digest": "sha256:input"},
            selected_source={"topic_id": "topic_1", "script_version": 1},
        )

    def _project(self, _payload):
        self.calls["project.read"] += 1
        return {"source": {"topic_id": "topic_1", "script_version": 1},
                "script_title": "第一篇", "script": "已经确认的口播文案"}

    def _capability(self, payload):
        self.calls["capability.read"] += 1
        self.assertEqual(payload["action"], "digital-ip-text-generate")
        return {"available": True, "gate_status": "unlocked"}

    def _assets(self, _payload):
        self.calls["assets.read"] += 1
        return {"avatar_ready": True, "voice_ready": True, "missing": [],
                "avatar_id": 6, "voice": "vip_voice"}

    def _quote(self, payload):
        self.calls["production.quote"] += 1
        self.assertEqual(payload["production_id"], "prod_1")
        return {"cost": 90, "points": 100, "expires_at": 9999999999,
                "quote_token": "private-quote"}

    def _submit(self, payload):
        self.calls["production.submit"] += 1
        self.assertEqual(payload["production_id"], "prod_1")
        return {"job_id": 8801, "status": "queued"}

    def _task(self, payload):
        self.calls["task.read"] += 1
        self.assertEqual(payload["production_id"], "prod_1")
        status = self.task_states.pop(0)
        return {"job_id": 8801, "status": status, "refund_status": "none",
                **({"asset_refs": [{"kind": "video", "url": "https://media.example/first.mp4"}]}
                   if status == "done" else {})}

    def _verify(self, payload):
        self.calls["artifact.verify"] += 1
        self.assertEqual(payload["production_id"], "prod_1")
        return {"decision": "pass", "issues": [],
                "media": {"duration": 20, "codec": "h264", "width": 1080, "height": 1920}}

    def _writeback(self, payload):
        self.calls["project.writeback"] += 1
        self.assertEqual(payload["production_id"], "prod_1")
        return {"artifact_id": "artifact_1", "version": 1}

    def test_full_state_machine_confirmation_async_verify_and_writeback(self):
        run = agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        self.assertEqual(run["status"], "awaiting_confirmation")
        self.assertEqual(run["awaiting"], "confirmation")
        self.assertEqual(self.calls["production.submit"], 0)

        run = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools,
            {"type": "confirm", "approval": "current_quote"},
        )
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["awaiting"], "external")
        self.assertEqual(self.calls["production.submit"], 1)
        self.assertEqual(self.calls["task.read"], 1)

        run = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools, {"type": "tick"}
        )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["result"]["writeback"]["artifact_id"], "artifact_1")
        self.assertEqual(self.calls["production.submit"], 1)
        self.assertEqual(self.calls["artifact.verify"], 1)
        self.assertEqual(self.calls["project.writeback"], 1)

        replay = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools,
            {"type": "confirm", "approval": "current_quote"},
        )
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(self.calls["production.submit"], 1)

    def test_missing_material_is_durable_and_quote_is_not_called(self):
        self.tools._tools["assets.read"]["handler"] = lambda _payload: {
            "avatar_ready": False, "voice_ready": False,
            "missing": ["avatar_id", "voice"], "avatar_id": None, "voice": None,
        }
        run = agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        self.assertEqual(run["status"], "needs_input")
        self.assertEqual(run["awaiting"], "material")
        self.assertIn("avatar_id", run["next_action"])
        self.assertEqual(self.calls["production.quote"], 0)
        restored = copy.deepcopy(self.project)
        self.assertEqual(restored["agent_runs"]["run_1"]["status"], "needs_input")

    def test_tool_contracts_confirmation_and_private_redaction(self):
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "confirmation required"):
            self.tools.execute("production.submit", {
                "production_id": "prod_1", "input_digest": "sha256:input",
            })
        run = agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        public = agent_runtime.public_run(run)
        rendered = str(public)
        self.assertNotIn("private-quote", rendered)
        self.assertNotIn("quote_token", rendered)
        self.assertNotIn("job_id", rendered)
        self.assertNotIn("inputs", public)
        self.assertEqual(set(agent_runtime.TOOL_CONTRACTS), {
            "account",
            "ip12-project", "video-avatars", "voices", "audio-slots", "assets",
            "project.read", "capability.read", "assets.read", "production.quote",
            "production.submit", "task.read", "artifact.verify", "project.writeback",
        })

    def test_illegal_transition_and_verification_failure_fail_closed(self):
        run = self.project["agent_runs"]["run_1"]
        with self.assertRaisesRegex(agent_runtime.AgentRuntimeError, "invalid agent transition"):
            agent_runtime.transition(run, "running")
        self.tools._tools["artifact.verify"]["handler"] = lambda _payload: {
            "decision": "fail", "issues": [{"code": "video_unplayable"}], "media": {},
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
        self.assertEqual(run["error"]["code"], "artifact_verification_failed")
        self.assertEqual(self.calls["project.writeback"], 0)

    def test_event_sequence_is_resumable_and_private_safe(self):
        run = self.project["agent_runs"]["run_1"]
        agent_runtime.append_event(run, "delta", {"content": "处理中"}, request_id="req_1")
        sequences = [event["sequence"] for event in run["events"]]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        for event in run["events"]:
            self.assertEqual(event["run_id"], "run_1")
            self.assertNotIn("quote_token", str(agent_runtime.public_event(event)))

    def test_running_and_verifying_resume_after_process_state_is_discarded(self):
        agent_runtime.resume(self.project, "run_1", self.policy, self.tools)
        running = agent_runtime.resume(
            self.project, "run_1", self.policy, self.tools,
            {"type": "confirm", "approval": "current_quote"},
        )
        self.assertEqual(running["status"], "running")

        restored_project = copy.deepcopy(self.project)
        restored = agent_runtime.resume(
            restored_project, "run_1", agent_runtime.TalkingHeadPolicy(),
            self.tools, {"type": "tick"},
        )
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["result"]["writeback"]["artifact_id"], "artifact_1")
        self.assertEqual(self.calls["production.submit"], 1)


if __name__ == "__main__":
    unittest.main()
