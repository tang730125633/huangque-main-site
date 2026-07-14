# -*- coding: utf-8 -*-
"""开放式生成：不再拼身份约束 —— 用户写什么就发什么。

kongli 的决定（2026-07-14）。原来发给 HeyGen 的是：

    payload["prompt"] + CINEMATIC_IDENTITY_GUARD

那段约束是我们替用户加的，他看不到也关不掉。现在开放式不加了。

## ⚠️ 代价（已跟 kongli 说清楚）

不拼的话，HeyGen 可能把参考视频里那个人的长相抄进成片 —— 用户拿到的就不是自己的脸了。
用户自己写的中文提示词里通常不会写「保持我的脸不变」这种话。真出现串脸，先看这里。

为此做了两件补偿：
  * 前端文案改成【如实】说明（原来写的是「系统会自动保证成片里的人还是你选的形象本人」，
    那句话的底气就是这段约束 —— 不改就是骗用户）
  * 6 个提示词模板【自带】身份表述，给大多数人一个安全的默认起点

## ⚠️ 动作模仿【仍然要拼】

它是线上唯一跑通 HeyGen 审核的配置（#2173 就是带着这段约束过的）。别顺手一起改了 ——
这条有专门的测试守着。
"""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
SRC = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
GEN = SRC.split("def gen_cinematic")[1].split("\ndef ")[0]


class OpenModeSendsTheUserPromptVerbatimTests(unittest.TestCase):
    def test_the_guard_is_only_appended_for_the_fixed_prompt_modes(self):
        self.assertIn('payload["prompt"] + CINEMATIC_IDENTITY_GUARD', GEN)
        self.assertIn('if payload.get("cine_mode") in CINEMATIC_FIXED_PROMPTS', GEN)
        self.assertIn('else payload["prompt"]', GEN)

    def test_open_mode_gets_nothing_appended(self):
        """一个字都不加。"""
        def sent(payload):
            m = payload.get("cine_mode")
            return (payload["prompt"] + video.CINEMATIC_IDENTITY_GUARD
                    if m in video.CINEMATIC_FIXED_PROMPTS else payload["prompt"])
        self.assertEqual(sent({"cine_mode": "open", "prompt": "在海边跳舞"}), "在海边跳舞")


class MotionKeepsTheGuardTests(unittest.TestCase):
    """⚠️ 最要紧的一条。动作模仿是线上【唯一跑通 HeyGen 审核】的配置 ——
    #2173 就是带着这段约束过的。别因为「开放式去掉了」就顺手把它也去掉。
    """

    def test_the_guard_still_exists(self):
        self.assertIn("from the reference video", video.CINEMATIC_IDENTITY_GUARD)
        self.assertIn("CRITICAL", video.CINEMATIC_IDENTITY_GUARD)

    def test_motion_still_gets_it(self):
        self.assertTrue(video.MOTION_PROMPT.endswith(video.CINEMATIC_IDENTITY_GUARD))

    def test_the_fixed_prompt_modes_are_the_ones_that_keep_it(self):
        self.assertIn("motion", video.CINEMATIC_FIXED_PROMPTS)


class TheUiNoLongerLiesTests(unittest.TestCase):
    """前端原来写的是「系统会自动保证成片里的人还是你选的形象本人」—— 那句话的底气
    就是这段约束。不拼了还留着它，就是骗用户。"""

    def test_the_false_promise_is_gone(self):
        visible = [ln for ln in HTML.splitlines() if not ln.lstrip().startswith(("//", "<!--"))]
        self.assertNotIn("系统会自动保证成片里的人还是你选的形象本人", "\n".join(visible))

    def test_it_tells_the_user_to_write_it_themselves(self):
        self.assertIn("你写什么就发什么，系统不再额外加任何约束", HTML)
        self.assertIn("请在描述里写清楚", HTML)

    def test_the_templates_carry_the_identity_line(self):
        """模板是大多数人的起点 —— 把身份表述写进去，等于给了一个安全的默认值。"""
        self.assertIn("var CINE_IDENTITY='人物必须是所选形象本人", HTML)
        block = HTML.split("var CINE_TEMPLATES=[")[1].split("];")[0]
        self.assertEqual(block.count("CINE_IDENTITY+"), 6, "6 个模板都得带上")


if __name__ == "__main__":
    unittest.main()
