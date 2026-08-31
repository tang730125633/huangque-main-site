import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


def decision(kind="ask_follow_up", *, reply="还想了解什么？", draft="", updates=None, checkpoint=0):
    return {
        "decision": kind,
        "checkpoint": checkpoint,
        "reply": reply,
        "draft": draft,
        "self_review": "已核对用户原话。" if kind == "propose_checkpoint" else "",
        "choices": [],
        "profile_updates": updates or [],
        "confidence": 0.9,
    }


def update(field, value, quote=None, kind="user_fact"):
    return {"field": field, "value": value, "kind": kind, "evidence_quote": quote or value}


CORE_UPDATES = [
    update("preferred_name", "阿青", "叫我阿青"),
    update("current_identity", "内容顾问", "目前做内容顾问"),
    update("core_skill_1", "内容整理", "内容整理"),
    update("core_skill_2", "方法拆解", "方法拆解"),
    update("target_audience", "社区店老板", "社区店老板"),
    update("help_goal", "帮他们解决内容问题", "帮他们解决内容问题"),
    update("primary_platform", "视频号", "主要做视频号", "user_preference"),
    update("niche", "实体店内容", "实体店内容"),
]
CORE_EVIDENCE = (
    "叫我阿青；目前做内容顾问；内容整理；方法拆解；社区店老板；"
    "帮他们解决内容问题；主要做视频号；实体店内容"
)


def completed_intake():
    state = harness.initial_state()
    state["intake"]["declined_fields"] = list(harness.INTAKE_COVERAGE_FIELDS)
    state, _, _ = harness.apply_intake_decision(
        state,
        decision("propose_checkpoint", reply="请核对。", draft="资料核对稿", checkpoint=1),
        "先这样",
    )
    action = next(item for item in harness.available_actions(state) if item["type"] == "confirm_intake")
    state, _ = harness.apply_action(state, action, state["revision"])
    return state


