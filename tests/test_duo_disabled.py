# -*- coding: utf-8 -*-
"""双人动作模仿：下掉。

线上 0 成 2 败 —— 被 HeyGen 的内容审核拦的。它的网页上写

    "Your content was flagged by our moderation system. Please try different images or
     prompts. No credits charged."

而 API 一个字都不给（v1/video_status.get 的 error 是 null，v3/videos 只有 4 个字段）。

嫌疑是它的英文提示词

    "Use these two avatars to replace the two people in the reference video"

字面就是换脸措辞，而审核模型是英文的。中文版换过，但【一次都没实测】。
与其让用户白等十几分钟再看到「生成失败」，先下掉。

## 下掉 ≠ 删掉

玩法本身保留（CINEMATIC_MODES / CINEMATIC_MODE_AVATARS / 提示词 / 单价都还在）。
实测通过后把 CINEMATIC_DUO_ENABLED=1 打开、前端把页签加回来即可，不用重写任何逻辑。

## 单人一个字没动

这次【只】下掉双人。单人的提示词和锁死的参数保持线上现状 —— 别顺手一起改了。
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class DuoIsGoneFromTheUiTests(unittest.TestCase):
    def test_the_tab_is_removed(self):
        """不是灰掉，是没有。"""
        self.assertNotIn('data-cine-mode="duo"', HTML)
        tabs = sorted(set(re.findall(r'data-cine-mode="(\w+)"', HTML)))
        self.assertEqual(tabs, ["motion", "open"])

    def test_the_frontend_also_guards_applyCineMode(self):
        """页签没了，但别的路径（URL/状态恢复）也可能调到它。"""
        self.assertIn("var CINE_COMING_SOON={duo:", HTML)
        self.assertIn("if(CINE_COMING_SOON[mode]){ toast(CINE_COMING_SOON[mode]); return; }", HTML)


class TheBackendRejectsDuoTests(unittest.TestCase):
    def test_a_direct_post_is_still_blocked(self):
        """⚠️ 前端【不是】安全边界。直接 POST 一个 cine_mode=duo 进来也得挡住，
        否则用户照样白等十几分钟再看到失败。"""
        self.assertIn("duo", video.CINEMATIC_COMING_SOON)
        with self.assertRaises(ValueError) as e:
            video.validate_cinematic_payload({"cine_mode": "duo", "avatar_ids": [1, 2]})
        self.assertIn("暂未开放", str(e.exception))

    def test_motion_and_open_still_work(self):
        self.assertNotIn("motion", video.CINEMATIC_COMING_SOON)
        self.assertNotIn("open", video.CINEMATIC_COMING_SOON)

    def test_it_can_be_turned_back_on_without_a_code_change(self):
        src = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("CINEMATIC_DUO_ENABLED"', src)


class TheModeItselfIsKeptTests(unittest.TestCase):
    """下掉 ≠ 删掉。开回来的时候不用重写任何逻辑。"""

    def test_the_mode_and_its_rules_survive(self):
        self.assertIn("duo", video.CINEMATIC_MODES)
        self.assertEqual(video.CINEMATIC_MODE_AVATARS["duo"], 2)
        self.assertIn("duo", video.CINEMATIC_FIXED_PROMPTS)
        self.assertEqual(video.cinematic_rate("duo"), 5)

    def test_the_frontend_config_survives(self):
        self.assertIn("duo:   {label:'双人动作模仿'", HTML)


class SinglePersonIsUntouchedTests(unittest.TestCase):
    """这次【只】下掉双人 —— 单人的提示词和锁死的参数保持线上现状，一个字都不许改。

    我一度把它改成第一版的英文、还去掉了身份约束，被 kongli 否掉了。
    这几条断言就是防止那种改动再溜进来。
    """

    def test_the_prompt_is_unchanged(self):
        self.assertEqual(video.CINEMATIC_FIXED_PROMPTS["motion"], "用这个人物形象模仿视频里面的动作")

    def test_the_identity_guard_is_still_appended(self):
        self.assertTrue(video.MOTION_PROMPT.endswith(video.CINEMATIC_IDENTITY_GUARD))
        self.assertIn("from the reference video", video.CINEMATIC_IDENTITY_GUARD)

    def test_the_locked_params_are_unchanged(self):
        self.assertEqual(video.CINEMATIC_MOTION_RESOLUTION, "1080p")
        self.assertEqual(video.cinematic_rate("motion"), 3)


if __name__ == "__main__":
    unittest.main()
