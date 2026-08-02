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


class NothingIsAppendedAnyMoreTests(unittest.TestCase):
    """payload 里的 prompt 就是发给 HeyGen 的 prompt，一个字不差。

    好处：jobs.payload 里存的 prompt == HeyGen 真正收到的 prompt，
    排查时不用再脑补「后端还偷偷加了什么」。
    """

    def test_gen_cinematic_sends_the_resolved_prompt_without_identity_guard(self):
        self.assertIn('prompt=provider_prompt, direct=True', GEN)
        self.assertIn('provider_prompt = resolve_image_mentions', GEN)
        # 只查【代码】—— 注释里为了讲清楚「原来拼的是什么」还会提到 guard 的名字
        code = chr(10).join(ln for ln in GEN.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("CINEMATIC_IDENTITY_GUARD", code,
                         "还在拼身份约束 —— 新的固定提示词是自包含的，会说两遍")


class TheMotionPromptIsSelfContainedTests(unittest.TestCase):
    """单人的固定提示词换成了 kongli 给的这段（2026-07-14），逐字。

    它【自包含】—— 已经带了 CRITICAL 身份约束和 no extra people。所以不能再拼 guard，
    否则同样的话说两遍。

    ⚠️ 风险（已跟 kongli 说清楚，他确认要换）：里面的
        "Do NOT copy the reference video person's appearance"
        "not the reference person"
    正是线上那版英文提示词的措辞 —— 战绩 5 败 1 成，被 HeyGen 的内容审核拦下。
    被它换掉的中文版是照抄 #2173 的，那是唯一验证过能过审核的配置。真挂了，先看这里。
    """

    def test_it_is_exactly_what_kongli_gave(self):
        self.assertEqual(
            video.CINEMATIC_FIXED_PROMPTS["motion"],
            "Create a realistic cinematic vertical video of the same person from the avatar photo. "
            "Follow the uploaded reference video ONLY for body movement, pose, timing, gestures, "
            "facial expression rhythm, framing and camera motion. CRITICAL: Keep the avatar person's "
            "exact identity, face, hairstyle, body shape, skin tone and clothing. Do NOT copy the "
            "reference video person's appearance, body proportions or outfit. The output must look like "
            "the avatar person performing the reference motion, not the reference person. Smooth "
            "realistic motion, no text, no logo, no extra people.")

    def test_it_carries_its_own_identity_constraint(self):
        """自包含 —— 不依赖任何外部拼接。"""
        self.assertIn("CRITICAL", video.MOTION_PROMPT)
        self.assertIn("Keep the avatar person's exact identity", video.MOTION_PROMPT)
        self.assertIn("no extra people", video.MOTION_PROMPT)

    def test_nothing_is_said_twice(self):
        """拼了 guard 就会出现两遍 CRITICAL、两遍 no extra people。"""
        self.assertEqual(video.MOTION_PROMPT, video.MOTION_PROMPT_BASE)
        self.assertEqual(video.MOTION_PROMPT.count("CRITICAL"), 1)
        self.assertEqual(video.MOTION_PROMPT.count("no extra people"), 1)

    def test_duo_is_self_contained_too(self):
        """双人现在下掉了，但开回来时不该再依赖外部拼接。"""
        self.assertIn("CRITICAL", video.DUO_MOTION_PROMPT)
        self.assertEqual(video.DUO_MOTION_PROMPT, video.DUO_MOTION_PROMPT_BASE)


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