class ConversationalIntakeTests(unittest.TestCase):
    def test_wants_chat_start_matches_product_phrases(self):
        for phrase in ("我先聊聊", "先聊聊", "不想填表", "先进入定位", "跳过剩余", "其余跳过", "先开始定位", "先不用填"):
            with self.subTest(phrase=phrase):
                self.assertTrue(harness.wants_chat_start(phrase))
        self.assertFalse(harness.wants_chat_start("我叫阿青，目前做内容顾问"))

    def test_intake_core_gaps_only_include_unknown_core_fields(self):
        state = harness.initial_state()
        self.assertEqual(list(harness.INTAKE_CORE_FIELDS), [
            "preferred_name", "current_identity", "core_skill_1", "core_skill_2",
            "target_audience", "help_goal", "primary_platform", "niche",
        ])
        self.assertEqual(harness.intake_core_gaps(state), list(harness.INTAKE_CORE_FIELDS))
        self.assertIn("one_year_goal", harness.intake_coverage_gaps(state))
        self.assertNotIn("one_year_goal", harness.intake_core_gaps(state))
        self.assertEqual(harness.intake_core_gaps(state, CORE_UPDATES), [])

    def test_model_wording_is_preserved_when_follow_up_is_not_the_next_gap(self):
        custom = "听起来你已经在帮店老板做内容了。你现在的目标人群更偏向社区店老板，还是想学内容的新手？"
        state, normalized, reply = harness.apply_intake_decision(
            harness.initial_state(),
            decision(reply=custom),
            "我在帮店老板做内容",
        )
        self.assertEqual(normalized["reply"], custom)
        self.assertEqual(reply, custom)
        self.assertNotIn(harness.INTAKE_NATURAL_QUESTIONS["preferred_name"], reply)
        self.assertEqual(state["intake"]["asked_follow_ups"], ["target_audience"])
        self.assertEqual(state["intake"]["current_question_field"], "target_audience")

    def test_core_profile_can_confirm_intake_and_skips_remaining_fields(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision(
                "propose_checkpoint", reply="请核对核心资料。", draft="核心资料已齐",
                updates=CORE_UPDATES, checkpoint=1,
            ),
            CORE_EVIDENCE,
            current_message=CORE_EVIDENCE,
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(harness.intake_core_gaps(state), [])
        self.assertIn("one_year_goal", state["intake"]["declined_fields"])
        self.assertIn("city", state["intake"]["declined_fields"])
        action = next(item for item in harness.available_actions(state) if item["type"] == "confirm_intake")
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["intake"]["status"], "complete")
        self.assertIn("模块 1", event["assistant_prefix"])
        self.assertEqual(state["ip_profile"]["facts"]["preferred_name"]["value"], "阿青")

    def test_chat_start_can_confirm_intake_without_filling_the_form(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision(
                "propose_checkpoint",
                reply="好，我们先按现有信息进入定位。",
                draft="用户选择先聊聊，其余项本人选择跳过。",
                checkpoint=1,
            ),
            "我先聊聊",
            current_message="我先聊聊",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        for field in harness.INTAKE_CORE_FIELDS:
            self.assertIn(field, state["intake"]["declined_fields"])
        action = next(item for item in harness.available_actions(state) if item["type"] == "confirm_intake")
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["intake"]["status"], "complete")
        self.assertIn("模块 1", event["assistant_prefix"])

    def test_skip_remaining_declines_optional_fields_but_not_core(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision(reply="好，那我们先把你最拿手、真实做出过结果的能力说清楚。"),
            "跳过剩余",
            current_message="跳过剩余",
        )
        declined = set(state["intake"]["declined_fields"])
        self.assertIn("one_year_goal", declined)
        self.assertIn("city", declined)
        self.assertNotIn("core_skill_1", declined)
        self.assertNotIn("preferred_name", declined)
        self.assertTrue(harness.intake_core_gaps(state))

    def test_chat_start_does_not_error_when_core_gaps_remain(self):
        state, normalized, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision("answer_only", reply="可以，我们先随便聊聊你最近在做的事。"),
            "先聊聊",
            current_message="先聊聊",
        )
        self.assertEqual(normalized["decision"], "answer_only")
        self.assertTrue(harness.intake_core_gaps(state))

    def test_prompts_no_longer_cap_reply_at_35_to_70_chars(self):
        intake_prompt = harness.intake_system_prompt(harness.initial_state())
        module_prompt = harness.system_prompt(completed_intake())
        for prompt in (intake_prompt, module_prompt):
            self.assertNotIn("35–70", prompt)
            self.assertNotIn("35-70", prompt)
        self.assertNotIn("本轮只问一个尚未覆盖的项目", intake_prompt)
        self.assertIn("IP 孵化教练", intake_prompt)
        self.assertIn("我先聊聊", intake_prompt)
        self.assertIn("先回应用户刚刚说的话", module_prompt)

    def test_model_still_cannot_confirm_checkpoints(self):
        state = completed_intake()
        raw = decision(
            "propose_checkpoint",
            reply="✅ 模块 1 完成。接下来进入模块 2。",
            draft="关键词：真实、行动",
            checkpoint=1,
        )
        next_state, _, _ = harness.apply_model_decision(state, raw, "用户原话", pending_id="p1")
        self.assertEqual(next_state["current_module"], 1)
        self.assertEqual(next_state["completed_modules"], [])
        self.assertEqual(next_state["module_step"], 0)
        self.assertEqual(next_state["pending"]["status"], "awaiting_confirmation")

        action = next(item for item in harness.available_actions(next_state) if item["type"] == "confirm_checkpoint")
        confirmed, _ = harness.apply_action(next_state, action, next_state["revision"])
        self.assertEqual(confirmed["module_step"], 1)

    def test_choice_and_pdf_gates_are_unchanged(self):
        state = completed_intake()
        unexpected = decision(reply="先聊一下")
        unexpected["choices"] = [{
            "title": "方向一", "summary": "清晰拆解真实问题",
            "reason": "行动路径明确", "caution": "记忆点需加强", "recommended": False,
        }]
        with self.assertRaises(harness.ChoiceValidationError):
            harness.apply_model_decision(state, unexpected, "用户原话")

        with self.assertRaisesRegex(harness.HarnessConflict, "确定性 PDF 确认流程"):
            harness.apply_action(
                state,
                {"type": "confirm_foundation_report", "target_id": "report-1"},
                state["revision"],
            )


if __name__ == "__main__":
    unittest.main()
