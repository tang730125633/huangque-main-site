# -*- coding: utf-8 -*-
"""通知中心「查看产物」→ 资产库定位高亮。

原 PR 只在 cloud-shell.js 的 href 上拼了 ?task=，但没有任何页面读它 ——
点击行为和改动前完全一样，却 bump 了全站 15 页缓存戳。这里把消费方补上并守住：

1. task_id 必须 encodeURIComponent（含 & ? # 时不能串参），且为空时不产生空的 ?task=
2. 已完成任务跳资产库并带 ?cat=&task=；失败/进行中仍跳生成页（便于重试/看进度）
3. 资产库真的消费这个参数：卡片带 data-job、按 ?task= 定位并高亮
"""
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
ASSETS = (ROOT / "site/workbench/assets.html").read_text(encoding="utf-8")


def _idx(src, needle):
    i = src.find(needle)
    assert i >= 0, "找不到: %r" % needle
    return i


class NoticeHrefTests(unittest.TestCase):
    def test_task_id_is_url_encoded(self):
        """裸拼 task_id 时，含 & ? # 会串参。"""
        self.assertIn("encodeURIComponent(tid)", SHELL)
        self.assertNotRegex(SHELL, r"\?task='\s*\+\s*\(x\.task_id", "task_id 不能裸拼进 URL")

    def test_no_empty_task_param_when_id_missing(self):
        """task_id 为空时不应产生空的 ?task=。"""
        self.assertIn("if(status==='done' && tid && cat)", SHELL)

    def test_done_goes_to_asset_library_with_cat_and_task(self):
        self.assertIn("'assets.html?cat='", SHELL)
        self.assertIn("'&task='", SHELL)

    def test_non_done_falls_back_to_generator_page(self):
        """失败/进行中仍回生成页，资产库里没有它们的产物。"""
        self.assertIn("return noticePage(x.kind);", SHELL)

    def test_notice_uses_noticeHref_not_raw_noticePage(self):
        self.assertIn("href:noticeHref(x,status)", SHELL)

    def test_asset_cat_mapping_covers_video_variants(self):
        """tryon / xiaole_video 的产物都落在资产库的 video 分类。"""
        m = re.search(r"function noticeAssetCat\(kind\)\{[^}]*cats=\{([^}]*)\}", SHELL, re.S)
        self.assertIsNotNone(m, "noticeAssetCat 未定义")
        cats = m.group(1)
        for k in ("tryon:'video'", "xiaole_video:'video'", "image:'image'", "leads:'leads'"):
            self.assertIn(k, cats)


class AssetLibraryConsumerTests(unittest.TestCase):
    """消费方必须真的存在 —— 否则 ?task= 就是个没人读的死参数。"""

    def test_reads_task_param(self):
        self.assertIn("params.get('task')", ASSETS)

    def test_cards_carry_data_job(self):
        self.assertIn("setAttribute('data-job'", ASSETS)

    def test_all_five_card_kinds_are_tagged(self):
        """image / audio / video / avatar / doc 五类卡片都要能被定位。"""
        self.assertEqual(5, ASSETS.count("grid.appendChild(withJob("),
                         "五处 appendChild 都要包 withJob")
        self.assertEqual(1, ASSETS.count("function withJob(el,x){"))

    def test_job_id_preferred_over_id(self):
        """job_id 才是与通知 task_id 同源的字段。"""
        self.assertIn("x.job_id!=null?x.job_id:x.id", ASSETS)

    def test_focus_runs_after_render(self):
        self.assertLess(_idx(ASSETS, "_renderBody();"), _idx(ASSETS, "focusJobCard();"))

    def test_focus_is_one_shot(self):
        """滚过去就消费掉，避免用户切分类时被反复拉回。"""
        self.assertIn("focusTask='';", ASSETS)

    def test_focus_scrolls_and_highlights(self):
        self.assertIn("scrollIntoView", ASSETS)
        self.assertIn("asset-focus", ASSETS)

    def test_highlight_style_defined_and_respects_reduced_motion(self):
        self.assertIn(".asset-focus{", ASSETS)
        self.assertIn("prefers-reduced-motion", ASSETS)

    def test_missing_card_does_not_throw(self):
        """目标不在当前分类/被筛掉时，静默返回而不是报错。"""
        self.assertIn("if(!el) return;", ASSETS)


if __name__ == "__main__":
    unittest.main()
