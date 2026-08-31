import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


CORE_UPDATES = [
    {"field": "previous_work_experience", "value": "做过修车和服务员", "kind": "user_fact", "evidence_quote": "做过修车和服务员"},
    {"field": "current_identity", "value": "学习和搭建 AI Agent", "kind": "user_fact", "evidence_quote": "学习和搭建 AI Agent"},
    {"field": "core_skill_1", "value": "AI Agent 落地", "kind": "user_fact", "evidence_quote": "AI Agent 落地"},
    {"field": "core_skill_2", "value": "拆解问题", "kind": "user_fact", "evidence_quote": "拆解问题"},
    {"field": "long_term_interest", "value": "研究 AI Agent", "kind": "user_fact", "evidence_quote": "研究 AI Agent"},
    {"field": "target_audience", "value": "不懂技术的普通人", "kind": "user_fact", "evidence_quote": "不懂技术的普通人"},
]
EVIDENCE = "做过修车和服务员；学习和搭建 AI Agent；AI Agent 落地；拆解问题；研究 AI Agent；不懂技术的普通人；我希望帮助他们解决 AI 落地问题。"


def decision(kind, reply):
    return {
        "decision": kind,
        "checkpoint": 0,
        "reply": reply,
        "draft": "",
        "self_review": "",
        "profile_updates": CORE_UPDATES if kind == "ask_follow_up" else [],
        "confidence": 0.9,
    }


class IntakeGuidanceRegressionTests(unittest.TestCase):
    def test_core_profile_still_asks_uncovered_questionnaire_items(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(), decision("ask_follow_up", "你现在主要在哪个城市？"), EVIDENCE
        )
        self.assertIn("city", state["intake"]["asked_follow_ups"])

    def test_ready_profile_cannot_stop_after_answering_a_question(self):
        state = harness.initial_state()
        state["intake"]["profile_updates"] = CORE_UPDATES
        with self.assertRaisesRegex(harness.HarnessError, "必须继续"):
            harness.apply_intake_decision(
                state, decision("answer_only", "城市可以跳过，我们不再追问。"), EVIDENCE
            )

    def test_empty_intake_answers_then_continues_with_one_question(self):
        state, normalized, reply = harness.apply_intake_decision(
            harness.initial_state(),
            decision("ask_follow_up", "手机号可以跳过。先请告诉我怎么称呼你？"),
            "手机号必须填吗？",
        )
        self.assertEqual(normalized["decision"], "ask_follow_up")
        self.assertEqual(state["intake"]["asked_follow_ups"], ["preferred_name"])
        self.assertIn("可以跳过", reply)

    def test_prompt_treats_core_as_required_and_marks_the_rest_optional(self):
        prompt = harness.intake_system_prompt(harness.initial_state())
        self.assertIn("IP 孵化教练", prompt)
        self.assertIn("敏感可选项", prompt)
        self.assertIn("我先聊聊", prompt)
        self.assertNotIn("必须覆盖《黄雀IP人设定位采集表》的全部项目", prompt)
        self.assertNotIn("35–70", prompt)
        self.assertNotIn("35-70", prompt)
        self.assertNotIn("本轮只问一个尚未覆盖的项目", prompt)


if __name__ == "__main__":
    unittest.main()
