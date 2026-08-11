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


class IP12HarnessTests(unittest.TestCase):
    def complete_intake(self):
        state = harness.initial_state()
        state, _ = harness.handle_intake_message(state, "泽龙｜22岁｜广州")
        state, _ = harness.handle_intake_message(state, "FDE｜1年｜技术服务")
        action = harness.available_actions(state)[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertIn("基础信息已确认", event["assistant_prefix"])
        return state

    def confirm_checkpoint(self, state):
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="pending-1")
        action = harness.available_actions(state)[0]
        return harness.apply_action(state, action, state["revision"])

    def test_intake_requires_explicit_confirmation(self):
        state = harness.initial_state()
        state, _ = harness.handle_intake_message(state, "泽龙｜22岁｜广州")
        state, _ = harness.handle_intake_message(state, "FDE｜1年｜技术服务")
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["current_module"], 1)

        state, reply = harness.handle_intake_message(state, "需要修改")
        self.assertEqual(state["intake"]["status"], "editing")
        self.assertIn("正确内容", reply)
        self.assertNotEqual(state["intake"]["status"], "complete")

    def test_intake_correction_is_shown_again_before_confirmation(self):
        state = harness.initial_state()
        state, _ = harness.handle_intake_message(state, "泽龙｜22岁｜广州")
        state, _ = harness.handle_intake_message(state, "FDE｜1年｜技术服务")
        state, _ = harness.handle_intake_message(state, "职业背景改为：FDE，负责连接客户和研发")
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertIn("FDE，负责连接客户和研发", state["intake"]["answers"]["确认或修正"])

    def test_typed_confirm_only_maps_when_a_confirmation_is_pending(self):
        state = harness.initial_state()
        self.assertIsNone(harness.shortcut_action(state, "确认"))
        state, _ = harness.handle_intake_message(state, "泽龙｜22岁｜广州")
        state, _ = harness.handle_intake_message(state, "FDE｜1年｜技术服务")
        action = harness.shortcut_action(state, "确认")
        self.assertEqual(action["type"], "confirm_intake")

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
