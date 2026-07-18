# -*- coding: utf-8 -*-
"""形象展示框：16:9 固定比例、图片完整不变形、点击不重新拉图。

三个线上问题，一起修：

1. 展示框比例乱。之前是「固定 158px 高 + 缩略图 flex:1 吃剩余空间」，框的宽高比取决于
   网格列宽，页面一缩放就变。改成 aspect-ratio:16/9 —— 宽度随页面走，高度跟着算，
   比例锁死，卡片高度也不再需要任何写死的 px。

2. 图片被裁。object-fit:cover 会把竖版自拍的上下裁掉一大半，用户看不到自己的脸。
   改 contain：完整装进框里，留边，但不裁切、不拉伸变形。

3. 「一点击图片就重新刷新，还很慢」。两个根因叠在一起：
     * toggleCineAvatar 直接调 renderCineAvatars()，innerHTML='' 全量重建 DOM，
       每张卡的 <img> 都是新的 → 全部重新走一遍鉴权 fetch。
     * assetUrl 每次都 fresh(u)+cache:'no-store' 击穿缓存，还每次新建一个 blob URL。
   → 选中态改用 syncCineSelection()（只动 class 和徽标），assetUrl 按路径缓存 Promise。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


def _rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", HTML)
    return m.group(1) if m else ""


class ThumbIsSixteenByNineTests(unittest.TestCase):
    def test_thumb_locks_the_aspect_ratio(self):
        css = _rule(".avatar-thumb")
        self.assertIn("aspect-ratio:16/9", css, "展示框必须锁 16:9")

    def test_thumb_is_not_stretched_by_flex(self):
        """给了 flex:1 就会被拉伸去填满卡片剩余高度 —— aspect-ratio 白锁。"""
        css = _rule(".avatar-thumb")
        self.assertIn("flex:none", css)
        self.assertNotIn("flex:1", css)

    def test_the_box_follows_the_page_not_a_hardcoded_pixel_height(self):
        """宽度由网格列宽给（随页面缩放），高度由 16:9 算出来 —— 卡片不该再写死 px 高。"""
        css = _rule(".avatar-card")
        self.assertNotRegex(css, r"(?<!min-)(?<!max-)height:\s*\d+px",
                            "卡片写死像素高，展示框就没法按页面比例缩放了")

    def test_image_is_shown_whole_not_cropped(self):
        """contain 而不是 cover：完整展示、不裁切、不变形。"""
        self.assertIn("object-fit:contain", _rule(".avatar-thumb img"))
        self.assertNotIn("object-fit:cover", _rule(".avatar-thumb img"))

    def test_a_bare_img_in_the_card_follows_the_same_rules(self):
        """兜底：万一有人又把裸 <img> 直接塞进卡片（我就干过），也要按 16:9 完整展示。"""
        css = _rule(".avatar-card > img")
        self.assertIn("aspect-ratio:16/9", css)
        self.assertIn("object-fit:contain", css)


class ClickDoesNotRebuildTheGridTests(unittest.TestCase):
    def test_toggle_only_syncs_the_selection(self):
        """选中/取消只该改 class 和序号徽标 —— 重建 DOM 会让所有形象图重新下载一遍。"""
        block = HTML.split("function toggleCineAvatar")[1].split("function ")[0]
        self.assertIn("syncCineSelection()", block)
        self.assertNotIn("renderCineAvatars()", block, "点一下就全量重建，正是「图片又刷新了」的根因")

    def test_sync_mutates_in_place(self):
        block = HTML.split("function syncCineSelection")[1].split("function ")[0]
        self.assertIn("classList.toggle('on'", block)
        self.assertNotIn("innerHTML=''", block, "syncCineSelection 绝不能清空重建")
        self.assertNotIn("setAssetImage", block, "选中态刷新不该碰图片")

    def test_the_grid_is_only_rebuilt_when_the_list_itself_changes(self):
        """renderCineAvatars（会 innerHTML='' 重建）只能由形象列表加载触发。"""
        callers = [
            HTML[max(0, m.start() - 260):m.start()].rsplit("function ", 1)[-1].split("(")[0]
            for m in re.finditer(r"renderCineAvatars\(\);", HTML)
        ]
        self.assertTrue(callers)
        self.assertNotIn("toggleCineAvatar", callers)


class AssetsAreCachedTests(unittest.TestCase):
    def setUp(self):
        self.block = HTML.split("function assetUrl")[1].split("function blobToDataUrl")[0]

    def test_protected_assets_are_cached_by_path(self):
        """缓存的是 Promise：并发调用会共享同一次请求，不会打两遍。"""
        self.assertIn("var _assetCache={}", HTML)
        self.assertIn("if(_assetCache[u]) return _assetCache[u]", self.block)
        self.assertIn("_assetCache[u]=p", self.block)

    def test_the_cache_is_not_busted_on_every_call(self):
        """fresh() 加时间戳 + no-store —— 每次调用都强制重新下载，缓存等于没有。"""
        self.assertNotIn("fetch(fresh(u)", self.block)
        self.assertNotIn("cache:'no-store'", self.block)

    def test_failures_are_not_cached(self):
        """失败的 Promise 留在缓存里，用户就再也重试不了了。"""
        self.assertIn("delete _assetCache[u]", self.block)


class CinematicCardUsesTheRightSkeletonTests(unittest.TestCase):
    def setUp(self):
        self.block = HTML.split("function renderCineAvatars")[1].split("function syncCineSelection")[0]

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

    def test_card_carries_its_avatar_id(self):
        """syncCineSelection 靠 data-avatar-id 找到该点亮哪张卡 —— 没有它就只能重建 DOM。"""
        self.assertIn("card.dataset.avatarId=a.id", self.block)

    def test_image_is_actually_loaded(self):
        """图片是受保护素材，必须显式喂给 setAssetImage 才会带鉴权取回来。
        漏了这句，图片压根不显示 —— 而 HTML 里看起来一切正常。
        """
        self.assertIn("setAssetImage(card.querySelector('img')", self.block)

    def test_no_inline_styles_for_the_pick_badge(self):
        # 选中序号挪进了 .avatar-pick，别再往 innerHTML 里塞一长串 style
        self.assertNotIn("position:absolute;top:6px", self.block)
        self.assertIn('avatar-pick', HTML.split("function syncCineSelection")[1].split("function ")[0])


class AvatarListLoadingTests(unittest.TestCase):
    def setUp(self):
        self.block = HTML.split("function loadMotionAvatars")[1].split("// =====", 1)[0]

    def test_removed_talking_batch_renderer_is_not_called(self):
        self.assertNotIn("renderTalkingBatchImages", self.block)

    def test_loading_and_errors_target_the_cinematic_grid(self):
        self.assertIn("var grid=$('cineAvatarGrid')", self.block)
        self.assertIn("renderCineAvatars()", self.block)


if __name__ == "__main__":
    unittest.main()
