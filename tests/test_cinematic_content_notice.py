# -*- coding: utf-8 -*-
"""电影化身的内容规范提示（kongli 2026-07-14）。

## 为什么这三行字很重要

HeyGen 的内容审核命中之后，**API 一个字的理由都不给**：

    v1/video_status.get  →  status=failed, error=null
    v3/videos            →  只有 id/status/title/created_at

所以我们只能告诉用户「生成失败」。他不知道为什么，只会换个提示词再试一次 —— 而失败的
真正原因（传了公众人物、传了低俗内容）他压根不知道。

这三行是**用户唯一能拿到的解释**。位置也是刻意的：在玩法页签之下、选形象之前，
**上传之前、花点数之前**就读到。

## 线上证据

近 24 小时 38 条被 HeyGen 判 failed 的电影化身任务里，36 条是 HeyGen 自己标的 failed
（不是我们超时掐的）。网页端给出的原话是：

    "Your content was flagged by our moderation system.
     Please try different images or prompts. No credits charged."

HeyGen 的[内容审核政策](https://www.heygen.com/moderation-policy)明确禁止「未经本人同意
使用真人肖像」，并且专门说了它的模型**认得出全世界的名人**，命中即转人工审核。

## ⚠️ 这是提示，不是拦截

我们**没有**在后端做名人识别 —— 做不到，也不该做（误判会拦掉用户自己的脸）。这三行只是
把 HeyGen 的规则如实转达给用户。真正的判决权在 HeyGen 手里。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class TheThreeLinesAreThereTests(unittest.TestCase):
    def test_no_public_figures(self):
        self.assertIn("禁止上传公众人物的形象或视频", HTML)

    def test_no_vulgar_content(self):
        self.assertIn("禁止上传低俗人物的形象或视频", HTML)

    def test_what_to_do_when_it_fails(self):
        """光说「禁止」没用 —— 得告诉他失败了该怎么办。否则他只会原样重试，
        再白等 20 分钟。"""
        self.assertIn("生成失败请重新选择人物形象或参考视频", HTML)


class ItIsWhereTheUserWillActuallySeeItTests(unittest.TestCase):
    """写在页面里但用户看不到，等于没写。"""

    def test_it_sits_between_the_mode_tabs_and_the_avatar_picker(self):
        """在玩法页签【之下】、选形象【之上】—— 也就是上传之前、花点数之前。"""
        i_tabs = HTML.index('id="cineModeTabs"')
        i_notice = HTML.index('class="cine-notice"')
        i_avatars = HTML.index('id="cineAvatarGrid"')
        self.assertLess(i_tabs, i_notice, "提示跑到玩法页签上面去了")
        self.assertLess(i_notice, i_avatars, "提示跑到选形象后面了 —— 用户可能已经上传完才看到")

    def test_it_is_shared_by_both_modes(self):
        """⚠️ 两个玩法【共用】这一块。

        它在 cineModeTabs 之外，不属于任何一个玩法的面板，所以切玩法时不会被隐藏。
        如果哪天有人把它挪进 motion 的面板里，开放式生成的用户就再也看不到了 ——
        而开放式恰恰是用户自己写提示词、最容易踩线的那个。
        """
        motion_only = HTML.split('id="cinePromptBlock"')[0] if 'id="cinePromptBlock"' in HTML else HTML
        self.assertIn('class="cine-notice"', motion_only)
        # 它不能带任何 data-cine-mode 限定 —— 那种元素会被切玩法的逻辑显示/隐藏
        block = HTML.split('class="cine-notice"')[1].split("</div>")[0]
        self.assertNotIn("data-cine-mode", block, "带上玩法限定 = 切到另一个玩法就看不见了")

    def test_it_is_styled_to_be_noticed(self):
        """灰不溜秋的一行小字没人看。用站内的金色描边，跟别的提示一致。"""
        self.assertIn(".cine-notice{", HTML)
        self.assertIn("rgba(231,178,76", HTML.split(".cine-notice{")[1][:200])


if __name__ == "__main__":
    unittest.main()
