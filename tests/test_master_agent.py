import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import master_agent


class MasterAgentShadowContractTests(unittest.TestCase):
    def test_incomplete_project_continues_one_ip12_step(self):
        decision = master_agent.decide(
            {}, {"completed_modules": [1, 2]}, "继续",
            {"legacy_route": "model_turn"},
        )
        self.assertEqual(decision["decision"], "continue_ip12")
        self.assertEqual(decision["execution_route"], "model_turn")

    def test_talking_head_intent_delegates_without_executing(self):
        decision = master_agent.decide(
            {}, {"completed_modules": [1, 2, 3, 4, 5, 6]}, "制作口播视频",
            {"legacy_route": "production_turn", "production_action": "digital-ip-text-generate"},
        )
        self.assertEqual(decision["decision"], "delegate")
        self.assertEqual(decision["delegate_to"], "talking_head_video_agent")
        self.assertEqual(decision["mode"], "shadow")

    def test_generic_continuation_resumes_active_quote(self):
        project = {"productions": {"prod_1": {
            "id": "prod_1", "status": "quoted",
            "specialist_agent": {
                "delegation_id": "delegate_1", "agent_id": "talking_head_video_agent",
            },
        }}}
        decision = master_agent.decide(
            project, {"completed_modules": [1, 2, 3, 4, 5, 6]}, "然后呢？",
            {"legacy_route": "model_turn"},
        )
        self.assertEqual(decision["decision"], "await_confirmation")
        self.assertEqual(decision["execution_route"], "master_resume")
        self.assertEqual(decision["awaiting"], "quote_confirmation")

    def test_shadow_record_is_redacted_and_tracks_mismatch(self):
        project = {}
        decision = master_agent._decision(
            "resume_task", "master_resume", "查询原任务",
            reasons=["active_async_task"],
        )
        event = master_agent.record_shadow(
            project, decision, "model_turn", "request-1", 9, "2026-08-23 18:00:00"
        )
        self.assertFalse(event["aligned"])
        self.assertNotIn("message", event)
        self.assertEqual(project["master_agent_shadow"]["metrics"], {
            "total": 1, "aligned": 0, "mismatched": 1,
        })


if __name__ == "__main__":
    unittest.main()
