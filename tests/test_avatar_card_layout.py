# -*- coding: utf-8 -*-
"""形象卡片：固定大小，内容自适应。

线上问题：剧情视频那边新建的形象，卡片高度参差不齐。

根因是我写 renderCineAvatars 时把裸 <img> 直接塞进了 .avatar-card：

    card.innerHTML='<img src="...">' + '<span class="avatar-name">...</span>'

没有 .avatar-thumb 包裹 → 图片按原始宽高比撑开 → 用户的照片有竖版自拍、有横版截图，
一行三张就高低不一。而 .avatar-card 当时是 min-height（会被内容撑开），不是 height。

两件事一起修：
  * CSS：min-height → 固定 height + flex 列。缩略图 flex:1 吃掉剩余空间，
    文字区 flex:none 只占自己那点 —— 让内容适应盒子，而不是反过来。
  * 结构：剧情视频的卡片改用和「我的形象」一致的 .avatar-thumb > img 骨架。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


def _rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", HTML)
    return m.group(1) if m else ""


class CardIsFixedSizeTests(unittest.TestCase):
    def test_card_height_is_fixed_not_minimum(self):
        """min-height 会被内容撑开 —— 这正是卡片参差不齐的根源。"""
        css = _rule(".avatar-card")
        self.assertRegex(css, r"height:\s*\d+px", ".avatar-card 必须有固定 height")
        self.assertNotIn("min-height", css, "min-height 会被内容撑开，卡片就不齐了")

    def test_card_is_a_flex_column_so_content_adapts(self):
        css = _rule(".avatar-card")
        self.assertIn("display:flex", css)
        self.assertIn("flex-direction:column", css)
        self.assertIn("box-sizing:border-box", css, "不加的话 padding/border 会把固定高度撑破")

    def test_thumb_absorbs_the_leftover_space(self):
        """缩略图 flex:1 吃掉剩余空间；min-height:0 是 flex 子项能被压缩的前提。"""
        css = _rule(".avatar-thumb")
        self.assertIn("flex:1", css)
        self.assertIn("min-height:0", css, "没有它，flex 子项不会被压缩，图片还是能顶开卡片")

    def test_image_is_cropped_to_fill(self):
        # object-fit:cover —— 裁切填充，不是拉伸变形
        self.assertIn("object-fit:cover", _rule(".avatar-thumb img"))

    def test_a_bare_img_in_the_card_is_still_constrained(self):
        """兜底：万一有人又把裸 <img> 直接塞进卡片（我就干过），也要被约束住。"""
        css = _rule(".avatar-card > img")
        self.assertIn("object-fit:cover", css)
        self.assertIn("min-height:0", css)


class CinematicCardUsesTheRightSkeletonTests(unittest.TestCase):
    def setUp(self):
        self.block = HTML.split("function renderCineAvatars")[1].split("function toggleCineAvatar")[0]

    def test_image_is_wrapped_in_a_thumb(self):
        self.assertIn('class="avatar-thumb"', self.block,
                      "裸 <img> 会按原始宽高比撑开卡片 —— 必须用 .avatar-thumb 包住")

    def test_name_is_wrapped_in_meta(self):
        self.assertIn('class="avatar-meta"', self.block)
        self.assertIn('class="avatar-name"', self.block)

    def test_card_declares_no_actions(self):
        # 这张卡没有改名/删除按钮，文字区不该给它们留 38px 空位
        self.assertIn("avatar-card no-actions", self.block)
        self.assertIn("padding-bottom:8px", _rule(".avatar-card.no-actions .avatar-meta"))

    def test_image_is_actually_loaded(self):
        """data-asset-src 只是占位，必须显式喂给 setAssetImage 才会加载（它带鉴权取私有资源）。

        漏了这句，图片压根不显示 —— 而 HTML 里看起来一切正常。
        """
        self.assertIn("data-asset-src", self.block)
        self.assertIn("setAssetImage(card.querySelector('[data-asset-src]')", self.block)

    def test_no_inline_styles_for_the_pick_badge(self):
        # 选中序号挪进了 .avatar-pick，别再往 innerHTML 里塞一长串 style
        self.assertIn('class="avatar-pick"', self.block)
        self.assertNotIn("position:absolute;top:6px", self.block)


if __name__ == "__main__":
    unittest.main()
