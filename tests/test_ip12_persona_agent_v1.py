import sys
import unittest
from copy import deepcopy
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


def update(field, value, quote=None, kind="user_preference"):
    return {"field": field, "value": value, "kind": kind, "evidence_quote": quote or value}


def covered_state(updates=()):
    state = harness.initial_state()
    provided = {item["field"] for item in updates}
    state["intake"]["declined_fields"] = [
        field for field in harness.INTAKE_COVERAGE_FIELDS if field not in provided
    ]
    return state


class IP12PersonaAgentV1Tests(unittest.TestCase):
    def test_release_and_prompt_contracts_are_versioned(self):
        self.assertEqual(harness.AGENT_RELEASE_MANIFEST["agent_release"], "ip12-a1-persona")
        self.assertEqual(harness.AGENT_RELEASE_MANIFEST["skills"]["intake"]["prompt_version"], "intake-v2")
        self.assertIn("business_goal", harness.intake_system_prompt(harness.initial_state()))

    def test_local_preview_uses_one_semantic_coordinator(self):
        preview = (Path(__file__).parent / "ip12_local_codex_preview.py").read_text(encoding="utf-8")
        self.assertIn('os.environ["HERMES_MASTER_AGENT_MODE"] = "off"', preview)
        self.assertIn('os.environ["HERMES_SEMANTIC_ROUTER_MODE"] = "live"', preview)
        self.assertIn('os.environ["HERMES_SEMANTIC_DEBUG"] = "0"', preview)
        self.assertIn("not _intake_pending(state)", (HERMES / "server.py").read_text(encoding="utf-8"))

    def test_initial_field_statuses_are_unknown(self):
        statuses = harness.normalize_state(harness.initial_state())["intake"]["field_statuses"]
        self.assertEqual(statuses["preferred_name"], "unknown")
        self.assertEqual(statuses["business_goal"], "unknown")

    def test_partial_intake_update_is_candidate(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision(reply="收到。你目前主要做什么工作？", updates=[update("preferred_name", "阿青", "叫我阿青", "user_fact")]),
            "叫我阿青",
            current_message="叫我阿青",
        )
        self.assertEqual(harness.intake_field_statuses(state)["preferred_name"], "candidate")

    def test_confirmed_intake_update_is_confirmed(self):
        state, _, _ = harness.apply_intake_decision(
            covered_state([update("preferred_name", "阿青", "叫我阿青", "user_fact")]),
            decision(
                "propose_checkpoint", reply="请核对。", draft="称呼：阿青",
                updates=[update("preferred_name", "阿青", "叫我阿青", "user_fact")], checkpoint=1,
            ),
            "叫我阿青",
            current_message="叫我阿青",
        )
        state, _ = harness.apply_action(state, harness.available_actions(state)[0], state["revision"])
        self.assertEqual(harness.intake_field_statuses(state)["preferred_name"], "confirmed")

    def test_explicit_privacy_refusal_is_durable(self):
        state = harness.initial_state()
        state["intake"]["asked_follow_ups"] = ["income"]
        state, _, _ = harness.apply_intake_decision(
            state, decision(reply="可以跳过。请问我应该怎么称呼你？"), "收入不想说", current_message="收入不想说"
        )
        refreshed = harness.normalize_state(deepcopy(state))
        self.assertIn("income", refreshed["intake"]["declined_fields"])
        self.assertEqual(refreshed["intake"]["field_statuses"]["income"], "declined")

    def test_refusal_overrides_an_unconfirmed_candidate(self):
        state = harness.initial_state()
        state["intake"]["asked_follow_ups"] = ["income"]
        state["intake"]["profile_updates"] = [update("income", "暂定", "暂定")]
        state, _, _ = harness.apply_intake_decision(
            state, decision(reply="可以跳过。请问我应该怎么称呼你？"), "收入不提供", current_message="收入不提供"
        )
        self.assertEqual(state["intake"]["field_statuses"]["income"], "declined")

    def test_model_skip_value_is_normalized_to_declined_state(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision(
                reply="收到。你目前主要做什么工作？",
                updates=[update("mobile", "本人选择跳过", "本人选择跳过")],
            ),
            "本人选择跳过",
            current_message="继续",
        )
        self.assertEqual(state["intake"]["field_statuses"]["mobile"], "declined")
        self.assertNotIn("mobile", [item["field"] for item in state["intake"].get("profile_updates", [])])

    def test_mixed_full_profile_only_declines_the_privacy_clause(self):
        message = "目前没有成熟内容账号。我计划提供陪跑服务，商业目标是获客。手机号、收入和年龄不提供。"
        self.assertEqual(
            set(harness._declined_intake_fields(message)),
            {"mobile", "income", "income_source", "income_range", "age"},
        )
        self.assertEqual(
            harness._declined_intake_fields(
                "我想帮助不知道拍什么、写什么的社区店老板解决内容问题", "income"
            ),
            [],
        )

    def test_declined_field_cannot_be_reasked(self):
        state = harness.initial_state()
        state["intake"]["declined_fields"] = ["income"]
        with self.assertRaisesRegex(harness.HarnessError, "已经拒答"):
            harness.apply_intake_decision(
                state,
                decision(reply="你目前主要的收入来源是什么？不方便也可以跳过。"),
                "收入不想说",
                current_message="收入不想说",
            )

    def test_commercial_goal_gaps_are_deterministic(self):
        self.assertEqual(
            harness.commercial_goal_gaps(harness.initial_state()),
            ["business_goal", "offer", "primary_platform", "desired_action"],
        )

    def test_intake_checkpoint_is_blocked_until_every_question_is_covered(self):
        state = covered_state()
        state["intake"]["declined_fields"].remove("one_year_goal")
        with self.assertRaisesRegex(harness.HarnessError, "一年目标"):
            harness.apply_intake_decision(
                state,
                decision("propose_checkpoint", reply="请核对。", draft="基础资料", checkpoint=1),
                "其他问题都已经回答",
            )

    def test_personality_traits_require_three_distinct_words(self):
        two_traits = update("personality_traits", "真诚,克制", "真诚、克制")
        with self.assertRaisesRegex(harness.HarnessError, "三个性格词"):
            harness.apply_intake_decision(
                covered_state([two_traits]),
                decision("propose_checkpoint", reply="请核对。", draft="性格：真诚、克制", updates=[two_traits], checkpoint=1),
                "真诚、克制",
            )
        three_traits = update("personality_traits", "真诚,克制,细致", "真诚、克制、细致")
        state, _, _ = harness.apply_intake_decision(
            covered_state([three_traits]),
            decision("propose_checkpoint", reply="请核对。", draft="性格：真诚、克制、细致", updates=[three_traits], checkpoint=1),
            "真诚、克制、细致",
        )
        self.assertEqual(state["intake"]["field_statuses"]["personality_traits"], "candidate")

    def test_intake_repairs_questions_outside_the_remaining_catalog(self):
        state, normalized, _ = harness.apply_intake_decision(
            harness.initial_state(),
            decision(reply="你更像专业技术控，还是治愈陪伴型？"),
            "我做宠物摄影",
        )
        self.assertIn("姓名或昵称", normalized["reply"])
        self.assertEqual(state["intake"]["asked_follow_ups"], ["preferred_name"])

    def test_module_five_checkpoint_is_blocked_without_commercial_goal(self):
        state = harness.initial_state()
        state.update(current_module=5, completed_modules=[1, 2, 3, 4], module_step=0)
        state["intake"]["status"] = "complete"
        state["foundation_report"] = {"status": "confirmed"}
        with self.assertRaisesRegex(harness.HarnessError, "商业目的"):
            harness.apply_model_decision(
                state,
                decision("propose_checkpoint", reply="请核对。", draft="三个内容种类", checkpoint=1),
                "三个内容种类",
            )

    def test_module_five_checkpoint_accepts_complete_commercial_goal(self):
        state = harness.initial_state()
        state.update(current_module=5, completed_modules=[1, 2, 3, 4], module_step=0)
        state["intake"]["status"] = "complete"
        state["foundation_report"] = {"status": "confirmed"}
        updates = [
            update("business_goal", "通过内容获客"), update("offer", "AI 咨询", kind="user_fact"),
            update("primary_platform", "视频号"), update("desired_action", "私信咨询"),
        ]
        evidence = "通过内容获客；AI 咨询；视频号；私信咨询"
        state, _, _ = harness.apply_model_decision(
            state,
            decision("propose_checkpoint", reply="请核对。", draft="三个内容种类", updates=updates, checkpoint=1),
            evidence,
        )
        self.assertEqual(state["pending"]["step"], 1)

    def test_persona_contract_uses_confirmed_selections_only(self):
        state = harness.initial_state()
        state["ip_profile"]["facts"]["preferred_name"] = update("preferred_name", "阿青", "叫我阿青", "user_fact")
        state["ip_profile"]["confirmed_outputs"]["2-2"] = {
            "content": "真实陪伴型",
            "choice_snapshot": {"choices": [{"title": "未选择的人设"}], "selected_choice_id": "choice-2"},
        }
        contract = harness.persona_contract(state)
        self.assertEqual(contract["persona"]["content"], "真实陪伴型")
        self.assertNotIn("choice_snapshot", contract["persona"])

    def test_persona_contract_survives_thirty_normalizations(self):
        state = harness.initial_state()
        state["ip_profile"]["facts"]["preferred_name"] = update("preferred_name", "阿青", "叫我阿青", "user_fact")
        expected = harness.persona_contract(state)
        for _ in range(30):
            state = harness.normalize_state(state)
        self.assertEqual(harness.persona_contract(state), expected)

    def test_confirming_complete_intake_continues_without_reasking_experience(self):
        evidence = "叫我阿青；目前做内容顾问；做过摄影；内容整理；方法拆解；研究实体店内容；社区店老板；帮助他们解决内容问题"
        updates = [
            update("preferred_name", "阿青", "叫我阿青", "user_fact"),
            update("current_identity", "内容顾问", "目前做内容顾问", "user_fact"),
            update("previous_work_experience", "做过摄影", "做过摄影", "user_fact"),
            update("core_skill_1", "内容整理", "内容整理", "user_fact"),
            update("core_skill_2", "方法拆解", "方法拆解", "user_fact"),
            update("long_term_interest", "研究实体店内容", "研究实体店内容", "user_fact"),
            update("target_audience", "社区店老板", "社区店老板", "user_fact"),
            update("help_goal", "帮助他们解决内容问题", "帮助他们解决内容问题", "user_fact"),
        ]
        state, _, _ = harness.apply_intake_decision(
            covered_state(updates),
            decision("propose_checkpoint", reply="请核对。", draft="完整基础资料", updates=updates, checkpoint=1),
            evidence,
            current_message=evidence,
        )
        state, event = harness.apply_action(state, harness.available_actions(state)[0], state["revision"])
        self.assertTrue(event["continue_model"])
        self.assertIn("不要重复追问", event["continuation_message"])
        self.assertNotIn("先讲一段", event["assistant_prefix"])
        self.assertEqual(state["intake"]["field_statuses"]["preferred_name"], "confirmed")

    def test_module_six_completion_stops_without_production_language(self):
        state = harness.initial_state()
        state.update(current_module=6, completed_modules=[1, 2, 3, 4, 5], module_step=2)
        state["intake"]["status"] = "complete"
        state["foundation_report"] = {"status": "confirmed"}
        state["pending"] = {
            "id": "m6-final", "kind": "checkpoint", "status": "awaiting_confirmation",
            "module": 6, "step": 3, "draft": "三篇口播已确认", "self_review": "已核对",
            "profile_updates": [], "confidence": 1.0,
        }
        action = harness.available_actions(state)[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertIn("当前版本到这里停止", event["assistant_prefix"])
        self.assertNotIn("开始制作", event["assistant_prefix"])
        self.assertEqual(state["completed_modules"], [1, 2, 3, 4, 5, 6])

    def test_story_quote_accepts_outer_chinese_quotes_and_sentence_punctuation(self):
        evidence = "曾经我自己开过一家小店，因为盲目扩张失败，后来通过记录顾客问题重新找到方向"
        draft = "### 节点 1\n事实原话：“%s”。\n包装建议：小店复盘故事" % evidence
        harness._validate_module_four_story_claims(draft, evidence)

    def test_grounded_story_nodes_are_built_only_from_confirmed_quotes(self):
        state = harness.initial_state()
        state.update(current_module=4, completed_modules=[1, 2, 3], module_step=0)
        state["intake"]["status"] = "complete"
        state["ip_profile"]["facts"]["story_comeback"] = update(
            "story_comeback", "退租改上门", "现金流低谷时我退租改做上门拍摄", "user_fact"
        )
        state["ip_profile"]["facts"]["previous_work_experience"] = update(
            "previous_work_experience", "经营失败后复盘", "我开店失败后开始每天复盘", "user_fact"
        )
        state["ip_profile"]["facts"]["team_project_experience"] = update(
            "team_project_experience", "带过小团队", "我带过4人的小项目团队", "user_fact"
        )
        result = harness.grounded_story_node_decision(state)
        self.assertEqual(result["checkpoint"], 1)
        self.assertIn("事实原话：现金流低谷时我退租改做上门拍摄", result["draft"])
        self.assertIn("事实原话：我开店失败后开始每天复盘", result["draft"])
        self.assertIn("事实原话：我带过4人的小项目团队", result["draft"])
        self.assertNotIn("客户结果", result["draft"])


if __name__ == "__main__":
    unittest.main()
