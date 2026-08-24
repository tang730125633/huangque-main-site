import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).resolve().parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import talking_head_agent as agent


def ready_state():
    return {
        "completed_modules": [1, 2, 3, 4, 5, 6],
        "foundation_report": {"status": "confirmed"},
        "ip_profile": {"preferences": {"tone": {"value": "邻家温和、去焦虑"}}},
    }


class TalkingHeadAgentContractTests(unittest.TestCase):
    def test_gate_requires_full_ip12_foundation_and_script(self):
        locked = agent.capability_gate(
            {"completed_modules": [1, 2, 3]}, {"script": ""}
        )
        self.assertEqual(locked["status"], "locked")
        self.assertIn("模块 6", locked["missing"])
        self.assertIn("确认模块 1–4 报告", locked["missing"])
        self.assertIn("确认一篇口播文案", locked["missing"])

    def test_plan_uses_confirmed_ip_style_without_choosing_paid_assets(self):
        plan = agent.plan(
            ready_state(), {"script": "这是一篇已经确认的口播文案。"},
            "digital-ip-text-generate",
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["recommended_options"], {
            "ratio": "9:16", "motion": "low", "subtitle": True,
            "subtitle_style": "white", "subtitle_position": "lower",
        })
        self.assertNotIn("avatar_id", plan["recommended_options"])
        self.assertNotIn("voice", plan["recommended_options"])
        self.assertIn("只在报价确认后提交一次", "".join(plan["quality_bar"]))

    def test_project_runtime_tracks_the_existing_production_state(self):
        production = {
            "id": "prod_abc", "status": "quoted",
            "specialist_agent": agent.new_delegation("prod_abc", agent.plan(
                ready_state(), {"script": "已确认口播"}, "digital-ip-text-generate"
            )),
        }
        project = {"productions": {"prod_abc": production}}
        self.assertTrue(agent.sync_project(project))
        self.assertEqual(production["specialist_agent"]["stage"], "awaiting_confirmation")
        self.assertEqual(project["agent_runtime"]["active_delegation_id"], "delegate_abc")
        production["status"] = "done"
        agent.sync_project(project)
        self.assertEqual(production["specialist_agent"]["stage"], "delivered")
        self.assertIsNone(project["agent_runtime"]["active_delegation_id"])


if __name__ == "__main__":
    unittest.main()
