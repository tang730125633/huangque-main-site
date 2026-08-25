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
    def test_ready_profile_cannot_ask_optional_demographics(self):
        with self.assertRaisesRegex(harness.HarnessError, "直接生成完整核对稿"):
            harness.apply_intake_decision(
                harness.initial_state(), decision("ask_follow_up", "你现在主要在哪个城市？"), EVIDENCE
            )

    def test_ready_profile_cannot_stop_after_answering_a_question(self):
        state = harness.initial_state()
        state["intake"]["profile_updates"] = CORE_UPDATES
        with self.assertRaisesRegex(harness.HarnessError, "直接生成完整核对稿"):
            harness.apply_intake_decision(
                state, decision("answer_only", "城市可以跳过，我们不再追问。"), EVIDENCE
            )

    def test_empty_intake_can_still_answer_without_advancing(self):
        state, normalized, reply = harness.apply_intake_decision(
            harness.initial_state(), decision("answer_only", "手机号可以不填。"), "手机号必须填吗？"
        )
        self.assertEqual(normalized["decision"], "answer_only")
        self.assertEqual(state["intake"]["status"], "collecting")
        self.assertIn("可以不填", reply)

    def test_prompt_marks_demographics_optional_and_resumes_after_questions(self):
        prompt = harness.intake_system_prompt(harness.initial_state())
        self.assertIn("城市、收入、性别和手机号都只是可选背景", prompt)
        self.assertIn("同一条回复必须 decision=propose_checkpoint", prompt)

    def test_question_shaped_answer_fills_current_help_goal_and_advances(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision("ask_follow_up", "你最希望帮他们解决的一个核心问题是什么？"),
            "；".join(item["evidence_quote"] for item in CORE_UPDATES),
        )
        self.assertEqual(state["intake"]["asked_field"], "help_goal")
        message = "如何使用agent去独自完成一个项目"
        raw = harness.compile_asked_intake_answer(state, message)
        self.assertEqual(raw["decision"], "propose_checkpoint")
        evidence = EVIDENCE + "；" + message
        next_state, decision_result, reply = harness.apply_intake_decision(
            state, raw, evidence,
        )
        self.assertEqual(next_state["intake"]["status"], "awaiting_confirmation")
        help_goal = next(
            item for item in decision_result["profile_updates"]
            if item["field"] == "help_goal"
        )
        self.assertEqual(help_goal["value"], message)
        self.assertEqual(help_goal["evidence_quote"], message)
        self.assertIn("基础定位核对稿", reply)
        self.assertIn("请确认资料", reply)

    def test_independent_question_without_asked_field_is_not_consumed_as_intake(self):
        state = harness.initial_state()
        self.assertIsNone(harness.compile_asked_intake_answer(state, "Agent 是什么？"))


if __name__ == "__main__":
    unittest.main()
