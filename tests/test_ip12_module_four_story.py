import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


U1 = (
    "我刚开始单干的时候，第一批货海运破损很厉害，包装没做好，亏了好几十万，差点想回去上班。"
    "后来把包装和供应链重新理了一遍，才慢慢缓过来。"
)
U2 = (
    "主要是我自己盯。我跑了好几家供应商，材料和报价一家一家比，"
    "最后选了包装更结实但价格没贵多少的那家。"
)


def module_four_state():
    state = harness.initial_state()
    state["current_module"] = 4
    state["module_step"] = 0
    return state


class ModuleFourStorySynthesizeTests(unittest.TestCase):
    def test_live_two_turn_quotes_become_story_nodes(self):
        result = harness.synthesize_module_four_decision(module_four_state(), U2, U1)
        self.assertEqual(result["decision"], "propose_checkpoint")
        self.assertEqual(result["checkpoint"], 1)
        self.assertEqual(result["profile_updates"], [])
        self.assertIn("我按你刚才的原话整理了故事节点，请先核对。", result["reply"])
        self.assertNotIn("故事内容", result["draft"])
        self.assertIn("事实原话：%s" % U1, result["draft"])
        self.assertIn("事实原话：%s" % U2, result["draft"])
        self.assertIn("海运", result["draft"])
        self.assertIn("破损", result["draft"])
        self.assertIn("供应商", result["draft"])
        harness._validate_module_four_story_claims(result["draft"], U1 + U2)

    def test_model_like_bad_draft_still_raises_but_synthesize_succeeds(self):
        evidence = U1 + U2
        with self.assertRaisesRegex(harness.HarnessError, "模块 4"):
            harness._validate_module_four_story_claims("故事内容：货损后我很崩溃", evidence)
        result = harness.synthesize_module_four_decision(module_four_state(), U2, U1)
        self.assertEqual(result["decision"], "propose_checkpoint")
        self.assertNotIn("故事内容", result["draft"])
        self.assertNotIn("我很崩溃", result["draft"])
        harness._validate_module_four_story_claims(result["draft"], evidence)

    def test_no_story_text_raises_same_as_grounded(self):
        with self.assertRaisesRegex(harness.HarnessError, "模块 4 还缺少一段可回查的真实经历"):
            harness.synthesize_module_four_decision(module_four_state(), "好的", "继续")


if __name__ == "__main__":
    unittest.main()
