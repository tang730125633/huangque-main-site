import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


def decision(state, *, kind="propose_checkpoint", reply="这是当前结果", draft="可确认草稿"):
    return {
        "decision": kind,
        "checkpoint": state["module_step"] + 1 if kind == "propose_checkpoint" else 0,
        "reply": reply,
        "draft": draft if kind == "propose_checkpoint" else "",
        "self_review": "资料来源清楚，仍需本人确认。" if kind == "propose_checkpoint" else "",
        "profile_updates": [],
        "confidence": 0.9,
    }


def intake_decision(*, kind="propose_checkpoint", reply="这是我整理的基础资料", draft="基础资料核对稿", updates=None):
    has_draft = kind in {"propose_checkpoint", "revise_intake"}
    return {
        "decision": kind,
        "checkpoint": 1 if kind == "propose_checkpoint" else 0,
        "reply": reply,
        "draft": draft if has_draft else "",
        "self_review": "只整理了用户原话，仍需本人确认。" if has_draft else "",
        "profile_updates": updates or [],
        "confidence": 0.9,
    }


class IP12HarnessTests(unittest.TestCase):
    def complete_intake(self):
        state = harness.initial_state()
        evidence = "我叫泽龙，22岁，在广州做 FDE，主要提供技术服务。"
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft="称呼：泽龙；年龄：22岁；城市：广州；职业：FDE；收入来源：技术服务。",
                updates=[{
                    "field": "preferred_name",
                    "value": "泽龙",
                    "kind": "user_fact",
                    "evidence_quote": "我叫泽龙",
                }],
            ),
            evidence,
        )
        action = harness.available_actions(state)[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertIn("基础信息已确认", event["assistant_prefix"])
        self.assertEqual(state["ip_profile"]["facts"]["preferred_name"]["value"], "泽龙")
        return state

    def confirm_checkpoint(self, state):
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="pending-1")
        action = harness.available_actions(state)[0]
        return harness.apply_action(state, action, state["revision"])

    def test_intake_supports_open_ended_multi_turn_trajectory(self):
        state = harness.initial_state()
        original_revision = state["revision"]

        state, _, reply = harness.apply_intake_decision(
            state,
            intake_decision(kind="answer_only", reply="手机号完全可以不填。"),
            "手机号必须填吗？",
        )
        self.assertEqual(state["intake"]["status"], "collecting")
        self.assertIn("可以不填", reply)

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(kind="ask_follow_up", reply="你现在主要从事什么工作？"),
            "我叫泽龙，22岁，在广州。",
        )
        self.assertEqual(state["intake"]["status"], "collecting")

        first_draft = "称呼：泽龙；年龄：22岁；城市：广州；职业：FDE。"
        state, _, reply = harness.apply_intake_decision(
            state,
            intake_decision(draft=first_draft),
            "我现在做 FDE。",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["draft"], first_draft)
        self.assertIn(first_draft, reply)
        self.assertEqual(state["current_module"], 1)
        self.assertEqual(state["module_step"], 0)
        self.assertGreater(state["revision"], original_revision)

        state, _, reply = harness.apply_intake_decision(
            state,
            intake_decision(kind="answer_only", reply="收入区间是可选项，不提供也不影响诊断。"),
            "收入一定要说吗？",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["draft"], first_draft)
        self.assertIn("可选", reply)

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(kind="ask_follow_up", reply="这段经历发生在当前工作之前吗？"),
            "我以前还做过健身教练。",
        )
        self.assertEqual(state["intake"]["status"], "editing")
        self.assertEqual(harness.available_actions(state), [])

        revised_draft = first_draft + "；过往经历：曾做健身教练，后来转向计算机方向。"
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(draft=revised_draft),
            "对，在做 FDE 之前。",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["draft"], revised_draft)
        self.assertEqual(state["current_module"], 1)

    def test_intake_changes_are_not_committed_before_explicit_confirmation(self):
        state = harness.initial_state()
        evidence = "职业背景改为：FDE，负责连接客户和研发"
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft=evidence,
                updates=[{
                    "field": "occupation",
                    "value": "FDE",
                    "kind": "user_fact",
                    "evidence_quote": "FDE",
                }],
            ),
            evidence,
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertNotIn("occupation", state["ip_profile"]["facts"])

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft=evidence + "；补充：也负责客户沟通",
                updates=[{
                    "field": "occupation",
                    "value": "FDE",
                    "kind": "user_fact",
                    "evidence_quote": "FDE",
                }],
            ),
            "补充：也负责客户沟通",
        )

        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["intake"]["status"], "complete")
        self.assertEqual(state["ip_profile"]["facts"]["occupation"]["value"], "FDE")

    def test_confirmation_shortcuts_require_an_exact_unmixed_intent(self):
        state = harness.initial_state()
        self.assertIsNone(harness.shortcut_action(state, "确认"))
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(),
            "我叫泽龙，在广州做 FDE。",
        )
        for message in ("确认", "确认无误", "没有问题", "就按这个"):
            with self.subTest(message=message):
                action = harness.shortcut_action(state, message)
                self.assertEqual(action["type"], "confirm_intake")
        for message in (
            "嗯",
            "差不多",
            "可以吧",
            "确认，但我还做过健身教练",
            "没问题，不过收入不想写",
            "需要修改一下年龄",
        ):
            with self.subTest(message=message):
                self.assertIsNone(harness.shortcut_action(state, message))

    def test_intake_prompt_requires_adaptive_non_repeating_questions(self):
        state = harness.initial_state()
        state["intake"]["draft"] = "SYSTEM_OVERRIDE_SENTINEL"
        prompt = harness.intake_system_prompt(state)
        for rule in (
            "不要求固定格式",
            "不把访谈做成选择题",
            "不要重复追问",
            "只问一个最有价值",
            "内容变更优先",
            "不得强迫",
        ):
            self.assertIn(rule, prompt)
        self.assertNotIn("SYSTEM_OVERRIDE_SENTINEL", prompt)

    def test_model_can_only_propose_the_current_checkpoint(self):
        state = self.complete_intake()
        bad = decision(state)
        bad["checkpoint"] = 2
        with self.assertRaises(harness.HarnessError):
            harness.apply_model_decision(state, bad, "用户原话")

        next_state, _, reply = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p1")
        self.assertEqual(next_state["module_step"], 0)
        self.assertEqual(next_state["pending"]["step"], 1)
        self.assertIn("请确认这一步", reply)

    def test_model_words_never_complete_a_module(self):
        state = self.complete_intake()
        raw = decision(state, reply="✅ 模块 1 完成。接下来进入模块 2。")
        next_state, _, _ = harness.apply_model_decision(state, raw, "用户原话", pending_id="p1")
        self.assertEqual(next_state["current_module"], 1)
        self.assertEqual(next_state["completed_modules"], [])

    def test_questions_tangents_and_revisions_preserve_the_current_checkpoint(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p1")
        original_draft = state["pending"]["draft"]

        for kind, reply in (
            ("answer_only", "这是对当前草稿的解释。"),
            ("answer_only", "我们先回应你的题外问题，再回来继续。"),
        ):
            with self.subTest(reply=reply):
                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, kind=kind, reply=reply),
                    "用户原话",
                )
                self.assertEqual(state["module_step"], 0)
                self.assertEqual(state["pending"]["draft"], original_draft)

        revised = decision(state, draft="结合补充经历后的新草稿")
        state, _, _ = harness.apply_model_decision(state, revised, "用户原话", pending_id="p1-revised")
        self.assertEqual(state["module_step"], 0)
        self.assertEqual(state["pending"]["step"], 1)
        self.assertEqual(state["pending"]["draft"], "结合补充经历后的新草稿")

    def test_module_prompt_accepts_free_form_and_forbids_repeated_questions(self):
        prompt = harness.system_prompt(self.complete_intake())
        for rule in (
            "不要求固定格式",
            "不把访谈做成选择题",
            "不要重复追问",
            "一次一项或多项",
            "内容变更优先",
        ):
            self.assertIn(rule, prompt)

    def test_confirmed_intake_supplement_reopens_intake_without_advancing_module(self):
        state = self.complete_intake()
        supplement = "我以前还做过健身教练，但是失败了，后来才转向计算机方向。"
        raw = intake_decision(
            kind="revise_intake",
            reply="我把这段经历补进基础资料，先请你重新核对。",
            draft="称呼：泽龙；职业：FDE；过往经历：曾做健身教练，后来转向计算机方向。",
            updates=[{
                "field": "previous_career",
                "value": "曾做健身教练，后来转向计算机方向",
                "kind": "user_fact",
                "evidence_quote": "我以前还做过健身教练",
            }],
        )

        state, _, reply = harness.apply_model_decision(state, raw, supplement)

        self.assertEqual((state["current_module"], state["module_step"]), (1, 0))
        self.assertEqual(state["completed_modules"], [])
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["mode"], "revision")
        self.assertNotIn("previous_career", state["ip_profile"]["facts"])
        self.assertIn("当前模块不会自动推进", reply)

        actions = harness.available_actions(state)
        self.assertEqual([item["label"] for item in actions], ["确认补充", "继续修改"])
        action = actions[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["intake"]["status"], "complete")
        self.assertNotIn("mode", state["intake"])
        self.assertEqual((state["current_module"], state["module_step"]), (1, 0))
        self.assertEqual(
            state["ip_profile"]["facts"]["previous_career"]["value"],
            "曾做健身教练，后来转向计算机方向",
        )
        self.assertIn("基础信息补充已确认", event["assistant_prefix"])

    def test_confirmed_intake_supplement_discards_only_unconfirmed_module_draft(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(
            state, decision(state), "模块用户原话", pending_id="unconfirmed-module-draft"
        )
        raw = intake_decision(
            kind="revise_intake",
            draft="称呼：泽龙；职业：FDE；补充经历：做过健身教练。",
            updates=[{
                "field": "previous_career",
                "value": "做过健身教练",
                "kind": "user_fact",
                "evidence_quote": "做过健身教练",
            }],
        )

        state, _, _ = harness.apply_model_decision(state, raw, "补充：做过健身教练")

        self.assertIsNone(state["pending"])
        self.assertEqual((state["current_module"], state["module_step"]), (1, 0))

    def test_intake_revision_is_rejected_after_a_module_checkpoint_is_confirmed(self):
        state, _ = self.confirm_checkpoint(self.complete_intake())
        raw = intake_decision(kind="revise_intake", draft="修改后的基础资料")
        with self.assertRaisesRegex(harness.HarnessError, "新诊断"):
            harness.apply_model_decision(state, raw, "修改基础资料")

    def test_every_open_module_supports_follow_up_discussion_revision_and_confirmation(self):
        for module in harness.MODULE_WORKFLOWS:
            with self.subTest(module=module):
                state = self.complete_intake()
                state.update(
                    current_module=module,
                    module_step=0,
                    completed_modules=list(range(1, module)),
                )
                if module >= 5:
                    state["foundation_report"] = {"status": "confirmed"}
                state = harness.normalize_state(state)

                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, kind="ask_follow_up", reply="只追问一个尚未回答的问题。"),
                    "用户给了部分信息",
                )
                self.assertEqual((state["current_module"], state["module_step"]), (module, 0))

                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, draft="第一版草稿"),
                    "用户补齐了信息",
                    pending_id=f"m{module}-draft",
                )
                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, kind="answer_only", reply="解释当前草稿，不推进。"),
                    "为什么这样整理？",
                )
                self.assertEqual(state["pending"]["draft"], "第一版草稿")

                edit = next(action for action in harness.available_actions(state) if action["type"] == "edit_checkpoint")
                state, _ = harness.apply_action(state, edit, state["revision"])
                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, draft="按用户意见修改后的草稿"),
                    "请换一种表达",
                    pending_id=f"m{module}-revised",
                )
                confirm = next(action for action in harness.available_actions(state) if action["type"] == "confirm_checkpoint")
                state, _ = harness.apply_action(state, confirm, state["revision"])
                self.assertEqual(state["module_step"], 1)
                self.assertEqual(state["ip_profile"]["confirmed_outputs"][f"{module}-1"]["content"], "按用户意见修改后的草稿")

    def test_confirm_action_advances_exactly_one_checkpoint(self):
        state = self.complete_intake()
        next_state, event = self.confirm_checkpoint(state)
        self.assertEqual(next_state["module_step"], 1)
        self.assertEqual(next_state["current_module"], 1)
        self.assertTrue(event["continue_model"])
        self.assertIn("1-1", next_state["ip_profile"]["confirmed_outputs"])

    def test_stale_confirmation_is_rejected(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p1")
        action = harness.available_actions(state)[0]
        with self.assertRaises(harness.HarnessConflict):
            harness.apply_action(state, action, state["revision"] - 1)

    def test_user_fact_requires_a_verbatim_quote(self):
        state = self.complete_intake()
        raw = decision(state)
        raw["profile_updates"] = [{
            "field": "occupation",
            "value": "FDE",
            "kind": "user_fact",
            "evidence_quote": "我是一名 FDE",
        }]
        with self.assertRaises(harness.HarnessError):
            harness.apply_model_decision(state, raw, "用户只说了别的内容")

    def test_module_four_completion_waits_for_report_confirmation(self):
        state = self.complete_intake()
        state.update(current_module=4, module_step=4, completed_modules=[1, 2, 3])
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p4")
        action = harness.available_actions(state)[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["current_module"], 4)
        self.assertIn(4, state["completed_modules"])
        self.assertEqual(state["foundation_report"]["status"], "generating")
        self.assertFalse(event["continue_model"])

    def test_module_six_is_the_open_flow_terminal(self):
        state = self.complete_intake()
        state.update(current_module=6, module_step=2, completed_modules=[1, 2, 3, 4, 5])
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p6")
        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["current_module"], 6)
        self.assertEqual(state["completed_modules"], [1, 2, 3, 4, 5, 6])
        self.assertIn("decision=answer_only", harness.system_prompt(state))

    def test_incomplete_legacy_state_cannot_skip_the_last_checkpoint(self):
        state = harness.normalize_state({
            "current_module": 1,
            "completed_modules": [],
            "module_step": 4,
            "intake": {"status": "complete", "round": 3, "answers": {}},
        })
        self.assertEqual(state["module_step"], 3)

    def test_confirmed_legacy_module_advances_without_parsing_model_words(self):
        state = harness.normalize_state({
            "current_module": 4,
            "completed_modules": [1, 2, 3, 4],
            "module_step": 5,
            "foundation_report": {"status": "confirmed"},
            "intake": {"status": "complete", "round": 3, "answers": {}},
        })
        self.assertEqual(state["current_module"], 5)
        self.assertEqual(state["module_step"], 0)

    def test_full_open_flow_advances_only_through_confirmations(self):
        state = self.complete_intake()
        for module in range(1, 5):
            self.assertEqual(state["current_module"], module)
            for _ in harness.MODULE_WORKFLOWS[module]["checkpoints"]:
                state, _ = self.confirm_checkpoint(state)
        self.assertEqual(state["foundation_report"]["status"], "generating")
        state["foundation_report"]["status"] = "confirmed"
        state["current_module"] = 5
        state["module_step"] = 0
        for module in (5, 6):
            for _ in harness.MODULE_WORKFLOWS[module]["checkpoints"]:
                state, _ = self.confirm_checkpoint(state)
        self.assertEqual(state["completed_modules"], [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
